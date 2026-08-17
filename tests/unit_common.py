"""发现核心逻辑单测:视频择优 / 极小文件 / pexels 封面映射 / 高清变换 / og 提取 / 文件头。

独立进程运行:python tests/unit_common.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from discover_common import (
    extract_media_from_html,
    highres_url,
    is_tiny,
    looks_like_image,
    pexels_cover_to_video,
    pick_best_video,
    video_highres_url,
)
from gui_crawler import Resource

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name}: got={got!r} want={want!r}")


# --- pick_best_video 强模式 ---
# 评审原话场景：abc_2026.mp4 不能再被误判为 20×26 —— 单数字年份无配对，分数为 0
check("evaluator case: no false 20x26",
      pick_best_video(["abc_2026.mp4", "vid_1920_1080.mp4"]),
      "vid_1920_1080.mp4")
check("year-only name scores 0 (real pair wins)",
      pick_best_video(["a_2026.mp4", "b_1280_720.mp4"]),
      "b_1280_720.mp4")
check("year-pair loses to true 4k",
      pick_best_video(["a_2026_1080.mp4", "b_3840_2160.mp4"]),
      "b_3840_2160.mp4")
check("real 4k beats hd",
      pick_best_video(["a_3840_2160.mp4", "b_1280_720.mp4"]),
      "a_3840_2160.mp4")
check("WxH dash form",
      pick_best_video(["a-1920x1080.mp4", "b-1280x720.mp4"]),
      "a-1920x1080.mp4")
check("large variant wins on tie",
      pick_best_video(["a_1920_1080_large.mp4", "b_1920_1080_small.mp4"]),
      "a_1920_1080_large.mp4")
check("pexels style picks mid pair",
      pick_best_video(["16598475_1280_720_25fps.mp4", "16598472_1920_1080_25fps.mp4"]),
      "16598472_1920_1080_25fps.mp4")

# --- is_tiny URL heuristics (size=0) ---
check("tiny icon url filtered", is_tiny(Resource("https://x.com/icons/logo.svg")), True)
check("thumb dir filtered", is_tiny(Resource("https://x.com/thumbnails/small.png")), True)
check("blank.gif filtered", is_tiny(Resource("https://x.com/img/blank.gif")), True)
check("normal media not filtered", is_tiny(Resource("https://x.com/photos/pic-12345.jpeg")), False)
check("tiny by size still works", is_tiny(Resource("https://x.com/pic.jpeg", size=512)), True)

# --- pexels cover mapping ---
r = pexels_cover_to_video("https://images.pexels.com/videos/9341151/brazil-milky-way-9341151.jpeg?cs=tinysrgb&w=1200")
check("cover->video",
      r if r is not None else (),
      ("https://www.pexels.com/download/video/9341151/", "brazil-milky-way.mp4"))
check("photo not mapped",
      pexels_cover_to_video("https://images.pexels.com/photos/123/pexels-photo-123.jpeg"),
      None)

# --- highres ---
hu = highres_url("https://images.pexels.com/photos/123/pexels-photo-123.jpeg?w=500&h=300")
check("highres pexels photo",
      "dl=pexels-photo-123.jpg" in hu and "fm=jpg" in hu, True)
vu = video_highres_url("https://cdn.pixabay.com/video/x_tiny.mp4")
check("video highres pixabay", vu.endswith("_large.mp4"), True)

# --- extract_media_from_html og ---
html = """<html><head>
<meta property="og:title" content="My Video"/>
<meta property="og:image" content="https://img.example.com/photo/123/t.jpg?w=400"/>
<meta property="og:video" content="https://cdn.example.com/videos/9c4e/video.mp4"/>
</head><body><video src="/media/clip.mp4"></video></body></html>"""
m = extract_media_from_html(html, "https://example.com/video/9c4e/my-video")
check("og video extracted", m["video_url"], "https://cdn.example.com/videos/9c4e/video.mp4")
# 有视频时图片不提取（与原有语义一致：图片只在无视频时作为回退）
check("image skipped when video found", m["image_url"], "")
check("title extracted", m["title"], "My Video")

html2 = """<html><body><video poster="/p.jpg"><source src="/media/c.mp4"/></video></body></html>"""
m2 = extract_media_from_html(html2, "https://example.com/")
check("video tag source", m2["video_url"], "/media/c.mp4")

# --- B 站 __playinfo__ 内嵌 DASH 提取 ---
from discover_common import playinfo_video_url

play_html = """<html><head><script>window.__playinfo__={"code":0,"data":{"dash":{"video":[
  {"id":32,"height":480,"width":854,"baseUrl":"https://upos-sz.bilivideo.com/480p.m4s"},
  {"id":80,"height":1080,"width":1920,"baseUrl":"https://upos-sz.bilivideo.com/1080p.m4s"},
  {"id":112,"height":2160,"width":3840,"baseUrl":"https://upos-sz.bilivideo.com/4k.m4s"}]}}}
