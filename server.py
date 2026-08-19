"""REST API 层（stdlib 实现，零新增依赖）。

仅绑定 127.0.0.1 —— 带下载能力的 HTTP 服务不做网络暴露。访问令牌可选：
设置 RESOURCES_API_TOKEN（或 --token）后，请求需带 X-Api-Token 头或 ?token=。

端点（全部 JSON）：
    POST /api/discover  {"urls": [...], "render": bool}  → 202 {"task_id": ...}
    POST /api/download  {"task_id": <discover任务>, "outdir": ""} → 202
    GET  /api/tasks                → 全部任务快照
    GET  /api/tasks/{id}           → 单任务详情（discover 任务含资源列表）
    GET  /api/stats                → 累计统计（任务回归 + stats.json）
    GET  /api/health               → {"ok": true, "version": ...}
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from headless import TaskRegistry, run_discover, run_download

REGISTRY = TaskRegistry()


def _read_stats_json() -> dict:
    try:
        import config
        path = os.path.join(config.INFORMATION_DIR, "stats.json")
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class _Handler(BaseHTTPRequestHandler):
    server_version = "webscoop-api/1"
    registry: TaskRegistry = REGISTRY
    token: str = ""

    # ---------- 框架 ----------
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.token:
            return True
        if self.headers.get("X-Api-Token") == self.token:
            return True
        return parse_qs(urlparse(self.path).query).get("token", [""])[0] == self.token

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, json.JSONDecodeError):
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        if path == "/api/health":
            from resources_reptile import __version__ as ver
            return self._send(200, {"ok": True, "version": ver})
        if path == "/api/tasks":
            return self._send(200, {"tasks": self.registry.snapshot()})
        if path.startswith("/api/tasks/"):
            detail = self.registry.describe(path.rsplit("/", 1)[-1])
            if detail is None:
                return self._send(404, {"error": "task not found"})
            if detail["kind"] == "discover":
                task = self.registry.get(detail["id"])
                if task:
                    detail["resources"] = task.resources[:500]
            return self._send(200, detail)
        if path == "/api/stats":
            snap = self.registry.snapshot()
            return self._send(200, {
                "tasks": len(snap),
                "by_state": {s: sum(1 for t in snap if t["state"] == s)
                             for s in ("queued", "running", "done", "failed")},
                "downloads_ok": sum(t["progress"].get("ok", 0) for t in snap),
                "downloads_fail": sum(t["progress"].get("fail", 0) for t in snap),
                "cumulative": _read_stats_json(),
            })
        return self._send(404, {"error": f"unknown path: {path}"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        if path == "/api/discover":
            body = self._body()
            urls = body.get("urls")
            if not isinstance(urls, list) or not urls:
                return self._send(400, {"error": "urls 必须为非空数组"})
            task = self.registry.submit(
                "discover", {"urls": [u for u in urls if isinstance(u, str)],
                             "render": bool(body.get("render"))},
                run_discover)
            return self._send(202, {"task_id": task.id})
        if path == "/api/download":
            body = self._body()
            task_id = body.get("task_id")
            src = self.registry.get(task_id or "")
            if not src:
                return self._send(400, {"error": "task_id 不存在"})
            task = self.registry.submit(
                "download",
                {"task_id": src.id, "outdir": str(body.get("outdir", "")),
                 "registry": self.registry},
                run_download)
            return self._send(202, {"task_id": task.id})
        return self._send(404, {"error": f"unknown path: {path}"})

    def log_message(self, fmt, *args):  # 静音：stdout 留给 CLI
        return


def serve(port: int = 8000, token: str = "") -> int:
    _Handler.token = token or os.environ.get("RESOURCES_API_TOKEN", "")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(serve())