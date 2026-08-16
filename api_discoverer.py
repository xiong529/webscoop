"""Pexels 官方 API 抓取器（https://www.pexels.com/api/documentation/）。

在 GUI「API 抓取」栏填入接口地址 + API Key 即可直接按官方 API 获取
图片/视频资源：支持预设接口（搜索图片/精选图片/搜索视频/热门视频）、
自定义接口地址（含 /photos/:id、/videos/:id、/collections/:id 等）、
自动跟随 next_page 翻页，并把结果统一转为主列表可勾选/下载的
Resource 对象（复用现有下载与预览体系）。

鉴权：请求头 Authorization: <API Key>（见官方文档 Authentication 节）。
"""

from __future__ import annotations

import json
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse

import config
from gui_crawler import Resource, basename_from_url
from gui_fetch import FetchSession

API_HOST = "https://api.pexels.com/v1"

# 预设接口：key -> (中文名, 端点, 是否需要关键词)
PRESETS = {
    "search": ("搜索图片", "/search", True),
    "curated": ("精选图片", "/curated", False),
    "video_search": ("搜索视频", "/videos/search", True),
    "video_popular": ("热门视频", "/videos/popular", False),
}


def build_preset_url(preset: str, keyword: str = "", per_page: int = 15) -> str:
    """构造预设接口的完整地址（如 search+nature -> /v1/search?query=nature&per_page=15）。"""
    entry = PRESETS.get(preset)
    if not entry:
        return ""
    _label, endpoint, needs_kw = entry
    params = {"per_page": max(1, min(int(per_page or 15), 80))}
    if needs_kw:
        params["query"] = (keyword or "").strip() or "nature"
    return f"{API_HOST}{endpoint}?{urlencode(params)}"


def convert_website_url(url: str) -> str:
    """把 pexels 网站地址转换为官方 API 接口地址；非 pexels 地址原样返回。

    支持常见搜索页：
    - https://www.pexels.com/search/<关键词>/        -> /v1/search
    - https://www.pexels.com/search/videos/<关键词>/ -> /v1/videos/search
    - https://www.pexels.com/videos/search/<关键词>/  -> /v1/videos/search
    保留 page= 参数。
    """
    u = (url or "").strip()
    if not u.startswith(("https://www.pexels.com", "http://www.pexels.com",
                         "https://pexels.com", "http://pexels.com")):
        return u
    parsed = urlparse(u)
    segs = [s for s in parsed.path.split("/") if s]
    page = (parse_qs(parsed.query).get("page") or ["1"])[0]
    endpoint = ""
    kw = ""
    if segs and segs[0] == "search":
        rest = segs[1:]
        if rest and rest[0] in ("photos", "videos"):
            endpoint = "/videos/search" if rest[0] == "videos" else "/search"
            kw = " ".join(rest[1:])
        else:
            endpoint = "/search"
            kw = " ".join(rest)
    elif len(segs) >= 2 and segs[0] in ("videos", "video") and segs[1] == "search":
        endpoint = "/videos/search"
        kw = " ".join(segs[2:])
    if not endpoint:
        return u
    params = {"query": kw.replace("-", " ").strip()}
    if page.isdigit() and int(page) > 1:
        params["page"] = str(int(page))
    return f"{API_HOST}{endpoint}?{urlencode(params)}"


_QUALITY_RANK = {"uhd": 4, "4k": 4, "hd": 3, "sd": 2}


def _best_video_file(files) -> dict | None:
    """挑选最高清、可直链下载的 mp4 变体。

    Pexels video_files 同时含 progressive mp4 与 HLS（m3u8），
    后者无法直接下载，先排除；再按 (mp4, 分辨率, 档位) 选取最优。
    """
    best, best_score = None, (-1, -1, -1)
    for f in files or []:
        if not isinstance(f, dict):
            continue
        link = str(f.get("link") or "").strip()
        if not link or not link.startswith("http"):
            continue
        ftype = str(f.get("file_type") or "")
        if ftype and ftype != "video/mp4":
            continue
        if not ftype and not link.lower().split("?")[0].endswith((".mp4", ".webm", ".mkv")):
            continue
        try:
            w, h = int(f.get("width") or 0), int(f.get("height") or 0)
        except (TypeError, ValueError):
            w = h = 0
        q = _QUALITY_RANK.get(str(f.get("quality") or "").lower(), 1)
        score = (1, w * h, q)
        if score > best_score:
            best, best_score = f, score
    return best


def photo_to_resource(p: dict) -> Resource | None:
    """照片对象 -> Resource（下载用 original 原图，预览用 medium）。"""
    src = p.get("src") or {}
    if not isinstance(src, dict):
        return None
    download = str(src.get("original") or src.get("large2x") or src.get("large") or "")
    if not download.startswith("http"):
        return None
    preview = str(src.get("medium") or src.get("small") or src.get("large2x") or download)
    name = basename_from_url(download) or f"pexels-photo-{p.get('id')}.jpg"
    r = Resource(download, page_url=str(p.get("url") or ""),
                 title=str(p.get("alt") or p.get("photographer") or ""),
                 preview_url=preview, name=name)
    r.kind = "image"
    r.category = "images"
    r.width = int(p.get("width") or 0)
    r.height = int(p.get("height") or 0)
    return r


