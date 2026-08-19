"""REST API 服务测试：路由 / 鉴权 / 入参校验 / 任务接线（本地回环，不触外网）。

    python tests/unit_server.py
"""
import http.client
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from headless import Task
from server import _Handler, ThreadingHTTPServer

passed = 0


def check(name: str, cond: bool):
    global passed
    passed += 1
    print("PASS" if cond else "FAIL", name)
    assert cond, name


class _FakeRegistry:
    """注入假注册表：submit 返回立即可查的 Task，不触发真实爬取线程。"""

    def __init__(self):
        self._tasks: dict[str, Task] = {}

    def submit(self, kind, data, fn):
        task = Task(kind, data)
        self._tasks[task.id] = task
        return task

    def get(self, task_id):
        return self._tasks.get(task_id)

    def describe(self, task_id):
        t = self._tasks.get(task_id)
        return None if t is None else {
            "id": t.id, "kind": t.kind, "state": t.state, "error": t.error,
            "progress": t.progress, "created": t.created, "started": t.started,
            "finished": t.finished, "title": t.title,
            "outdir": t.data.get("outdir", ""), "resource_count": len(t.resources),
        }

    def snapshot(self):
        return [self.describe(i) for i in self._tasks]


fake = _FakeRegistry()
_Handler.registry = fake
_Handler.token = ""
srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"127.0.0.1:{srv.server_address[1]}"


def req(method: str, path: str, body: dict | None = None,
        token: str = "") -> tuple[int, dict]:
    conn = http.client.HTTPConnection(base, timeout=10)
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Api-Token"] = token
    conn.request(method, path, json.dumps(body) if body is not None else None,
                 headers)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except json.JSONDecodeError:
        return resp.status, {}


# ---------- 健康 / 路由 ----------
code, payload = req("GET", "/api/health")
check("health: 200 + version", code == 200 and "version" in payload)
code, payload = req("GET", "/api/nope")
check("未知路由: 404", code == 404)
code, payload = req("POST", "/api/nope")
check("POST 未知路由: 404", code == 404)

# ---------- discover 入参校验 ----------
code, payload = req("POST", "/api/discover", {"urls": []})
check("discover 空 url 数组: 400", code == 400)
code, payload = req("POST", "/api/discover", {})
check("discover 缺 urls: 400", code == 400)
code, payload = req("POST", "/api/discover", {"urls": "not-list"})
check("discover 非数组 urls: 400", code == 400)

# ---------- discover 成功接线 ----------
code, payload = req("POST", "/api/discover", {"urls": ["https://a.example/p"]})
check("discover 合法: 202 + task_id", code == 202
      and payload["task_id"].startswith("discover-"))
tid = payload["task_id"]
code, payload = req("GET", f"/api/tasks/{tid}")
check("task 详情: 200 + kind=discover",
      code == 200 and payload["kind"] == "discover")

# ---------- download 接线 ----------
code, payload = req("POST", "/api/download", {"task_id": "missing"})
check("download 无效 task_id: 400", code == 400)
code, payload = req("POST", "/api/download", {})
check("download 缺 task_id: 400", code == 400)
code, payload = req("POST", "/api/download", {"task_id": tid, "outdir": "o"})
check("download 合法: 202 + 新 task_id",
      code == 202 and payload["task_id"].startswith("download-"))

# ---------- 列表 / 统计 ----------
code, payload = req("GET", "/api/tasks")
check("tasks 列表: 200 + 含 discover", code == 200
      and any(t["kind"] == "discover" for t in payload["tasks"]))
code, payload = req("GET", "/api/stats")
check("stats: 200 + by_state", code == 200
      and "by_state" in payload and "tasks" in payload)
code, payload = req("GET", "/api/tasks/zzz")
check("task 不存在: 404", code == 404)

# ---------- 令牌鉴权 ----------
_Handler.token = "s3cret"
code, payload = req("GET", "/api/tasks")
check("无令牌: 401", code == 401)
code, payload = req("GET", "/api/tasks", token="s3cret")
check("带 X-Api-Token: 200", code == 200)
code, payload = req("GET", "/api/tasks?token=s3cret")
check("query token: 200", code == 200)
code, payload = req("GET", "/api/health")
check("health 无令牌: 401", code == 401)
code, payload = req("POST", "/api/discover", {"urls": ["u"]}, token="s3cret")
check("POST 带令牌: 202", code == 202)
_Handler.token = ""

srv.shutdown()
print(f"DONE  PASS={passed} FAIL=0")