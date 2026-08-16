"""统一 HTTP 获取层。

后端选择：
1. curl_cffi（Scrapling 的依赖之一）—— 可模拟浏览器 TLS/JA3 指纹、
   HTTP/2，且支持 `head()` 与流式下载、本机代理。requests 风格 API。
2. 不可用时回退到 requests。

这借鉴了 Scrapling 的 Fetcher（TLS 指纹模拟、代理）思路，
以及 MediaCrawler 的「代理池 + 优雅回退」设计。
"""

from __future__ import annotations

import io
import os
import random

import config

try:  # curl_cffi 可用（推荐）
    import curl_cffi.requests as _cr

    CURL_CFFI_AVAILABLE = True
except Exception:  # pragma: no cover
    _cr = None
    CURL_CFFI_AVAILABLE = False

import requests
from bs4 import BeautifulSoup

from resources_reptile.utils.proxy import pool as _proxy_pool
from resources_reptile.utils.user_agents import random_user_agent

# 403/网络异常时的指纹回退序列（借鉴 MediaCrawler 的优雅降级）
_IMPERSONATE_FALLBACKS = ("chrome", "firefox", "safari", "chrome120", "edge")


def _scrapling_get(url: str, headers: dict | None, timeout: int,
                   proxy: str | None, impersonate: str | None) -> "FetchResponse | None":
    """第三层兜底：curl_cffi/requests 全部失败时用 Scrapling Fetcher 再试一次。

    Scrapling 的 Fetcher 是 curl-cffi 的封装（补充 http3 协商、stealthy 头），
    对少数「严格 TLS 风控」站点成功率更高。轻量语义（同 requests），
    不引入 Playwright/Camoufox 等重型依赖。成功返回 FetchResponse，否则 None。
    """
    if not config.SCRAPLING_FALLBACK:
        return None
    try:
        import logging
        from scrapling.fetchers import Fetcher
        logging.getLogger("scrapling").setLevel(logging.WARNING)
    except Exception:
        return None
    try:
        resp = Fetcher().get(
            url,
            impersonate=impersonate or config.IMPERSONATE,
            timeout=timeout,
            follow_redirects="safe",
            retries=1,
            headers=headers or {},
            proxies={"http": proxy, "https": proxy} if proxy else None,
        )
    except Exception:
        return None
    if not getattr(resp, "status", 0) or resp.status >= 400:
        return None
    return FetchResponse(
        status_code=resp.status,
        headers=dict(resp.headers or {}),
        content=resp.body or b"",
        url=str(getattr(resp, "url", None) or url),
    )


def _session_proxy(host: str) -> str | None:
    """当前请求应使用的代理：本机中转优先，其次代理池按站绑定。"""
    if config.DEFAULT_PROXY:
        return config.DEFAULT_PROXY
    return _proxy_pool.proxy(host)


