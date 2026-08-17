"""下载器中间件：完整反爬策略。

1. 随机 User-Agent（模拟真实浏览器）
2. 模拟浏览器请求头（Accept / Referer / 等）
3. 随机代理 IP（从配置的代理池中挑选）
4. 随机请求延迟 + 重试退避
5. Cookie 会话保持（站点维护中自动切换，避免被封锁）
"""

import random
import time

from twisted.internet import reactor
from twisted.internet.task import deferLater

import config
from .utils.proxy import current_pool
from .utils.user_agents import random_user_agent


class RandomUserAgentMiddleware:
    """为每个请求随机分配一个浏览器 User-Agent。"""

    def __init__(self, crawler):
        use_tool_agents = crawler.settings.getbool("USER_AGENT_INCLUDE_TOOL", False)
        self.use_tool_agents = use_tool_agents

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def process_request(self, request):
        request.headers.setdefault(b"User-Agent", random_user_agent(self.use_tool_agents).encode())


class BrowserHeadersMiddleware:
    """补齐真实浏览器的标准请求头，降低被识别为爬虫的概率。"""

    BROWSER_HEADERS = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        # 不宣称 br：部分站点（如 pexels）的 br 流经网关后偶发损坏，
        # scrapy 的 HttpCompressionMiddleware 解码会直接报错导致整个请求失败。
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
    }

    _MEDIA_DEST_EXT = (
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".heic", ".bmp",
        ".svg", ".ico", ".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v",
        ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".pdf", ".zip", ".rar",
    )

    @staticmethod
    def _sec_fetch_site(target_host: str, referer_host: str) -> str:
        """按 Fetch 规范推算 Sec-Fetch-Site（近似：子域归并到主域）。"""
        if not target_host:
            return "cross-site"
        if not referer_host:
            return "none"
        if referer_host == target_host:
            return "same-origin"
        if referer_host.endswith("." + target_host) or target_host.endswith("." + referer_host):
            return "same-site"
        return "cross-site"

    def process_request(self, request):
        from urllib.parse import urlparse
        for name, value in self.BROWSER_HEADERS.items():
            request.headers.setdefault(name.encode(), value.encode())
        # 补上 Referer：跨域资源请求带上来源，模拟真实页面内的资源加载
        if "Referer" not in request.headers and request.meta.get("referer"):
            request.headers["Referer"] = str(request.meta["referer"]).encode()
        # Sec-Fetch-* 与 Referer 联动：同域 same-origin、跨子域 same-site、
        # 其他 cross-site。写死 same-origin 会被严格反爬（如 Cloudflare）校验出矛盾。
        target_host = (urlparse(request.url).hostname or "").lower()
        ref_raw = request.headers.get("Referer") or request.meta.get("referer") or ""
        if isinstance(ref_raw, bytes):
            ref_raw = ref_raw.decode(errors="replace")
        ref_host = (urlparse(str(ref_raw)).hostname or "").lower()
        request.headers["Sec-Fetch-Site"] = self._sec_fetch_site(target_host, ref_host).encode()
        # 资源请求（图片/视频/文件）不是导航：no-cors + 对应 dest，去掉导航专属头
        if request.url.lower().endswith(self._MEDIA_DEST_EXT):
            dest = "image" if request.url.lower().endswith((
                ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".heic",
                ".bmp", ".svg", ".ico")) else "video" if request.url.lower().endswith((
                ".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v")) else "audio"
            request.headers["Sec-Fetch-Mode"] = b"no-cors"
            request.headers["Sec-Fetch-Dest"] = dest.encode()
            request.headers.pop(b"Sec-Fetch-User", None)
            request.headers.pop(b"Upgrade-Insecure-Requests", None)


