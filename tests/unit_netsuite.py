"""新网络层功能测试：代理池 / Cookie 注入规则 / 平台适配器 / mkv 解析上限。

覆盖本轮改造：
1. utils.proxy ProxyPool：按站绑定、吊销冷却、失败计数、success 清零
2. utils.cookies：cookies.txt 规则解析、域名匹配优先级
3. platform_adapters：页面匹配、api_filters、douyin/kuaishou/xhs 提取（合成样本）
4. discover_common.mkv_dimensions：畸形头 64KB 上限防护不退化
5. stats：线程安全累计与 summary
"""

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources_reptile"))

import config  # noqa: E402

PASS = 0
FAIL = 0


def check(name, got, expected=True):
    global PASS, FAIL
    if isinstance(expected, bool) or expected is None:
        ok = (got is expected)
    else:
        ok = (got == expected)
    if ok:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name}: got={got!r} expected={expected!r}")


def expect_raises(name, fn):
    global PASS, FAIL
    try:
        fn()
        FAIL += 1
        print(f"FAIL {name}: no exception")
    except Exception:
        PASS += 1
        print(f"PASS {name}")


# ---- 1. 代理池 ----
from resources_reptile.utils.proxy import ProxyPool, current_pool, pool as _pool_singleton  # noqa: E402
import resources_reptile.utils.proxy as _pmod  # noqa: E402


def _test_proxy_pool():
    p = ProxyPool(proxies=["1.1.1.1:3128", "2.2.2.2:3128", "3.3.3.3:3128"])
    # 按站绑定：同一站点返回同一代理
    a1 = p.proxy("example.com")
    a2 = p.proxy("example.com")
    check("pool: same site same proxy (preferred)", a1 == a2)
    # 不同站点允许不同代理
    b1 = p.proxy("other.site")
    check("pool: other site serves proxy", b1 in ("1.1.1.1:3128", "2.2.2.2:3128", "3.3.3.3:3128"))
    # 吊销：连续 2 次失败才进入冷却，站点换新代理
    p.revoke(a1, "conn-fail")
    p.revoke(a1, "conn-fail")
    a3 = p.proxy("example.com")
    check("pool: revoked proxy not reselected", a3 != a1)
    check("pool: still serves", a3 is not None)
    # 2 次失败后永久拉黑
    p.revoke(a3, "conn-fail")
    p.revoke(a3, "conn-fail")
    a4 = p.proxy("example.com")
    check("pool: perma-banned after max_fails", a4 not in (a1, a3))
    # success 清零
    p2 = ProxyPool(proxies=["9.9.9.9:80"])
    p2._fails["9.9.9.9:80"] = 1
    p2.success("9.9.9.9:80")
    check("pool: success clears fail count", "9.9.9.9:80" not in p2._fails)
    # force：连接级强信号一次即进入冷却，且失败计数归零
    pf = ProxyPool(proxies=["7.7.7.7:80"])
    pf.revoke("7.7.7.7:80", "conn-fail", force=True)
    check("pool: force revoke immediate",
          pf._revoked_until.get("7.7.7.7:80", 0) > 0 and pf._fails.get("7.7.7.7:80") == 0)
    # 普通 revoke 仍走计数（force=False 默认）
    pn = ProxyPool(proxies=["8.8.8.8:80"])
    pn.revoke("8.8.8.8:80", "403")
    check("pool: plain revoke still counts", pn._fails.get("8.8.8.8:80") == 1
          and pn._revoked_until.get("8.8.8.8:80", 0) == 0)
    # 健康检测：探活失败立即吊销（force），探活成功保留
    orig_probe = _pmod._probe_one
    _pmod._probe_one = lambda p, u, t: p == "7.7.7.7:80"
    try:
        ph = ProxyPool(proxies=["7.7.7.7:80", "9.9.9.9:80"])
        res = ph.health_check(probe_url="http://probe.local/204", timeout=2)
        check("pool: health reports both",
              set(res) == {"7.7.7.7:80", "9.9.9.9:80"})
        check("pool: health dead revoked",
              res.get("9.9.9.9:80") is False
              and "9.9.9.9:80" not in ph._candidates_locked())
        check("pool: health alive kept", "7.7.7.7:80" in ph._candidates_locked())
    finally:
        _pmod._probe_one = orig_probe
    pe = ProxyPool(proxies=[])
    check("pool: health empty safe", pe.health_check() == {})
    # 空池（用空的缓存文件阻断项目 proxies.txt 的自动加载）
    with tempfile.TemporaryDirectory() as td:
        ef = os.path.join(td, "empty.txt")
        with open(ef, "w", encoding="utf-8") as fh:
            pass
        p3 = ProxyPool(proxies=[], cache_file=ef)
        p3.proxy("x.com")
        check("pool: empty pool returns None", p3.proxy("x.com") is None)
        # 缓存文件加载
        f = os.path.join(td, "proxies.txt")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("# comment\n5.5.5.5:8080\n6.6.6.6:8080\n")
        p4 = ProxyPool(cache_file=f)
        p4.proxy("example.net")
        check("pool: cache file loaded", p4._proxies == ["5.5.5.5:8080", "6.6.6.6:8080"])


