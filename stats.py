"""抓取统计收集（GUI 与 Scrapy 共用，进程内线程安全，可落盘）。

指标：
- totals: 各类别抓到的资源总数（images / videos / audios / docs / software / archives / others）
- downloaded: 成功下载的文件数
- failed: 下载失败数 + 失败原因分布（按 host、按错误类别）
- pages: 抓取页面数
- start / finish: 本次任务起止时间
- save_json / load_json: 落盘到 outdir/stats.json，重启后仍可复盘

典型用法：
    stats = get_stats()               # 全局单例
    stats.add_downloaded(1, 8.5_000_000)  # 下载成功 1 个，字节数 8.5MB
    stats.add_failed(1, reason=..., host=...)  # 失败 + 原因
    stats.add_category("videos", 3)   # 各类别资源计数
    stats.add_page(2)                 # 抓取页面数
    print(stats.summary())            # 一键格式化摘要
    stats.save_json(outdir)           # 落盘 stats.json
    stats.reset()                     # 新一轮任务清零
"""

from __future__ import annotations

import json
import os
import threading
import time

_CATEGORIES = ("images", "videos", "audios", "docs", "software", "archives", "others")

# 失败原因 → 类别桶（用于分布展示，未知归 others）
_REASON_BUCKETS = {
    "403": "403", "401": "403", "404": "404", "410": "404",
    "429": "429", "5xx": "5xx",
    "超时": "timeout", "timed out": "timeout", "Timeout": "timeout",
    "连接": "network", "Network": "network", "Connection": "network",
    "不完整": "incomplete", "TooManyRedirects": "redirects",
    "download-error": "network", "DOWNLOAD_ERROR": "network",
}


def bucket_for_reason(reason: str) -> str:
    reason = str(reason or "")
    for key, bucket in _REASON_BUCKETS.items():
        if key in reason:
            return bucket
    for code in ("500", "502", "503", "504", "522", "524"):
        if code in reason:
            return "5xx"
    return "other"


class _Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.totals: dict[str, int] = {c: 0 for c in _CATEGORIES}
            self.downloaded = 0
            self.download_bytes = 0
            self.failed = 0
            self.failed_bytes = 0
            self.pages = 0
            self.fail_reason: dict[str, int] = {}   # 类别桶 -> 次数
            self.fail_host: dict[str, int] = {}     # host -> 失败次数
            self.start = time.time()
            self.finish: float | None = None

    # ---------------- 写入 ----------------

    def add_category(self, category: str, count: int = 1):
        with self._lock:
            self.totals[self._safe_cat(category)] += count

    def add_downloaded(self, count: int = 1, bytes_: int = 0):
        with self._lock:
            self.downloaded += count
            self.download_bytes += bytes_

    def add_failed(self, count: int = 1, bytes_: int = 0,
                   reason: str = "", host: str = ""):
        with self._lock:
            self.failed += count
            self.failed_bytes += bytes_
            if reason:
                bucket = bucket_for_reason(reason)
                self.fail_reason[bucket] = self.fail_reason.get(bucket, 0) + count
            if host:
                h = (host or "").lower()
                self.fail_host[h] = self.fail_host.get(h, 0) + count

    def add_page(self, count: int = 1):
        with self._lock:
            self.pages += count

    def mark_finish(self):
        with self._lock:
            self.finish = time.time()

    # ---------------- 读取 ----------------

    @staticmethod
    def _safe_cat(cat: str) -> str:
        return cat if cat in _CATEGORIES else "others"

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "totals": dict(self.totals),
                "downloaded": self.downloaded,
                "download_bytes": self.download_bytes,
                "failed": self.failed,
                "pages": self.pages,
                "fail_reason": dict(self.fail_reason),
                "fail_host": dict(self.fail_host),
                "start": self.start,
                "finish": self.finish,
            }

    @property
    def total_resources(self) -> int:
        with self._lock:
            return sum(self.totals.values())

    @property
    def duration(self) -> float:
        with self._lock:
            end = self.finish or time.time()
            return max(0.0, end - self.start)

    def summary(self, include: set[str] | None = None) -> str:
        """一键摘要（供任务结束 / GUI 状态栏 / Scrapy 日志显示）。"""
        s = self.snapshot()
        elapsed = (s["finish"] or time.time()) - s["start"]
        lines = [f"耗时 {elapsed:.1f}s，抓取页面 {s['pages']}"]
        inners = [f"{k}={v}" for k, v in s["totals"].items() if v]
        lines.append("发现资源：" + ("、".join(inners) if inners else "0"))
        done = s["downloaded"]
        failed = s["failed"]
        mb = s["download_bytes"] / 1024 / 1024
        lines.append(f"下载：成功 {done}（{mb:.1f} MB）失败 {failed}")
        if failed:
            reason_top = sorted(s["fail_reason"].items(),
                                key=lambda kv: -kv[1])[:3]
            if reason_top:
                lines.append("失败原因：" + "、".join(
                    f"{b}×{c}" for b, c in reason_top))
            host_top = sorted(s["fail_host"].items(),
                              key=lambda kv: -kv[1])[:3]
            if host_top:
                lines.append("失败站点：" + "、".join(
                    f"{h}×{c}" for h, c in host_top))
        return "\n".join(lines)

    # ---------------- 落盘 ----------------

    def save_json(self, outdir: str, filename: str = "stats.json") -> str | None:
        """把快照写入 outdir/stats.json（原子写：临时文件 + rename）。"""
        path = os.path.join(outdir, filename)
        try:
            os.makedirs(outdir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({**self.snapshot(), "duration": round(self.duration, 1)},
                          f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return path
        except OSError:
            return None


_stats = _Stats()


def get_stats() -> _Stats:
    """全局统计单例（GUI 下载线程与渲染回调共用它）。"""
    return _stats


def stats() -> _Stats:
    """别名，便于 `from stats import stats` 后直接 `stats().add_*()`。"""
    return _stats