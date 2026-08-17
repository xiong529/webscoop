"""全局下载存档（学 yt-dlp 的 download-archive）——「已成功下载」持久化清单。

与「断点续载」的区别：断点续载只在本地文件仍存在时跳过；存档则跨会话、
与文件系统无关——即使文件被清理/移动，重新爬同一批资源也会直接跳过，
只下新增作品（定时跟进博主页面的关键省流量手段）。

URL 归一：取 ``scheme://host/path``（丢弃查询串）。原因：抖音/快手等签名
直链的参数每次过期变化，但同一资源的 path 稳定；按 path 判定不会误判不同
清晰度（不同变体路径不同）。

线程安全：进程内锁 + 写临时文件后原子替换；写失败静默（不阻塞下载）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import urlparse

_lock = threading.Lock()
_cache: dict[str, dict] | None = None  # canonical url -> {"t": ts, "size": bytes}


def archive_file() -> str:
    """存档文件路径（环境变量 RESOURCES_ARCHIVE_FILE 可换位置）。"""
    return os.environ.get("RESOURCES_ARCHIVE_FILE", "download_archive.json")


def canonical_url(url: str) -> str:
    """资源 URL 的稳定标识：scheme://host/path（去查询串，host 小写）。"""
    p = urlparse(url or "")
    return f"{p.scheme}://{p.netloc.lower()}{p.path}"


def _load_locked() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(archive_file(), "r", encoding="utf-8") as f:
                data = json.load(f)
            _cache = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _cache = {}
    return _cache


def contains(url: str) -> bool:
    """URL 是否已在存档中（已成功下载过）。"""
    with _lock:
        return canonical_url(url) in _load_locked()


def record(url: str, size: int = 0) -> None:
    """记录一次成功下载（URL 归一后写入；写失败静默，不阻塞下载）。"""
    if not url:
        return
    with _lock:
        data = _load_locked()
        data[canonical_url(url)] = {"t": int(time.time()), "size": size}
        path = archive_file()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=0)
            os.replace(tmp, path)
        except OSError:
            pass


def clear() -> None:
    """清空内存缓存（测试/手动重置用；下次访问时按文件重建）。"""
    global _cache
    with _lock:
        _cache = None


def size() -> int:
    with _lock:
        return len(_load_locked())
