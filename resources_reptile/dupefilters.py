"""规范化 URL 去重过滤器。

默认的 RFPDupeFilter 对「完整 URL + 请求方法」做指纹去重，翻页/分享链接
带 utm_*、fbclid 等追踪参数时，同一页面会被重复抓取。本过滤器在去重前
做完整规范化（追踪参数剔除 + fragment 删除 + host 小写 + www 归一 +
默认端口剔除 + query 参数排序），再做与 RFPDupeFilter 相同的指纹计算。

用法（settings.py）：
    DUPEFILTER_CLASS = "resources_reptile.dupefilters.NormalizedRFPDupeFilter"
    DUPEFILTER_STRIP_PARAMS = ("utm_", "fbclid", ...)   # 可选覆盖
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunsplit

from scrapy.dupefilters import RFPDupeFilter

DEFAULT_STRIP_PARAMS = (
    "utm_", "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid",
    "yclid", "gbraid", "wbraid", "_ga", "_gl",
)

_DEFAULT_PORTS = {"http": "80", "https": "443", "ftp": "21"}


def strip_tracking_params(url: str, strip_params: tuple[str, ...]) -> str:
    """剔除 URL 查询串中的追踪参数，返回规范化后的 URL。

    前缀参数（以 _ 结尾，如 "utm_"）匹配任意以该前缀开头的参数名；
    其余按完整参数名精确匹配（大小写不敏感）。
    """
    parsed = urlparse(url)
    if not parsed.query:
        return url
    prefixes = {name.lower() for name in strip_params if name.endswith("_")}
    exact = {name.lower() for name in strip_params if not name.endswith("_")}
    kept = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in exact
        and not any(k.lower().startswith(p) for p in prefixes)
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                       urlencode(kept, doseq=True), parsed.fragment))


def normalize_url(url: str, strip_params: tuple[str, ...] = DEFAULT_STRIP_PARAMS) -> str:
    """完整的 URL 规范化，用于去重指纹：

    - 剔除追踪参数（strip_tracking_params）
    - 删除 fragment（#xxx 不发给服务器，同一页的不同锚点视为同一 URL）
    - host 转小写
    - 去掉 www. 前缀（www 与非 www 通常是同一内容）
    - 剔除默认端口（https://a.com:443/ == https://a.com/）
    - query 参数按键排序（参数顺序不同视为同一 URL）
    """
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    userinfo, _, hostport = netloc.rpartition("@")
    host, _, port = hostport.rpartition(":")
    if not host:  # IPv6 或无 host 的异常形态，原样保留
        host = hostport
        port = ""
    if port and _DEFAULT_PORTS.get(parsed.scheme.lower(), "") == port:
        port = ""
    normalized_host = host[:-len("www.")] if host.startswith("www.") else host
    hostport = normalized_host + (f":{port}" if port else "")
    netloc = f"{userinfo}@" + hostport if userinfo else hostport

    # fragment：删除（Scrapy 静态抓取不发送 fragment；SPA 站的路由参数
    # 在 query 中，不受影响）
    query = parsed.query
    q = dict(parse_qsl(query, keep_blank_values=True))
    if strip_params:
        prefixes = {n.lower() for n in strip_params if n.endswith("_")}
        exact = {n.lower() for n in strip_params if not n.endswith("_")}
        q = {k: v for k, v in q.items()
             if k.lower() not in exact
             and not any(k.lower().startswith(p) for p in prefixes)}
    query = urlencode(sorted(q.items()), doseq=True)
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, query, ""))


class NormalizedRFPDupeFilter(RFPDupeFilter):
    """去重前做完整 URL 规范化的 RFPDupeFilter。"""

    def __init__(self, path=None, debug=False, fingerprinter=None,
                 strip_params=DEFAULT_STRIP_PARAMS):
        super().__init__(path=path, debug=debug, fingerprinter=fingerprinter)
        self.strip_params = tuple(strip_params or DEFAULT_STRIP_PARAMS)

    @classmethod
    def from_settings(cls, settings):
        params = settings.gettuple("DUPEFILTER_STRIP_PARAMS") or DEFAULT_STRIP_PARAMS
        return cls(path=settings.get("JOBDIR"), debug=settings.getbool("DUPEFILTER_DEBUG"),
                   strip_params=params)

    def request_fingerprint(self, request):
        url = normalize_url(request.url, self.strip_params)
        if url == request.url:
            return super().request_fingerprint(request)
        return super().request_fingerprint(request.replace(url=url))