"""有界键值日志（append-only JSONL + 惰性压缩）——存档/死链表等「只增查」场景。

问题背景：原来 download_archive.json / dead_urls.json 每记一条都全量重写
整个 JSON，条目多到几十万时写一次卡一次，且文件无上限无限膨胀。

本实现：
- 写入 O(1)：每次只 append 一行 JSON（无序化更新，最后一行胜出）
- 读取一次性加载为内存 dict（与旧行为一致，查询零成本）
- 有界：条目数超过 max_entries 时淘汰最旧（按记录内 "t" 时间戳）；
  日志文件体积超过内存表达 2 倍（历史重写污染）时触发一次全量压缩
- 兼容旧格式：整文件是一个 JSON 对象 `{...}`  时按旧格式读取
  （升级无缝）；压缩后也写这种「单 JSON 对象」形态
- 线程安全；写失败静默（不阻塞下载主流程）

用法：
    j = KVJournal(path, max_entries=200_000)
    j.set(url, {"t": ..., "size": ...})
    j.contains(url)  /  j.get(url)  /  j.size()  /  j.clear()
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any


class KVJournal:
    def __init__(self, path: str, max_entries: int = 200_000,
                 compact_growth: float = 2.0):
        self._path = path
        self._max_entries = max(1, int(max_entries))
        self._compact_growth = max(1.1, float(compact_growth))
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] | None = None  # 惰性加载；None = 未加载

    # ---------------- 读取 ----------------

    def _parse_line(self, line: str) -> tuple[str, dict[str, Any]] | None:
        line = line.strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict) or len(obj) != 1:
            return None  # 每行恰好一个「key -> value」对
        k, v = next(iter(obj.items()))
        return k, v

    def _load_locked(self) -> dict[str, dict[str, Any]]:
        if self._data is not None:
            return self._data
        data: dict[str, dict[str, Any]] = {}
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                text = f.read()
            stripped = text.lstrip("\ufeff \t\r\n")
            if not stripped:
                return data
            # 整文件就是一个 JSON 对象（旧格式/压缩形态）→ 直接读；
            # 否则是逐行 JSONL（多行时整体解析必然失败，回退逐行）
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    return obj
            except (ValueError, TypeError):
                pass
            for ln in text.splitlines():
                parsed = self._parse_line(ln)
                if parsed:
                    data[parsed[0]] = parsed[1]
        except OSError:
            pass
        self._data = data
        return data

    # ---------------- 写入 ----------------

    def _evict_locked(self) -> bool:
        """超过上限：按条目内 "t"（时间戳）淘汰最旧，返回是否需要压缩重写。"""
        data = self._load_locked()
        if len(data) <= self._max_entries:
            return False
        ordered = sorted(data.items(),
                         key=lambda kv: (kv[1].get("t", 0) if isinstance(kv[1], dict) else 0, kv[0]))
        for key, _ in ordered[:len(data) - self._max_entries]:
            data.pop(key, None)
        return True

    def _compact_locked(self) -> None:
        """全量重写为单 JSON 对象（日志尾部事务化，防无限膨胀）。"""
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=0)
            os.replace(tmp, self._path)
        except OSError:
            pass

    def _append(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            data = self._load_locked()
            data[key] = value
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({key: value}, ensure_ascii=False) + "\n")
            except OSError:
                return  # 写失败静默（下载主流程不被存档卡死）
            if self._evict_locked():
                self._compact_locked()  # 淘汰后一次性重写
            elif os.path.getsize(self._path) > self._compact_growth \
                    * len(json.dumps(data, ensure_ascii=False)):
                self._compact_locked()  # 日志被大量历史重写污染，压缩回收

    # ---------------- 公开 API ----------------

    def set(self, key: str, value: dict[str, Any]) -> None:
        """记录/覆盖一个键（append 一行，O(1)）。"""
        if not key or not isinstance(value, dict):
            return
        self._append(key, value)

    def get(self, key: str) -> dict[str, Any] | None:
        self._lock.acquire()
        try:
            return self._load_locked().get(key)
        finally:
            self._lock.release()

    def contains(self, key: str) -> bool:
        self._lock.acquire()
        try:
            return key in self._load_locked()
        finally:
            self._lock.release()

    def size(self) -> int:
        self._lock.acquire()
        try:
            return len(self._load_locked())
        finally:
            self._lock.release()

    def clear(self) -> None:
        """清空内存缓存（文件原样保留：下次访问按文件重建——与旧行为一致）。"""
        self._lock.acquire()
        try:
            self._data = None
        finally:
            self._lock.release()

    def reset(self) -> None:
        """清空内存与文件（测试/彻底重置用）。"""
        with self._lock:
            self._data = {}
            try:
                os.remove(self._path)
            except OSError:
                pass