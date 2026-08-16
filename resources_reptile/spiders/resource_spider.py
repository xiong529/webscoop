"""通用网站资源爬虫：爬取并下载图片、视频、文档、软件安装包等资源。

发现逻辑复用 discover_common（与 GUI Discoverer 同一套）：
高清 URL 变换、pexels 视频封面 -> 真视频、og:image/og:video 提取、
详情页跟进。Spider 端不再维护第二套弱化逻辑。

用法示例：
    scrapy crawl resource -a start_urls="https://example.com/videos/" \
        -a allowed_domains="example.com,cdn.example.com" \
        -a download_extensions="mp4,mkv,zip"
"""

import re
from urllib.parse import urlparse

import scrapy

import config
from discover_common import (
    classify_url,
    extract_media_from_html,
    highres_url,
    is_download_endpoint,
    pexels_cover_to_video,
    video_highres_url,
)

from ..items import ResourceItem

# GUI 与 Scrapy 共用同一统计单例：CLI 爬完 spider.closed 里统一落盘 stats.json
from stats import get_stats as _get_stats

DEFAULT_DOWNLOAD_EXTENSIONS = (
    "jpg|jpeg|png|gif|webp|bmp|svg|ico|avif|heic|"
    "mp4|mkv|avi|mov|wmv|flv|webm|m4v|ts|rmvb|3gp|"
    "mp3|wav|flac|aac|ogg|m4a|wma|"
    "pdf|doc|docx|xls|xlsx|ppt|pptx|txt|epub|mobi|csv|"
    "exe|msi|apk|dmg|pkg|deb|rpm|jar|"
    "zip|rar|7z|tar|gz|bz2|xz|iso"
)

LINK_SELECTORS = (
    "a::attr(href)",
    "img::attr(src)",
    "img::attr(data-src)",
    "img::attr(data-original)",
    "video::attr(src)",
    "video source::attr(src)",
    "source::attr(src)",
    "audio::attr(src)",
    "audio source::attr(src)",
    "a::attr(data-url)",
    "a::attr(data-href)",
)

# 无扩展名的下载端点特征：见 discover_common.is_download_endpoint


