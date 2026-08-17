"""MIME 嗅探管道端到端测试:无扩展名链接 (/download?id=42) 按 Content-Type 归类+补扩展名。

运行:python tests/e2e_mime.py
"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HTML = """<!doctype html><html><body>
<a href="/download?id=42">download no ext</a>
</body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            b = HTML.encode()
            c = "text/html"
        elif self.path.startswith("/download"):
            b = b"\xff\xd8\xff\xe0" + b"\x00" * 32
            c = "image/jpeg"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", c)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


def free_port():
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    srv.server_close()
    return port


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
server = http.server.HTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=server.serve_forever, daemon=True).start()

TMP = tempfile.mkdtemp(prefix="rr_e2e_mime_")
STORE = os.path.join(TMP, "store")
OUT = os.path.join(TMP, "items.json")

r = subprocess.run([sys.executable, "-m", "scrapy", "crawl", "resource",
    "-a", f"start_urls={BASE}/",
    "-a", "max_depth=0", "-a", "render=0",
    "-s", "PROXY_ENABLED=False", "-s", "AUTOTHROTTLE_ENABLED=False",
    "-s", "DOWNLOAD_DELAY=0", "-s", "LOG_LEVEL=WARNING",
    "-s", f"FILES_STORE={STORE}",
    "-O", OUT],
    cwd=ROOT, capture_output=True, text=True, timeout=240)
if r.returncode != 0:
    print(r.stderr[-1500:])
    raise SystemExit(1)

server.shutdown()

with open(OUT, encoding="utf-8") as f:
    items = json.load(f)
urls = [u for it in items for u in it.get("file_urls", [])]
print("file_urls:", urls)

found = []
for base, _dirs, files in os.walk(STORE):
    for fn in files:
        p = os.path.join(base, fn)
        found.append(os.path.relpath(p, STORE))
print("stored:", sorted(found))

assert any(f.startswith("images") and f.endswith(".jpg") for f in found), \
    "expected no-ext URL classified as images/xxx.jpg by Content-Type sniffing"
print("MIME OK: /download?id=42 ->", [f for f in found if f.startswith("images")])