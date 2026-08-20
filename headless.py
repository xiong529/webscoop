"""无头核心：CLI 与 REST API 共用的任务注册表与发现/下载执行。

设计：tkinter GUI 只是宿主；这里把 gui_crawler.Discoverer / Downloader
以「任务」形式跑在后台线程，每任务独立状态（队列→运行→完成/失败），
进度与结果可被 CLI 轮询或 API 查询。新增任务 = TaskRegistry.submit()。

定时跟进（follow run）不走任务注册表：由 CLI 循环驱动，每轮快照
follow_list.urls() 后逐条 发现→下载，配合全局下载存档只抓新增。
"""
from __future__ import annotations

import threading
import time
import uuid

from gui_crawler import Discoverer, Downloader, Resource


class Task:
    """单个后台任务的不可在本线程执行的可观测状态。"""

    KINDS = ("discover", "download")

    def __init__(self, kind: str, data: dict) -> None:
        self.id = f"{kind}-{uuid.uuid4().hex[:8]}"
        self.kind = kind
        self.data = data
        self.state = "queued"          # queued / running / done / failed / cancelled
        self.error: str = ""
        self.created = time.time()
        self.started = 0.0
        self.finished = 0.0
        self.progress: dict = {"total": 0, "ok": 0, "fail": 0}
        self.resources: list[dict] = []    # discover 任务的结果（to_dict）
        self.title: str = ""               # discover 首个页面的标题
        self.cancel_event = threading.Event()  # 协作式取消：置位后任务尽快停止

    @property
    def done(self) -> bool:
        return self.state in ("done", "failed", "cancelled")


class TaskRegistry:
    """内存任务表：submit 立即启动后台线程执行 fn(task)，线程安全。"""

    def __init__(self, max_workers: int = 4) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}
        self._sema = threading.Semaphore(max_workers)

    def submit(self, kind: str, data: dict, fn) -> Task:
        task = Task(kind, data)
        with self._lock:
            self._tasks[task.id] = task
        threading.Thread(target=self._run, args=(task, fn), daemon=True).start()
        return task

    def _run(self, task: Task, fn) -> None:
        self._sema.acquire()
        try:
            task.state = "running"
            task.started = time.time()
            fn(task)
            if task.cancel_event.is_set():
                task.state = "cancelled"
            else:
                task.state = "done"
        except Exception as exc:  # 任务边界：任何异常都转成失败状态，不炸线程
            task.state = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
        finally:
            task.finished = time.time()
            self._sema.release()

    def cancel(self, task_id: str) -> bool:
        """协作式取消（discover 立即停队列，download 停止新分片）。返回是否命中。"""
        task = self.get(task_id)
        if task is None:
            return False
        task.cancel_event.set()
        return True

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def describe(self, task_id: str) -> dict | None:
        with self._lock:
            t = self._tasks.get(task_id)
            return self._describe(t) if t else None

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [self._describe(t) for t in self._tasks.values()]

    def _describe(self, t: Task) -> dict:
        return {
            "id": t.id, "kind": t.kind, "state": t.state,
            "error": t.error, "progress": t.progress,
            "created": t.created, "started": t.started, "finished": t.finished,
            "title": t.title, "outdir": t.data.get("outdir", ""),
            "resource_count": len(t.resources),
        }


def run_discover(task: Task) -> None:
    """发现任务体：逐 URL 发现（单 URL 失败不中断），结果入 task.resources。"""
    urls = task.data["urls"]
    task.progress["total"] = len(urls)
    task.title = ""
    for url in urls:
        if task.cancel_event.is_set():
            break
        try:
            discoverer = Discoverer(render_mode=bool(task.data.get("render")),
                                    stop_event=task.cancel_event)
            resources, title = discoverer.discover(url)
            task.title = task.title or (title or "")
            for r in resources:
                if isinstance(r, Resource):
                    task.resources.append(r.to_dict())
        except Exception as exc:
            task.progress["fail"] += 1
            task.progress.setdefault("errors", []).append(f"{url}: {type(exc).__name__}: {exc}")
        else:
            task.progress["ok"] += 1


def run_download(task: Task) -> None:
    """下载任务体：取 discover 任务的结果资源，Downloader 落盘。"""
    registry = task.data["registry"]
    src = registry.get(task.data["task_id"])
    if not src or not src.resources:
        task.error = "来源任务不存在或无资源"
        task.state = "failed"
        return
    resources = [Resource(**{k: v for k, v in d.items()}) for d in src.resources]
    outdir = task.data.get("outdir") or ""

    def on_progress(done: int, total: int, name: str, ok: bool):
        task.progress["total"] = total
        if ok:
            task.progress["ok"] = done - task.progress["fail"]
        else:
            task.progress["fail"] = done - task.progress["ok"]

    downloader = Downloader(outdir=outdir)
    downloader.start(resources, progress_cb=on_progress,
                     cancel_event=task.cancel_event)
    task.progress["ok"] = downloader.stat.downloaded
    task.progress["fail"] = downloader.stat.failed
    task.progress["total"] = downloader.stat.total


def discover_and_download(urls: list[str], outdir: str, render: bool = False,
                          workers: int | None = None) -> dict:
    """CLI 同步路径：发现 + 下载一体化，返回汇总统计。"""
    from config import INFORMATION_DIR
    discoveries: list[dict] = []
    per_url_errors: list[str] = []
    for url in urls:
        try:
            discoverer = Discoverer(render_mode=render, stop_event=threading.Event())
            resources, _title = discoverer.discover(url)
            discoveries.extend(r.to_dict() for r in resources)
        except Exception as exc:
            per_url_errors.append(f"{url}: {type(exc).__name__}: {exc}")
    outdir = outdir or INFORMATION_DIR
    stats = {"urls": len(urls), "found": len(discoveries),
             "errors": per_url_errors, "outdir": outdir,
             "downloaded": 0, "failed": 0, "failed_names": []}
    if discoveries:
        resources = [Resource(**d) for d in discoveries]
        downloader = Downloader(outdir=outdir, workers=workers)
        downloader.start(resources)
        stats["downloaded"] = downloader.stat.downloaded
        stats["failed"] = downloader.stat.failed
        stats["failed_names"] = sorted(downloader.failures)[:50]
    return stats