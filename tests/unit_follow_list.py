"""定时跟进关注列表测试：增删 / 去重 / 持久化。

    python tests/unit_follow_list.py
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="ws_follow_")
os.environ["RESOURCES_FOLLOW_FILE"] = os.path.join(_TMP, "follow.json")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from follow_list import add, clear, items, remove, urls

clear()
passed = 0


def check(name: str, cond: bool):
    global passed
    passed += 1
    print("PASS" if cond else "FAIL", name)
    assert cond, name


check("follow: 初始为空", items() == [])
check("follow: 添加", add("https://www.douyin.com/user/a") is True)
check("follow: 去重", add("https://www.douyin.com/user/a") is False)
check("follow: 带备注添加", add("https://www.bilibili.com/video/BV1xx", "B站视频") is True)
check("follow: 列表顺序与字段",
      urls() == ["https://www.douyin.com/user/a",
                 "https://www.bilibili.com/video/BV1xx"]
      and items()[1]["name"] == "B站视频")
check("follow: 空 URL 拒绝", add("   ") is False)

# 持久化：清内存缓存后从文件重建
clear()
check("follow: 持久化重读", len(items()) == 2)

check("follow: 删除", remove("https://www.douyin.com/user/a") is True)
check("follow: 删除后仅剩 1", urls() == ["https://www.bilibili.com/video/BV1xx"])
check("follow: 重复删除 False", remove("https://www.douyin.com/user/a") is False)
check("follow: 文件存在", os.path.exists(os.environ["RESOURCES_FOLLOW_FILE"]))

print(f"DONE  PASS={passed} FAIL=0")