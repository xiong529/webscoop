"""短视频「页面空壳 + 签名接口」平台适配器注册表（与传输层无关）。

背景：抖音/快手/小红书等平台页面 HTML 是 JS 空壳，真实视频/图片数据全部
来自带签名的内部接口（如抖音 ``aweme/v1/web/...``）。签名（a_bogus /
X-Bogus / xsec_token 等）由浏览器内 JS 自动计算，与其逆向签名算法不如
让浏览器把接口响应交给我们（见 renderer.render_page_api 的响应捕获）。

本模块 = 平台适配器注册表：

- 每个平台一个适配器类（MediaCrawler 也是这个组织方式：media_platform/
  下每平台一个目录），负责三件事：
  1. ``match_page(url)`` —— 页面域名是否属于本平台
  2. ``api_filters`` —— 捕获哪些接口 URL 特征（避免收集配置类 JSON 噪音）
  3. ``extract(apis)`` —— 把捕获的接口响应解析成 gallery 风格的资源条目
- 框架（gui_crawler.Discoverer / renderer）不感知平台细节，只按注册表分发：
  渲染 → 捕获接口 JSON → 适配器提取。
- 新增平台 = 注册新的适配器类，框架零改动。

本模块不得 import gui_crawler / gui_fetch / scrapy / renderer，仅依赖标准库。
"""

from __future__ import annotations

from urllib.parse import urlparse

# ================================================================
# 适配器基类与注册表
# ================================================================


class PlatformAdapter:
    """短视频平台适配器基类。"""

    #: 平台名（用于日志/标记，如 "douyin"）
    name: str = ""
    #: 页面域名列表（含子域名后缀，如 "douyin.com" 匹配 www.douyin.com）
    hosts: tuple[str, ...] = ()
    #: 接口 URL 特征子串：只有 URL 包含任一特征的 JSON 响应才会被捕获
    api_filters: tuple[str, ...] = ()
    #: 渲染捕获时自动滚动的次数（信息流/列表站触发懒加载；详情页设为 0）
    scroll_max: int = 0

    def match_page(self, url: str) -> bool:
        """页面 URL 是否属于本平台。"""
        if not url or not self.hosts:
            return False
        host = (urlparse(url).netloc or "").lower()
        for dom in self.hosts:
            if host == dom or host.endswith("." + dom):
                return True
        return False

    def extract(self, apis: list[dict], limit: int = 300) -> list[dict]:
        """从捕获的接口 JSON 里提取媒体资源条目（gallery 风格 dict）。

        条目字段：url / kind("image"|"video") / name / size / ext /
        page / preview / width / height / alt_url（稳定下载端点，可选）。
        返回空列表表示本平台未命中或无可提取资源。
        """
        return []


#: 已注册的平台适配器（新增平台 append 进来即可）
PLATFORM_ADAPTERS: list[PlatformAdapter] = []


def page_adapter(page_url: str) -> PlatformAdapter | None:
    """返回匹配页面 URL 的适配器；无命中返回 None。"""
    for ad in PLATFORM_ADAPTERS:
        if ad.match_page(page_url or ""):
            return ad
    return None


def api_filters_for(page_url: str) -> tuple[str, ...] | None:
    """页面 URL 对应适配器的接口捕获过滤特征；不适用时返回 None（不过滤）。"""
    ad = page_adapter(page_url)
    return ad.api_filters if ad and ad.api_filters else None


def extract_media_from_api(apis: list[dict], limit: int = 300,
                           url: str = "") -> list[dict]:
    """按页面 URL 选适配器提取资源；URL 未命中时遍历所有适配器合并（去重）。"""
    results: list[dict] = []
    seen: set[str] = set()
    chosen = page_adapter(url) if url else None
    adapters = [chosen] if chosen else list(PLATFORM_ADAPTERS)
    for ad in adapters:
        for it in ad.extract(apis, limit=limit):
            key = (it.get("url") or "").split("?")[0] + it.get("kind", "")
            if key in seen:
                continue
            seen.add(key)
            results.append(it)
            if len(results) >= limit:
                return results
    return results


# ================================================================
# 平台适配器实现
# ================================================================


