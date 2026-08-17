"""GUI 用资源甄别与下载核心。

与 Scrapy spider 互补：spider 用于命令行/全站深度爬取，本模块面向 GUI，
单页快速发现资源、识别类型、提取封面/缩略图，并支持勾选后逐个下载。

纯逻辑（URL 分类/高清变换/封面映射/文件头识别/详情页媒体提取）已抽到
discover_common.py，GUI 与 Scrapy spider 共用一套，避免两套发现逻辑重复。

请求层使用 Scrapling 的 FetcherSession（TLS 指纹模拟 + 本机代理），
不可用时自动回退 requests —— 见 gui_fetch.FetchSession。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunsplit

import requests
from bs4 import BeautifulSoup

import config
from discover_common import (  # noqa: F401  兼容外部 `from gui_crawler import ...`
    EXTENSION_CATEGORY,
    apply_rules,
    basename_from_url,
    classify_url,
    dimensions_from_head,
    extract_media_from_html,
    highres_url,
    is_download_endpoint,
    is_icon_url,
    is_tiny,
    looks_like_image,
    pexels_cover_to_video,
    pick_best_video,
    render_dest_template,
    safe_filename,
    sanitize_name,
    video_highres_url,
)
from gui_fetch import FetchSession, FetchResponse
from renderer import close_renderer, render_page
from resources_reptile.utils.user_agents import random_user_agent

# 旧名兼容（本模块内部与历史导入仍引用 _looks_like_image）
_looks_like_image = looks_like_image

REQUEST_TIMEOUT = config.REQUEST_TIMEOUT
MAX_RESOURCES = config.MAX_RESOURCES
PROBE_WORKERS = config.PROBE_WORKERS

_EXT_RE = re.compile(r"\.([a-z0-9]{2,6})$", re.IGNORECASE)

# 常见的视频 URL 无扩展名但可从路径/查询判断
VIDEO_HINTS = ("/video", "/videos", "/download", "/stream", ".mp4", ".mkv", ".webm")
IMAGE_HINTS = ("/image", "/images", ".jpg", ".jpeg", ".png", ".gif", ".webp")

# 「页面空壳 + 签名接口」的站（抖音/快手/小红书等）：HTML 里没有直链，
# 真实数据全在浏览器内 JS 带签名调用的接口里。适配器注册表见
# platform_adapters；命中适配器即启用「渲染 + 捕获接口 + 提取」路径。
from platform_adapters import api_filters_for as _adapter_api_filters
from platform_adapters import page_adapter as _page_adapter
from stats import get_stats as _get_stats

DISPLAY_KIND = {
    "images": "image",
    "videos": "video",
}


# ---------------- 失败持久化（断点续爬完整化） ----------------
# 下载失败记录落盘到 outdir/failures.json，重启 GUI 后可「重试失败」重新勾选。
# 记录格式：{url: {"reason": str, "failed_at": "YYYY-MM-DDTHH:MM:SS"}}
_FAILURE_FILE = "failures.json"


def failures_path(outdir: str) -> str:
    return os.path.join(outdir, _FAILURE_FILE)


def load_failures(outdir: str) -> dict[str, dict]:
    """读取 outdir 下的失败记录；不存在/损坏返回空表。"""
    try:
        with open(failures_path(outdir), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        pass
    return {}


def save_failures(outdir: str, entries: dict[str, dict]) -> None:
    """原子写入失败记录（临时文件 + rename）。"""
    path = failures_path(outdir)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


class _DownloadError(Exception):
    """下载失败（reason 可读；status=0 表示网络层错误；retry_after 为 429 建议等待秒）。"""

    def __init__(self, reason: str, retryable: bool, status: int = 0,
                 retry_after: float = 0.0):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.status = status
        self.retry_after = retry_after


def _code_retryable(status: int) -> bool:
    # 4xx 大多为永久失败（404/403/410…），重试无意义；429/5xx 属瞬时
    return status == 0 or status == 429 or status >= 500


def _exc_status(exc: Exception) -> int:
    """从异常对象提取 HTTP 状态码（0 = 无法确定，视为网络层错误）。"""
    for src in (exc, getattr(exc, "response", None)):
        if src is None:
            continue
        st = getattr(src, "status_code", 0) or 0
        if st:
            return int(st)
    m = re.search(r"HTTP\s+(\d{3})", str(exc))
    return int(m.group(1)) if m else 0


def _retry_after_seconds(exc: Exception) -> float:
    """解析响应头 Retry-After（秒数或 HTTP 日期），无法解析返回 0。"""
    resp = getattr(exc, "response", None)
    hdr = ""
    if resp is not None:
        hdrs = getattr(resp, "headers", None) or {}
        hdr = str(hdrs.get("Retry-After", "") or "").strip()
    if hdr.isdigit():
        return float(hdr)
    if hdr:
        try:
            from email.utils import parsedate_to_datetime
            return max(0.0, (parsedate_to_datetime(hdr) -
                             datetime.now(timezone.utc)).total_seconds())
        except Exception:
            return 0.0
    return 0.0


def browser_headers(referer: str | None = None) -> dict:
    headers = {
        "User-Agent": random_user_agent(),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }
    if referer:
        headers["Referer"] = referer
    return headers


class Statistics:
    """线程安全的下载统计。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.downloaded = 0
        self.failed = 0
        self.total = 0

    def set_total(self, n):
        with self._lock:
            self.total = n

    def ok(self):
        with self._lock:
            self.downloaded += 1

    def fail(self):
        with self._lock:
            self.failed += 1

    def snapshot(self):
        with self._lock:
            return self.downloaded, self.failed, self.total


