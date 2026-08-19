"""全局下载存档（学 yt-dlp 的 download-archive）——「已成功下载」持久化清单。

与「断点续载」的区别：断点续载只在本地文件仍存在时跳过；存档则跨会话、
与文件系统无关——即使文件被清理/移动，重新爬同一批资源也会直接跳过，
只下新增作品（定时跟进博主页面的关键省流量手段）。

URL 归一：取 ``scheme://host/path``（丢弃查询串）。原因：抖音/快手等签名
直链的参数每次过期变化，但同一资源的 path 稳定；按 path 判定不会误判不同
清晰度（不同变体路径不同）。

存储（kvjournal）：append-only JSONL + 上限淘汰 + 惰性压缩，量大时不卡写
（旧版每记一条全量重写整个 JSON，几十万条时逐下载卡顿）。旧 JSON 文件
读取兼容，升级无缝。
"""

from __future__ import annotations

import os
import time
from urllib.parse import urlparse

from kvjournal import KVJournal

_locked_journal: KVJournal | None = None


def _journal() -> KVJournal:
    global _locked_journal
    if _locked_journal is None:
        _locked_journal = KVJournal(archive_file(),
                                    max_entries=int(os.environ.get(
                                        "RESOURCES_ARCHIVE_MAX", "200000")))
    return _locked_journal


def archive_file() -> str:
    """存档文件路径（环境变量 RESOURCES_ARCHIVE_FILE 可换位置）。"""
    return os.environ.get("RESOURCES_ARCHIVE_FILE", "download_archive.json")


def canonical_url(url: str) -> str:
    """资源 URL 的稳定标识：scheme://host/path（去查询串，host 小写）。"""
    p = urlparse(url or "")
    return f"{p.scheme}://{p.netloc.lower()}{p.path}"


def contains(url: str) -> bool:
    """URL 是否已在存档中（已成功下载过）。"""
    return _journal().contains(canonical_url(url))


def record(url: str, size: int = 0) -> None:
    """记录一次成功下载（URL 归一后追加一行；写失败静默，不阻塞下载）。"""
    if not url:
        return
    _journal().set(canonical_url(url), {"t": int(time.time()), "size": size})


def clear() -> None:
    """清空内存缓存（测试/手动重置用；下次访问时按文件重建）。"""
    _journal().clear()


def size() -> int:
    return _journal().size()