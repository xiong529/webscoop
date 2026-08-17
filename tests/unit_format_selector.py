"""统一格式选择器测试：spec 解析 / 条件过滤 / best-worst / URL 择优兼容。

    python tests/unit_format_selector.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from format_selector import Format, pick_video_url, select_formats  # noqa: E402

passed = 0


def check(name: str, cond: bool):
    global passed
    passed += 1
    print("PASS" if cond else "FAIL", name)
    assert cond, name


fs = [
    Format(url="a.mp4", height=480, width=854, size=1000),
    Format(url="b.mp4", height=1080, width=1920, size=5000),
    Format(url="c.mp4", height=2160, width=3840, size=20000),
]

check("fs: best", select_formats(fs, "best").url == "c.mp4")
check("fs: worst", select_formats(fs, "worst").url == "a.mp4")
check("fs: cap 1080", select_formats(fs, "best[height<=1080]").url == "b.mp4")
check("fs: min 720", select_formats(fs, "best[height>=720]").url == "c.mp4")
check("fs: and conds",
      select_formats(fs, "best[height>=720,size<=10000]").url == "b.mp4")
check("fs: no match None", select_formats(fs, "best[height>=99999]") is None)
check("fs: empty None", select_formats([], "best") is None)
check("fs: bad spec None", select_formats(fs, "nonsense") is None)
check("fs: unknown field filtered",
      select_formats(fs, "best[fps>=60]") is None)

check("url: 1920x1080 > 1280x720",
      pick_video_url(["https://x.com/b-1280x720.mp4",
                      "https://x.com/a-1920x1080.mp4"])
      == "https://x.com/a-1920x1080.mp4")
check("url: 年份不误判",
      pick_video_url(["https://x.com/abc_2026.mp4"])
      == "https://x.com/abc_2026.mp4")
check("url: large 优先",
      pick_video_url(["https://x.com/v_720_small.mp4",
                      "https://x.com/v_720_large.mp4"])
      == "https://x.com/v_720_large.mp4")
check("url: cap 1080 过滤",
      pick_video_url(["https://x.com/a-2160x3840.mp4",
                      "https://x.com/b-1920x1080.mp4"], "best[height<=1080]")
      == "https://x.com/b-1920x1080.mp4")
check("url: empty returns empty", pick_video_url([]) == "")
check("url: 无分辨率退第一个",
      pick_video_url(["https://x.com/none.mp4",
                      "https://x.com/b-1280x720.mp4"])
      == "https://x.com/b-1280x720.mp4")

f = Format.from_url("https://cdn/x/photo_1920x1080_large.mp4")
check("from_url: height/width", f.width == 1920 and f.height == 1080)
check("from_url: label", f.label == "large")
g = Format.from_url("https://cdn/x/a_2026.mp4")
check("from_url: 年份不解析", g.height == 0 and g.width == 0)

print(f"DONE  PASS={passed} FAIL=0")
