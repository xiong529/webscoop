"""抖音适配器（douyin.com / iesdouyin.com）。

页面空壳，数据接口在 ``aweme/v1/web/*``（现代浏览器内 JS 自动签
a_bogus 等参数）。捕获到的响应里，带 ``aweme_id`` + ``video`` /
``images`` 的节点即一个作品条目，其中：

- 视频：``video.play_addr_*``（按质量高低）或 ``video.play_addr`` 的
  url_list 第一条是签名直链；签名链会过期/403，因此再附一个
  ``aweme/v1/play/?video_id=`` 的稳定端点（已实测可下）放进 alt_url。
- 图集：``images[*].url_list`` 第一条是原图。
"""

from __future__ import annotations

from platform_adapters import PlatformAdapter


class DouyinAdapter(PlatformAdapter):
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
            for _i, u in enumerate(lst):
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