_test_proxy_pool()

# ---- 2. Cookie 规则 ----
import resources_reptile.utils.cookies as ck  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    _OLD_FILE = config.COOKIE_FILE
    f = os.path.join(td, "cookies.txt")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("# comment line\n")
        fh.write("douyin.com:  sid=abc; ttwid=zzz\n")
        fh.write("iesdouyin.com: sid=ies\n")
        fh.write("no-domain-cookie=1\n")
    config.COOKIE_FILE = f
    ck._loaded = False  # 强制重载
    ck._rules = {}
    check("cookie: domain rule", ck.cookie_for("www.douyin.com") == "sid=abc; ttwid=zzz")
    check("cookie: exact host rule", ck.cookie_for("iesdouyin.com") == "sid=ies")
    check("cookie: geo-rule subdomain", ck.cookie_for("play.douyin.com") == "sid=abc; ttwid=zzz")
    check("cookie: global fallback", ck.cookie_for("unmatched.site") == "no-domain-cookie=1")
    check("cookie: header with prefix", ck.cookie_header_for("douyin.com")
          == "Cookie: sid=abc; ttwid=zzz")
    check("cookie: unmatched with explicit domain wins over global",
          ck.cookie_for("iesdouyin.com") == "sid=ies")
    config.COOKIE_FILE = _OLD_FILE

# ---- 3. 平台适配器 ----
from platform_adapters import (  # noqa: E402
    PLATFORM_ADAPTERS,
    BilibiliAdapter,
    DouyinAdapter,
    KuaishouAdapter,
    XiaohongshuAdapter,
    api_filters_for,
    extract_media_from_api,
    page_adapter,
)

check("adapter: registry has 3", len(PLATFORM_ADAPTERS) >= 3)
check("adapter: douyin page match",
      isinstance(page_adapter("https://www.douyin.com/user/xxx"), DouyinAdapter))
check("adapter: douyin short link match",
      isinstance(page_adapter("https://v.douyin.com/xxxx/"), DouyinAdapter))
check("adapter: iesdouyin share match",
      isinstance(page_adapter("https://www.iesdouyin.com/share/user/abc"), DouyinAdapter))
check("adapter: douyin short link filters",
      api_filters_for("https://v.douyin.com/xxxx/") == ("/aweme/v1/web/",))
check("adapter: kuaishou page match",
      isinstance(page_adapter("https://www.kuaishou.com/short-video/123"), KuaishouAdapter))
check("adapter: xhs page match",
      isinstance(page_adapter("https://www.xiaohongshu.com/explore/note"), XiaohongshuAdapter))
check("adapter: normal site no match",
      page_adapter("https://www.example.com/gallery") is None)
check("adapter: filters for douyin",
      api_filters_for("https://www.douyin.com/") == ("/aweme/v1/web/",))
check("adapter: no filters for normal site",
      api_filters_for("https://www.example.com/") is None)
check("adapter: douyin scroll_max > 0", DouyinAdapter.scroll_max > 0)

