"""统一格式选择器（学 yt-dlp 的 format selector 思路，跨平台复用）。

语法（简化子集）::

    best                    最高清（分辨率/尺寸优先）
    worst                   最低清
    best[height<=1080]      最高清且高不超过 1080
    best[height>=720,size>=1000000]   多个条件 AND

字段: height / width / size / id / label（数值比较，label 用于优先级并列时的排序）。

- select_formats: 对 Format 列表（含元数据）做筛选 + 排序取一个
- pick_video_url: 对纯 URL 列表（文件名里带 WxH 或 _large 等变体标记）直接选，
  与 discover_common.pick_best_video 旧语义兼容（分辨率乘积主序 + 变体名次序）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SPEC_RE = re.compile(r"^(best|worst)(?:\[([^\]]+)\])?$")
_COND_RE = re.compile(r"^(\w+)(<=|>=|==|<|>)(-?\d+)$")

# 变体名排序权重（大 > 中 > 小 > 极小），解析自 URL 文件名
_VARIANT_ORDER = {"large": 3, "medium": 2, "small": 1, "tiny": 0}


@dataclass
class Format:
    """一个可选格式：URL + 元数据。height/width/size 为 0 表示未知。"""

    url: str
    height: int = 0
    width: int = 0
    size: int = 0
    label: str = ""  # 供并列时的次级排序（如 "1080p"、变体名）

    @classmethod
    def from_url(cls, url: str) -> "Format":
        """从 URL 文件名解析 WxH 与变体标记（无元数据调用入口）。

        文件名约定为「宽 x 高」：``1920x1080`` 宽 1920 高 1080，
        ``1080x1920`` 为竖版。两数不区分大小写/×/下划线分隔。
        """
        name = (url.rsplit("/", 1)[-1] if "/" in url else url).lower()
        height = width = 0
        m = re.search(r"(\d{3,4})[xX×_](\d{3,4})", name)
        if m:
            width, height = int(m.group(1)), int(m.group(2))
        label = ""
        for variant, _order in _VARIANT_ORDER.items():
            if variant in name:
                label = variant
                break
        return cls(url=url, height=height, width=width, label=label)


def _matches(cond: str, fmt: Format) -> bool:
    m = _COND_RE.match(cond.strip())
    if not m:
        return False
    field_name, op, raw = m.group(1), m.group(2), m.group(3)
    value = getattr(fmt, field_name, None)
    if not isinstance(value, int):
        return False
    rhs = int(raw)
    return {
        "<=": value <= rhs, ">=": value >= rhs,
        "==": value == rhs, "<": value < rhs, ">": value > rhs,
    }[op]


def _sort_key(fmt: Format) -> tuple:
    """排序键：主序分辨率，未知(0)垫底；次序宽度；再序尺寸；末序变体名权重。"""
    return (fmt.height or -1, fmt.width or -1, fmt.size, _VARIANT_ORDER.get(fmt.label, -1))


def select_formats(items: list[Format], spec: str = "best") -> Format | None:
    """按 spec 从格式列表里选一个；无匹配返回 None。"""
    m = _SPEC_RE.match(spec.strip())
    if not m or not items:
        return None
    direction, conds = m.group(1), m.group(2)
    pool = items
    if conds:
        cond_list = [c for c in conds.split(",") if c.strip()]
        pool = [f for f in pool if all(_matches(c, f) for c in cond_list)]
    if not pool:
        return None
    return (max if direction == "best" else min)(pool, key=_sort_key)


def pick_video_url(candidates: list[str], spec: str = "best") -> str:
    """从候选视频 URL 里按 spec 选一个；无匹配时退回第一个。"""
    if not candidates:
        return ""
    picked = select_formats([Format.from_url(u) for u in candidates], spec)
    return picked.url if picked else candidates[0]