class DouyinAdapter(PlatformAdapter):
    """抖音（douyin.com / iesdouyin.com）。

    页面空壳，数据接口在 ``aweme/v1/web/*``（现代浏览器内 JS 自动签
    a_bogus 等参数）。捕获到的响应里，带 ``aweme_id`` + ``video`` /
    ``images`` 的节点即一个作品条目，其中：

    - 视频：``video.play_addr_*``（按质量高低）或 ``video.play_addr`` 的
      url_list 第一条是签名直链；签名链会过期/403，因此再附一个
      ``aweme/v1/play/?video_id=`` 的稳定端点（已实测可下）放进 alt_url。
    - 图集：``images[*].url_list`` 第一条是原图。
    """

    name = "douyin"
    hosts = ("douyin.com", "iesdouyin.com")
    api_filters = ("/aweme/v1/web/",)
    scroll_max = 6

    #: aweme 详情里的视频直链字段（按质量从高到低）
    _PLAY_KEYS = ("play_addr_265", "play_addr_h264", "play_addr_h265", "play_addr")

    def _pick_play_url(self, video: dict) -> str:
        for key in self._PLAY_KEYS:
            pa = video.get(key) or {}
            lst = pa.get("url_list") or []
            for u in lst:
                if isinstance(u, str) and u.startswith("http"):
                    return u
        return ""

    def _pick_cover_url(self, video: dict) -> str:
        for key in ("cover", "origin_cover", "dynamic_cover", "gaussian_cover"):
            c = video.get(key) or {}
            lst = c.get("url_list") or []
            for u in lst:
                if isinstance(u, str) and u.startswith("http"):
                    return u
        return ""

    def _video_resources(self, aweme: dict) -> list[dict]:
        video = aweme.get("video") or {}
        play = self._pick_play_url(video)
        if not play:
            return []
        pa = video.get("play_addr") or {}
        uri = pa.get("uri") or ""
        # 签名直链会过期/403：无条件补一个「video_id + 官方端点」的稳定地址，
        # 下载时若签名链失败会自动用稳定地址重试（已实测 /aweme/v1/play/?video_id= 可下）。
        dl = (f"https://www.douyin.com/aweme/v1/play/?video_id={uri}&line=0&is_play_url=1"
              if uri else "")
        name = (aweme.get("desc") or aweme.get("caption") or "").strip()[:60] or (
            aweme.get("aweme_id") or "douyin-video")
        return [{
            "url": play,
            "kind": "video",
            "name": name,
            "size": pa.get("data_size") or None,
            "ext": "mp4",
            "page": play,
            "preview": self._pick_cover_url(video) or "",
            "width": pa.get("width") or video.get("width") or 0,
            "height": pa.get("height") or video.get("height") or 0,
            "alt_url": dl,
        }]

    def _image_resources(self, aweme: dict) -> list[dict]:
        out = []
        imgs = aweme.get("images") or []
        for img in imgs:
            lst = img.get("url_list") or []
            for i, u in enumerate(lst):
                if isinstance(u, str) and u.startswith("http"):
                    out.append({
                        "url": u,
                        "kind": "image",
                        "name": (aweme.get("desc") or aweme.get("caption") or "")
                                .strip()[:60]
                                or (aweme.get("aweme_id") or "douyin-image"),
                        "size": img.get("data_size") or None,
                        "ext": "jpg",
                        "page": u,
                        "preview": u,
                        "width": img.get("width") or 0,
                        "height": img.get("height") or 0,
                        "alt_url": "",
                    })
                    break  # url_list 里第一条通常是原图
        return out

    def extract(self, apis: list[dict], limit: int = 300) -> list[dict]:
        results: list[dict] = []
        seen: set[str] = set()
        acks: list[dict] = []

        def walk(o):
            if isinstance(o, dict):
                # 搜索/话题结果：真实作品被包在 aweme_info 节点下
                if "aweme_info" in o and isinstance(o["aweme_info"], dict):
                    walk(o["aweme_info"])
                    return
                if "aweme_id" in o and ("video" in o or "images" in o):
                    acks.append(o)
                    return
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        for ap in apis:
            walk(ap)

        for aw in acks:
            for it in self._video_resources(aw) + self._image_resources(aw):
                key = it["url"].split("?")[0] + it["name"]
                if key in seen:
                    continue
                seen.add(key)
                results.append(it)
                if len(results) >= limit:
                    return results
        return results


PLATFORM_ADAPTERS.append(DouyinAdapter())


