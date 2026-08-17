"""B 站适配器（bilibili.com / b23.tv）——完整级适配器。

B 站接口开放度高（无强制 JS 签名），视频页数据来源：

- 接口响应（渲染捕获）：``x/web-interface/view``（元信息：title / pic /
  bvid）与 ``x/player/*/playurl``（播放流：data.dash.video[*] 或
  data.durl[*]，b23.tv 短链渲染后同样落到这些接口）。
- 图集/动态暂不提取；多 P 视频取第一个 DASH 流。

extract 宽松遍历捕获的 JSON：

- 含 ``dash.video`` 的节点 → DASH 视频流，用统一格式选择器挑最高清
  （默认 best[height<=1080]，qn 高未必 height 高，以元数据为准）。
- 含 ``durl`` 的节点 → 单文件流（flv/mp4，带 size），durl[0] 为主流。
- 含 ``bvid/aid`` + ``title`` 的节点 → 标题/封面（用于命名与预览）。
"""

from __future__ import annotations

from platform_adapters import PlatformAdapter


class BilibiliAdapter(PlatformAdapter):
    name = "bilibili"
    hosts = ("bilibili.com", "b23.tv")
    api_filters = ("api.bilibili.com/",)
    scroll_max = 4

    def _dash_resources(self, dash: dict, title: str, pic: str) -> list[dict]:
        from format_selector import Format, select_formats
        pairs: list[tuple[dict, Format]] = []
        for v in (dash.get("video") or []):
            if not isinstance(v, dict):
                continue
            u = v.get("baseUrl") or v.get("base_url") or ""
            if not (isinstance(u, str) and u.startswith("http")):
                continue
            pairs.append((v, Format(
                url=u,
                height=v.get("height") or 0,
                width=v.get("width") or 0,
                size=v.get("bandwidth") or 0,
                label=f"qn{v.get('id')}",
            )))
        if not pairs:
            return []
        formats = [f for _, f in pairs]
        picked = (select_formats(formats, "best[height<=1080]")
                  or select_formats(formats, "best"))
        if not picked:
            return []
        v = next(v for v, f in pairs if f is picked)
        return [{
            "url": picked.url,
            "kind": "video",
            "name": title or f"bilibili-{v.get('id')}" or "bilibili",
            "size": v.get("size") or None,
            "ext": "mp4",
            "page": picked.url,
            "preview": pic or "",
            "width": picked.width,
            "height": picked.height,
            "alt_url": "",
        }]

    def _durl_resources(self, durls: list, title: str, pic: str) -> list[dict]:
        for d in durls:
            if not isinstance(d, dict):
                continue
            u = d.get("url") or ""
            if not (isinstance(u, str) and u.startswith("http")):
                continue
            return [{
                "url": u, "kind": "video",
                "name": title or "bilibili",
                "size": d.get("size") or None,
                "ext": "flv", "page": u, "preview": pic or "",
                "width": 0, "height": 0, "alt_url": "",
            }]
        return []

    def extract(self, apis: list[dict], limit: int = 300) -> list[dict]:
        results: list[dict] = []
        seen: set[str] = set()
        title = pic = ""
        dashes: list[dict] = []
        durls: list[list] = []

        def walk(o):
            nonlocal title, pic
            if isinstance(o, dict):
                dash = o.get("dash")
                if isinstance(dash, dict) and dash.get("video"):
                    dashes.append(dash)
                durl = o.get("durl")
                if isinstance(durl, list) and durl:
                    durls.append(durl)
                t = o.get("title")
                if ("bvid" in o or "aid" in o) and isinstance(t, str) and t:
                    title = title or t[:60]
                    p = o.get("pic") or o.get("cover") or ""
                    if isinstance(p, str) and p.startswith("http"):
                        pic = pic or p
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        for ap in apis:
            walk(ap)

        for dash in dashes:
            for it in self._dash_resources(dash, title, pic):
                key = it["url"].split("?")[0]
                if key in seen:
                    continue
                seen.add(key)
                results.append(it)
                if len(results) >= limit:
                    return results
        for dl in durls:
            for it in self._durl_resources(dl, title, pic):
                key = it["url"].split("?")[0]
                if key in seen:
                    continue
                seen.add(key)
                results.append(it)
                if len(results) >= limit:
                    return results
        return results
