"""短视频「页面空壳 + 签名接口」平台适配器注册表（与传输层无关）。

背景：抖音/快手/小红书等平台页面 HTML 是 JS 空壳，真实视频/图片数据全部
来自带签名的内部接口（如抖音 ``aweme/v1/web/...``）。签名（a_bogus /
X-Bogus / xsec_token 等）由浏览器内 JS 自动计算，与其逆向签名算法不如
让浏览器把接口响应交给我们（见 renderer.render_page_api 的响应捕获）。

本包 = 平台适配器注册表（目录化 + 自动发现，学 yt-dlp 的 extractor 热加载）：

- 一个平台一个模块（douyin.py / kuaishou.py / xiaohongshu.py / bilibili.py），
  每个模块定义 PlatformAdapter 子类，负责三件事：
  1. ``match_page(url)`` —— 页面域名是否属于本平台
  2. ``api_filters`` —— 捕获哪些接口 URL 特征（避免收集配置类 JSON 噪音）
  3. ``extract(apis)`` —— 把捕获的接口响应解析成 gallery 风格的资源条目
- 包导入时自动扫描目录、实例化并注册所有子类（无需手动 append）：
  新增平台 = 丢一个 .py 文件，主流程零改动。
- 框架（gui_crawler.Discoverer / renderer / scrapy middlewares）不感知平台
  细节，只按注册表分发：渲染 → 捕获接口 JSON → 适配器提取。

本包仅依赖标准库与 format_selector（同样无第三方依赖）。
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from urllib.parse import urlparse


class PlatformAdapter:
    """短视频平台适配器基类。"""

    #: 平台名（用于日志/标记，如 "douyin"）
    name: str = ""
    #: 页面域名列表（含子域名后缀，如 "douyin.com" 匹配 www.douyin.com）
    hosts: tuple[str, ...] = ()
    #: 已知页面路径正则（如 ^/(video|note)/，对标 yt-dlp 的 _VALID_URL）。
    #: 仅作适配器文档/未来消歧用，**不参与匹配**——短链（v.douyin.com/xxxx、
    #: b23.tv/xxx）必须仅凭 hosts 命中（tests/unit_netsuite 契约）
    path_regex: tuple[str, ...] = ()
    #: 接口 URL 特征子串：只有 URL 包含任一特征的 JSON 响应才会被捕获
    api_filters: tuple[str, ...] = ()
    #: 渲染捕获时自动滚动的次数（信息流/列表站触发懒加载；详情页设为 0）
    scroll_max: int = 0

    def match_page(self, url: str) -> bool:
        """页面 URL 是否属于本平台（仅 hosts 域名匹配）。"""
        if not url or not self.hosts:
            return False
        host = (urlparse(url).netloc or "").lower()
        for dom in self.hosts:
            if host == dom or host.endswith("." + dom):
                return True
        return False

    def extract(self, apis: list[dict], limit: int = 300) -> list[dict]:
        """从捕获的接口 JSON 里提取媒体资源条目（gallery 风格 dict）。

        条目字段：url / kind("image"|"video") / name / size / ext /
        page / preview / width / height / alt_url（稳定下载端点，可选）。
        返回空列表表示本平台未命中或无可提取资源。
        """
        return []


def _discover_adapters() -> list[PlatformAdapter]:
    """扫描本包目录，自动实例化并注册所有 PlatformAdapter 子类。

    约定：一个平台一个模块，模块内定义 name 非空的 PlatformAdapter 子类；
    无需手动 append —— 新增平台即新增文件。
    """
    found: dict[str, PlatformAdapter] = {}
    mod_names = sorted(
        m.name for m in pkgutil.iter_modules(__path__) if m.name != "__init__")
    for mod_name in mod_names:
        mod = importlib.import_module(f"{__name__}.{mod_name}")
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if cls is PlatformAdapter or not issubclass(cls, PlatformAdapter):
                continue
            if not getattr(cls, "name", ""):
                continue
            if cls.name in found:
                continue
            try:
                found[cls.name] = cls()
            except Exception:
                continue
    return [found[k] for k in sorted(found)]


#: 已注册的平台适配器（按平台名排序；自动发现，无需手动维护）
PLATFORM_ADAPTERS: list[PlatformAdapter] = _discover_adapters()

# 向后兼容：让 `from platform_adapters import DouyinAdapter` 依旧可用
for _ad in PLATFORM_ADAPTERS:
    globals().setdefault(type(_ad).__name__, type(_ad))
del _ad


def page_adapter(page_url: str) -> PlatformAdapter | None:
    """返回匹配页面 URL 的适配器；无命中返回 None。"""
    for ad in PLATFORM_ADAPTERS:
        if ad.match_page(page_url or ""):
            return ad
    return None


def api_filters_for(page_url: str) -> tuple[str, ...] | None:
    """页面 URL 对应适配器的接口捕获过滤特征；不适用时返回 None（不过滤）。"""
    ad = page_adapter(page_url)
    return ad.api_filters if ad and ad.api_filters else None


def _apply_format_selection(items: list[dict]) -> list[dict]:
    """统一格式择优：条目带 ``formats``（多清晰度）时，按全局格式选择器
    挑一条最优作为主 url，原顶级链保留在 ``url_original``（避免回归）。

    与 bilibili 适配器内联选择逻辑对齐（都走 format_selector），
    不提供 formats 的条目原样返回。
    """
    from format_selector import Format, select_formats
    import config
    spec = getattr(config, "FORMAT_SELECT_SPEC", "best[height<=1080]")
    for it in items:
        fmts = it.get("formats") or []
        if not fmts:
            continue
        selections = [Format(
            url=f.get("url", ""),
            width=f.get("width") or 0,
            height=f.get("height") or 0,
            size=f.get("size") or 0,
            label=f.get("label") or "",
        ) for f in fmts if f.get("url")]
        picked = select_formats(selections, spec) or select_formats(selections, "best")
        if picked and picked.url != it.get("url"):
            it["url_original"] = it.get("url")
            it["url"] = picked.url
    return items


def extract_media_from_api(apis: list[dict], limit: int = 300,
                           url: str = "") -> list[dict]:
    """按页面 URL 选适配器提取资源；URL 未命中时遍历所有适配器合并（去重）。

    提取后的条目若带多清晰度 ``formats``，由统一格式选择器挑主 url
    （见 _apply_format_selection）。
    """
    results: list[dict] = []
    seen: set[str] = set()
    chosen = page_adapter(url) if url else None
    adapters = [chosen] if chosen else list(PLATFORM_ADAPTERS)
    for ad in adapters:
        for it in ad.extract(apis, limit=limit):
            key = (it.get("url") or "").split("?")[0] + it.get("kind", "")
            if key in seen:
                continue
            seen.add(key)
            results.append(it)
            if len(results) >= limit:
                return _apply_format_selection(results)
    return _apply_format_selection(results)
