"""抖音「页面空壳 + 签名接口」e2e：本地模拟站验证全链。

场景：用户粘贴 v.douyin.com 短链（或主页 URL）进 GUI，未勾选渲染模式。
问题：适配器命中前代码要求 render_mode 才走「渲染 + 捕获接口」，否则静态
GET 只能扒到页面推荐位等无关内容（用户反馈的 bug）。

修复后：适配器命中即强制渲染 + 捕获接口 JSON + API 提取。
本测试用本地 HTTP 服务模拟抖音空壳页（页面自身 fetch /aweme/v1/web/... 接口），
验证：
1. renderer.render_page_api 能捕获本地模拟接口 JSON
2. DouyinAdapter.extract 从捕获 JSON 提取出视频/图集
3. Discoverer(render_mode=False) 对适配器命中页：强制渲染 + API 补全资源
（通过 monkeypatch 让 page_adapter 命中本地测试 URL）
"""

import http.server
import json
import os
import sys
import threading
import unittest.mock as mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "resources_reptile"))

import config

config.PROXY_ENABLED = False
config.DEFAULT_PROXY = ""

import gui_crawler
import platform_adapters as pa
import renderer

PASS = 0
FAIL = 0


def check(name, got, expected=True):
    global PASS, FAIL
    if isinstance(expected, bool) or expected is None:
        ok = (got is expected)
    else:
        ok = (got == expected)
    if ok:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name}: got={got!r} expected={expected!r}")


SAMPLE = {
    "status_code": 0,
    "aweme_list": [
        {
            "aweme_id": "72123456789",
            "desc": "主页测试视频",
            "video": {
                "play_addr": {"uri": "v0200f100000abc", "width": 1920, "height": 1080},
                "play_addr_265": {"url_list": ["https://v.douyin.com/xxxx"],
                                  "width": 1920, "height": 1080},
                "cover": {"url_list": ["https://p.douyin.com/cover.jpg"]},
            },
        },
        {
            "aweme_id": "72123456790",
            "desc": "主页测试图集",
            "images": [{"url_list": ["https://p.douyin.com/img1.jpg"],
                        "width": 800, "height": 600}],
        },
    ],
}

PAGE_HTML = b"""<!doctype html><html><head><title>dy-empty-shell</title></head>
<body><script>
fetch('/aweme/v1/web/aweme/post/?sec_user_id=x&max_cursor=0');
</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/aweme/v1/web/"):
            body = json.dumps(SAMPLE).encode()
            ct = "application/json"
        elif self.path == "/profile":
            body, ct = PAGE_HTML, "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class TServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


def _start() -> str:
    srv = TServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


def _patch_adapter():
    real_page = pa.page_adapter
    real_filters = pa.api_filters_for

    def fake_page(url):
        if "127.0.0.1" in (url or ""):
            return pa.DouyinAdapter()
        return real_page(url)

    def fake_filters(url):
        if "127.0.0.1" in (url or ""):
            return pa.DouyinAdapter().api_filters
        return real_filters(url)

    mock.patch.object(pa, "page_adapter", fake_page).start()
    mock.patch.object(pa, "api_filters_for", fake_filters).start()
    mock.patch.object(gui_crawler, "_page_adapter", fake_page).start()
    mock.patch.object(gui_crawler, "_adapter_api_filters", fake_filters).start()


def _test_render_capture(base):
    html, apis = renderer.render_page_api(
        base + "/profile", api_filters=("/aweme/v1/web/",), timeout=30)
    check("render: page rendered", bool(html and len(html) > 0))
    check("render: api captured", len(apis) >= 1)
    items = pa.extract_media_from_api(apis, url="https://www.douyin.com/user/x")
    check("adapter: extracted video+image from captured api", len(items) == 2)


def _test_discover_forced_render(base):
    d = gui_crawler.Discoverer(render_mode=False)
    res, title = d.discover(base + "/profile")
    check("discover: forced render captured api", len(d.api_records) >= 1)
    kinds = {r.kind for r in res}
    check("discover: got video from api", "video" in kinds)
    check("discover: got image from api", "image" in kinds)
    vids = [r for r in res if r.kind == "video"]
    if vids:
        check("discover: video url from api", vids[0].url == "https://v.douyin.com/xxxx")
        check("discover: video alt_url stable endpoint",
              vids[0].raw_url.startswith("https://www.douyin.com/aweme/v1/play/?video_id="))


def an():
    base, srv = _start()
    _patch_adapter()
    try:
        _test_render_capture(base)
        _test_discover_forced_render(base)
    finally:
        srv.shutdown()
        mock.patch.stopall()


if __name__ == "__main__":
    an()
    print(f"\n{'-' * 40}\ne2e_douyin_api: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)