def video_to_resource(v: dict) -> Resource | None:
    """视频对象 -> Resource（下载用最高清 mp4 直链，封面用 image）。"""
    best = _best_video_file(v.get("video_files"))
    if not best:
        return None
    link = str(best.get("link") or "")
    if not link.startswith("http"):
        return None
    preview = str(v.get("image") or "")
    user = v.get("user") or {}
    name = basename_from_url(link) or f"pexels-video-{v.get('id')}.mp4"
    r = Resource(link, page_url=str(v.get("url") or ""),
                 title=str(v.get("alt") or user.get("name") or ""),
                 preview_url=preview, name=name)
    r.kind = "video"
    r.category = "videos"
    r.width = int(best.get("width") or v.get("width") or 0)
    r.height = int(best.get("height") or v.get("height") or 0)
    return r


class ApiError(Exception):
    """Pexels API 调用失败（携带 HTTP 状态码）。"""

    def __init__(self, message: str, status: int = 0):
        self.status = status
        super().__init__(message)


def describe_api_error(resp) -> str:
    code = resp.status_code
    hints = {
        400: "请求参数有误",
        401: "API Key 无效或未提供（请到 https://www.pexels.com/api/ 申请）",
        403: "无权限访问该接口",
        404: "接口地址不存在",
        429: "请求过于频繁，已达 Pexels 限流配额，请稍后重试",
    }
    hint = hints.get(code, "")
    body = ""
    try:
        obj = json.loads(resp.text)
        if isinstance(obj, dict):
            body = str(obj.get("error") or obj.get("message") or "")
    except Exception:
        pass
    parts = [p for p in (f"HTTP {code}", hint, body) if p]
    return " / ".join(parts) or "未知错误"


class ApiDiscoverer:
    """Pexels API 抓取：请求 + 解析 + 自动翻页，生成 Resource 列表。"""

    def __init__(self, session: FetchSession | None = None):
        self.session = session or FetchSession()
        self.rate_remaining = ""   # 响应头 X-Ratelimit-Remaining
        self.rate_limit = ""

    def fetch(self, url: str, api_key: str, max_pages: int = 0,
              progress_cb: Callable | None = None) -> tuple[list[Resource], dict]:
        """按接口地址 + API Key 抓取并自动跟随 next_page 分页。

        :return: (资源列表, 信息字典)；info 含 label / pages / total / quota。
        """
        max_pages = max(1, min(max_pages or config.API_PAGE_LIMIT, 30))
        url = convert_website_url(url)
        resources: list[Resource] = []
        seen: set[str] = set()
        seen_pages: set[str] = set()
        info = {"label": url, "pages": 0, "total": 0, "quota": ""}
        current = url
        for page in range(1, max_pages + 1):
            if current in seen_pages:
                break
            seen_pages.add(current)
            if progress_cb:
                progress_cb(page, max_pages, "获取资源页")
            data, remaining, limit = self._request_json(current, api_key)
            self.rate_remaining = remaining or ""
            self.rate_limit = limit or ""
            if remaining:
                info["quota"] = f"{remaining}/{limit}" if limit else str(remaining)
            info["pages"] = page
            if isinstance(data, dict):
                info["total"] = int(data.get("total_results") or info["total"] or 0)
                for r in self._parse(data):
                    if r.url not in seen:
                        seen.add(r.url)
                        resources.append(r)
            next_page = data.get("next_page") if isinstance(data, dict) else ""
            if not next_page:
                break
            current = str(next_page)
        return resources, info

    def _request_json(self, url: str, api_key: str):
        headers = {"Authorization": api_key.strip(), "Accept": "application/json"}
        try:
            resp = self.session.get(url, headers=headers)
        except Exception as exc:
            raise ApiError(f"网络请求失败：{exc}") from exc
        remaining = resp.headers.get("X-Ratelimit-Remaining") or ""
        limit = resp.headers.get("X-Ratelimit-Limit") or ""
        if resp.status_code >= 400:
            raise ApiError(describe_api_error(resp), resp.status_code)
        try:
            data = json.loads(resp.text)
        except (ValueError, TypeError) as exc:
            raise ApiError(f"响应不是有效 JSON，请检查接口地址：{url}") from exc
        return data, remaining, limit

    @staticmethod
    def _parse(data: dict) -> list[Resource]:
        items: list[Resource] = []
        for p in data.get("photos") or []:
            if isinstance(p, dict):
                r = photo_to_resource(p)
                if r:
                    items.append(r)
        for v in data.get("videos") or []:
            if isinstance(v, dict):
                r = video_to_resource(v)
                if r:
                    items.append(r)
        for m in data.get("media") or []:   # /collections/:id 等混合媒体接口
            if not isinstance(m, dict):
                continue
            t = str(m.get("type") or "").lower()
            if t == "video":
                r = video_to_resource(m)
            elif t == "photo":
                r = photo_to_resource(m)
            else:
                continue
            if r:
                items.append(r)
        if not items:                        # 单条资源接口（/photos/:id 等）
            r = None
            if "video_files" in data:
                r = video_to_resource(data)
            elif "src" in data:
                r = photo_to_resource(data)
            if r:
                items.append(r)
        return items