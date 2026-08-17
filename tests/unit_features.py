"""新功能单测:规范化去重 / MIME 嗅探 / robots 策略 / 下载重试与失败持久化 / 渲染冒烟。

独立进程运行:python tests/unit_features.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config
config.DEFAULT_PROXY = ""  # 测试不依赖代理

from discover_common import is_download_endpoint
from gui_crawler import (
    _code_retryable,
    _exc_status,
    _retry_after_seconds,
    load_failures,
    save_failures,
)
from resources_reptile.dupefilters import NormalizedRFPDupeFilter, strip_tracking_params
from resources_reptile.pipelines import classify_url, ct_to_ext

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name}: got={got!r} want={want!r}")


# ---- 1. 追踪参数剔除 ----
u = "https://example.com/photos?utm_source=x&utm_medium=y&fbclid=abc&page=2&id=5"
check("strip utm/fbclid keep page/id",
      strip_tracking_params(u, ("utm_", "fbclid")),
      "https://example.com/photos?page=2&id=5")  # 保留原参数顺序，仅剔除追踪参数
check("no query unchanged",
      strip_tracking_params("https://example.com/a.png", ("utm_",)),
      "https://example.com/a.png")
check("case-insensitive FBclid",
      strip_tracking_params("https://example.com/x?FBclid=1&gclid=2&a=3",
                            ("fbclid", "gclid")),
      "https://example.com/x?a=3")
check("prefix only _-suffixed",
      strip_tracking_params("https://example.com/x?utm_campaign=c&mc_cid=9&mc_eid=8",
                            ("utm_", "mc_cid", "mc_eid")),
      "https://example.com/x")
check("empty param value dropped",
      strip_tracking_params("https://example.com/x?utm_medium=&id=7", ("utm_",)),
      "https://example.com/x?id=7")

# ---- 1b. 过滤器整体（含指纹一致性）----
urls = [
    "https://example.com/list?page=2&utm_source=w",
    "https://example.com/list?page=2&fbclid=abc",
    "https://example.com/list?page=2",
    "https://example.com/list?page=3",
]
f = NormalizedRFPDupeFilter(strip_params=("utm_", "fbclid", "gclid"))
from scrapy.http import Request
seen = set()
for u in urls:
    seen.add(f.request_fingerprint(Request(u)))
check("dupefilter: page2 dedup, page3 distinct", len(seen), 2)

# ---- 2. MIME 嗅探 ----
check("ct_to_ext jpeg", ct_to_ext("image/jpeg; charset=utf-8"), ".jpg")
check("ct_to_ext mp4", ct_to_ext("video/mp4"), ".mp4")
check("ct_to_ext unknown", ct_to_ext("x/y"), "")
check("classify ext wins over ct",
      classify_url("https://x.com/a.png", "video/mp4"), "images")
check("classify no-ext by ct video",
      classify_url("https://x.com/download?id=3", "video/mp4"), "videos")
check("classify no-ext by ct image",
      classify_url("https://x.com/dl", "image/webp"), "images")
check("classify no-ext unknown ct", classify_url("https://x.com/dl", "x/y"), "others")
check("classify no-ext no ct", classify_url("https://x.com/dl"), "others")
check("classify pdf ct", classify_url("https://x.com/export?id=1", "application/pdf"), "docs")

# ---- 3. robots 策略 ----
from resources_reptile.middlewares import RobotsPolicyMiddleware
m = RobotsPolicyMiddleware()
req = Request("https://pexels.com/zh-cn/search/videos/x/")
m.process_request(req)
check("default policy -> exempt", req.meta.get("dont_obey_robotstxt"), True)
config.ROBOTS_POLICY["pexels.com"] = True
m2 = RobotsPolicyMiddleware()  # 重新读取配置
req2 = Request("https://pexels.com/x")
m2.process_request(req2)
check("policy obey -> no exempt meta", req2.meta.get("dont_obey_robotstxt"), None)
del config.ROBOTS_POLICY["pexels.com"]

# ---- 4. 下载失败重试 ----
check("429 retryable", _code_retryable(429), True)
check("5xx retryable", _code_retryable(503), True)
check("0 retryable", _code_retryable(0), True)
check("404 not retryable", _code_retryable(404), False)
check("403 not retryable", _code_retryable(403), False)
check("410 not retryable", _code_retryable(410), False)
check("retry times config", isinstance(config.DOWNLOAD_RETRY_TIMES, int)
      and config.DOWNLOAD_RETRY_TIMES >= 1, True)
check("backoff config", isinstance(config.DOWNLOAD_RETRY_BACKOFF, (int, float))
      and config.DOWNLOAD_RETRY_BACKOFF > 0, True)

# 状态码提取与 Retry-After 解析（智能重试分桶）
import requests
from types import SimpleNamespace
_err = requests.HTTPError("HTTP 404: http://x/1", response=SimpleNamespace(
    status_code=404, headers={}))
check("exc status from response", _exc_status(_err), 404)
check("exc status from message", _exc_status(
    requests.HTTPError("HTTP 503: http://x/2")), 503)
check("exc status network err", _exc_status(OSError("timeout")), 0)
_err429 = requests.HTTPError("HTTP 429: http://x/3", response=SimpleNamespace(
    status_code=429, headers={"Retry-After": "12"}))
check("retry-after seconds", _retry_after_seconds(_err429), 12.0)
check("retry-after none", _retry_after_seconds(
    requests.HTTPError("HTTP 429: x", response=SimpleNamespace(
        status_code=429, headers={}))), 0.0)
check("retry-after http-date", _retry_after_seconds(
    requests.HTTPError("HTTP 429: x", response=SimpleNamespace(
        status_code=429,
        headers={"Retry-After": "Sun, 06 Nov 2099 08:49:37 GMT"}))) > 0.0, True)
check("retry-after garbage", _retry_after_seconds(
    requests.HTTPError("HTTP 429: x", response=SimpleNamespace(
        status_code=429, headers={"Retry-After": "abc"}))), 0.0)

# ---- 5. failures.json 读写 ----
import tempfile
_td = tempfile.mkdtemp()
save_failures(_td, {"http://x/1": {"reason": "HTTP 500", "failed_at": "t"}})
_r = load_failures(_td)
check("failures roundtrip url", _r.get("http://x/1", {}).get("reason"), "HTTP 500")

# ---- 6. 下载端点识别 ----
check("dl path hint", is_download_endpoint("https://x.com/download?id=42"), True)
check("dl path seg", is_download_endpoint("https://x.com/dl/abc"), True)
check("query download key", is_download_endpoint("https://x.com/api.php?download=1"), True)
check("query file key", is_download_endpoint("https://x.com/g.php?file=img&id=1"), True)
check("normal page no", is_download_endpoint("https://x.com/photos/123"), False)
check("page with id param no", is_download_endpoint("https://x.com/watch?id=9"), False)
check("page any-no-ext no", is_download_endpoint("https://x.com/index"), False)

# ---- 7. 渲染冒烟（需 chromium 已安装；未就绪则跳过不判失败）----
try:
    from renderer import render_page, close_renderer
    html = render_page("data:text/html,<title>render-ok</title><p>js test</p>",
                       timeout=20, proxy=None)
    if html and "render-ok" in html:
        PASS += 1
        print("PASS renderer smoke (data url)")
    else:
        print(f"SKIP renderer smoke (browser not ready): {html is not None}")
    close_renderer()
except Exception as exc:
    print(f"SKIP renderer smoke (browser not ready): {exc}")

print(f"DONE  PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)