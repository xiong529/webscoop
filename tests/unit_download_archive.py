"""下载存档 / 死链表测试：URL 归一 / 记录-命中闭环 / 持久化 / 状态守卫。

    python tests/unit_download_archive.py
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="ws_arch_")
os.environ["RESOURCES_ARCHIVE_FILE"] = os.path.join(_TMP, "archive.json")
os.environ["RESOURCES_DEAD_FILE"] = os.path.join(_TMP, "dead.json")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from download_archive import canonical_url, clear as arch_clear, contains, record, size  # noqa: E402
from dead_list import DEAD_STATUS, clear as dead_clear, is_dead, mark_dead  # noqa: E402

arch_clear()
dead_clear()
passed = 0


def check(name: str, cond: bool):
    global passed
    passed += 1
    print("PASS" if cond else "FAIL", name)
    assert cond, name


# ---- URL 归一 ----
check("canonical: 去查询串",
      canonical_url("https://v.douyin.com/x.mp4?sign=abc&b=1")
      == "https://v.douyin.com/x.mp4")
check("canonical: host 小写（path 保留原样）",
      canonical_url("HTTPS://V.DouYin.COM/X.MP4")
      == "https://v.douyin.com/X.MP4")
check("canonical: 不同 path 不同",
      canonical_url("https://x.com/a.mp4") != canonical_url("https://x.com/b.mp4"))

# ---- 存档闭环 ----
check("archive: 未记录不在", contains("https://a.com/1.mp4") is False)
record("https://a.com/1.mp4?token=abc", size=12345)
check("archive: 命中（参数不同也命中）", contains("https://a.com/1.mp4") is True)
check("archive: 命中原样 URL", contains("https://a.com/1.mp4?token=abc") is True)
check("archive: 其他 path 不命中", contains("https://a.com/2.mp4") is False)
record("https://b.com/x.mp4")
check("archive: 计数", size() == 2)

# ---- 死链表闭环 ----
check("dead: 初始不命中", is_dead("https://c.com/gone.mp4") is False)
mark_dead("https://c.com/gone.mp4", 404)
check("dead: 404 命中", is_dead("https://c.com/gone.mp4?x=1") is True)
mark_dead("https://c.com/never.gif", 403)
check("dead: 403 不标记（风控非死链）", is_dead("https://c.com/never.gif") is False)
check("dead: DEAD_STATUS 守卫", DEAD_STATUS == {404, 410, 451})

# ---- 持久化（内存缓存清空后从文件重建）----
arch_clear()
check("archive: 持久化重读", contains("https://a.com/1.mp4") is True)
dead_clear()
check("dead: 持久化重读", is_dead("https://c.com/gone.mp4") is True)

# ---- 原子写：无 .tmp 残留 ----
check("archive: 无临时残留",
      not os.path.exists(os.environ["RESOURCES_ARCHIVE_FILE"] + ".tmp"))
check("dead: 无临时残留",
      not os.path.exists(os.environ["RESOURCES_DEAD_FILE"] + ".tmp"))
check("archive: 文件存在", os.path.exists(os.environ["RESOURCES_ARCHIVE_FILE"]))
check("dead: 文件存在", os.path.exists(os.environ["RESOURCES_DEAD_FILE"]))

print(f"DONE  PASS={passed} FAIL=0")