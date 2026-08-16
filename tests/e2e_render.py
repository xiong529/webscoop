"""端到端渲染测试:本地 HTTP 服务,页面 JS 延迟注入图片链接。

- 静态模式:抓不到注入的图片(页面源码里没有)
- render=1:无头浏览器渲染后能抓到
需要 chromium 已安装。

运行:python tests/e2e_render.py
"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HTML = """<!doctype html><html><head><title>JS Test Page</title></head><body>
<h1>hello</h1>
<script>
  setTimeout(function () {
    var img = document.createElement("img");
    img.src = "/js-injected.png";
    document.body.appendChild(img);
  }, 400);
</script>
<a href="/static-link.jpg">static</a>
</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body = HTML.encode()
            ctype = "text/html"
        elif self.path == "/js-injected.png":
            body = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
            ctype = "image/png"
        elif self.path == "/static-link.jpg":
            body = b"\xff\xd8\xff" + b"\x00" * 64
            ctype = "image/jpeg"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def free_port():
    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    srv.server_close()
    return port


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()

TMP = tempfile.mkdtemp(prefix="rr_e2e_render_")
OUT = os.path.join(TMP, "items.json")


def run_spider(render):
    if os.path.exists(OUT):
        os.remove(OUT)
    args = [sys.executable, "-m", "scrapy", "crawl", "resource",
            "-a", f"start_urls={BASE}/",
            "-a", "allowed_domains=127.0.0.1",
            "-a", "max_depth=0", "-a", f"render={render}",
            "-s", "ITEM_PIPELINES={}", "-s", "LOG_LEVEL=ERROR",
            "-s", "PROXY_ENABLED=False",
            "-s", "AUTOTHROTTLE_ENABLED=False",
            "-s", "DOWNLOAD_DELAY=0",
            "-O", OUT]
    proc = subprocess.run(args, cwd=ROOT, check=False, timeout=240,
                          capture_output=True, text=True, env=dict(os.environ))
    if proc.returncode != 0:
        print(proc.stderr[-1500:])
        raise SystemExit(1)
    with open(OUT, encoding="utf-8") as f:
        items = json.load(f)
    return items


static_items = run_spider("0")
static_urls = set(u for it in static_items for u in it.get("file_urls", []))
print("static  mode files:", sorted(static_urls))

render_items = run_spider("1")
render_urls = set(u for it in render_items for u in it.get("file_urls", []))
print("render  mode files:", sorted(render_urls))

server.shutdown()

assert f"{BASE}/static-link.jpg" in static_urls, "static link missing in static mode"
assert f"{BASE}/js-injected.png" in render_urls, "JS-injected img missing in render mode"
print("E2E OK: render mode captured JS-injected resource")