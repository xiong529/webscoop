"""热搜榜抓取（热点模式）。

目前支持 B 站热门排行：`x/web-interface/ranking/v2` 无需签名，GET 即得
``data.list[*]``（bvid / title / pic / owner.name / stat.view / duration）。

返回条目为「页面级」信息（bilibili 视频页 URL），真正的视频直链由主流程
对视频页走「渲染 + 接口捕获 + bilibili 适配器」提取（见 platform_adapters）。

仅标准库，无第三方依赖。
"""

from __future__ import annotations

import json
import urllib.request

_BILI_RANKING = "https://api.bilibili.com/x/web-interface/ranking/v2"

_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/126.0.0.0 Safari/537.36")


def bilibili_hot(endpoint: str = _BILI_RANKING, limit: int = 50,
                 timeout: float = 15.0) -> list[dict]:
    """抓取 B 站热门排行，返回条目列表（按榜单顺序，最多 limit 条）。

    每条：{"rank", "bvid", "title", "url", "preview", "author", "view", "duration"}。
    接口异常/解析失败抛 RuntimeError（调用方弹窗提示即可）。
    """
    req = urllib.request.Request(endpoint, headers={
        "User-Agent": _USER_AGENT,
        "Referer": "https://www.bilibili.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"热搜接口返回非 JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("code") != 0:
        raise RuntimeError(f"B站接口错误: code={data.get('code')} "
                           f"msg={data.get('message')}")
    out: list[dict] = []
    for it in ((data.get("data") or {}).get("list") or []):
        if not isinstance(it, dict):
            continue
        bvid = it.get("bvid") or ""
        if not bvid:
            continue
        owner = it.get("owner") or {}
        stat = it.get("stat") or {}
        out.append({
            "rank": len(out) + 1,
            "bvid": bvid,
            "title": (it.get("title") or "").strip()[:120],
            "url": f"https://www.bilibili.com/video/{bvid}",
            "preview": it.get("pic") or "",
            "author": owner.get("name") or "",
            "view": stat.get("view") or 0,
            "duration": it.get("duration") or 0,
        })
        if len(out) >= limit:
            break
    return out