</script></head><body><div id="app"></div></body></html>"""
check("playinfo: 默认 cap 1080",
      playinfo_video_url(play_html), "https://upos-sz.bilivideo.com/1080p.m4s")
check("playinfo: 无 playinfo 返回空", playinfo_video_url("<html></html>"), "")
check("playinfo: 空串安全", playinfo_video_url(""), "")
m3 = extract_media_from_html(play_html, "https://www.bilibili.com/video/BV1xx")
check("extract: playinfo 兜底进 video_url",
      m3["video_url"], "https://upos-sz.bilivideo.com/1080p.m4s")

# durl 回退（无 DASH）
play_durl = """<script>window.__playinfo__={"data":{"durl":[{"url":"https://upos-sz.bilivideo.com/a.flv"}]}}</script>"""
check("playinfo: durl 回退",
      playinfo_video_url(play_durl), "https://upos-sz.bilivideo.com/a.flv")
check("playinfo: 坏 JSON 安全", playinfo_video_url("<script>window.__playinfo__={oops</script>"), "")

# --- looks_like_image avif ---
p = os.path.join(tempfile.gettempdir(), "t_avif_check.avif")
with open(p, "wb") as f:
    f.write(b"\x00\x00\x00\x1cftypavif" + b"\x00" * 40)
check("avif magic ok", looks_like_image(p), True)
os.remove(p)

# --- render_dest_template 文件名模板 ---
from discover_common import render_dest_template
r = Resource("https://img.example.com/path/photo-12345-1920.jpg",
             page_url="https://example.com/gallery/pegasus",
             title="Pegasus 壁纸", name="photo-12345.jpg", size=2048)
r.width, r.height = 1920, 1080
check("default template = category/name",
      render_dest_template(r), "images/photo-12345.jpg")
check("custom subdir with site",
      render_dest_template(r, "{site}/{title}/{name}"),
      "example.com/Pegasus 壁纸/photo-12345.jpg")
check("stem+ext split",
      render_dest_template(r, "{category}/{stem}_{width}x{height}{ext}"),
      "images/photo-12345_1920x1080.jpg")
check("kind token",
      render_dest_template(r, "{kind}/{name}"), "image/photo-12345.jpg")
check("ext empty when name has none",
      render_dest_template(Resource("https://x.com/dl?id=9", name="resource"),
                           "{category}/{name}"),
      "others/resource")
check("auto restore ext when dropped",
      render_dest_template(r, "{category}/{stem}"),
      "images/photo-12345.jpg")
check("traversal neutralized",
      render_dest_template(r, "{category}/../{name}"),
      "images/photo-12345.jpg")

# --- regex_sub 通用正则变换器（LLM 规则生成的安全出口） ---
from urllib.parse import urlparse
from discover_common import _apply_regex_sub
rp = urlparse("https://cdn.example.com/a/hello-768x432.jpg?q=1")
rp2 = _apply_regex_sub(rp, {"search": r"-\d{3,4}x\d{3,4}", "replace": ""})
check("regex_sub strip suffix", rp2.path, "/a/hello.jpg")
check("regex_sub keeps query", rp2.query, "q=1")
rp3 = _apply_regex_sub(rp, {"search": r"[", "replace": ""})
check("regex_sub bad regex passthrough", rp3.geturl(), rp.geturl())
rp4 = _apply_regex_sub(rp, {"search": r"zzz_marker", "replace": ""})
check("regex_sub no-match returns original", rp4.geturl(), rp.geturl())

print(f"DONE  PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)