# douyin 提取（合成 aweme 响应）
sample_api = {
    "aweme_list": [
        {
            "aweme_id": "72123456789",
            "desc": "测试视频",
            "video": {
                "play_addr": {"uri": "v0200f100000abc", "width": 1920, "height": 1080,
                              "data_size": 1024},
                "play_addr_265": {"url_list": ["https://v.douyin.com/xxxx"],
                                  "width": 1920, "height": 1080},
                "cover": {"url_list": ["https://p.douyin.com/cover.jpg"]},
            },
        },
        {
            "aweme_id": "72123456790",
            "desc": "测试图集",
            "images": [{"url_list": ["https://p.douyin.com/img1.jpg"],
                        "width": 800, "height": 600}],
        },
        {"aweme_id": "7812", "desc": "无媒体节点"},  # 不命中
    ]
}
dy_items = extract_media_from_api([sample_api], url="https://www.douyin.com/user/xxx")
check("adapter: douyin extracts 2", len(dy_items) == 2)
kinds = {i["kind"] for i in dy_items}
check("adapter: kinds video+image", kinds == {"video", "image"})
vid = next(i for i in dy_items if i["kind"] == "video")
check("adapter: video alt_url stable endpoint",
      vid["alt_url"] == "https://www.douyin.com/aweme/v1/play/?video_id=v0200f100000abc&line=0&is_play_url=1")
check("adapter: video url from 265", vid["url"] == "https://v.douyin.com/xxxx")
check("adapter: video dims from play_addr", (vid["width"], vid["height"]) == (1920, 1080))
img = next(i for i in dy_items if i["kind"] == "image")
check("adapter: image url", img["url"] == "https://p.douyin.com/img1.jpg")

# 抖音搜索页响应：作品包在 aweme_info 下（walk 先解包再递归）
search_api = {"data": {"aweme_info": [sk for sk in sample_api["aweme_list"]]}}
dy_search = extract_media_from_api([search_api], url="https://www.douyin.com/search/x?type=video")
check("adapter: search aweme_info unwrapped", len(dy_search) == 2)
check("adapter: search video url",
      next(i for i in dy_search if i["kind"] == "video")["url"] == "https://v.douyin.com/xxxx")

# kuaishou 合成
ks_api = {"data": {"allData": [{"feed": [{
    "id": "3xyz", "photoId": "abc123", "caption": "快手视频",
    "mainMvUrls": [{"url": "https://v.kuaishou.com/ks.mp4"}],
    "coverUrl": "https://img.kuaishou.com/c.jpg",
}]}]}}
ks_items = extract_media_from_api([ks_api], url="https://www.kuaishou.com/short-video/abc123")
check("adapter: kuaishou extracts 1", len(ks_items) == 1)
check("adapter: kuaishou video url", ks_items[0]["url"] == "https://v.kuaishou.com/ks.mp4")
check("adapter: kuaishou alt_url share page",
      ks_items[0]["alt_url"] == "https://www.kuaishou.com/short-video/abc123")

# xhs 合成
xhs_api = {"data": {"notes": [{
    "id": "note1", "title": "小红书笔记",
    "imageList": [{"urlDefault": "https://sns-img.xhscdn.com/a.jpg",
                   "urlOrigin": "https://sns-img.xhscdn.com/a-origin.jpg"}],
    "video": {"media": {"stream": {"h264": [
        {"masterUrl": "https://sns-video.xhscdn.com/v.mp4"}]}},
        "cover": {"urlDefault": "https://sns-img.xhscdn.com/cover.jpg"}},
}]}}
xhs_items = extract_media_from_api([xhs_api], url="https://www.xiaohongshu.com/explore/note1")
check("adapter: xhs extracts 2 (video+image)", len(xhs_items) == 2)
xhs_v = next(i for i in xhs_items if i["kind"] == "video")
xhs_i = next(i for i in xhs_items if i["kind"] == "image")
check("adapter: xhs video masterUrl", xhs_v["url"] == "https://sns-video.xhscdn.com/v.mp4")
check("adapter: xhs image prefers urlOrigin",
      xhs_i["url"] == "https://sns-img.xhscdn.com/a-origin.jpg")

# 去重：同一视频在多个接口响应里只出现一次
dy2 = extract_media_from_api([sample_api, sample_api],
                             url="https://www.douyin.com/user/xxx")
check("adapter: dedupe across apis", len(dy2) == 2)

# bilibili：页面匹配 / 短链 / 过滤特征
check("adapter: bilibili page match",
      isinstance(page_adapter("https://www.bilibili.com/video/BV1xx411c7mD"),
                 BilibiliAdapter))
check("adapter: b23.tv short link match",
      isinstance(page_adapter("https://b23.tv/abc123"), BilibiliAdapter))
check("adapter: bilibili filters",
      api_filters_for("https://www.bilibili.com/video/BV1xx")
      == ("api.bilibili.com/",))