class RandomProxyMiddleware:
    """从代理池中挑选代理，吊销失效代理（与 GUI FetchSession 共用同一池）。

    默认关闭代理。通过设置 PROXY_ENABLED=True 启用，
    代理来源见 utils/proxy.py 的 load_proxies()。

    选择逻辑与 FetchSession._session_proxy 一致：本机默认代理优先，
    否则从代理池按站点挑选（同一站点尽量绑定同一出口，避免抖 IP）。
    挑选结果写入 request.meta["proxy"]，由 ImpersonatedDownloadHandler
    实际使用（下载处理器与页面请求走同一条池路径）。
    """

    def __init__(self, crawler):
        self.enabled = crawler.settings.getbool("PROXY_ENABLED", False)
        self.pool = current_pool()
        self.logger = crawler.logger if hasattr(crawler, "logger") else None

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def process_request(self, request):
        if not self.enabled:
            return None
        from urllib.parse import urlparse
        # 本机默认代理优先，其次代理池按站挑选
        proxy = config.DEFAULT_PROXY or self.pool.proxy(
            (urlparse(request.url).hostname or "").lower())
        if proxy:
            request.meta["proxy"] = proxy
        return None

    def process_exception(self, request, exception):
        """代理异常时换一个代理重试，最多 3 次。"""
        if not self.enabled or request.meta.get("proxy") is None:
            return None
        attempts = request.meta.get("proxy_attempts", 0)
        if attempts >= 3:
            return None
        # 吊销失败代理（仅池代理；本机默认代理失败由 FetchSession 的
        # 403/连接回退链自行处理，不在这里吊销）
        cur = request.meta["proxy"]
        if cur != config.DEFAULT_PROXY:
            self.pool.revoke(cur, "conn-fail", force=True)
        other = config.DEFAULT_PROXY or self.pool.proxy(
            (request.url.split("/")[2] if "//" in request.url else "").lower())
        if not other:
            return None
        new_request = request.replace(meta=dict(request.meta))
        new_request.meta["proxy"] = other
        new_request.meta["proxy_attempts"] = attempts + 1
        return new_request


class RandomDelayMiddleware:
    """随机请求延迟：两次请求之间异步等待随机时长，不阻塞事件循环。

    基于每个请求元数据 delay_until 控制。默认开启，但若已启用
    AutoThrottle（settings 默认启用），Scrapy 会接管全局限速，
    这里仅在 AutoThrottle 关闭时提供随机化延迟。
    """

    def __init__(self, delay_range=(1.0, 4.0), autothrottle_enabled=False):
        self.delay_range = delay_range
        self.autothrottle_enabled = autothrottle_enabled
        self._last_request_time = {}

    @classmethod
    def from_crawler(cls, crawler):
        min_delay = crawler.settings.getfloat("RANDOM_DELAY_MIN", 1.0)
        max_delay = crawler.settings.getfloat("RANDOM_DELAY_MAX", 4.0)
        return cls(
            (min_delay, max_delay),
            autothrottle_enabled=crawler.settings.getbool("AUTOTHROTTLE_ENABLED", False),
        )

    def process_request(self, request):
        if self.autothrottle_enabled or self.delay_range[1] <= 0:
            return None
        domain = request.url.split("/")[2] if "//" in request.url else request.url
        now = time.time()
        last = self._last_request_time.get(domain, 0.0)
        elapsed = now - last
        wait = self.delay_range[0] + random.random() * (self.delay_range[1] - self.delay_range[0])
        needed = max(0.0, wait - elapsed)
        if needed > 0:
            self._last_request_time[domain] = now + needed
            return deferLater(reactor, needed, lambda: None)
        self._last_request_time[domain] = now
        return None


class MaintainedSessionMiddleware:
    """Cookie 会话保持：开启 COOKIES_ENABLED 时由 Scrapy 自动维护。
    此处仅为显式声明与日志，确保会话机制可用并提示被 429/5xx 时的处理方式。
    """

    def process_request(self, request, spider):
        return None


