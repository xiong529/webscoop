"""小红书适配器（xiaohongshu.com）——实验性适配器。

数据来自 ``edith.xiaohongshu.com/api/sns/web/*``（x-s / x-t 签名）。
笔记结构：``note.imageList[*].urlDefault/urlOrigin``（图）、
``note.video.media.stream.h264[*].masterUrl``（视频）。页面/接口
改动频繁且部分接口需登录，命中率不做保证。
"""

from __future__ import annotations

from platform_adapters import PlatformAdapter


class XiaohongshuAdapter(PlatformAdapter):
    name = "xiaohongshu"
    hosts = ("xiaohongshu.com",)
    path_regex = (r"^/(explore|user|search_result|discovery)/",)
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