# bilibili DASH：元信息 + 三档视频流，默认 cap 1080
bili_api = {"code": 0, "data": {
    "bvid": "BV1xx411c7mD",
    "title": "测试视频",
    "pic": "https://i0.hdslb.com/bfs/archive/x.jpg",
    "dash": {"video": [
        {"id": 32, "height": 480, "width": 854,
         "baseUrl": "https://upos-sz.bilivideo.com/a/480p.mp4"},
        {"id": 80, "height": 1080, "width": 1920,
         "baseUrl": "https://upos-sz.bilivideo.com/a/1080p.mp4"},
        {"id": 112, "height": 2160, "width": 3840,
         "baseUrl": "https://upos-sz.bilivideo.com/a/4k.mp4"},
    ]},
}}
bili_items = extract_media_from_api(
    [bili_api], url="https://www.bilibili.com/video/BV1xx")
check("adapter: bilibili dash extracts 1", len(bili_items) == 1)
check("adapter: bilibili picks 1080p (cap)",
      "1080p" in bili_items[0]["url"] and bili_items[0]["height"] == 1080)
check("adapter: bilibili name from view", bili_items[0]["name"] == "测试视频")
check("adapter: bilibili preview pic",
      bili_items[0]["preview"] == "https://i0.hdslb.com/bfs/archive/x.jpg")

# bilibili durl 回退（无 DASH 时）
bili_durl = {"code": 0, "data": {"title": "t", "durl": [
    {"url": "https://upos-sz.bilivideo.com/d.flv", "size": 999}]}}
bili_d = extract_media_from_api(
    [bili_durl], url="https://www.bilibili.com/video/BV1xx")
check("adapter: bilibili durl fallback",
      len(bili_d) == 1 and bili_d[0]["size"] == 999)

# bilibili 空响应安全
check("adapter: bilibili empty safe",
      extract_media_from_api([{"code": 0, "data": {}}],
                             url="https://www.bilibili.com/video/BV1xx") == [])

# ---- 4. mkv 畸形头防护 ----
from discover_common import mkv_dimensions  # noqa: E402

ok_mkv = (b"\x1aS\xb6g"  # EBML
          + b"\x18S\x80g" + b"\xff" * 8  # 段头（unknown size）
          + b"\xAE\x86" + b"\xB0\x81\x08" + b"\xBA\x81\x06")  # TrackEntry(size 6): w=8 h=6
w, h = mkv_dimensions(ok_mkv)
check("mkv: normal header parse", (w, h) == (8, 6))

# 畸形：首字节 0xFF 后跟大量垃圾（自引用 vint），旧实现会 O(n^2) 退化
weird = bytearray(b"\x18S\x80g" + b"\xFF" + b"\x00" * 8192)
# 再追加一个正常段头保证能进入解析
weird = b"\x1aS\xb6g" + bytes(weird) + b"\xAE\x81\x08\xB0\x81\x08\xBA\x81\x06"
t0 = __import__("time").time()
res = mkv_dimensions(bytes(weird))
dt = __import__("time").time() - t0
check("mkv: malformed scan fast (<1s)", dt < 1.0)
# 巨大文件（>1MB）也只扫前 64KB：解析时间与全量无关
big = bytes(0x1A * 1024 * 1042)  # 乱码大文件
t1 = __import__("time").time()
res2 = mkv_dimensions(b"\x1aS\xb6g" + b"\x18S\x80g" + big + b"\xAE\x81\x08\xB0\x81\x08\xBA\x81\x06")
dt2 = __import__("time").time() - t1
check("mkv: big file capped fast (<1s)", dt2 < 1.0)
check("mkv: malformed w/h not found", res2 is None)

# ---- 5. stats ----
from stats import get_stats, bucket_for_reason  # noqa: E402

st = get_stats()
st.reset()
def _bump():
    for _ in range(50):
        st.add_category("videos", 1)
        st.add_downloaded(1, 1_000_000)
        st.add_failed(1)
ts = [threading.Thread(target=_bump) for _ in range(4)]
for t in ts:
    t.start()
for t in ts:
    t.join()
check("stats: threadsafe totals", st.total_resources == 200)
check("stats: threadsafe downloaded", st.downloaded == 200)
check("stats: threadsafe failed", st.failed == 200)
check("stats: bytes", st.download_bytes == 200_000_000)
s = st.summary()
check("stats: summary mentions pages", "耗时" in s)
st.add_page(3)
s2 = st.summary()
check("stats: summary has page count", "页面 3" in s2)

