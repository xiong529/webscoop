"""快手适配器（kuaishou.com / gifshow.com）——实验性适配器。

页面空壳，数据走 ``/graphql`` 接口（浏览器内签名）。响应结构通常为
``data.data.allData[*].feed    ->  video.photo（含 photoUrl /
mainMvUrls）`` 或 ``vision/videoInfo`` 类详情结构，形态不固定，
所以 `extract` 做宽松搜索：找含 ``photoUrl`` / ``mainMvUrls`` 且带
``id`` 的节点。快手接口与页面结构改动频繁，命中率不做保证。
"""

from __future__ import annotations

from platform_adapters import PlatformAdapter


class KuaishouAdapter(PlatformAdapter):
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
