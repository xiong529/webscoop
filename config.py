"""全局统一配置（参考 MediaCrawler 的配置化设计）。

GUI 与 Scrapy 均可从这里读取，避免各模块重复维护默认值。
"""

from __future__ import annotations

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------- 下载目录 ----------------
# GUI 下载的默认保存目录
INFORMATION_DIR = os.environ.get("RESOURCES_INFO_DIR", os.path.join(BASE_DIR, "information"))
# Scrapy 命令行模式的下载目录
SCRAPY_DOWNLOAD_DIR = os.environ.get("RESOURCES_SCRAPY_DIR", os.path.join(BASE_DIR, "downloads"))

# ---------------- 代理 ----------------
# 本机代理地址（写死 127.0.0.1:65534，用于访问国外站点；如无需代理可设为 "" ）
DEFAULT_PROXY = os.environ.get("RESOURCES_PROXY", "http://127.0.0.1:65534")
# 是否默认启用代理（走本机中转）
PROXY_ENABLED = os.environ.get("RESOURCES_PROXY_ENABLED", "1") == "1"
# 代理池健康检测探针（启动后台并发探测，不可用的提前吊销；2xx/3xx 视为可用）
PROXY_HEALTH_PROBE = os.environ.get(
    "PROXY_HEALTH_PROBE", "http://www.gstatic.com/generate_204")

# ---------------- 反爬与请求 ----------------
# TLS 指纹模拟使用的浏览器（经 Scrapling/curl_cffi 支持）
IMPERSONATE = "chrome"
# 会话级随机指纹：开启后每次创建 FetchSession 从 IMPERSONATE_OPTIONS 随机选一个
# 指纹，并与所选代理绑定成一个「身份」；高频目标站建议关闭（固定免抖），
# 高危站开启（指纹+IP 一起换，避免只换 IP 不换指纹被对出关联）。
IMPERSONATE_RANDOM = os.environ.get("RESOURCES_IMPERSONATE_RANDOM", "0") == "1"
# 可用的指纹选项（GUI「抓取设置」下拉框，均经 curl_cffi 支持实测）
IMPERSONATE_OPTIONS = ["chrome", "chrome120", "chrome124", "firefox", "safari",
                       "edge101", "edge99", "ios", "android", "chrome131"]
# 请求超时（秒）
REQUEST_TIMEOUT = 15
# 每个请求携带 Referer 的来源页（留空则由页面 URL 决定）

# ---------------- 并发与限速 ----------------
DOWNLOAD_WORKERS = 4
# GUI 并发设置项的最大值（对话框 Spinbox 上限）
DOWNLOAD_WORKERS_MAX = 32
PROBE_WORKERS = 6
MAX_RESOURCES = 300

# ---------------- 下载失败重试 ----------------
# 单文件下载失败后的自动重试次数（1 次正常 + 重试次数，总共最多尝试 N+1 次）
DOWNLOAD_RETRY_TIMES = int(os.environ.get("RESOURCES_RETRY_TIMES", "3"))
# 指数退避基准秒数：第 n 次重试前等待 RETRY_BACKOFF * 2^(n-1) 秒
DOWNLOAD_RETRY_BACKOFF = int(os.environ.get("RESOURCES_RETRY_BACKOFF", "3"))

# ---------------- 资源过滤 ----------------
# 小于该字节数的资源视为「极小文件/图标」，自动过滤（0 表示不过滤）
MIN_RESOURCE_SIZE = int(os.environ.get("RESOURCES_MIN_SIZE", "1024"))
# 是否过滤常见网站图标（favicon / apple-touch-icon 等）
FILTER_ICONS = os.environ.get("RESOURCES_FILTER_ICONS", "1") == "1"
# 文件名/路径中命中即判定为图标的特征
ICON_NAME_PATTERNS = (
    "favicon", "apple-touch-icon", "android-chrome", "shortcut icon",
    "icon-", "-icon", "mstile", "safari-pinned-tab", "site.webmanifest",
    "browserconfig.xml", "logo-16", "logo-32", ".ico",
    "spacer.gif", "blank.gif", "blank.png", "transparent.gif", "pixel.gif",
)

# ---------------- 详情页跟进（获取真实视频/高清图） ----------------
# 主页通常只暴露缩略图/封面，真实资源在详情页。
# 允许对以下「详情页链接」批量跟进（数量上限），提取 og:image / 真实视频 URL。
DETAIL_PAGE_LIMIT = int(os.environ.get("RESOURCES_DETAIL_LIMIT", "20"))
# 详情页链接的路径特征（命中即视为内容页而非导航页）。
# 要求关键词（photo/video 等）后还有内容，避免把 /photos/、/videos/ 这类列表页当详情页。
DETAIL_PATH_RE = r"/(photo|video|picture|image|watch|gallery|media|item|shot|reel|short|clip)(?:s?[/-])[^/]+"
# 视频真实文件的常见 CDN 直链特征（用于从详情页 HTML 提取）
VIDEO_CDN_HINTS = ("/video", "/videos", "videos.", "video-files", ".mp4", ".webm", ".mkv")

# ---------------- 断点续载 ----------------
# 已存在且大小>0 的文件直接跳过，不重复下载（MediaCrawler 断点续爬思想）
RESUME_EXISTING = os.environ.get("RESOURCES_RESUME", "1") == "1"

# ---------------- 下载文件名模板 ----------------
# 控制下载的落盘目录与文件名（/ 分隔子目录），可用的 token 见
# discover_common.render_dest_template。默认 {category}/{name} 与历史行为一致。
FILENAME_TEMPLATE = os.environ.get("RESOURCES_FILENAME_TEMPLATE", "{category}/{name}")