# 失败原因分布：403/超时归桶，未知归 other，站点分布
st.reset()
st.add_failed(1, reason="403 Forbidden", host="a.com")
st.add_failed(2, reason="连接超时 timed out", host="b.com")
st.add_failed(1, reason="weird-err", host="a.com")
snap = st.snapshot()
check("stats: fail reason 403 bucket", snap["fail_reason"].get("403") == 1)
check("stats: fail reason timeout bucket", snap["fail_reason"].get("timeout") == 2)
check("stats: fail reason other bucket", snap["fail_reason"].get("other") == 1)
check("stats: fail host a.com", snap["fail_host"].get("a.com") == 2)
check("stats: fail host b.com", snap["fail_host"].get("b.com") == 2)
check("stats: bucket 5xx (500)", bucket_for_reason("HTTPError 500") == "5xx")
check("stats: bucket 429", bucket_for_reason("429 Too Many") == "429")

# 落盘：临时目录写 stats.json，读回内容一致且含 duration
with tempfile.TemporaryDirectory() as td:
    st.reset()
    st.add_page(7)
    st.add_category("videos", 3)
    st.add_downloaded(2, 5_000_000)
    st.add_failed(1, reason="403 Forbidden", host="x.example")
    st.mark_finish()
    saved = st.save_json(td) or ""
    import json
    check("stats: save_json returns path",
          saved == os.path.join(td, "stats.json") and os.path.exists(saved))
    with open(saved, encoding="utf-8") as fh:
        data = json.load(fh)
    check("stats: json pages", data["pages"] == 7)
    check("stats: json totals", data["totals"].get("videos") == 3)
    check("stats: json downloaded", data["downloaded"] == 2)
    check("stats: json fail_reason", data["fail_reason"] == {"403": 1})
    check("stats: json fail_host", data["fail_host"] == {"x.example": 1})
    check("stats: json duration present", isinstance(data.get("duration"), (int, float)))
    check("stats: json finish present", data["finish"] is not None)

# ---- 6. cookie_capture（假浏览器注入，无需 Playwright） ----
from cookie_capture import CookieCaptureSession  # noqa: E402

class _FakeCtx:
    def cookies(self):
        return [
            {"name": "sessionid", "value": "abc123", "domain": ".douyin.com"},
            {"name": "ttwid", "value": "xyz", "domain": "www.douyin.com"},
            {"name": "sid", "value": "q1", "domain": ".www.bilibili.com"},
            {"name": "", "value": "empty-name"},   # 空 name 应被过滤
            {"name": "spy", "value": "x", "domain": ""},  # 空域名应被过滤
        ]

class _FakeBrowser:
    def is_connected(self):
        return True
    def close(self):
        pass

cc = CookieCaptureSession()
cc._context = _FakeCtx()
cc._browser = _FakeBrowser()
cc._page = None
snap = cc.snapshot()
check("capture: groups by base domain", set(snap) == {"douyin.com", "bilibili.com"})
check("capture: www merged into base", "ttwid=xyz" in snap["douyin.com"])
check("capture: dot-domain merged", "sessionid=abc123" in snap["douyin.com"])
check("capture: no session leakage", "spy" not in {v for vs in snap.values() for v in vs})
rows = cc.readable_candidates()
check("capture: candidates host prefixed",
      any(r.startswith("douyin.com:") for r in rows))
with tempfile.TemporaryDirectory() as td:
    f = os.path.join(td, "cookies.txt")
    n, msg = cc.save_to_file(f)
    check("capture: saved 2 domains", n == 2)
    with open(f, encoding="utf-8") as fh:
        content = fh.read()
    check("capture: cookie file has douyin", "douyin.com:  sessionid=abc123; ttwid=xyz" in content)
    check("capture: cookie file has bilibili", "bilibili.com:  sid=q1" in content)
    # save_to_file 需要先有快照；空快照返回 0
    cc2 = CookieCaptureSession()
    cc2._context = _FakeCtx() if False else None
    cc2._browser = None
    n2, msg2 = cc2.save_to_file(f)
    check("capture: empty snapshot refuses save", n2 == 0 and len(msg2) > 0)

print(f"DONE  PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)