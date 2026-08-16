"""重试 + failures.json 端到端验证。

服务器:/flaky.txt 前 2 次请求返回 500(随后 200);/perm.txt 默认一直 500
         (首轮后切换为 200);/ok.txt 恒 200。
断言:
  1) flaky 经过重试最终成功落盘
  2) perm 首轮重试 3 次仍失败,原因含「重试 3 次」,且写入 failures.json
  3) ok 正常成功、flaky/ok 不出现在 failures.json
  4) 切换服务器后重跑 perm -> 成功,failures.json 中该 URL 被清掉

运行:python tests/e2e_retry.py
"""
import http.server
import os
import shutil
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config
config.DOWNLOAD_RETRY_TIMES = 3
config.DOWNLOAD_RETRY_BACKOFF = 0.1
config.MIN_RESOURCE_SIZE = 1024

from gui_crawler import Downloader, Resource, load_failures
from gui_fetch import FetchSession

BODY = b"d" * 2048
STATE = {"perm_ok": False}
fail_requests = [0]  # flaky.txt HEAD/GET 失败请求计数


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/flaky.txt":
            fail_requests[0] += 1
            if fail_requests[0] <= 2:
                self._err(500)
                return
            self._ok(b"flaky-" + BODY)
        elif path == "/perm.txt":
            if not STATE["perm_ok"]:
                self._err(500)
                return
            self._ok(b"perm-" + BODY)
        elif path == "/ok.txt":
            self._ok(b"ok-" + BODY)
        else:
            self._err(404)

    def do_HEAD(self):
        self.do_GET()

    def _ok(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command == "GET":
            self.wfile.write(body)

    def _err(self, code: int):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()


def free_port():
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    srv.server_close()
    return port


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
server = http.server.HTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=server.serve_forever, daemon=True).start()

OUTDIR = tempfile.mkdtemp(prefix="rr_e2e_retry_")
session = FetchSession(proxy_enabled=False)


def res(url: str) -> Resource:
    return Resource(url=url, page_url=f"{BASE}/",
                    name=os.path.basename(url.split("?")[0]))


def find(*names):
    found = set()
    for root, _, files in os.walk(OUTDIR):
        for fn in files:
            if fn in names:
                found.add(fn)
    return found


dl = Downloader(OUTDIR, session=session, workers=2)
failed_names = []
dl.start([res(f"{BASE}/flaky.txt"),
          res(f"{BASE}/perm.txt"),
          res(f"{BASE}/ok.txt")],
         progress_cb=lambda d, t, n, ok: failed_names.append((n, ok)))

first = find("flaky.txt", "perm.txt", "ok.txt")
print("first-run files:", sorted(first))
print("failed_names:", failed_names)
print("failures:", dl.failures)

assert "flaky.txt" in first, "flaky 经重试后应成功落盘"
assert "ok.txt" in first, "ok 应直接成功"
assert "perm.txt" not in first, "perm 首轮应失败"

detail = dl.failures.get("perm.txt", "")
assert "重试 3 次" in detail, f"perm 原因应含重试次数, 实际: {detail}"
assert "HTTP 500" in detail, f"perm 原因应含 HTTP 500, 实际: {detail}"

entries = load_failures(OUTDIR)
perm_url = f"{BASE}/perm.txt"
assert perm_url in entries, "failures.json 应记录 perm"
assert "reason" in entries[perm_url] and "failed_at" in entries[perm_url]
assert f"{BASE}/flaky.txt" not in entries, "flaky 成功不应进失败表"
assert f"{BASE}/ok.txt" not in entries, "ok 成功不应进失败表"
print("PASS first-round asserts")

# ---- 第二轮:服务器放行 perm,重跑应成功并从 failures.json 移除 ----
STATE["perm_ok"] = True
dl2 = Downloader(OUTDIR, session=session, workers=1)
dl2.start([res(f"{BASE}/perm.txt")])
assert find("perm.txt"), "重跑 perm 应成功"
entries2 = load_failures(OUTDIR)
assert perm_url not in entries2, "成功后应从 failures.json 移除该 URL"
print("PASS second-round asserts (清理失败表)")

server.shutdown()
print("RETRY-E2E OK")