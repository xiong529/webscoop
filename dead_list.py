"""死链列表（404/410/451 等永久失败 URL 的持久化清单）。

与 download_archive 对称：这些 URL 已确认「资源不存在」（不是风控、不是
瞬时），后续爬取直接跳过，不再重试，避免每次重爬同一批旧站都撞一次 404。

安全边界：仅 404/410/451 这种确定性状态进入死链表；403（风控）、5xx、网络
错误（瞬时）一律不标记——签名过期导致的 404 在 r.raw_url 兜底后才判定，
见调用方。

存储（kvjournal）：append-only JSONL + 上限淘汰 + 惰性压缩；旧 JSON 文件
读取兼容，升级无缝。
"""

from __future__ import annotations

import os
import time

from download_archive import canonical_url
from kvjournal import KVJournal

#: 判定为「永久死链」的 HTTP 状态码
DEAD_STATUS = {404, 410, 451}

_locked_journal: KVJournal | None = None


def _journal() -> KVJournal:
    global _locked_journal
    if _locked_journal is None:
        _locked_journal = KVJournal(dead_file(),
                                    max_entries=int(os.environ.get(
                                        "RESOURCES_DEAD_MAX", "100000")))
    return _locked_journal


def dead_file() -> str:
    """死链表文件路径（环境变量 RESOURCES_DEAD_FILE 可换位置）。"""
    return os.environ.get("RESOURCES_DEAD_FILE", "dead_urls.json")


def is_dead(url: str) -> bool:
    """URL 是否已确认永久失效（命中则跳过下载）。"""
    return _journal().contains(canonical_url(url))


def mark_dead(url: str, status: int = 404) -> None:
    """把 URL 记入死链表（写失败静默）。"""
    if not url or status not in DEAD_STATUS:
        return
    _journal().set(canonical_url(url), {"status": status, "t": int(time.time())})


def clear() -> None:
    """清空内存缓存（测试/手动重置用）；文件下次访问时重建。"""
    _journal().clear()