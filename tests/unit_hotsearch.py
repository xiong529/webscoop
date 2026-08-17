"""热搜榜测试：本地 HTTP 服务模拟 B 站 ranking/v2 接口。

    python tests/unit_hotsearch.py
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hot_search import bilibili_hot  # noqa: E402

passed = 0


def check(name: str, cond: bool):
    global passed
    passed += 1
    print("PASS" if cond else "FAIL", name)
    assert cond, name


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(_PAYLOAD).encode("utf-8")
        self.send_response(200 if self.path != "/bad" else 500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


_PAYLOAD = {
    "code": 0,
    "data": {"list": [
        {"bvid": "BV1xx1", "title": "第一条", "pic": "https://i0.hdslb.com/a.jpg",
         "owner": {"name": "up主A"}, "stat": {"view": 123456}, "duration": 100},
        {"bvid": "BV1xx2", "title": "第二条", "pic": "https://i0.hdslb.com/b.jpg",
         "owner": {"name": "up主B"}, "stat": {"view": 99}, "duration": 200},
        {"bvid": "", "title": "无bvid应跳过"},   # 无效条目
        {"bvid": "BV1xx3", "title": "", "pic": "https://i0.hdslb.com/c.jpg",
         "owner": {"name": "up主C"}, "stat": {"view": 7}, "duration": 300},
    ]},
}

srv = HTTPServer(("127.0.0.1", 0), _Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{port}"

items = bilibili_hot(endpoint=f"{BASE}/ranking/v2")
check("hot: 3 条（1 条无效跳过）", len(items) == 3)
check("hot: 排名顺序", [it["rank"] for it in items] == [1, 2, 3])
check("hot: 首条字段完整",
      items[0]["url"] == "https://www.bilibili.com/video/BV1xx1"
      and items[0]["title"] == "第一条"
      and items[0]["author"] == "up主A" and items[0]["view"] == 123456)
check("hot: 空标题保留", items[2]["title"] == "")

check("hot: limit 截断", len(bilibili_hot(endpoint=f"{BASE}/ranking/v2", limit=2)) == 2)

try:
    bilibili_hot(endpoint=f"{BASE}/bad")
    check("hot: 5xx 抛异常", False)
except Exception:
    check("hot: 5xx 抛异常", True)

_PAYLOAD = {"code": -412}   # 接口业务错误
try:
    bilibili_hot(endpoint=f"{BASE}/ranking/v2")
    check("hot: code!=0 抛异常", False)
except RuntimeError as exc:
    check("hot: code!=0 抛异常", "-412" in str(exc))

_PAYLOAD = {"code": 0}
check("hot: 空榜单返回空", bilibili_hot(endpoint=f"{BASE}/ranking/v2") == [])

srv.shutdown()
print(f"DONE  PASS={passed} FAIL=0")