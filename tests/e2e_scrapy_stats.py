"""Scrapy CLI 侧 stats 落盘端到端：本地 HTTP 站 → crawl → FILES_STORE/stats.json。

验证 Scrapy 路径（GUI 之外的命令行爬虫）统计闭环：
  1) 发现统计：spider parse 里按 URL 分类累计（totals）
  2) 下载统计：pipelines.media_downloaded 记成功+字节，media_failed 记失败原因/站点
  3) 落盘：spider.closed 统一把 stats.json 写到 FILES_STORE（与下载文件同处）

运行: python tests/e2e_scrapy_stats.py
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "resources_reptile"))

# 下载存档/死链表隔离到临时目录，避免污染仓库根目录
_TMP_ARCH = tempfile.mkdtemp(prefix="ws_e2e_arch_")
os.environ["RESOURCES_ARCHIVE_FILE"] = os.path.join(_TMP_ARCH, "archive.json")
os.environ["RESOURCES_DEAD_FILE"] = os.path.join(_TMP_ARCH, "dead.json")

from stats import get_stats

BODY = b"x" * 2048


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            base = f"http://127.0.0.1:{port}"
            body = (f"<html><head><title>stats e2e</title>"
                    f"</head><body><img src='{base}/img2.png'>"
                    f"<video src='{base}/clip.mp4'></video>"
                    f"<a href='{base}/ok.txt'>ok</a>"
                    f"<a href='{base}/boom.txt'>boom</a>"
                    f"</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/img2.png", "/clip.mp4", "/ok.txt"):
            body = BODY
            self.send_response(200)
            self.send_header("Content-Type",
                             "image/png" if path.endswith(".png") else
                             "video/mp4" if path.endswith(".mp4") else "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/boom.txt":
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_HEAD(self):
        self.do_GET()


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

from scrapy.crawler import CrawlerProcess

td = tempfile.mkdtemp(prefix="scrapystats_")
store = os.path.join(td, "downloads")
get_stats().reset()

process = CrawlerProcess(settings={
    "LOG_LEVEL": "WARNING",
    "ROBOTSTXT_OBEY": False,
    "SPIDER_MODULES": ["resources_reptile.spiders"],
    "FILES_STORE": store,
    "ITEM_PIPELINES": {
        "resources_reptile.pipelines.ResourceFilesPipeline": 300,
    },
    "DOWNLOADER_MIDDLEWARES": {
        "resources_reptile.middlewares.RandomUserAgentMiddleware": 100,
    },
    "DOWNLOAD_HANDLERS": {},
    "COOKIES_ENABLED": False,
    "AUTOTHROTTLE_ENABLED": False,
})
try:
    process.crawl(
        "resource",
        start_urls=f"http://127.0.0.1:{port}/",
        allowed_domains=f"127.0.0.1:{port}",
        download_extensions="jpg,png,mp4,txt",
        max_depth=0,
        robots="0",
    )
    process.start()
finally:
    srv.shutdown()

csv_path = os.path.join(store, "stats.json")
print("stats path exists:", os.path.exists(csv_path), csv_path)
assert os.path.exists(csv_path), "stats.json not written!"

with open(csv_path, encoding="utf-8") as f:
    data = json.load(f)

print("pages:", data["pages"], "downloaded:", data["downloaded"],
      "failed:", data["failed"], "totals:", data["totals"],
      "fail_host:", data.get("fail_host"), "fail_reason:", data.get("fail_reason"))
assert data["pages"] >= 1
assert data["downloaded"] >= 3, data  # img2/clip/ok
assert data["totals"].get("videos", 0) >= 1
assert data["totals"].get("images", 0) >= 1
assert data["failed"] >= 1, data  # boom.txt 500
assert data.get("fail_host", {}).get("127.0.0.1", 0) >= 1, data  # 失败站点分布
assert data.get("fail_reason", {}), data  # 失败原因桶
assert isinstance(data.get("duration"), (int, float))
assert data.get("finish") is not None

print("JSON OK — Scrapy stats 落盘闭环通过")
shutil.rmtree(td, ignore_errors=True)
sys.exit(0)