class KuaishouAdapter(PlatformAdapter):
    """快手（kuaishou.com / gifshow.com）——实验性适配器。

    页面空壳，数据走 ``/graphql`` 接口（浏览器内签名）。响应结构通常为
    ``data.data.allData[*].feed    ->  video.photo（含 photoUrl /
    mainMvUrls）`` 或 ``vision/videoInfo`` 类详情结构，形态不固定，
    所以 `extract` 做宽松搜索：找含 ``photoUrl`` / ``mainMvUrls`` 且带
    ``id`` 的节点。快手接口与页面结构改动频繁，命中率不做保证。
    """

    name = "kuaishou"
    hosts = ("kuaishou.com", "gifshow.com")
    # graphql 是统一数据口；只匹配快手域的 graphql 避免撞上其他站
    api_filters = ("/graphql?", "/graphql")
    scroll_max = 4

    def _photo_resources(self, photo: dict) -> list[dict]:
        out = []
        # 主视频：mainMvUrls 是 [{url,...}] 候选流，photoUrl 是低清直链
        urls = [u.get("url") for u in (photo.get("mainMvUrls") or [])
                if isinstance(u, dict) and isinstance(u.get("url"), str)]
        urls = [u for u in urls if u.startswith("http")]
        if not urls and isinstance(photo.get("photoUrl"), str) \
                and photo["photoUrl"].startswith("http"):
            urls = [photo["photoUrl"]]
        pic = photo.get("coverUrl") or photo.get("poster")
        if urls:
            name = (photo.get("caption") or photo.get("photoId") or "kuaishou")
            vid = photo.get("photoId") or photo.get("id") or ""
            out.append({
                "url": urls[0],
                "kind": "video",
                "name": str(name)[:60],
                "ext": "mp4",
                "page": urls[0],
                "preview": pic if isinstance(pic, str) else "",
                "width": 0, "height": 0,
                "alt_url": f"https://www.kuaishou.com/short-video/{vid}" if vid else "",
            })
        # 图集
        for img in (photo.get("images") or []):
            u = img.get("url") if isinstance(img, dict) else None
            if isinstance(u, str) and u.startswith("http"):
                out.append({
                    "url": u, "kind": "image", "name": str(
                        photo.get("caption") or "kuaishou-img")[:60],
                    "ext": "jpg", "page": u, "preview": u,
                    "width": 0, "height": 0, "alt_url": "",
                })
        return out

    def extract(self, apis: list[dict], limit: int = 300) -> list[dict]:
        results: list[dict] = []
        seen: set[str] = set()
        acks: list[dict] = []

        def walk(o):
            if isinstance(o, dict):
                if "id" in o and ("mainMvUrls" in o or "photoUrl" in o):
                    acks.append(o)
                    return
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        for ap in apis:
            walk(ap)
        for ph in acks:
            for it in self._photo_resources(ph):
                key = it["url"].split("?")[0]
                if key in seen:
                    continue
                seen.add(key)
                results.append(it)
                if len(results) >= limit:
                    return results
        return results


PLATFORM_ADAPTERS.append(KuaishouAdapter())


class XiaohongshuAdapter(PlatformAdapter):
    """小红书（xiaohongshu.com）——实验性适配器。

    数据来自 ``edith.xiaohongshu.com/api/sns/web/*``（x-s / x-t 签名）。
    笔记结构：``note.imageList[*].urlDefault/urlOrigin``（图）、
    ``note.video.media.stream.h264[*].masterUrl``（视频）。页面/接口
    改动频繁且部分接口需登录，命中率不做保证。
    """

    name = "xiaohongshu"
    hosts = ("xiaohongshu.com",)
    api_filters = ("edith.xiaohongshu.com/api/sns/web/",)
    scroll_max = 4

    def _note_resources(self, note: dict) -> list[dict]:
        out = []
        nid = note.get("id") or note.get("noteId") or ""
        name = str(note.get("title") or nid or "xhs")[:60]
        v = note.get("video") or {}
        streams = []

        def collect_streams(obj):
            if isinstance(obj, dict):
                u = obj.get("masterUrl")
                if isinstance(u, str) and u.startswith("http"):
                    streams.append(u)
                    return
                for vv in obj.values():
                    collect_streams(vv)
            elif isinstance(obj, list):
                for vv in obj:
                    collect_streams(vv)

        # 递归收集所有 masterUrl（media.stream.h264[*].masterUrl 等形态）
        collect_streams(v)
        if streams:
            out.append({
                "url": streams[0], "kind": "video", "name": name,
                "ext": "mp4", "page": streams[0],
                "preview": (v.get("cover") or {}).get("urlDefault")
                if isinstance(v.get("cover"), dict) else "",
                "width": 0, "height": 0, "alt_url": "",
            })
        for img in (note.get("imageList") or []):
            if not isinstance(img, dict):
                continue
            u = img.get("urlOrigin") or img.get("urlDefault")
            if isinstance(u, str) and u.startswith("http"):
                out.append({
                    "url": u, "kind": "image", "name": name,
                    "ext": "jpg", "page": u, "preview": u,
                    "width": 0, "height": 0, "alt_url": "",
                })
        return out

    def extract(self, apis: list[dict], limit: int = 300) -> list[dict]:
        results: list[dict] = []
        seen: set[str] = set()
        acks: list[dict] = []

        def walk(o):
            if isinstance(o, dict):
                if "id" in o and ("imageList" in o or "video" in o):
                    acks.append(o)
                    return
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        for ap in apis:
            walk(ap)
        for note in acks:
            for it in self._note_resources(note):
                key = it["url"].split("?")[0] + it["kind"]
                if key in seen:
                    continue
                seen.add(key)
                results.append(it)
                if len(results) >= limit:
                    return results
        return results


PLATFORM_ADAPTERS.append(XiaohongshuAdapter())