class RenderPageMiddleware:
    """页面自动渲染中间件：命中 JS 动态加载站时先用无头浏览器渲染。

    触发条件（满足任一即渲染）：
    - spider 显式标记 meta["render"]=True（render=1 起始页）；
    - 页面 URL 命中 platform_adapters 注册表（抖音/快手/小红书等空壳平台，
      渲染同时捕获签名接口 JSON 并提取媒体资源挂到 response.meta）；
    - 页面域名命中 settings.RENDER_AUTO_DOMAINS（手动养名单的 JS 站）。

    渲染在后台线程进行（deferToThread），不阻塞 twisted 事件循环；
    渲染失败自动回退：重放原始请求走静态抓取（带 render_done 防循环）。
    """

    def __init__(self, crawler):
        self.enabled = crawler.settings.getbool("RENDER_MIDDLEWARE_ENABLED", False)
        self.auto_domains = tuple(crawler.settings.get("RENDER_AUTO_DOMAINS", ()))
        self.logger = crawler.logger if hasattr(crawler, "logger") else None

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def _should_render(self, request) -> bool:
        if request.meta.get("render_done") or request.meta.get("dont_render"):
            return False
        # 顶层标记（spider render=1）
        if request.meta.get("render"):
            return True
        from urllib.parse import urlparse
        host = (urlparse(request.url).hostname or "").lower()
        for dom in self.auto_domains:
            d = dom.lower()
            if host == d or host.endswith("." + d):
                return True
        # 平台适配器命中（douyin/kuaishou/xhs 等空壳站）
        from platform_adapters import page_adapter
        return page_adapter(request.url) is not None

    def process_request(self, request):
        if not self.enabled or not self._should_render(request):
            return None
        # 标记「已尝试渲染」，防失败回退后无限循环
        marked = request.replace(meta=dict(request.meta, render_done=True))
        from twisted.internet.threads import deferToThread
        return deferToThread(self._render_in_thread, marked)

    def _render_in_thread(self, request):
        """后台线程渲染：返回 HtmlResponse 或回退原始请求。"""
        from renderer import render_page_api
        from platform_adapters import (
            api_filters_for,
            extract_media_from_api,
            page_adapter,
        )
        url = request.url
        proxy = request.meta.get("proxy") or None
        ua = request.headers.get(b"User-Agent", b"").decode(errors="ignore") or None
        filters = api_filters_for(url)
        ad = page_adapter(url)
        scroll = ad.scroll_max if ad else 0
        html, apis = render_page_api(
            url, api_filters=filters, proxy=proxy, user_agent=ua, scroll_max=scroll)
        # 渲染失败 → 回退静态抓取（render_done 已置位，本轮不再渲染）
        if not html:
            return request
        meta = dict(request.meta)
        if ad and apis:
            # 空壳平台：从捕获的接口响应直接提取媒体资源挂给 spider
            items = extract_media_from_api(apis, limit=config.RENDER_API_LIMIT,
                                           url=url)
            if items:
                meta["render_api_resources"] = items
                meta["render_api_count"] = len(items)
        from scrapy.http import HtmlResponse
        return HtmlResponse(
            url, status=200, headers={}, body=html.encode("utf-8"),
            encoding="utf-8", request=request, meta=meta,
        )


class RobotsPolicyMiddleware:
    """按域名决定是否遵守 robots.txt（配合内置 RobotsTxtMiddleware）。

    内置中间件在 ROBOTSTXT_OBEY=True 时对所有请求生效，这里在它之前
    （order 900 < 1000）根据 config 的按域策略设置 dont_obey_robotstxt：
    - config.ROBOTS_POLICY[域名] 为 True → 遵守（不设豁免）
    - 其余 → 豁免（默认不遵守，保持历史行为）
    """

    def __init__(self):
        # 不在 __init__ 快照策略：spider 的 robots=1 参数会在爬虫 __init__
        # 时把起始域写入 config.ROBOTS_POLICY，快照会漏掉。
        pass

    @classmethod
    def from_crawler(cls, crawler):
        return cls()

    def process_request(self, request):
        from urllib.parse import urlparse
        host = (urlparse(request.url).hostname or "").lower()
        # 内置 RobotsTxtMiddleware 请求 robots.txt 本身时已自带豁免，不再干预
        if request.meta.get("dont_obey_robotstxt"):
            return None
        obey = config.ROBOTS_POLICY.get(host, config.ROBOTS_OBEY_DEFAULT)
        if not obey:
            request.meta["dont_obey_robotstxt"] = True
        return None