class FetchSession:
    """统一会话：get/head/iter_content 接口一致，返回 FetchResponse。

    身份模型（借鉴 MediaCrawler）：
    - 每个会话是一个「指纹 + 出口 IP」绑定身份：构造时按配置决定指纹
      （固定 chrome / 随机指纹），代理由 _session_proxy 按站挑选；
    - 遭遇 403 或连接级失败，自动优雅回退：
      1) 代理池模式：吊销当前代理、换池中另一代理重试；
      2) 关闭代理直连重试；
      3) 依次更换浏览器指纹重试。
    这样既能满足需要代理的国外站点，又能绕过像 pexels 这类
    「代理出口被风控、直连反而放行」的反爬。
    """

    def __init__(self, impersonate: str | None = None, proxy_enabled: bool = True):
        self._impersonate = impersonate or config.IMPERSONATE
        if config.IMPERSONATE_RANDOM and config.IMPERSONATE_OPTIONS:
            # 会话级随机指纹：与代理绑定成「一个身份」，避免换来换去触发风控
            self._impersonate = random.choice(config.IMPERSONATE_OPTIONS)
        self._proxy_enabled = proxy_enabled and config.PROXY_ENABLED
        self._use_cffi = CURL_CFFI_AVAILABLE
        self._session = None
        if self._use_cffi:
            try:
                self._session = _cr.Session(
                    impersonate=self._impersonate,
                    proxies=config.proxy_dict() if self._proxy_enabled
                    and config.DEFAULT_PROXY else None,
                )
            except Exception:
                self._use_cffi = False
        if not self._use_cffi:
            self._session = requests.Session()
            self._session.headers.update(
                {"User-Agent": random_user_agent(), "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
            )
            if self._proxy_enabled and config.DEFAULT_PROXY:
                self._session.proxies = config.proxy_dict()

    @property
    def using_cffi(self) -> bool:
        return self._use_cffi

    def close(self):
        try:
            if self._session is not None:
                self._session.close()
        except Exception:
            pass

    def _wrap(self, resp, url, content=None) -> "FetchResponse":
        body = content if content is not None else resp.content
        return FetchResponse(
            status_code=resp.status_code,
            headers=dict(resp.headers) if resp.headers else {},
            content=body,
            url=resp.url or url,
        )

    def _new_cffi_session(self, impersonate: str, proxy: str | None):
        return _cr.Session(
            impersonate=impersonate,
            proxies={"http": proxy, "https": proxy} if proxy else None,
        )

    def _do_request(self, session, method, url, headers, timeout, stream,
                    proxy: str | None = None):
        args = dict(headers=headers, timeout=timeout)
        if method == "head":
            args["allow_redirects"] = True
        if method == "get":
            args["stream"] = stream
        if proxy:
            # 每次请求动态选代理（代理池按站轮换/绑定）
            args["proxies"] = {"http": proxy, "https": proxy}
        # Cookie 注入：cookies.txt 的规则命中该域名时带上登录态
        # （调用方显式给的 Cookie 优先，不覆盖）
        hdrs = dict(args.get("headers") or {})
        if "Cookie" not in hdrs and "cookie" not in hdrs:
            from urllib.parse import urlparse as _up2
            from resources_reptile.utils.cookies import cookie_for as _cookie_for
            c = _cookie_for((_up2(url).hostname or "").lower())
            if c:
                hdrs["Cookie"] = c
                args["headers"] = hdrs
        return getattr(session, method)(url, **args)

    def _request_with_fallback(self, method, url, headers, timeout, stream=False,
                               proxy_override: str | None = None):
        """带 403/连接失败 优雅回退的请求。返回 (resp, using_cffi)。

        回退链（同一方法内按序尝试，任一成功即返回）：
        1) 按配置：会话指纹 + 当前代理（proxy_override > 本机中转 > 代理池按站）
        2) 连接失败且用了代理池：吊销该代理，换池中另一个代理重试
        3) 连接失败（无池或池空）：撤代理、同指纹直连重试
        4) 403：撤代理 + 依次更换浏览器指纹直连重试
        """
        temp_sessions = []  # 回退时新建的会话，用后立即释放

        def cleanup():
            for s in temp_sessions:
                try:
                    s.close()
                except Exception:
                    pass

        from urllib.parse import urlparse as _up
        host = (_up(url).hostname or "").lower()

        # 1) 首次尝试：会话指纹 + 当前代理
        proxy = proxy_override if proxy_override is not None else (
            _session_proxy(host) if self._proxy_enabled else None)
        # 仅池代理参与吊销/计数（override 与 DEFAULT_PROXY 由外部管理）
        pool_managed = proxy_override is None and proxy and not config.DEFAULT_PROXY
        try:
            resp = self._do_request(self._session, method, url, headers, timeout,
                                    stream, proxy=proxy)
        except Exception:
            if not self._use_cffi:
                raise
            # 2) 连接级失败：若当前用的是代理池 → 吊销并换一个池代理重试
            if pool_managed:
                _proxy_pool.revoke(proxy, "conn-fail")
                alt = _proxy_pool.proxy(host)
                if alt and alt != proxy:
                    s = self._new_cffi_session(self._impersonate, alt)
                    temp_sessions.append(s)
                    try:
                        return self._do_request(s, method, url, headers, timeout,
                                                stream, proxy=alt), True
                    except Exception:
                        cleanup()
                        raise
            # 3) 撤代理、同指纹直连重试
            s = self._new_cffi_session(self._impersonate, None)
            temp_sessions.append(s)
            try:
                return self._do_request(s, method, url, headers, timeout, stream), True
            except Exception:
                cleanup()
                raise

        # 首次就成功（或非 403）：直接返回
        if resp.status_code != 403 or not self._use_cffi:
            if pool_managed:
                _proxy_pool.success(proxy)
            return resp, self._use_cffi

        # 4) 403：吊销当前池代理，撤代理 + 依次更换浏览器指纹重试
        if pool_managed:
            _proxy_pool.revoke(proxy, "403")
        seen = {self._impersonate}
        for imp in (self._impersonate,) + _IMPERSONATE_FALLBACKS:
            if imp in seen:
                continue
            seen.add(imp)
            s = self._new_cffi_session(imp, None)
            temp_sessions.append(s)
            try:
                r = self._do_request(s, method, url, headers, timeout, stream)
                if r.status_code != 403:
                    cleanup()
                    return r, True
            except Exception:
                continue
        # 全部失败：释放临时会话，返回最初的 403
        cleanup()
        return resp, self._use_cffi


    def get(self, url: str, headers: dict | None = None, timeout: int | None = None,
            stream: bool = False, proxy: str | None = None):
        timeout = timeout or config.REQUEST_TIMEOUT
        try:
            resp, using = self._request_with_fallback("get", url, headers, timeout, stream,
                                                      proxy_override=proxy)
        except Exception:
            if not stream:
                # 第三层：常规链（TLS 指纹+代理轮换）全失败 -> Scrapling Fetcher
                fb = _scrapling_get(
                    url, headers, timeout,
                    proxy if proxy is not None else (
                        config.DEFAULT_PROXY if self._proxy_enabled else None),
                    self._impersonate)
                if fb is not None:
                    return fb
            raise
        if not stream and resp.status_code >= 400:
            # 403 已被指纹轮换兜底；其余 4xx/5xx 也值得试一次 Scrapling
            fb = _scrapling_get(
                url, headers, timeout,
                proxy if proxy is not None else (
                    config.DEFAULT_PROXY if self._proxy_enabled else None),
                self._impersonate)
            if fb is not None:
                return fb
        if using:
            return self._wrap(resp, url, None)
        return self._wrap(resp, url, resp.content)

    def head(self, url: str, headers: dict | None = None, timeout: int | None = None,
             allow_redirects: bool = True, proxy: str | None = None):
        timeout = timeout or config.REQUEST_TIMEOUT
        resp, using = self._request_with_fallback("head", url, headers, timeout, False,
                                                  proxy_override=proxy)
        return self._wrap(resp, url, b"")

    def iter_content(self, url: str, headers: dict | None = None, chunk_size: int = 65536,
                     timeout: int = 60, proxy: str | None = None):
        """流式下载文件内容（内存友好），同样带 403/失败回退。

        额外返回 (chunk, content_length)：content_length 为响应头的 Content-Length，
        供调用方校验完整性（流提前中断时能发现文件损坏）。
        """
        resp, using = self._request_with_fallback("get", url, headers, timeout, stream=True,
                                                  proxy_override=proxy)
        if getattr(resp, "status_code", 0) >= 400:
            raise requests.HTTPError(f"HTTP {resp.status_code}: {url}")
        try:
            total = int(resp.headers.get("Content-Length", 0) or 0)
        except (ValueError, TypeError):
            total = 0
        for chunk in resp.iter_content(chunk_size=chunk_size):
            if chunk:
                yield chunk, total

    def read_prefix(self, url: str, n: int = 65536, headers: dict | None = None,
                    timeout: int = 30) -> tuple[bytes | None, int | None, str]:
        """读取文件头部最多 n 字节，并尽可能拿回完整文件长度与内容类型。

        用 Range 请求只拉头部（服务器不支持 Range 时也会截断读取后中断连接）。
        返回 (head_bytes, total_size|None, content_type)：
        - total 来自 Content-Range；content_type 来自响应头
        失败返回 (None, None, "")，调用方静默跳过即可。
        """
        h = dict(headers or {})
        h["Range"] = f"bytes=0-{n - 1}"
        try:
            resp, _using = self._request_with_fallback("get", url, h, timeout, stream=True)
        except Exception:
            return None, None, ""
        if getattr(resp, "status_code", 0) >= 400:
            return None, None, ""
        ct = resp.headers.get("Content-Type", "")
        total = None
        cr = resp.headers.get("Content-Range", "")
        if cr and "/" in cr:
            last = cr.rsplit("/", 1)[1].strip()
            if last.isdigit():
                total = int(last)
        buf = bytearray()
        try:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    buf += chunk
                    if len(buf) >= n:
                        break
        except Exception:
            return None, None, ""
        finally:
            try:
                resp.close()
            except Exception:
                pass
        if not buf:
            return None, None, ""
        return bytes(buf), total, ct

    def download_ranges(self, url: str, total: int, headers: dict | None = None,
                        chunk_mb: float = 7.0, max_retries: int = 5,
                        progress_cb=None, dest_path: str | None = None):
        """分片（HTTP Range）下载完整文件，规避单流长度受限的代理。

        本地代理对单条长连接下载超过一定上限（约 10MB）会强制断开；
        分片每段小于该上限，稳定取回，最后校验并拼合。
        结果逐片写入文件，避免把大文件整体驻留内存。

        :param dest_path: 若指定，直接写入该文件（推荐）；否则写临时文件。
        :return: 完整文件字节数（成功时 == total）
        :raises RuntimeError: 重试后仍无法完整取回
        """
        import tempfile
        if total <= 0:
            total = self._probe_total(url, headers)
        if total <= 0:
            raise RuntimeError("无法探测文件大小")
        slice_size = int(chunk_mb * 1024 * 1024)  # 每片字节数
        if dest_path:
            fobj = open(dest_path, "wb")
        else:
            fd, dest_path = tempfile.mkstemp(suffix=".part")
            fobj = os.fdopen(fd, "wb")
        completed = False
        try:
            done = 0
            attempt = 0
            with fobj as f:
                while done < total:
                    start, end = done, min(done + slice_size - 1, total - 1)
                    got, ok = self._fetch_range(url, start, end, headers)
                    if ok:
                        f.write(got)
                        done = end + 1
                        attempt = 0
                        if progress_cb:
                            progress_cb(done, total)
                        continue
                    attempt += 1
                    if attempt >= max_retries:
                        raise RuntimeError(
                            f"Range 下载失败过多（{max_retries} 次），已下载 {done}/{total}"
                        )
                f.flush()
                size = f.tell()
            if size != total:
                raise RuntimeError(f"文件不完整：{size}/{total}")
            completed = True
            return size
        finally:
            if not completed and os.path.exists(dest_path):
                try:
                    os.remove(dest_path)
                except OSError:
                    pass

    def _probe_total(self, url: str, headers: dict | None) -> int:
        try:
            r = self.head(url, headers=headers)
            return int(r.headers.get("Content-Length") or 0)
        except Exception:
            return 0

    def _fetch_range(self, url: str, start: int, end: int,
                     headers: dict | None) -> tuple[bytes, bool]:
        hdrs = dict(headers or {})
        hdrs["Range"] = f"bytes={start}-{end}"
        try:
            resp = self._do_request(self._session, "get", url, hdrs, config.REQUEST_TIMEOUT, False)
            if resp.status_code == 206:
                data = resp.content
                if len(data) == end - start + 1:
                    return data, True
                # 返回内容超过请求范围（代理可能给了完整文件）：只取需要的部分
                if len(data) > end - start + 1:
                    return data[:end - start + 1], True
            return b"", False
        except Exception:
            return b"", False


class CaseInsensitiveDict(dict):
    def __init__(self, data=None):
        super().__init__()
        if data:
            for k, v in dict(data).items():
                self[str(k).lower()] = v

    def __getitem__(self, key):
        return dict.__getitem__(self, str(key).lower())

    def get(self, key, default=None):
        return dict.get(self, str(key).lower(), default)

    def __contains__(self, key):
        return dict.__contains__(self, str(key).lower())


class FetchResponse:
    __slots__ = ("status_code", "headers", "content", "url")

    def __init__(self, status_code, headers, content, url):
        self.status_code = status_code
        self.headers = CaseInsensitiveDict(headers or {})
        self.content = content or b""
        self.url = url

    @property
    def text(self) -> str:
        charset = (self.headers.get("Content-Type") or "").split("charset=")[-1].strip()
        for enc in (charset or "", "utf-8", "gbk", "latin-1"):
            try:
                return self.content.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return self.content.decode("latin-1", errors="replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code} for {self.url}", response=None)

    @property
    def soup(self) -> BeautifulSoup:
        return BeautifulSoup(self.content, "html.parser")