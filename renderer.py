"""无头浏览器渲染模块（Playwright 驱动）。

供 GUI「渲染模式」开关与 Scrapy spider 的 render 参数共用：
静态抓取拿不到 JS 渲染后内容的站点，走真实浏览器渲染再交给解析层。

用法：
    html = render_page(url, proxy=...)        # 渲染一次，失败返回 None
    html, apis = render_page_api(url, ...)    # 渲染 + 捕获页面发出的 JSON API 响应
    renderer_available()                      # 浏览器是否就绪
    close_renderer()                          # 程序退出时释放浏览器进程

实现说明：
- Playwright 异步 API 跑在模块级专属线程的事件循环里；浏览器【单例复用】
  （缓存 Browser 实例，每次请求用独立 BrowserContext 隔离会话），
  避免了「每次调用冷启动浏览器」的开销。—— 历史版本每次 launch/close。
- 等待策略：不再固定 sleep。DOM 模式轮询「页面资源数量稳定」即返回；
  接口捕获模式轮询「捕获到的 JSON 数量不再增长」即返回（可配滚动次数
  触发懒加载/加载更多）。网络慢/懒加载站也不会无限等（有总预算封顶）。
- 线程安全：GUI 下载线程池与 Scrapy（deferToThread）都会并发调用渲染，
  任务统一投递到专属事件循环线程执行。
- render_page_api 用于短视频平台这类「页面是空壳、真实数据在签名接口里」
  的站：浏览器内 JS 会自行携带签名调接口，我们只需在 response 事件里把
  JSON 接住。api_filters 指定接口 URL 特征，见 platform_adapters 注册表。
"""

from __future__ import annotations

import asyncio
import sys
import threading

import config

_LAUNCH_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
)

# 轮询节流（秒）：两次轮询间隔
_POLL_INTERVAL = 0.35
# 接口捕获模式：数量连续多少次轮询不增长即判定稳定（约 1s）
_STABLE_SLICES = 3
# DOM 模式：资源数量连续多少次轮询不变即判定稳定
_DOM_STABLE_SLICES = 3

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_browser = None
_playwright = None
_closed = False


def _make_loop() -> asyncio.AbstractEventLoop:
    """在 Windows 上显式使用 ProactorEventLoop（父亲进程可能把策略改成 Selector）。"""
    if sys.platform == "win32":
        try:
            return asyncio.ProactorEventLoop()
        except Exception:
            pass
    return asyncio.new_event_loop()


def _ensure_background_loop_locked():
    """确保模块级事件循环线程在跑（只在本模块内部加锁后调用）。"""
    global _loop, _loop_thread, _browser, _playwright, _closed
    if _closed:
        _closed = False
    if _loop is None or _loop_thread is None or not _loop_thread.is_alive():
        _loop = _make_loop()
        _loop_thread = threading.Thread(
            target=_loop.run_forever, name="renderer-loop", daemon=True)
        _loop_thread.start()


async def _launch_browser_async():
    """在专属事件循环里启动 playwright 与浏览器（只调用一次）。"""
    global _browser, _playwright
    from playwright.async_api import async_playwright
    p = await async_playwright().start()
    b = await p.chromium.launch(headless=True, args=list(_LAUNCH_ARGS))
    _playwright = p
    _browser = b


def _get_browser(timeout: int):
    """拿到单例 Browser（必要时在专属线程内启动）。"""
    global _browser, _closed
    with _lock:
        if _closed:
            raise RuntimeError("renderer 已关闭")
        _ensure_background_loop_locked()
        if _browser is None:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    _launch_browser_async(), _loop)
                fut.result(timeout=timeout)
            except Exception:
                _browser = None
                raise
        return _browser


def _run_on_loop(coro, timeout: float | None):
    """把协程提交到专属循环线程执行，返回结果或 None（失败/超时/已关闭）。"""
    with _lock:
        if _loop is None:
            return None
        loop = _loop
    try:
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout)
    except Exception:
        # 超时/异常一律视为渲染失败，调用方静默回退；后台任务自行收尾
        return None


