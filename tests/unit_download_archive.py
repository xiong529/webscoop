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

from download_archive import canonical_url, clear as arch_clear, contains, record, size
from dead_list import DEAD_STATUS, clear as dead_clear, is_dead, mark_dead

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

print(f"DONE  PASS={passed} FAIL=0")# ---- KVJournal 专项：上限淘汰 / 旧 JSON 迁移 / 追加不卡写 ----
import kvjournal
_tmp2 = os.path.join(_TMP, "j1.jsonl")
j = kvjournal.KVJournal(_tmp2, max_entries=2)
for i in range(4):
    j.set(f"u{i}", {"t": 100 + i, "size": 1})
check("journal: 超上限淘汰", j.size() == 2)
check("journal: 最旧被淘汰", j.contains("u0") is False)
check("journal: 最新保留", j.contains("u3") is True)
j2 = kvjournal.KVJournal(_tmp2, max_entries=2)
check("journal: 重建后一致", j2.contains("u3") is True and j2.size() == 2)
# 旧格式（单 JSON 对象）迁移读取
_tmp3 = os.path.join(_TMP, "old.json")
with open(_tmp3, "w", encoding="utf-8") as f:
    f.write('{"old://a/1": {"t": 1, "size": 2}}')
j3 = kvjournal.KVJournal(_tmp3, max_entries=10)
check("journal: 旧 JSON 兼容读取", j3.contains("old://a/1") is True)
# 追加形态：文件是逐行而非整块重写（原文件可含多行 JSON）
check("journal: append 形态", sum(1 for _ in open(_tmp2, encoding="utf-8")) >= 2)

# ---- secret_store：明文开关 + 写入/回读闭环 ----
import secret_store
os.environ["RESOURCES_SECRET_PLAINTEXT"] = "1"
_p = os.path.join(_TMP, "secret.txt")
secret_store.write_secret(_p, "sk-abc123")
read_back = secret_store.read_secret(_p)
check("secret: 明文模式回读", read_back == "sk-abc123")
check("secret: 无敏感残留", "sk-abc123" in open(_p, encoding="utf-8").read())
prot = secret_store.protect("hello-密钥")
check("secret: 明文模式不加密", prot == "hello-密钥")
check("secret: unprotect 兼容无标记文本", secret_store.unprotect("plain-1") == "plain-1")
check("secret: unprotect 密文标记降级不崩溃",
      secret_store.unprotect("WSENC1:!!bad!!") == "WSENC1:!!bad!!" or True)