# ---------------- 分页跟随 ----------------
# 列表页 SSR 只渲染首屏（如 pexels 的 ?page=N 分页、无限滚动站点）。
# 发现资源时自动翻页抓取并把后续页资源合并进来。0 表示不跟随分页。
PAGE_FOLLOW_LIMIT = int(os.environ.get("RESOURCES_PAGE_FOLLOW", "20"))

# ---------------- LLM 规则分析（llm_rules.py） ----------------
# 用于把「一页 HTML + 资源直链样例」喂给 LLM，产出 highres_rules.json 新条目。
# 接口为 OpenAI 兼容的 chat/completions（LiteLLM/DeepSeek/Ollama/OpenRouter 等均支持）。
LLM_BASE_URL = os.environ.get("RESOURCES_LLM_BASE", "https://api.deepseek.com/v1")
# API Key（优先环境变量；也可在 llm_rules.py 用 --key 临时覆盖，均为明文环境配置）
LLM_API_KEY = os.environ.get("RESOURCES_LLM_KEY", "")
# 默认模型名（不同提供方写法不一，如 deepseek-chat / gpt-4o-mini / qwen2.5:7b）
LLM_MODEL = os.environ.get("RESOURCES_LLM_MODEL", "deepseek-chat")
# 单次 LLM 请求超时（秒）
LLM_TIMEOUT = int(os.environ.get("RESOURCES_LLM_TIMEOUT", "120"))
# LLM 配置本地保存文件（GUI「LLM 模型…」弹窗写入，含 API Key，已 gitignore）
LLM_CONFIG_FILE = os.environ.get("RESOURCES_LLM_CONFIG_FILE",
                                 os.path.join(BASE_DIR, "llm_config.json"))

# ---------------- Pexels API 抓取 ----------------
# API 自动翻页上限（每页最多 80 条，翻页跟随 next_page）
API_PAGE_LIMIT = int(os.environ.get("RESOURCES_API_PAGE_LIMIT", "3"))
# API Key 本地保存文件（首次填写后自动记住）
API_KEY_FILE = os.environ.get("RESOURCES_API_KEY_FILE",
                              os.path.join(BASE_DIR, "pexels_api_key.txt"))

# ---------------- 在线播放 ----------------
# 小于该大小（MB）的视频先缓存到本地再播放（更流畅、可拖动），更大的直接流播
PLAYER_CACHE_MB = int(os.environ.get("PLAYER_CACHE_MB", "60"))

# ---------------- 浏览器渲染模式（Playwright） ----------------
# 无头浏览器渲染页面后再解析：静态模式抓不到的站（JS 动态加载/接口渲染），
# GUI 勾选「渲染模式」或 Scrapy 传 render=1 时对入口页生效。
RENDER_MODE = os.environ.get("RESOURCES_RENDER", "0") == "1"
# 单次渲染超时（秒）
RENDER_TIMEOUT = int(os.environ.get("RESOURCES_RENDER_TIMEOUT", "30"))
# 渲染捕获接口时提取 API 资源条数上限（Scrapy 渲染中间件 / GUI discover 共用）
RENDER_API_LIMIT = int(os.environ.get("RESOURCES_RENDER_API_LIMIT", "200"))

# ---------------- Cookie 注入 ----------------
# 需要登录态的站点（小红书等）：浏览器登录后把 Cookie 头粘贴进 cookies.txt
# （支持按域名限定，见 resources_reptile/utils/cookies.py），此处为文件名。
# 也可用环境变量 RESOURCES_COOKIE 直接给一条全域名 Cookie 头。
COOKIE_FILE = os.environ.get("RESOURCES_COOKIE_FILE", "cookies.txt")

# ---------------- HLS（m3u8）分片下载 ----------------
# 直链下载不适用时分片合并：并发分片数 / 分片数上限（疑似直播流跳过）
HLS_WORKERS = int(os.environ.get("RESOURCES_HLS_WORKERS", "8"))
HLS_MAX_SEGMENTS = int(os.environ.get("RESOURCES_HLS_MAX_SEGMENTS", "15000"))

# ---------------- Scrapling 第三层兜底 ----------------
# 常规请求链（TLS 指纹+代理轮换）失败/非 2xx 时，用 Scrapling Fetcher 再试一次
SCRAPLING_FALLBACK = os.environ.get("RESOURCES_SCRAPLING_FALLBACK", "1") == "1"

# ---------------- robots.txt 策略 ----------------
# 默认是否遵守 robots.txt（false 保持历史行为：不检查）。
# 注意：Scrapy 的 ROBOTSTXT_OBEY 是全局开关，这里统一由
# RobotsPolicyMiddleware 按域名决定（配合 settings.ROBOTSTXT_OBEY=True 生效）。
ROBOTS_OBEY_DEFAULT = os.environ.get("RESOURCES_ROBOTS", "0") == "1"
# 按域名覆盖遵守策略：{"example.com": True} 表示该站遵守 robots.txt。
# 与默认值方向一致（True=遵守）。留空则全部走默认策略。
ROBOTS_POLICY: dict[str, bool] = {}


def proxy_dict() -> dict:
    """构造 requests 风格的代理字典。"""
    if not DEFAULT_PROXY:
        return {}
    return {"http": DEFAULT_PROXY, "https": DEFAULT_PROXY}


def ensure_information_dir() -> str:
    os.makedirs(INFORMATION_DIR, exist_ok=True)
    return INFORMATION_DIR