def _cookie_ctx_for(url: str) -> tuple[list[dict], dict]:
    """把 cookies.txt 的登录态转成 Playwright add_cookies 参数。

    按域名作用域注入（cookie 只对本域及子域生效），避免 extra_http_headers
    把 Cookie 头泄漏给跨域第三方资源；全局 `*` 规则无域可挂，退回头注入。
    短链入口（v.douyin.com/xxx 分享链接）与真实接口域（www.douyin.com）往往
    不在同一子域：命中子域规则时把作用域提升到注册域（v./www./m. 家族互通）。
    返回 (cookie_list, extra_http_headers)。
    """
    from urllib.parse import urlparse as _up
    from resources_reptile.utils.cookies import (
        load_cookie as _load_cookie,
        match_rule,
        registrable as _registrable,
    )
    target = (_up(url).hostname or "").lower()
    rules = _load_cookie()
    if not rules:
        return [], {}
    rule = match_rule(rules, target)
    if not rule:
        return [], {} if not rules.get("*") else {"Cookie": rules["*"]}  # 全域名兜底：头注入
    # 子域规则（v./www./m. 等）提升到注册域作用域，保证短链->真实接口域都带登录态
    if len(rule.split(".")) >= 3:
        scope = "." + _registrable(rule)
    else:
        scope = "." + rule
    pairs = []
    for seg in (rules[rule] or "").split(";"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        name, _, value = seg.partition("=")
        name, value = name.strip(), value.strip()
        if name and value:
            pairs.append({"name": name, "value": value,
                          "domain": scope, "path": "/"})
    return pairs, {}


async def _render_async(browser, url, timeout, proxy, user_agent,
                        api_filters: tuple[str, ...] | None = None,
                        scroll_max: int = 0) -> list:
    """渲染页面；api_filters 非空时同时捕获匹配的接口 JSON 响应。

    返回 [(HTML, [api_json_dict, ...])] 形式以便统一调度；api_filters 为空
    时与旧 render_page 行为一致（不捕获接口）。
    """
    # 登录态注入：优先按域名作用域 add_cookies（跨域子资源不泄漏 Cookie 头）
    cookie_list, extra_headers = _cookie_ctx_for(url)
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        locale="zh-CN",
        user_agent=user_agent,
        extra_http_headers=extra_headers or None,
        proxy={"server": proxy} if proxy else None,
    )
    if cookie_list:
        try:
            await context.add_cookies(cookie_list)
        except Exception:
            pass
    try:
        page = await context.new_page()
        apis: list[dict] = []
        if api_filters:
            import json as _json

            async def _on_response(resp):
                rurl = resp.url
                # 只捕获带特征（如 /aweme/v1/web/）的接口响应，避免收集静态资源/配置噪音
                if not any(f in rurl for f in api_filters):
                    return
                try:
                    ctype = resp.headers.get("content-type", "")
                except Exception:
                    ctype = ""
                if ctype and "json" not in ctype:
                    return
                try:
                    body = await resp.text()
                except Exception:
                    return
                if not body or len(body) > 2_000_000:
                    return
                try:
                    data = _json.loads(body)
                except Exception:
                    return
                if isinstance(data, dict):
                    apis.append(data)

            page.on("response", _on_response)
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=(timeout or config.RENDER_TIMEOUT) * 1000,
        )

        budget = (timeout or config.RENDER_TIMEOUT) if api_filters else (
            min((timeout or config.RENDER_TIMEOUT), 8.0))
        started = asyncio.get_event_loop().time()
        if api_filters:
            await _wait_api_stable(page, apis, budget, scroll_max, started)
        else:
            await _wait_dom_settled(page, budget, started)
        html = await page.content()
        return [(html, apis)]
    finally:
        try:
            await context.close()
        except Exception:
            pass


async def _wait_api_stable(page, apis, budget: float, scroll_max: int, started: float):
    """接口捕获模式：轮询捕获数量不再增长；可选滚动触发懒加载/下一页。

    - 已有数据且连续稳定窗口不变 → 视为稳定返回；
    - 数据始终为空时给满预算的等待（首屏接口慢），不因空轮询提前退出；
    - 滚动次数受 scroll_max 限制；总预算封顶，不会无限等。
    """
    slices = 0
    prev = len(apis)
    scrolled = 0
    while True:
        now = asyncio.get_event_loop().time()
        remain = started + budget - now
        if remain <= 0:
            break
        wait = min(_POLL_INTERVAL, remain)
        await page.wait_for_timeout(int(wait * 1000))
        cur = len(apis)
        if cur > prev:
            prev = cur
            slices = 0
        else:
            slices += 1
            if slices >= _STABLE_SLICES:
                if prev > 0:
                    break  # 有数据且无增长 → 稳定
                if scrolled >= scroll_max:
                    break  # 无数据且滚完了 → 不再等
                scrolled += 1
                try:
                    await page.mouse.wheel(0, 2400)
                    await page.wait_for_timeout(500)
                except Exception:
                    break
                slices = 0
        # 数据不增长但还未到稳定窗口：继续滚动触发下一页
        if scrolled < scroll_max and slices >= _STABLE_SLICES - 2 and prev > 0:
            scrolled += 1
            try:
                await page.mouse.wheel(0, 2400)
                await page.wait_for_timeout(600)
            except Exception:
                break
            slices = 0
    return


