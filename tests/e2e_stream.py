"""Discoverer 流式回调 + 可打断 端到端测试。

本地 HTTP 服务:
- 一个含 30 张图片链接的页面,每张图片响应延迟 120ms(制造"抓取慢"场景)
- 验证:on_resource 逐个到达(不用等全部探测完)、stop_event 置位后提前返回部分结果

运行:python tests/e2e_stream.py
"""
import http.server
import os
import sys
import tempfile
import threading
import time


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 下载存档/死链表隔离到临时目录，避免污染仓库根目录
_TMP_ARCH = tempfile.mkdtemp(prefix="ws_e2e_arch_")
os.environ["RESOURCES_ARCHIVE_FILE"] = os.path.join(_TMP_ARCH, "archive.json")
os.environ["RESOURCES_DEAD_FILE"] = os.path.join(_TMP_ARCH, "dead.json")

import config
from gui_crawler import Discoverer
from gui_fetch import FetchSession

config.PROXY_ENABLED = False
config.DEFAULT_PROXY = ""
config.FILTER_ICONS = True
config.MIN_RESOURCE_SIZE = 0  # 不按体积过滤，专注流式/停止行为

N = 30
IMG = b"\xff\xd8\xff\xe0" + b"\x00" * 256


class H(http.server.BaseHTTPRequestHandler):
    page = ("<!doctype html><html><body>" +
            "".join(f'<img src="/img/{i}.jpg">' for i in range(N)) +
            "</body></html>").encode()

    def do_GET(self):
        if self.path == "/":
            self._ok("text/html", self.page)
        elif self.path.startswith("/img/"):
            time.sleep(0.12)
            self._ok("image/jpeg", IMG)
        else:
            self.send_response(404)
            self.end_headers()

    def _ok(self, ct, body):
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):
        pass


def free_port():
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    srv.server_close()
    return port


PORT = free_port()
server = http.server.HTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"

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


# --- 测试 1:流式回调(不等待全部探测完成) ---
arrived = []
first_arrive_at = [0.0]


def on_r(r):
    if not arrived and hasattr(r, "url"):
        first_arrive_at[0] = time.time()
    arrived.append(r)


session = FetchSession(proxy_enabled=False)
d = Discoverer(session=session, on_resource=on_r)
start = time.time()
resources, title = d.discover(BASE, progress_cb=None)
full_elapsed = time.time() - start

check("all resources discovered", len(resources), N)
check("on_resource got every resource", len(arrived), N)
# 流式:第一条应在整体完成之前到达(探测是带延迟并发的,首批应早于全部)
check("streaming: first item arrives before completion",
      first_arrive_at[0] < start + full_elapsed, True)
check("streaming: first item early (< full_elapsed - margin)",
      (full_elapsed - (first_arrive_at[0] - start)) > 0.2, True)
session.close()

# --- 测试 2:stop_event 提前打断 ---
arrived2 = []


def on_r2(r):
    arrived2.append(r)


session2 = FetchSession(proxy_enabled=False)
stop_ev = threading.Event()


def stopper():
    time.sleep(0.55)  # 大约在探测到一小半时结束
    stop_ev.set()


th = threading.Thread(target=stopper, daemon=True)
th.start()
d2 = Discoverer(session=session2, stop_event=stop_ev, on_resource=on_r2)
start2 = time.time()
resources2, title2 = d2.discover(BASE, progress_cb=None)
elapsed2 = time.time() - start2

# 资源列表在构建时就含全部 URL(内存开销,不慢);停止打断的是「探测网络」,
# 因此真正语义:上屏数(arrived)显著少于总数。
check("stopped run streams fewer than all", len(arrived2) < N, True)
check("stopped run still streams something", len(arrived2) > 0, True)
check("stopped run is quick (< 3s)", elapsed2 < 3.0, True)
session2.close()
server.shutdown()

print(f"DONE  PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)