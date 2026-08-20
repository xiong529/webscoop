"""平台适配器单测：本地 fixture 喂 extract()，无需网络/浏览器，CI 可跑。

    python tests/unit_adapters.py
覆盖注册表自动发现 / match_page / api_filters / 四个适配器提取 /
extract_media_from_api 去重。fixture 为构造的接口 JSON 样例（形态贴近线上
aweme/graphql/note/playurl 结构，改动适配器解析逻辑时这里最先报警）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from platform_adapters import PLATFORM_ADAPTERS
from platform_adapters import (
    DouyinAdapter, KuaishouAdapter, XiaohongshuAdapter, BilibiliAdapter,
    api_filters_for, extract_media_from_api, page_adapter)

passed = 0


def check(name: str, cond: bool):
    global passed
    passed += 1
    print("PASS" if cond else "FAIL", name)
    assert cond, name


# ---------- 注册表 / 匹配 ----------
names = sorted(a.name for a in PLATFORM_ADAPTERS)
check("registry: 自动发现 4 平台", names == ["bilibili", "douyin", "kuaishou", "xiaohongshu"])
du = DouyinAdapter()
kua = KuaishouAdapter()
xhs = XiaohongshuAdapter()
bili = BilibiliAdapter()
check("match_page: douyin 详情页", du.match_page("https://www.douyin.com/video/123"))
check("match_page: douyin 短链 v.douyin.com", du.match_page("https://v.douyin.com/abc"))
check("match_page: B 站短链 b23.tv", bili.match_page("https://b23.tv/xxxx"))
check("match_page: 反例", du.match_page("https://example.com/x") is False)
check("page_adapter: 分发到快手", page_adapter("https://www.kuaishou.com/short-video/x").name == "kuaishou")
check("api_filters_for: douyin", "/aweme/v1/web/" in (api_filters_for("https://www.douyin.com/") or ()))
check("api_filters_for: 非平台页 None", api_filters_for("https://example.com/") is None)

# ---------- 抖音 ----------
aweme = {
    "aweme_id": "7300123456789012345",
    "desc": "测试视频",
    "video": {
        "cover": {"url_list": ["https://dummy.cover/1.jpg"]},
        "play_addr": {
            "uri": "v0200fg10000abc",
            "width": 1080, "height": 1920, "data_size": 1024000,
            "url_list": ["https://aweme.snssdk.com/play/abc.mp4?token=x"],
        },
    },
}
items = du.extract([{"aweme_list": [aweme]}])
check("douyin: 命中 1 视频", len(items) == 1 and items[0]["kind"] == "video")
check("douyin: 稳定 alt_url 端点", items[0]["alt_url"].startswith("https://www.douyin.com/aweme/v1/play/"))
check("douyin: formats 多清晰度候选", len(items[0]["formats"]) >= 1)
aweme_img = {"aweme_id": "2", "desc": "图文", "images": [
    {"url_list": ["https://xhs/img1.jpg"], "width": 800, "height": 600}]}
items = du.extract([aweme_img])
check("douyin: 图集命中", len(items) == 1 and items[0]["kind"] == "image")
check("douyin: 空接口返回空", du.extract([]) == [])

# ---------- 快手 ----------
feed = {"data": {"list": [{
    "id": "photo1", "caption": "快手段子",
    "mainMvUrls": [{"url": "https://v.kuaishou.com/a.mp4"}],
    "coverUrl": "https://c.kuaishou.com/c.jpg"}]}}
items = kua.extract([feed])
check("kuaishou: mainMvUrls 命中视频", len(items) == 1 and items[0]["kind"] == "video")
feed2 = {"id": "p2", "photoUrl": "https://v2.kuaishou.com/b.mp4"}
items = kua.extract([feed2])
check("kuaishou: photoUrl 兜底", len(items) == 1 and items[0]["url"] == "https://v2.kuaishou.com/b.mp4")

# ---------- 小红书 ----------
note = {"id": "note1", "title": "笔记",
        "imageList": [{"urlOrigin": "https://sns-webpic-qc.xhscdn.com/1.jpg"}],
        "video": {"cover": {"urlDefault": "https://sns-webpic-qc.xhscdn.com/c.jpg"},
                  "media": {"stream": {"h264": [
                      {"masterUrl": "https://v.xhscdn.com/n.mp4"}]}}}}
items = xhs.extract([note])
kinds = {i["kind"] for i in items}
check("xhs: 图文+视频各一", len(items) == 2 and kinds == {"image", "video"})

# ---------- B 站 ----------
api = {"data": {
    "bvid": "BV1GJ411x7h7", "title": "bilibili 官方",
    "pic": "https://i0.hdslb.com/bfs/archive/p.jpg",
    "dash": {"video": [{
        "id": 7, "baseUrl": "https://upos-sz-mirror.bilivideo.com/v.mp4",
        "width": 1920, "height": 1080, "bandwidth": 5000000}]}}}
items = bili.extract([api])
check("bilibili: DASH 命中视频", len(items) == 1 and items[0]["kind"] == "video")
check("bilibili: 标题进文件名", "bilibili" in items[0]["name"])
api2 = {"data": {"durl": [{"url": "https://upos-sz-mirror.bilivideo.com/d.flv", "size": 999}]}}
items = bili.extract([api2])
check("bilibili: durl 兜底 flv", len(items) == 1 and items[0]["ext"] == "flv")

# ---------- 路由：去重 + 格式择优 ----------
mixed = {"aweme_list": [aweme, aweme]}  # 同一作品重复出现
items = extract_media_from_api([mixed], url="https://www.douyin.com/user/1")
check("route: 去重后 1 条", len(items) == 1 and items[0]["kind"] == "video")


print(f"DONE  PASS={passed} FAIL=0")