async def _wait_dom_settled(page, budget: float, started: float):
    """DOM 模式：轮询页面资源数量稳定（懒加载停止）即返回。"""
    slices = 0
    prev = -1
    while True:
        now = asyncio.get_event_loop().time()
        remain = started + budget - now
        if remain <= 0:
            break
        wait = min(_POLL_INTERVAL, remain)
        await page.wait_for_timeout(int(wait * 1000))
        try:
            n = await page.evaluate(
                "() => document.querySelectorAll('img,video,audio,source,a[href]').length")
        except Exception:
            n = prev
        if n != prev:
            prev = n
            slices = 0
        else:
            slices += 1
            if slices >= _DOM_STABLE_SLICES:
                break
    return


def _run_render(url: str, timeout: int | None = None, proxy: str | None = None,
                user_agent: str | None = None,
                api_filters: tuple[str, ...] | None = None,
                scroll_max: int = 0):
    """共享调度：起一次浏览器完成渲染与（可选）API 捕获。"""
    try:
        import playwright  # noqa: F401
    except ImportError:
        raise ImportError(
            "未安装 Playwright（pip install playwright）。"
            "渲染模式需要它：.venv\\Scripts\\python.exe -m playwright install chromium"
        )

    budget = (timeout or config.RENDER_TIMEOUT) + 15
    try:
        browser = _get_browser(budget)
    except Exception:
        return None
    return _run_on_loop(
        _render_async(browser, url, timeout, proxy, user_agent,
                      api_filters, scroll_max),
        budget,
    )


def render_page(url: str, timeout: int | None = None, proxy: str | None = None,
                user_agent: str | None = None, scroll_max: int = 0) -> str | None:
    """无头浏览器打开 URL，等待页面 JS 完成首轮渲染，返回渲染后的 HTML。

    失败（网络错误/超时/无浏览器）返回 None，不抛异常（调用方静默回退静态模式）。
    """
    res = _run_render(url, timeout, proxy, user_agent, api_filters=None,
                      scroll_max=scroll_max)
    if not res:
        return None
    return res[0][0]


def render_page_api(url: str, api_filters: tuple[str, ...] | None = None,
                    timeout: int | None = None, proxy: str | None = None,
                    user_agent: str | None = None,
                    scroll_max: int = 0) -> tuple[str | None, list]:
    """渲染页面并捕获接口 JSON 响应。

    适用场景：短视频平台这类页面 HTML 是空壳、真实视频/图片数据全部来自
    **带签名**的内部接口（如抖音 ``aweme/v1/web/...``）。浏览器内的 JS 会
    自动为这些接口计算签名，我们只需在 response 事件里把 JSON 响应体接住
    即可——签名参数（a_bogus / X-Bogus / msToken 等）完全由页面代为完成。

    api_filters 为接口 URL 特征子串集合，只捕获 URL 含任一特征的 JSON 响应
    （建议由 platform_adapters 的适配器提供）；为 None 时捕获全部 JSON 响应。
    scroll_max 为滚动次数（列表/信息流站触发加载更多）。

    :returns: (渲染后 HTML, [已解析的 JSON 响应 dict, ...])；失败返回 (None, [])。
    """
    res = _run_render(url, timeout, proxy, user_agent, api_filters=api_filters,
                      scroll_max=scroll_max)
    if not res:
        return None, []
    html, apis = res[0]
    return html, apis


def close_renderer() -> None:
    """关闭单例浏览器与事件循环线程（进程退出时释放）。"""
    global _browser, _playwright, _loop, _loop_thread, _closed
    with _lock:
        if _loop is None:
            return
        loop = _loop

        async def _shutdown():
            global _browser, _playwright
            if _browser is not None:
                try:
                    await _browser.close()
                except Exception:
                    pass
                _browser = None
            if _playwright is not None:
                try:
                    await _playwright.stop()
                except Exception:
                    pass
                _playwright = None

        fut = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
        try:
            fut.result(timeout=10)
        except Exception:
            pass
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        _closed = True
        _loop = _loop_thread = None


def renderer_available() -> bool:
    """Playwright 及浏览器是否就绪（供 GUI 在启动时提示）。"""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False