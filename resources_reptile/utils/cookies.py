"""Cookie 注入规则（GUI FetchSession / 渲染浏览器 / Scrapy 共用）。

来源：
1. 项目根目录 cookies.txt —— 浏览器登录后把完整 Cookie 头粘贴进来，
   支持按域名限定：`example.com:  name=value; other=1`（冒号前有域名）
   或不带域名（对除「明确不需要」外的所有请求注入，谨慎使用）。
2. 环境变量 RESOURCES_COOKIE —— 单条 Cookie 头，计入"全域名"规则。

用法（调用方按本模块函数取，不要自己解析文件）：
    load_cookie()                # 解析一次并缓存（lazy）
    cookie_for(host) -> str|None # 命中规则的 Cookie 头（含 "Cookie: "）
"""

from __future__ import annotations

import os
import re

try:
    import config  # 项目配置（提供 COOKIE 相关开关/路径）
except Exception:  # pragma: no cover - 独立测试环境
    config = None  # type: ignore[assignment]

_loaded = False
_rules: dict[str, str] = {}  # host(lower, 无通配) -> cookie 值；"*" 为全域名


def _parse_cookie_line(line: str) -> tuple[str, str] | None:
    """解析一行：`domain: cookie` 或无前缀的纯 cookie。返回 (host 或 "*", cookie)。"""
    line = line.strip()
    if not line or line.startswith(("#", "//")):
        return None
    # 冒号分隔：前半是域名（含点/星号）才视为域名，否则冒号是 cookie 语法的一部分
    if ":" in line:
        head, rest = line.split(":", 1)
        head = head.strip().lower()
        if re.match(r"^[\w.*-]+$", head) and ("." in head or head == "*"):
            return head, rest.strip()
    return "*", line.strip()


def cookie_file_path(path: str = "") -> str:
    """把（可能相对的）Cookie 文件路径解析为绝对路径。

    相对路径一律按「项目根目录」解析（与 cookie_capture 保存、渲染注入
    读取同一文件）：本模块位于 resources_reptile/utils/，上溯三级即根目录。
    绝对路径（测试临时文件等）原样返回。
    """
    if config is not None:
        p = path or getattr(config, "COOKIE_FILE", "cookies.txt")
    else:
        p = path or "cookies.txt"
    if os.path.isabs(p):
        return p
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, p)


def _load_once() -> None:
    global _loaded, _rules
    if _loaded:
        return
    _rules = {}
    env = os.environ.get("RESOURCES_COOKIE", "")
    if env.strip():
        _rules["*"] = env.strip()
    path = cookie_file_path()
    if os.path.exists(path):
        try:
            from secret_store import read_secret
            content = read_secret(path)
            for ln in content.splitlines():
                parsed = _parse_cookie_line(ln)
                if parsed:
                    host, cookie = parsed
                    if host == "*":
                        _rules["*"] = cookie  # 后行覆盖
                    else:
                        _rules.setdefault(host, cookie)
        except OSError:
            pass
    _loaded = True


def load_cookie() -> dict[str, str]:
    """返回 {host: cookie 值} 规则表（内部缓存，重复调用零成本）。"""
    _load_once()
    return dict(_rules)


def reload_cookie() -> dict[str, str]:
    """清缓存并重新从 cookies.txt 读取（登录抓取保存后调用，让运行中的会话生效）。"""
    global _loaded
    _loaded = False
    _load_once()
    return dict(_rules)


def registrable(host: str) -> str:
    """粗略注册域（不引入公共后缀库）：取最后两级。用于 v./www./m. 等子域家族归一。"""
    parts = (host or "").strip().lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "").lower()


def match_rule(rules: dict[str, str], host: str | None) -> str | None:
    """在给定规则表里匹配页面域名，返回命中规则键（未命中 None）。

    匹配优先级：精确域名 > 父域（example.com 匹配 www.example.com）
    > 同注册域家族（www./v./m. 等子域互通，如 v.douyin.com 命中 www.douyin.com）。
    短链（v.douyin.com/xxx）与真实接口域（www.douyin.com）往往不同子域，
    家族匹配保证登录态能送达真实接口所在域。
    """
    h = (host or "").strip().lower()
    if h.startswith("www."):
        h = h[4:]
    if not h:
        return None
    for dom in rules:
        if dom == "*":
            continue
        if h == dom or h.endswith("." + dom):
            return dom
    family = registrable(h)
    best = None
    for dom in rules:
        if dom == "*" or dom == family:
            continue  # 父域规则已在上面命中
        if registrable(dom) == family:
            # 取最接近根域（标签最少）的规则，覆盖范围最广
            if best is None or len(dom.split(".")) < len(best.split(".")):
                best = dom
    return best


def cookie_for(host: str | None) -> str | None:
    """命中规则的 Cookie 值（不含 "Cookie: " 前缀）；未命中返回 None。

    匹配优先级：精确域名 > 父域（example.com 匹配 www.example.com）
    > 同注册域家族 > 全域名。
    """
    _load_once()
    rule = match_rule(_rules, host)
    if rule:
        return _rules.get(rule)
    return _rules.get("*")


def rule_for(host: str | None) -> str | None:
    """返回命中的规则键（壳函数，供渲染器等对规则做作用域提升时获取键名）。"""
    _load_once()
    return match_rule(_rules, host)


def cookie_header_for(host: str | None) -> str | None:
    """带 "Cookie: " 前缀的 HTTP 头值（方便直接注入 headers）。"""
    c = cookie_for(host)
    return f"Cookie: {c}" if c else None