class Resource:
    """一个待展示/下载的资源。"""

    def __init__(self, url: str, page_url: str = "", title: str = "",
                 preview_url: str = "", name: str = "", size: int = 0,
                 content_type: str = "", raw_url: str = ""):
        self.url = url
        self.raw_url = raw_url        # 真实媒体直链（备用解析提供；可能被反爬改用页面 URL）
        self.page_url = page_url
        self.title = title or ""
        self.preview_url = preview_url  # 封面/缩略图地址（可为空）
        self.name = name or basename_from_url(url)
        self.size = size
        self.content_type = content_type
        self.category = classify_url(url)
        self.kind = DISPLAY_KIND.get(self.category, "file")
        self.width = 0      # 宽（像素），未知为 0
        self.height = 0     # 高（像素），未知为 0

    @property
    def is_media(self) -> bool:
        return self.kind in ("image", "video")

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "raw_url": self.raw_url,
            "page_url": self.page_url,
            "title": self.title,
            "preview_url": self.preview_url,
            "name": self.name,
            "size": self.size,
            "content_type": self.content_type,
            "category": self.category,
            "kind": self.kind,
        }


class _RenderedPage:
    """渲染结果的轻量响应对象（暴露与 FetchResponse 相同的字段，供 discover 解析）。"""

    def __init__(self, html: str, url: str):
        self.url = url
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.content = html.encode("utf-8", errors="replace")

    @property
    def soup(self) -> BeautifulSoup:
        return BeautifulSoup(self.content, "html.parser")