class ResourceSpider(scrapy.Spider):
    name = "resource"

    def __init__(self, start_urls="", allowed_domains="", download_extensions="",
                 max_depth=10, render="0", robots="0", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.start_urls = [u.strip() for u in start_urls.split(",") if u.strip()]
        self.allowed_domains = [d.strip() for d in allowed_domains.split(",") if d.strip()]
        # 始终将起始域名纳入允许域名，保证页面链接可被跟进。
        # 注意用 hostname（不带端口）：allowed_domains 与 OffsiteMiddleware
        # 的 hostname 比较同源，netloc 带端口会导致媒体请求被误判为站外。
        start_domains = [urlparse(u).hostname or "" for u in self.start_urls]
        start_domains = [d.removeprefix("www.").lower() for d in start_domains if d]
        self.allowed_domains = list(dict.fromkeys(self.allowed_domains + start_domains))
        # 渲染模式：起始页经无头浏览器渲染后再解析（JS 动态加载站点）
        self.render_mode = str(render).strip().lower() in ("1", "true", "yes")
        # 遵守 robots.txt：把起始域名临时加入遵守名单（其余站默认豁免）
        if str(robots).strip().lower() in ("1", "true", "yes"):
            for u in self.start_urls:
                host = urlparse(u).hostname
                if host:
                    config.ROBOTS_POLICY[host.lower()] = True
        exts = download_extensions.strip() or DEFAULT_DOWNLOAD_EXTENSIONS
        self.download_ext = set(e.lower() for e in re.split(r"[\s,|]+", exts) if e.strip())
        self.resource_pattern = re.compile(
            r"\.(" + "|".join(re.escape(e) for e in self.download_ext) + r")(?:\?.*)?$",
            re.IGNORECASE | re.VERBOSE,
        )
        try:
            self.max_depth = int(max_depth)
        except (TypeError, ValueError):
            self.max_depth = 10
        # 详情页特征与数量上限（与 GUI Discoverer 同源）
        self.detail_path_re = re.compile(config.DETAIL_PATH_RE, re.IGNORECASE)
        self.detail_limit = config.DETAIL_PAGE_LIMIT
        self._detail_seen: set[str] = set()

    def _safe_url(self, url, response):
        url = response.urljoin(url.strip())
        # 去掉 HTTP 之外的协议
        if not url.startswith(("http://", "https://")):
            return None
        return url

    def _same_site(self, url):
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[len("www."):]
        return any(
            netloc == dom or netloc.endswith("." + dom)
            for dom in self.allowed_domains
        )

    async def start(self):
        """起始页：render=1 时标记渲染模式（仅入口页走浏览器渲染）。

        Scrapy 2.13+ 以 Spider.start() 生成起始请求（start_requests 已废弃）。
        """
        for url in self.start_urls:
            yield scrapy.Request(
                url,
                callback=self.parse,
                meta={"referer": url, "depth": 0, "render": self.render_mode},
                dont_filter=True,
            )

    def _upgrade_url(self, url: str) -> str:
        """高清变换 + pexels 视频封面 -> 真视频直链（复用 discover_common）。"""
        dl = pexels_cover_to_video(url)
        if dl:
            return dl[0]
        kind = classify_url(url)
        if kind == "images":
            return highres_url(url)
        if kind == "videos":
            return video_highres_url(url)
        return url

    def _looks_like_download(self, url: str) -> bool:
        """无扩展名 URL 是否为下载端点（/download?id=、/dl/xxx 等）。"""
        return is_download_endpoint(url)

    def parse(self, response):
        depth = response.meta.get("depth", 0)
        resource_urls = set()
        page_links = set()
        detail_links = []
        _get_stats().add_page(1)

        # 空壳平台（抖音/快手/小红书等）：渲染中间件已从捕获的接口响应
        # 提取媒体条目，直接产出，不再对空壳 HTML 做选择器解析
        api_res = response.meta.get("render_api_resources")
        if api_res:
            final = set()
            names = []
            for it in api_res:
                # 下载稳定性优先：alt_url（官方稳定端点）> 签名的播放直链
                u = it.get("alt_url") or it.get("url")
                if u:
                    final.add(u)
                    names.append(str(it.get("name") or "")[:120])
            if final:
                st = _get_stats()
                for u in final:
                    st.add_category(classify_url(u), 1)
                yield ResourceItem(
                    url=response.url,
                    title=self._extract_title(response) or response.url,
                    file_urls=sorted(final),
                    file_names=names if len(names) == len(final) else [],
                )
            # 空壳页仍可能有少量入口链接，继续普通解析（通常无命中）
            self.logger.info("adapter %s: %d resources from API capture",
                             response.url, len(api_res))

        for selector in LINK_SELECTORS:
            for raw in response.css(selector).getall():
                url = self._safe_url(raw, response)
                if not url:
                    continue
                parsed = urlparse(url)
                # 直链端点（/download/video/<id>/、/download/file/...）：是资源而非详情页
                if "/download/" in parsed.path and any(
                        t in parsed.path for t in ("/video", "/file", "/media")):
                    resource_urls.add(url)
                    continue
                # 判断是否为资源文件
                if self.resource_pattern.search(url):
                    resource_urls.add(url)
                elif self._looks_like_download(url):
                    # 无扩展名下载端点：交给管道按 Content-Type 归类
                    resource_urls.add(url)
                elif self.detail_path_re.search(url):
                    detail_links.append(url)
                else:
                    page_links.add(url)

        # 页面级 og:image / og:video（整页媒体，与 GUI 同一套提取逻辑）
        og = extract_media_from_html(response.text, response.url)
        if og.get("video_url") and self.resource_pattern.search(og["video_url"]):
            resource_urls.add(og["video_url"])
        if og.get("image_url"):
            resource_urls.add(og["image_url"])

        # 高清升级 + 封面映射
        final = {self._upgrade_url(u) for u in resource_urls}
        # 产出资源 Item
        if final:
            st = _get_stats()
            for u in final:
                st.add_category(classify_url(u), 1)
            yield ResourceItem(
                url=response.url,
                title=self._extract_title(response),
                file_urls=sorted(final),
                file_names=[],
            )

        if depth >= self.max_depth:
            return

        # 跟进详情页：提取真实视频原片 / og:image 高清图
        followed = 0
        for url in detail_links:
            if followed >= self.detail_limit:
                break
            if url in self._detail_seen:
                continue
            if not self._same_site(url):
                continue
            self._detail_seen.add(url)
            followed += 1
            yield scrapy.Request(
                url,
                callback=self.parse_detail,
                meta={"referer": response.url, "depth": depth + 1},
            )
        # 继续跟踪页面链接（仅在允许的域名内，且控制深度）
        for url in page_links:
            if not self._same_site(url):
                continue
            yield scrapy.Request(
                url,
                callback=self.parse,
                meta={"referer": response.url, "depth": depth + 1},
            )

    def parse_detail(self, response):
        """详情页：提取 og:video / og:image / CDN 直链（复用 discover_common）。"""
        # 防误跟进：详情链接实际指向媒体文件/重定向时，直接跳过
        ct = (response.headers.get("Content-Type") or b"").decode("latin-1", "replace")
        if ct and "html" not in ct and "text" not in ct:
            return
        _get_stats().add_page(1)
        m = extract_media_from_html(response.text, response.url)
        final = set()
        if m.get("video_url"):
            final.add(video_highres_url(m["video_url"]))
        if m.get("image_url"):
            final.add(m["image_url"])
        if final:
            st = _get_stats()
            for u in final:
                st.add_category(classify_url(u), 1)
            yield ResourceItem(
                url=response.url,
                title=m["title"] or response.url,
                file_urls=sorted(final),
                file_names=[],
            )

    @staticmethod
    def _extract_title(response):
        title = response.css("title::text").get("")
        return title.strip() or response.url

    def closed(self, reason):
        """爬完统一落盘 stats.json（FILES_STORE 目录，与下载文件同处）。"""
        st = _get_stats()
        st.mark_finish()
        store = self.settings.get("FILES_STORE", "downloads")
        saved = st.save_json(store)
        if saved:
            self.logger.info("stats saved -> %s\n%s", saved, st.summary())
        else:
            self.logger.warning("stats.json 落盘失败（目录 %s 不可写）", store)
