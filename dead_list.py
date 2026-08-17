"""死链列表（404/410/451 等永久失败 URL 的持久化清单）。

与 download_archive 对称：这些 URL 已确认「资源不存在」（不是风控、不是瞬时），
后续爬取直接跳过，不再重试，避免每次重爬同一批旧站都撞一次 404。

安全边界：仅 404/410/451 这种确定性状态进入死链表；403（风控）、5xx、网络错
（瞬时）一律不标记——签名过期导致的 404 走 r.raw_url 兜底后才判定，见调用方。
"""

from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import urlparse

from download_archive import canonical_url

#: 判定为「永久死链」的 HTTP 状态码
DEAD_STATUS = {404, 410, 451}

_lock = threading.Lock()
_cache: dict[str, dict] | None = None  # canonical url -> {"status": 404, "t": ts}


def dead_file() -> str:
    """死链表文件路径（环境变量 RESOURCES_DEAD_FILE 可换位置）。"""
    return os.environ.get("RESOURCES_DEAD_FILE", "dead_urls.json")


def _load_locked() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(dead_file(), "r", encoding="utf-8") as f:
                data = json.load(f)
            _cache = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _cache = {}
    return _cache


def is_dead(url: str) -> bool:
    """URL 是否已确认永久失效（命中则跳过下载）。"""
    with _lock:
        return canonical_url(url) in _load_locked()


def mark_dead(url: str, status: int = 404) -> None:
    """把 URL 记入死链表（写失败静默）。"""
    if not url or status not in DEAD_STATUS:
        return
    with _lock:
        data = _load_locked()
        data[canonical_url(url)] = {"status": status, "t": int(time.time())}
        path = dead_file()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=0)
            os.replace(tmp, path)
        except OSError:
            pass


def clear() -> None:
    """清空内存缓存（测试/手动重置用）。"""
    global _cache
    with _lock:
        _cache = None