class Discoverer:
    """单页资源发现器：提取超链接/图片/视频，探测类型与大小，生成 Resource 列表。

    分两轮：
    1. 直接露出层：页面里 img / video / a 指向的真实媒体文件（缩略图居多）。
    2. 详情页跟进层：命中 DETAIL_PATH_RE 的内容页链接（如 /photo/123、/videos/xx）
       批量并行抓取，从 og:image / og:video / HTML 中的 CDN 直链提取真实高清资源。
    """

    LINK_SELECTORS = {
        "a[href]": "href",
        "img[src]": "src",
        "img[data-src]": "data-src",
        "img[data-original]": "data-original",
        "video[src]": "src",
        "video source[src]": "src",
        "source[src]": "src",
        "audio[src]": "src",
        "audio source[src]": "src",
        "meta[content]": "content",  # og:image 等
    }

    def __init__(self, session: FetchSession | None = None, render_mode: bool = False,
                 stop_event: threading.Event | None = None,
                 on_resource: Callable | None = None):
        self.session = session or FetchSession()
        self.render_mode = render_mode  # 渲染模式：入口页经无头浏览器渲染后再解析
        self.stop_event = stop_event    # 非空时各阶段检查；置位则提前停止并返回已收集部分
        self.on_resource = on_resource  # 每确认一个有效资源就回调（流式上屏）
        self.filtered_count = 0  # 本次发现中被过滤的图标/极小文件数
        self.filtered_icons = 0  # 被图标规则过滤的数量
        self.pages_followed = 0  # 本次实际跟随的分页数（>0 表示翻过页）
        self.api_records: list[dict] = []  # 渲染时捕获的接口 JSON（见 render_page_api）
        self._adapter = None  # 当前页面命中的平台适配器（见 platform_adapters）

    @property
    def stopped(self) -> bool:
        return bool(self.stop_event and self.stop_event.is_set())

    def _proxy_for_render(self):
        """渲染用的代理：会话启用代理时用它，避免本地无代理时拖慢/失败。"""
        proxy = config.DEFAULT_PROXY if getattr(self.session, "_proxy_enabled", False) else None
        return proxy or None

    def _render_proxy_chain(self, url: str) -> list[str | None]:
        """渲染代理候选链（与 FetchSession 同一套：中转 > 池按站 > 直连）。

        返回依次尝试的代理列表（None 表示直连）。池代理按站点绑定，
        失败时吊销并由调用方更换下一个候选，避免渲染路径只试一个代理。
        """
        chain: list[str | None] = []
        if getattr(self.session, "_proxy_enabled", False):
            if config.DEFAULT_PROXY:
                chain.append(config.DEFAULT_PROXY)
            else:
                from resources_reptile.utils.proxy import current_pool
                host = (urlparse(url).hostname or "").lower()
                p = current_pool().proxy(host)
                if p:
                    chain.append(p)
        chain.append(None)  # 直连兜底（无代理重试）
        return chain

    def _render_once(self, url: str, proxy: str | None, capture_api: bool,
                     filters, scroll: int):
        """单次渲染尝试：成功返回 Html 或响应，失败返回 None。"""
        if capture_api:
            from renderer import render_page_api
            html, apis = render_page_api(url, api_filters=filters, proxy=proxy,
                                         scroll_max=scroll)
            return html, apis
        from renderer import render_page
        html = render_page(url, proxy=proxy)
        return html, []

    def _fetch_page(self, url: str, render: bool = False, capture_api: bool = False):
        """容错获取页面，失败返回 None（分页时单页失败不中断整体）。

        render=True 时先用无头浏览器渲染（JS 动态加载站），失败自动回退静态。
        capture_api=True 时（短视频签名接口站）渲染并捕获页面发出的 JSON 接口
        响应到 self.api_records，供 discover 尾部做 API 资源提取。
        """
        if render:
            from platform_adapters import api_filters_for, page_adapter
            filters = api_filters_for(url) if capture_api else None
            ad = page_adapter(url)
            scroll = ad.scroll_max if ad else 0
            # 完整回退链：本机中转 → 代理池按站 → 直连（与 FetchSession 同语义）
            for cand in self._render_proxy_chain(url):
                html, apis = self._render_once(url, cand, capture_api, filters,
                                               scroll)
                if html:
                    if capture_api:
                        self.api_records.extend(apis)
                    return _RenderedPage(html, url)
                # 池代理渲染失败：吊销换下一个候选（只管池代理，DEFAULT_PROXY/直连不吊销）
                if cand and cand != config.DEFAULT_PROXY:
                    from resources_reptile.utils.proxy import current_pool
                    current_pool().revoke(cand, "render-fail")
        try:
            resp = self.session.get(url)
        except Exception:
            return None
        if resp.status_code >= 400:
            return None
        return resp

    def discover(self, page_url: str, progress_cb: Callable | None = None) -> tuple[list[Resource], str]:
        """发现页面上的资源。返回 (资源列表, 页面标题)。

        支持分页跟随：列表页 SSR 只渲染首屏时，自动抓取 `?page=N` 等后续页
        并把它们的资源合并进来（见 _page_follow_links / config.PAGE_FOLLOW_LIMIT）。
        分页后的内容页链接全部通过，之后再按 _detail_re 跟进详情页。

        抖音等「页面空壳 + 签名接口」的站：HTML 里没有直链，数据全部来自
        浏览器内 JS 带签名调用的接口。适配器注册表命中后自动走
        「渲染 + 捕获接口 JSON」路径，末尾用 API 提取结果补全资源
        （见 platform_adapters.extract_media_from_api）。
        """
        self._adapter = _page_adapter(page_url or "")
        # 命中平台适配器（抖音/快手/小红书）时：页面是 JS 空壳，真实作品全部
        # 来自带签名接口（如 /aweme/v1/web/aweme/post/），即使未勾选渲染模式
        # 也强制走「渲染 + 捕获接口 JSON」路径（与 Scrapy 中间件行为一致），
        # 否则只能扒到页面推荐位等无关内容。
        render = bool(self.render_mode or self._adapter)
        capture_api = bool(self._adapter)
        self.api_records = []
        first = self._fetch_page(page_url, render=render, capture_api=capture_api)
        if first is None:
            raise requests.HTTPError(f"页面加载失败：{page_url}")
        ctype = first.headers.get("Content-Type", "")
        if "html" not in ctype:
            # 直接就是文件的链接：单个资源
            r = Resource(first.url or page_url, page_url=page_url,
                         name=basename_from_url(page_url),
                         size=len(first.content or b""),
                         content_type=ctype)
            return [r], page_url

        soup = first.soup
        # 复用 discover_common 的标题提取（h1/title/og:title）
        title = extract_media_from_html(str(soup), first.url)["title"] or page_url

        # ---- 收集原始链接（首屏 + 跟随的分页）----
        pages: list[tuple] = [(soup, first.url)]
        pending, follow_mode = self._page_follow_links(soup, first.url)
        visited: set[str] = {first.url.rstrip("/")}
        fetched = 0
        total_pending = len(pending)
        # 数字分页（numbered）：各页相互独立，并发抓取提速；
        # 单链分页（chain）：后继页依赖前页解析，只能串行。
        if follow_mode == "numbered" and pending:
            with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
                futs = {pool.submit(self._fetch_page, n): n for n in pending}
                for fut in as_completed(futs):
                    if self.stopped:
                        break
                    nxt = futs[fut]
                    resp = fut.result()
                    if resp is None or "html" not in resp.headers.get("Content-Type", ""):
                        continue
                    pages.append((resp.soup, resp.url))
                    self.pages_followed += 1
                    fetched += 1
                    if progress_cb:
                        progress_cb(fetched, max(len(pending), fetched), "跟随分页")
            pending.clear()  # numbered 已并发抓完，串行兜底循环跳过
        while pending and fetched < config.PAGE_FOLLOW_LIMIT:
            nxt = pending.pop(0)
            key = nxt.rstrip("/")
            if key in visited:
                continue
            visited.add(key)
            if progress_cb:
                progress_cb(fetched + 1, max(total_pending, fetched + 1), "跟随分页")
            resp = self._fetch_page(nxt)
            if resp is None or "html" not in resp.headers.get("Content-Type", ""):
                break
            pages.append((resp.soup, resp.url))
            self.pages_followed += 1
            fetched += 1
            if follow_mode == "chain":
                # 单链分页（rel=next / 下一页）：从新页继续找后继
                nxt2 = self._next_of_page(resp.soup, resp.url)
                if nxt2 and nxt2.rstrip("/") not in visited:
                    pending.append(nxt2)
                    total_pending += 1
            if self.stopped:
                break

        raw_urls: set[str] = set()
        for sub, base in pages:
            for selector, attr in self.LINK_SELECTORS.items():
                for node in sub.select(selector):
                    val = node.get(attr)
                    if not val:
                        continue
                    if attr == "content" and "og:" not in node.get("property", ""):
                        # meta 仅保留 og:image / og:video 等带 property 的
                        continue
                    abs_url = urljoin(base, val.strip())
                    if abs_url.startswith(("http://", "https://")):
                        raw_urls.add(abs_url)

        # ---- 构建资源（图片/视频先升级为高清直链）----
        # 分页站点页面 URL 常带 ?page= 等参数，page_url 统一记首屏 URL。
        page_url_lower = first.url
        resources: list[Resource] = []
        seen: set[str] = set()
        for url in list(raw_urls)[:MAX_RESOURCES]:
            if url in seen:
                continue
            seen.add(url)
            r = Resource(url, page_url=page_url_lower, title=title,
                         preview_url=url)
            # pexels 视频封面：`images.pexels.com/videos/<id>/xxx.jpeg` 映射为真视频直链
            dl = Discoverer._pexels_cover_to_video(url)
            if dl:
                r = Resource(dl[0], page_url=page_url_lower, title=title,
                             preview_url=url, name=dl[1])
                r.kind = "video"
                r.category = "videos"
                resources.append(r)
                continue
            # 图片直接资源：优先升级为高清（缩略图参数 w/h<=500 -> 1200）
            if r.kind == "image":
                hu = highres_url(url)
                if hu != url:
                    r = Resource(hu, page_url=page_url_lower, title=title,
                                 preview_url=url, name=basename_from_url(hu))
            elif r.kind == "video":
                vu = video_highres_url(url)
                if vu != url:
                    r = Resource(vu, page_url=page_url_lower, title=title,
                                 preview_url=url, name=basename_from_url(vu))
            resources.append(r)

        # 排除导航页链接（html/无扩展名路由/首页）——它们不是可下载资源
        def _is_pageish(r: Resource) -> bool:
            parsed = urlparse(r.url)
            path = parsed.path
            # pexels 封面映射的视频直链 `/download/video/<id>/`：显式豁免
            if parsed.netloc.lower().endswith("pexels.com") \
                    and re.match(r"^/download/video/\d+/?$", path, re.IGNORECASE):
                return False
            # 无扩展名下载端点（/download?id=、/dl/xxx）：也是资源候选，
            # 探测时按 Content-Type 归类（MIME 嗅探），不作为页面路由过滤
            if is_download_endpoint(r.url):
                return False
            if path.endswith("/") or path == "":
                return True
            last = path.rsplit("/", 1)[-1].lower()
            if last in ("index.html", "index.htm", "default.html", "default.htm"):
                return True
            if last.endswith((".html", ".htm", ".aspx", ".php", ".jsp", ".asp")):
                return True
            # 无扩展名且不是明显媒体路径（.mp4/.jpg 等）：视为路由/页面，非资源
            if "." not in last:
                return True
            return False

        resources = [r for r in resources if not _is_pageish(r)]

        # 过滤常见网站图标/附属小资源（favicon、.ico 等）
        if config.FILTER_ICONS:
            before = len(resources)
            resources = [r for r in resources if not is_icon_url(r.url)]
            self.filtered_icons = before - len(resources)

        # 分配封面：视频尝试 poster / 同祖先 img
        self._assign_covers(resources, soup)

        # 探测时的每项都会做体积过滤 + 流式回调（见 _probe_in_background）。
        # 全部探测完成前的回调已逐个把有效资源推给 on_resource（GUI 增量上屏）。
        stopped = self.stopped
        self._probe_in_background(resources, progress_cb, on_resource=self.on_resource)

        # 探测出体积后才暴露的极小文件（几十~几百 B 的图标/占位图）也过滤掉
        if config.MIN_RESOURCE_SIZE > 0:
            before = len(resources)
            resources = [r for r in resources if not is_tiny(r)]
            self.filtered_count += before - len(resources)
        if stopped or self.stopped:
            return resources, title

        # 第二轮：跟进详情页，提取真实高清资源（视频原片 / 大图）。
        # 入口本身是内容详情页：直接提取其真实媒体（视频原片 / og 高清图），
        # 不跟进页内推荐视频（避免抓到相关视频/赞助内容）。
        if self._detail_re().search(first.url):
            detail_resources = []
            own = self._extract_from_detail(first.url, soup)
            if own:
                detail_resources = [own]
        else:
            detail_resources = self._follow_detail_pages(soup, first.url)
        if detail_resources:
            resources = self._merge_resources(resources, detail_resources)
        if self.on_resource is not None:
            # 详情页新资源流式补充（探测阶段已回调的直接资源不再重复）
            for r in resources:
                if r in (detail_resources or []):
                    self.on_resource(r)

        # 第三轮：签名接口站（抖音/快手等）——HTML 提取结果不足以覆盖的，补 API 数据
        if capture_api and self.api_records:
            from platform_adapters import extract_media_from_api
            api_items = extract_media_from_api(self.api_records, limit=MAX_RESOURCES,
                                               url=page_url)
            for it in api_items:
                if any(r.url.split("?")[0] == it["url"].split("?")[0] for r in resources):
                    continue
                r = Resource(it["url"], page_url=page_url_lower, title=title,
                             preview_url=it.get("preview") or "",
                             name=it.get("name") or "")
                r.kind = it.get("kind") or "file"
                r.category = {"image": "images", "video": "videos"}.get(r.kind, "others")
                r.raw_url = it.get("alt_url") or ""
                r.width = it.get("width") or 0
                r.height = it.get("height") or 0
                resources.append(r)
            if resources:
                title = title or "(签名接口站)"
        # 统计：页面数与各类别资源数（供 stats.summary() 显示）
        st = _get_stats()
        st.add_page(1 + self.pages_followed)
        for r in resources:
            st.add_category(r.category or r.kind, 1)
        return resources, title

    def _page_follow_links(self, soup, base_url: str) -> tuple[list[str], str]:
        """找出后续分页 URL，返回 (应跟随的页面列表, 跟随模式)。

        模式：
        - "numbered"：数字分页（?page=N），一次性返回 page=2..N 全列表
        - "chain"：单链分页（rel=next / 「下一页」文本），逐页递归跟随
        - ""：无分页，不跟随
        受 config.PAGE_FOLLOW_LIMIT 限制（0 表示不跟随）。
        """
        limit = config.PAGE_FOLLOW_LIMIT
        if limit <= 0:
            return [], ""
        base_netloc = urlparse(base_url).netloc
        follow: list[str] = []
        seen_page: set[str] = set()

        rel_next = ""
        max_page = 0
        for link in soup.find_all("link", rel=True):
            rel_val = link.get("rel")
            rels = set(rel_val) if isinstance(rel_val, list) else set(str(rel_val).split())
            href = link.get("href")
            if not href:
                continue
            abs_h = urljoin(base_url, href)
            if urlparse(abs_h).netloc != base_netloc:
                continue
            if "next" in rels:
                rel_next = abs_h
            if "last" in rels:
                n = self._extract_page_number(abs_h)
                if n and n > max_page:
                    max_page = n

        page_re = re.compile(r"(?:^|[?&])page=(\d+)", re.IGNORECASE)
        for a in soup.find_all("a", href=True):
            abs_h = urljoin(base_url, a["href"])
            if urlparse(abs_h).netloc != base_netloc:
                continue
            if abs_h == base_url.rstrip("/"):
                continue
            m = page_re.search(abs_h)
            if m:
                try:
                    n = int(m.group(1))
                except ValueError:
                    continue
                if n > 1 and n > max_page:
                    max_page = n

        # 数字分页：page=2..N（以第一页 URL 为基础规约到同域路径）
        if max_page > 1:
            base_parsed = urlparse(base_url)
            for n in range(2, min(max_page, limit) + 1):
                q = parse_qsl(base_parsed.query, keep_blank_values=True)
                q = [(k, v) for k, v in q if k.lower() != "page"]
                q.append(("page", str(n)))
                u = urlunsplit((base_parsed.scheme, base_parsed.netloc,
                                base_parsed.path, urlencode(q, doseq=True),
                                base_parsed.fragment))
                if u not in seen_page:
                    seen_page.add(u)
                    follow.append(u)
            return follow, "numbered"

        # 无数字分页：跟随 rel=next（或「下一页」文本链接）逐个翻页
        if rel_next and urlparse(rel_next).netloc == base_netloc:
            return [rel_next], "chain"
        for a in soup.find_all("a", href=True):
            text = (a.get_text(strip=True) or "").strip().lower()
            abs_h = urljoin(base_url, a["href"])
            if urlparse(abs_h).netloc != base_netloc:
                continue
            if abs_h == base_url.rstrip("/"):
                continue
            if text in ("下一页", "下一頁", "next", "next page", "加载更多", "load more", "older"):
                return [abs_h], "chain"
        return [], ""

    @staticmethod
    def _next_of_page(soup, base_url: str) -> str:
        """单链分页模式下，从当前页找后继页 URL（rel=next 优先）。"""
        base_netloc = urlparse(base_url).netloc
        for link in soup.find_all("link", rel=True):
            rel_val = link.get("rel")
            rels = set(rel_val) if isinstance(rel_val, list) else set(str(rel_val).split())
            href = link.get("href")
            if "next" in rels and href:
                abs_h = urljoin(base_url, href)
                if urlparse(abs_h).netloc == base_netloc and abs_h != base_url.rstrip("/"):
                    return abs_h
        for a in soup.find_all("a", href=True):
            text = (a.get_text(strip=True) or "").strip().lower()
            abs_h = urljoin(base_url, a["href"])
            if urlparse(abs_h).netloc != base_netloc:
                continue
            if abs_h == base_url.rstrip("/"):
                continue
            if text in ("下一页", "下一頁", "next", "next page", "older"):
                return abs_h
        return ""

    @staticmethod
    def _extract_page_number(url: str) -> int:
        """从 URL 的 page/start/p 参数提取页码；无则返回 0。"""
        parsed = urlparse(url)
        for key in ("page", "p", "start"):
            vals = parse_qs(parsed.query).get(key)
            if vals and vals[0].strip().isdigit():
                return int(vals[0])
        return 0

    _DETAIL_RE = None

    @classmethod
    def _detail_re(cls):
        if cls._DETAIL_RE is None:
            import re as _re
            cls._DETAIL_RE = _re.compile(config.DETAIL_PATH_RE, _re.IGNORECASE)
        return cls._DETAIL_RE

    def _follow_detail_pages(self, soup, base_url: str = ""):
        """并发的详情页跟进：从内容页提取 og:image（高清图）/ 真实视频 URL。"""
        detail_links: list[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            abs_href = urljoin(base_url, href)
            if not abs_href.startswith(("http://", "https://")):
                continue
            if self._detail_re().search(abs_href):
                detail_links.append(abs_href)

        # 去重并限制数量
        seen = set()
        uniq = []
        for u in detail_links:
            if u not in seen:
                seen.add(u)
                uniq.append(u)
        detail_links = uniq[: config.DETAIL_PAGE_LIMIT]
        if not detail_links:
            return []

        results: list[Resource] = []
        lock = threading.Lock()

        def extract(detail_url: str):
            try:
                resp = self.session.get(detail_url, timeout=12)
            except Exception:
                return
            if resp.status_code >= 400 or self._not_html(resp):
                return
            sub = resp.soup
            r = self._extract_from_detail(resp.url, sub)
            if r:
                with lock:
                    results.append(r)

        pool = ThreadPoolExecutor(max_workers=PROBE_WORKERS)
        try:
            futs = [pool.submit(extract, u) for u in detail_links]
            for fut in as_completed(futs):
                if self.stopped:
                    for f in futs:
                        f.cancel()
                    break
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        # 详情页结果内部去重（同一视频的多个变体/同一图片）
        dedup: dict[str, Resource] = {}
        for r in results:
            dedup.setdefault(self._dedupe_key(r.url), r)
        return list(dedup.values())

    def _not_html(self, resp) -> bool:
        ct = resp.headers.get("Content-Type", "")
        return bool(ct) and "html" not in ct and "text" not in ct

    @staticmethod
    def _pick_best_video(candidates: list[str]) -> str:
        """从候选视频 URL 里选最高清的一个（复用 discover_common）。"""
        return pick_best_video(candidates)

    def _extract_from_detail(self, detail_url: str, soup):
        """从单个详情页提取高清图片或视频资源。返回 Resource 或 None。

        提取逻辑在 discover_common.extract_media_from_html，与 Scrapy spider 共用。
        """
        m = extract_media_from_html(str(soup), detail_url)
        title = m["title"]
        video_url = m["video_url"]
        image_url = m["image_url"]
        if video_url:
            video_url = video_highres_url(video_url)
            r = Resource(video_url, page_url=detail_url, title=title,
                         preview_url=(image_url or ""),
                         name=basename_from_url(video_url))
            r.kind = "video"
            r.category = "videos"
            return r
        if image_url:
            r = Resource(image_url, page_url=detail_url, title=title,
                         preview_url=image_url)
            r.kind = "image"
            r.category = "images"
            return r
        return None

    @staticmethod
    def _merge_resources(direct: list[Resource], details: list[Resource]) -> list[Resource]:
        """合并直接资源与详情页资源：去重，优先详情页的「真实内容」。

        策略：详情页资源代表真实高清资源，优先保留；同名的直接缩略图资源降权。
        视频详情页：direct 里与主视频不同 host 的推荐视频（如 pexels aigc/相关）过滤。
        """
        merged = list(details)
        detail_keys: set[str] = set()
        detail_video_apices: set[str] = set()

        def _apex(host: str) -> str:
            parts = (host or "").lower().split(".")
            return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "")

        for d in details:
            detail_keys.add(Discoverer._dedupe_key(d.url))
            if d.kind == "video":
                detail_video_apices.add(_apex(urlparse(d.url).netloc))
        for d in direct:
            if Discoverer._dedupe_key(d.url) in detail_keys:
                continue
            # 视频：仅过滤「根域名」都不同的外来推荐视频（跨站赞助/相关）。
            # 同根域名（如 pexels 的 www. 与 content. 子域）视为本站内容，保留。
            if d.kind == "video" and detail_video_apices:
                if _apex(urlparse(d.url).netloc) not in detail_video_apices:
                    continue
            merged.append(d)
        return merged

    @staticmethod
    def _dedupe_key(url: str) -> str:
        """视频变体（_tiny/_small/_medium/_large）视为同一资源。"""
        key = urlparse(url).path.rstrip("/")
        for name in ("_tiny", "_small", "_medium", "_large"):
            key = key.replace(name, "")
        return key

    @staticmethod
    def _pexels_cover_to_video(url: str):
        """pexels 视频封面 -> 真视频直链（复用 discover_common）。"""
        return pexels_cover_to_video(url)

    @staticmethod
    def _assign_covers(resources, soup):
        videos = [r for r in resources if r.kind == "video"]
        if not videos:
            return
        # 页面中 video 标签自身的 poster 优先
        for video in soup.find_all("video"):
            src = video.get("src")
            if not src:
                source = video.find("source")
                src = source.get("src") if source else None
            poster = video.get("poster")
            if src and poster:
                abs_src = urljoin(str(soup), str(src)).rstrip("/")
                for r in videos:
                    if r.url.rstrip("/") == abs_src:
                        r.preview_url = poster
                        break
        # 其次使用 og:image 作为该页视频的封面
        if any(r.preview_url == r.url for r in videos):
            meta = soup.find("meta", {"property": "og:image"})
            if meta and meta.get("content"):
                for r in videos:
                    if r.preview_url == r.url:
                        r.preview_url = meta["content"]
        # 查找视频链接所在容器内的 img 作为封面
        if any(r.preview_url == r.url for r in videos):
            for a in soup.find_all("a"):
                href = a.get("href")
                if not href:
                    continue
                abs_href = urljoin(str(soup), str(href)).rstrip("/")
                for r in videos:
                    if r.url.rstrip("/") == abs_href:
                        img = a.find("img")
                        if img and img.get("src"):
                            r.preview_url = urljoin(str(soup), str(img["src"]))
                        break

    def _probe_in_background(self, resources, progress_cb, only_unknown=True,
                             on_resource: Callable | None = None):
        """并发探测：每个资源最多发一次 Range 请求，同时拿内容类型/大小/分辨率。

        历史实现先发 HEAD 再发 Range 拿头部，探测开销翻倍（且更易被限流）。
        现在合并为单次 Range 请求（read_prefix 返回头部字节 + 完整长度 + 内容类型）。

        探测完成的资源立即可回调（on_resource）：过滤极小文件后再推给上游，
        实现「抓到一个显示一个」；stop_event 置位时终止剩余提交并立即返回。
        """
        to_probe = [r for r in resources
                    if (not only_unknown or r.size == 0) or r.kind in ("image", "video")]

        def probe(r: Resource):
            try:
                # 已知大小且非媒体文件：无需再探测
                if r.kind not in ("image", "video") and r.size > 0:
                    return r
                data, total, ct = self.session.read_prefix(r.url, 65536, timeout=15)
                if not data:
                    return r
                if ct:
                    r.content_type = ct
                    self._reclassify_by_ct(r)
                if total and r.size == 0:
                    r.size = total
                if r.kind in ("image", "video"):
                    dims = dimensions_from_head(data, r.kind)
                    if dims:
                        r.width, r.height = dims
            except Exception:
                pass
            return r

        if not to_probe:
            return
        done = 0
        pool = ThreadPoolExecutor(max_workers=PROBE_WORKERS)
        try:
            futures = {pool.submit(probe, r): r for r in to_probe}
            for fut in as_completed(futures):
                if self.stopped:
                    for f in futures:
                        f.cancel()
                    break
                done += 1
                r = futures[fut]
                if on_resource is not None and config.MIN_RESOURCE_SIZE > 0 \
                        and is_tiny(r):
                    self.filtered_count += 1
                else:
                    if on_resource is not None:
                        on_resource(r)
                if progress_cb:
                    progress_cb(done, len(to_probe), "探测资源")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _reclassify_by_ct(r: Resource):
        """根据 Content-Type 修正类型（无扩展名链接）。"""
        if r.kind != "file":
            return
        ct = r.content_type
        if "image" in ct:
            r.kind = "image"
            r.category = "images"
        elif "video" in ct:
            r.kind = "video"
            r.category = "videos"
        elif "audio" in ct:
            r.kind = "file"
            r.category = "audios"



