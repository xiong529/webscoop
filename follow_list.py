"""定时跟进关注列表（follow_list.json）读写。

条目：{"url": 页面地址, "name": 备注(可选), "added_at": 时间戳}
持久化在项目根目录（环境变量 RESOURCES_FOLLOW_FILE 可换位置），
供「定时跟进」弹窗使用；配合全局下载存档，每轮只下新增作品。
"""

from __future__ import annotations

import json
import os
import threading
import time

_lock = threading.Lock()
_cache: list[dict] | None = None


def follow_file() -> str:
    return os.environ.get("RESOURCES_FOLLOW_FILE", "follow_list.json")


def _load_locked() -> list[dict]:
    global _cache
    if _cache is None:
        try:
            with open(follow_file(), "r", encoding="utf-8") as f:
                data = json.load(f)
            _cache = data if isinstance(data, list) else []
        except (OSError, ValueError):
            _cache = []
    return _cache


def items() -> list[dict]:
    """当前关注列表（浅拷贝，供调度线程快照）。"""
    with _lock:
        return list(_load_locked())


def urls() -> list[str]:
    return [it["url"] for it in items() if isinstance(it.get("url"), str)]


def add(url: str, name: str = "") -> bool:
    """添加关注（按 URL 去重）。返回是否新增。"""
    if not url:
        return False
    url = url.strip()
    if not url:
        return False
    with _lock:
        data = _load_locked()
        if any(it.get("url") == url for it in data):
            return False
        data.append({"url": url, "name": (name or "").strip(),
                     "added_at": int(time.time())})
        _save_locked()
        return True


def remove(url: str) -> bool:
    with _lock:
        data = _load_locked()
        nxt = [it for it in data if it.get("url") != url]
        if len(nxt) == len(data):
            return False
        _cache[:] = nxt
        _save_locked()
        return True


def _save_locked() -> None:
    path = follow_file()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cache or [], f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def clear() -> None:
    global _cache
    with _lock:
        _cache = None