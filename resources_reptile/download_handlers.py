"""Scrapy 下载处理器：基于 curl_cffi 的 TLS 指纹模拟（借鉴 Scrapling Fetcher）。

- 所有请求（页面/API/媒体）统一走 gui_fetch.FetchSession：
  浏览器 TLS/JA3 指纹 + 本机代理，遇 403/连接失败自动「撤代理直连 +
  换指纹」优雅回退（与 GUI 行为完全一致）。
- 保留内置 HTTP11DownloadHandler 作为 curl_cffi 不可用时的兜底（流式落盘）。

用法（settings.py）：
    DOWNLOAD_HANDLERS = {
        "http": "resources_reptile.download_handlers.ImpersonatedDownloadHandler",
        "https": "resources_reptile.download_handlers.ImpersonatedDownloadHandler",
    }

Scrapy 2.17 的手写处理器需为协程风格：download_request(self, request) 返回
可 await 的协程（spider 参数已被移除）。
"""

from __future__ import annotations

from scrapy.core.downloader.handlers.http11 import (
    HTTP11DownloadHandler,
    maybe_deferred_to_future,
)
from scrapy.http import HtmlResponse, Response as ScrapyResponse
from twisted.internet.threads import deferToThread

import config
from gui_fetch import FetchSession, CURL_CFFI_AVAILABLE
from renderer import close_renderer, render_page


class ImpersonatedDownloadHandler:
    """带浏览器指纹模拟 + 403 优雅回退的下载处理器。"""

    lazy = True

    def __init__(self, crawler):
        self._crawler = crawler
        self._proxy_enabled = crawler.settings.getbool("PROXY_ENABLED", config.PROXY_ENABLED)
        self._impersonate = crawler.settings.get(
            "SCRAPLING_IMPERSONATE", config.IMPERSONATE
        )
        self._session = FetchSession(
            impersonate=self._impersonate,
            proxy_enabled=self._proxy_enabled,
        )
        # curl_cffi 不可用时兜底到内置流式处理器
        self._default_handler = HTTP11DownloadHandler(crawler)

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    async def download_request(self, request):
        if not CURL_CFFI_AVAILABLE:
            return await self._default_handler.download_request(request)
        return await maybe_deferred_to_future(deferToThread(self._fetch, request))

    def _fetch(self, request):
        # 渲染模式：起始页标记 render=True 时，先经无头浏览器渲染再解析。
        # 渲染失败（无浏览器/超时/网络）自动回退静态抓取，不阻塞任务。
        if request.meta.get("render"):
            html = render_page(
                request.url,
                proxy=config.DEFAULT_PROXY if self._proxy_enabled else None,
                user_agent=request.headers.get(b"User-Agent", b"").decode(errors="ignore")
                or None,
            )
            if html:
                return HtmlResponse(
                    request.url,
                    status=200,
                    headers={},
                    body=html.encode("utf-8"),
                    encoding="utf-8",
                    request=request,
                )
        headers = dict(request.headers.to_unicode_dict())
        # 中间件挑选的代理（本机中转或代理池按站绑定）真正生效：
        # 显式传给 FetchSession，与页面请求走同一条池路径
        meta_proxy = request.meta.get("proxy") or None
        resp = self._session.get(
            request.url,
            headers=headers or None,
            timeout=request.meta.get("download_timeout", config.REQUEST_TIMEOUT),
            proxy=meta_proxy,
        )
        if resp.status_code >= 400:
            resp.raise_for_status()
        body = resp.content
        ctype = str(resp.headers.get("Content-Type", "")) or ""
        status = resp.status_code
        # curl_cffi 已自动解压 body，透传的 Content-Encoding/Length 会误导
        # scrapy 的 HttpCompressionMiddleware 对「已解压内容」二次解压而崩溃。
        scrapy_headers = {k: v for k, v in dict(resp.headers).items()
                          if k.lower() not in
                          ("content-encoding", "content-length", "transfer-encoding")}
        # 统一编码优先显式指定或从响应头推导（支持 gbk 中文站点）
        charset = request.encoding
        if not charset and "charset=" in ctype.lower():
            charset = ctype.lower().split("charset=")[-1].split(";")[0].strip().strip('"')
        if "html" in ctype.lower():
            rsp = HtmlResponse(
                request.url,
                status=status,
                headers=scrapy_headers,
                body=body,
                encoding=charset or "utf-8",
                request=request,
            )
        else:
            rsp = ScrapyResponse(
                request.url,
                status=status,
                headers=scrapy_headers,
                body=body,
                request=request,
            )
        return rsp

    async def close(self):
        try:
            self._session.close()
        except Exception:
            pass
        close_renderer()
        return await self._default_handler.close()