class Downloader:
    """根据已勾选的资源列表，下载到目标目录（断点续载：已存在跳过）。"""

    def __init__(self, outdir: str, session: FetchSession | None = None,
                 workers: int | None = None, filename_template: str = ""):
        self.outdir = outdir or config.INFORMATION_DIR
        self.session = session or FetchSession()
        self.workers = workers or config.DOWNLOAD_WORKERS
        self.filename_template = filename_template or config.FILENAME_TEMPLATE
        self.stat = Statistics()
        # 失败原因记录：文件名 -> 人类可读原因（供 GUI 展示 403/超时/不完整等）
        self.failures: dict[str, str] = {}
        # 本次会话的成败 URL（下载完合并进 outdir/failures.json，供重启后重试）
        self.ok_urls: set[str] = set()
        self.fail_urls: dict[str, str] = {}

    def start(self, resources: list[Resource], progress_cb=None):
        # 下载前的最后一道防线：过滤图标/极小文件，避免落到磁盘
        resources = [r for r in resources
                     if not is_icon_url(r.url) and not is_tiny(r)]
        self.stat.set_total(len(resources))
        done = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._download_one, r, done_idx): r
                       for done_idx, r in enumerate(resources)}
            for fut in as_completed(futures):
                r = futures[fut]
                r_name, ok, reason = fut.result()
                done += 1
                if ok:
                    self.stat.ok()
                    self.ok_urls.add(r.url)
                    self.fail_urls.pop(r.url, None)
                else:
                    self.stat.fail()
                    self.ok_urls.discard(r.url)
                    if reason:
                        self.failures[r_name] = reason
                        self.fail_urls[r.url] = reason
                if progress_cb:
                    progress_cb(done, len(resources), r_name, ok)
        self._persist_failures()
        _get_stats().mark_finish()
        _get_stats().save_json(self.outdir)

    def _persist_failures(self):
        """把本次成败合并进 outdir/failures.json：成功即从历史失败中移除，失败则记原因。"""
        try:
            entries = load_failures(self.outdir)
        except Exception:
            entries = {}
        for url in self.ok_urls:
            entries.pop(url, None)
        if self.fail_urls:
            ts = datetime.now().isoformat(timespec="seconds")
            for url, reason in self.fail_urls.items():
                entries[url] = {"reason": reason, "failed_at": ts}
        save_failures(self.outdir, entries)

    def _download_one(self, r: Resource, idx: int) -> tuple[str, bool, str]:
        rel = render_dest_template(r, self.filename_template)
        dest_dir = os.path.join(self.outdir, os.path.dirname(rel))
        # 非法/非 http URL 直接跳过（永久失败，不重试），避免崩下载线程池
        if not r.url.startswith(("http://", "https://")):
            return r.name, False, "非 HTTP(S) 链接"
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            return r.name, False, f"目录不可用: {exc}"
        # 图片下载统一用高清 URL（缩略图链接自动升级到高清，避免落盘小图）
        dl_url = r.url
        if r.kind == "image":
            dl_url = highres_url(r.url)
        elif r.kind == "video":
            dl_url = video_highres_url(r.url)
        headers = browser_headers(referer=r.page_url)
        # 下载必须禁用内容压缩：否则 Content-Length（未压缩值）与
        # curl 自动解压后的字节数不符，导致完整性校验误判文件损坏。
        headers = {k: v for k, v in headers.items()
                   if k.lower() not in ("accept-encoding",)}
        headers.setdefault("Accept-Encoding", "identity")
        # 高清 URL 探测：404（原图不存在）时回退到原 URL 下载；
        # 签名直链失效（抖音等 403）时回退到 raw_url（稳定下载端点）。
        probe_status = 0
        try:
            probe_status = self.session.head(dl_url, headers=headers).status_code
        except Exception:
            probe_status = 0
        if probe_status >= 400:
            alt = getattr(r, "raw_url", "") or ""
            if alt and alt != dl_url:
                dl_url = alt
            elif dl_url != r.url:
                dl_url = r.url
        dest = safe_filename(dl_url, os.path.join(dest_dir, os.path.basename(rel)))
        # 断点续载：已有文件直接跳过
        if config.RESUME_EXISTING and os.path.exists(dest) and os.path.getsize(dest) > 0:
            return r.name, True, ""
        # 全局下载存档（yt-dlp download-archive 语义）：任一候选地址已成功下载过
        # 即跳过，即使本地文件已被清理——定时跟进博主时只下新增作品。
        from download_archive import contains as _arch_contains
        if _arch_contains(r.url) or _arch_contains(dl_url) or _arch_contains(
                getattr(r, "raw_url", "") or ""):
            return r.name, True, ""
        # 死链列表（404/410/451 永久失败）：直接跳过，不再重试
        from dead_list import is_dead as _is_dead
        if _is_dead(r.url) or _is_dead(dl_url):
            return r.name, True, ""
        tmp = dest + ".part"
        retries = int(config.DOWNLOAD_RETRY_TIMES)
        backoff = float(config.DOWNLOAD_RETRY_BACKOFF)
        last_reason: str | None = None
        last_status = 0
        retried = 0
        # 1 次正常尝试 + DOWNLOAD_RETRY_TIMES 次分桶退避重试：
        # - 429：按 Retry-After（未给出则双倍指数），上限 60s
        # - 网络层错误（status=0）：快速重试（0.5s/1s/2s…，上限 8s）
        # - 5xx 等：原指数退避（3s/6s/12s…）
        # - 404/410 等非瞬时：不重试
        for attempt in range(retries + 1):
            try:
                self._download_attempt(r, dl_url, headers, tmp, dest)
                _st = _get_stats()
                size = os.path.getsize(dest) if os.path.exists(dest) else 0
                _st.add_downloaded(1, size)
                _st.add_category(r.kind, 1)
                # 全局下载存档：成功即记录（原链接 + 高清/回退地址都记，防止
                # 下次爬到不同变体地址时重复下载）
                from download_archive import record as _arch_record
                for u in {r.url, dl_url, getattr(r, "raw_url", "") or ""}:
                    if u:
                        _arch_record(u, size)
                return r.name, True, ""
            except _DownloadError as exc:
                last_reason = exc.reason
                last_status = exc.status
                if not exc.retryable or attempt >= retries:
                    break
                retried = attempt + 1
                if exc.status == 429:
                    wait = exc.retry_after or (backoff * (2 ** attempt) * 2)
                    wait = min(max(wait, 1.0), 60.0)
                elif exc.status == 0:
                    wait = min(0.5 * (2 ** attempt), 8.0)
                else:
                    wait = backoff * (2 ** attempt)
                time.sleep(wait)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        # 404/410/451 永久失败 → 记入死链表，下次直接跳过
        from dead_list import DEAD_STATUS as _DEAD_STATUS, mark_dead as _mark_dead
        if last_status in _DEAD_STATUS:
            for u in {r.url, dl_url, getattr(r, "raw_url", "") or ""}:
                if u:
                    _mark_dead(u, last_status)
        reason = last_reason or "未知下载错误"
        if retried:
            reason = f"{reason}（重试 {retried} 次后仍失败）"
        _get_stats().add_failed(1, reason=reason,
                                host=(urlparse(r.url).hostname or "") or "")
        return r.name, False, reason

    def _download_attempt(self, r: Resource, dl_url: str, headers: dict,
                          tmp: str, dest: str):
        """单次下载尝试。成功返回；失败抛 _DownloadError（含是否应重试）。"""
        # HLS（m3u8）分片流：走专用合并下载，输出 .ts（直链下载不适用）
        from hls_downloader import download_hls, is_hls
        try:
            hls_ct = self.session.head(dl_url, headers=headers).headers.get(
                "Content-Type", "")
        except Exception:
            hls_ct = ""
        if is_hls(dl_url, hls_ct):
            hls_dest = os.path.splitext(dest)[0] + ".ts"
            got = download_hls(
                dl_url, dest_dir=os.path.dirname(dest),
                out_name=os.path.splitext(os.path.basename(dest))[0],
                referer=headers.get("Referer", ""),
                workers=int(getattr(config, "HLS_WORKERS", 8)),
                max_segments=int(getattr(config, "HLS_MAX_SEGMENTS", 15_000)),
            )
            if got:
                if got != hls_dest:
                    try:
                        os.replace(got, hls_dest)
                    except OSError:
                        pass
                return
            raise _DownloadError("m3u8 分片合并失败（加密/直播/代理中断）",
                                 retryable=False)
        # 签名直链失效（抖音等 403/过期）时回退到 raw_url（稳定下载端点）。
        # 注意：有些服务器 HEAD 能过但 GET 才拒（403），这里只做 HEAD 探测；
        # GET 阶段的 403 由下方 _download_attempt_once 的 raw_url 兜底覆盖。
        try:
            probe_status = self.session.head(dl_url, headers=headers).status_code
            if probe_status >= 400:
                alt = getattr(r, "raw_url", "") or ""
                if alt and alt != dl_url:
                    dl_url = alt
                elif dl_url != r.url:
                    dl_url = r.url
        except Exception:
            pass  # HEAD 探测异常视为瞬时，先按当前地址试 GET
        # GET 起步阶段 403（签名链失效）→ 自动换 raw_url 重跑一次完整下载
        if dl_url != getattr(r, "raw_url", ""):
            try:
                self._download_attempt_once(r, dl_url, headers, tmp, dest)
                return
            except _DownloadError as exc:
                alt = getattr(r, "raw_url", "") or ""
                if not (alt and exc.reason and "HTTP 4" in exc.reason):
                    raise
                try:
                    self._download_attempt_once(r, alt, headers, tmp, dest)
                except _DownloadError:
                    raise
                return
        self._download_attempt_once(r, dl_url, headers, tmp, dest)

    def _download_attempt_once(self, r: Resource, dl_url: str, headers: dict,
                               tmp: str, dest: str):
        """单地址单次下载尝试。成功返回；失败抛 _DownloadError。"""
        try:
            size = 0
            declared = 0
            # 先探测文件大小，判断是否走分片下载（规避单流长度受限的代理）
            try:
                probe = self.session.head(dl_url, headers=headers)
                declared = int(probe.headers.get("Content-Length") or 0)
            except Exception:
                declared = 0
            # 本地代理对单条长连接超上限会断流：>8MB 时先用 Range 分片下载
            if declared > 8 * 1024 * 1024:
                try:
                    size = self.session.download_ranges(
                        dl_url, declared, headers=headers,
                        chunk_mb=7.0, progress_cb=None, dest_path=tmp,
                    )
                except Exception:
                    size = 0
                if size < declared:
                    # Range 被服务器拒绝（部分反爬/限流只放行整段 GET）：
                    # 清空分片残留，退回单流下载，靠下方完整性补拉兜底。
                    try:
                        size = 0
                        with open(tmp, "wb") as f:
                            for chunk, total in self.session.iter_content(
                                    dl_url, headers=headers, chunk_size=65536):
                                if chunk:
                                    f.write(chunk)
                                    size += len(chunk)
                    except Exception:
                        size = 0
            else:
                with open(tmp, "wb") as f:
                    for chunk, total in self.session.iter_content(dl_url, headers=headers, chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            size += len(chunk)
                            if not declared:
                                declared = total
            if size == 0:
                # 空文件：视为失败（可能被反爬拦空），瞬时，交给重试
                raise _DownloadError("下载内容为空（可能被反爬拦截）", True)
            # 完整性：声明了 Content-Length 但实际不足（流被代理掐断）→ 用 Range 补拉缺失部分
            if declared and size < declared:
                missing = declared - size
                fixed = False
                if 0 < missing <= declared:
                    try:
                        got, ok = self.session._fetch_range(
                            dl_url, size, declared - 1, headers
                        )
                        if ok and len(got) == missing:
                            with open(tmp, "ab") as f:
                                f.write(got)
                            size = declared
                            fixed = True
                    except Exception:
                        pass
                if not fixed:
                    # 补拉失败：改用分片完整重下（分片每段独立验证，稳定）
                    try:
                        size = self.session.download_ranges(
                            dl_url, declared, headers=headers,
                            chunk_mb=7.0, progress_cb=None, dest_path=tmp,
                        )
                    except Exception:
                        pass
                if size < declared:
                    raise _DownloadError("文件不完整（Content-Length 不足，补拉失败）", True)
            # 图片 integrity：校验文件头 magic，非图片文件不落盘
            if r.kind == "image":
                if not _looks_like_image(tmp):
                    raise _DownloadError("非图片文件（疑似被反爬拦截/错误页）", False)
            if os.path.exists(tmp):
                # 下载后发现仍是极小文件（如 404 小页面/图标）：不落盘
                if config.MIN_RESOURCE_SIZE > 0 and size < config.MIN_RESOURCE_SIZE:
                    raise _DownloadError(
                        f"文件过小（{size} B，疑似图标/错误页）", False)
                os.replace(tmp, dest)
                # 移入目标后如再成功，无需清理 tmp（已被 rename 走）
                return
        except _DownloadError:
            raise
        except Exception as exc:
            status = _exc_status(exc)
            raise _DownloadError(
                f"异常: {type(exc).__name__}: {str(exc)[:80]}",
                _code_retryable(status) if status else True,
                status=status,
                retry_after=_retry_after_seconds(exc)) from exc
