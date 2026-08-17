"""GUI 与 Scrapy 共享的资源发现核心逻辑（与传输层无关）。

这里只放「纯逻辑」：URL 分类 / 高清变换 / 封面映射 / 文件头识别 /
详情页媒体提取等。GUI 的 Discoverer 与命令行 Scrapy spider 都复用本模块，
避免两套发现逻辑各写一份（历史问题：spider 端曾缺少高清规则、详情页跟进、
og:image 提取，均源于此）。

本模块不得 import gui_crawler / gui_fetch / scrapy，仅依赖 config 与标准库。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import config
from resources_reptile.pipelines import (  # noqa: F401  再导出给 gui_crawler 兼容导入
    EXTENSION_CATEGORY, classify_url)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# 无扩展名的下载端点特征（命中视为资源候选，下载时按 Content-Type 归类）
DOWNLOAD_ENDPOINT_HINTS = ("/download", "/attachment", "/dl/", "/getfile", "/raw/")
QUERY_DOWNLOAD_KEYS = {"download", "file", "attachment", "resource"}


def is_download_endpoint(url: str) -> bool:
    """无扩展名 URL 是否为下载端点（/download?id=、/dl/xxx 等）。

    命中则视为资源候选：由管道/探测按响应 Content-Type 归类（MIME 嗅探），
    避免只认扩展名漏掉 `/download?id=xxx` 这类资源。
    """
    parsed = urlparse(url)
    path = parsed.path.lower()
    if any(h in path for h in DOWNLOAD_ENDPOINT_HINTS):
        return True
    query_keys = {k.lower() for k, _v in parse_qs(parsed.query).items()}
    return bool(query_keys & QUERY_DOWNLOAD_KEYS)

# ================================================================
# 文件名工具
# ================================================================

def sanitize_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = unquote(name).strip(" .")
    return name or "resource"


def basename_from_url(url: str) -> str:
    """从 URL 提取文件名；优先取 query 中的 filename/name 参数。"""
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    for key in ("filename", "name", "file", "download"):
        if key in q and q[key] and q[key][0].strip():
            return sanitize_name(q[key][0])
    raw = os.path.basename(parsed.path)
    return sanitize_name(unquote(raw)) or "resource"


def safe_filename(url: str, name: str, suffix_len: int = 8) -> str:
    """避免同名冲突：目标文件已存在时，在文件名前加 URL 短哈希。"""
    directory, base = os.path.split(os.path.normpath(name))
    if not os.path.exists(name) and "." in base:
        return name
    h = hashlib.md5(url.encode()).hexdigest()[:suffix_len]
    stem, dot, ext = base.rpartition(".")
    if not dot:
        return os.path.join(directory, f"{h}_{base}")
    return os.path.join(directory, f"{h}_{stem}.{ext}")


_EXT_SPLIT_RE = re.compile(r"\.[a-z0-9]{2,5}$", re.IGNORECASE)


def render_dest_template(res, template: str = "") -> str:
    """把「文件名模板」渲染为目标相对路径（/ 分隔子目录）。

    支持 token：
      {category}   分类目录 images/videos/audios/docs/software/archives/others
      {kind}       展示类型 image / video / file
      {name}       资源原名（含扩展名则原样保留）
      {stem}       原名去掉扩展名后的主体
      {ext}        原名扩展名（含点，如 .jpg；原名无扩展名则为空）
      {site}       站点域名（页面主机名，去 www.）
      {title}      页面标题（无则回退站点域名）
      {size}       文件大小（字节）
      {width}x{height}  分辨率（如 1920x1080；未知为空）

    渲染后的各路径段都经过 sanitize_name，无法穿越目录、包含非法字符。
    若渲染结果无扩展名而原名带扩展名，自动补回扩展名，避免下载后无法识别类型。
    """
    template = (template or config.FILENAME_TEMPLATE or "{category}/{name}").strip()
    url = getattr(res, "url", "") or ""
    page_url = getattr(res, "page_url", "") or ""
    name = getattr(res, "name", "") or basename_from_url(url)

    idx = name.rfind(".")
    if 0 < idx < len(name) - 1 and _EXT_SPLIT_RE.search(name[idx:]):
        stem, ext = name[:idx], name[idx:]
    else:
        stem, ext = name, ""

    host = urlparse(page_url or url).netloc or ""
    site = re.sub(r"^www\.", "", host.lower())
    title = getattr(res, "title", "") or site or "page"
    w, h = getattr(res, "width", 0) or 0, getattr(res, "height", 0) or 0
    res_txt = f"{w}x{h}" if w and h else ""

    tokens = {
        "{category}": getattr(res, "category", "") or "others",
        "{kind}": getattr(res, "kind", "") or "file",
        "{name}": name,
        "{stem}": stem,
        "{ext}": ext,
        "{site}": site,
        "{title}": title,
        "{size}": str(getattr(res, "size", 0) or 0),
        "{width}x{height}": res_txt,
    }
    rendered = template
    for key, val in tokens.items():
        rendered = rendered.replace(key, val)
    parts = [p for p in rendered.split("/") if p.strip(" .")]
    parts = [sanitize_name(p) for p in parts]
    rel = "/".join(parts)
    if not rel:
        rel = f"{tokens['{category}']}/{tokens['{name}']}"
    if ext and "." not in os.path.basename(rel):
        rel = rel + ext
    return rel


def kind_of_url(url: str) -> str:
    """按分类表把 URL 映射为下载语义的 kind：image / video / file。"""
    cat = classify_url(url)
    if cat == "images":
        return "image"
    if cat == "videos":
        return "video"
    return "file"


# ================================================================
# 高清规则引擎（原 highres_rules.py，规则表驱动，见 highres_rules.json）
# 规则结构：
#   { "site": "说明", "enabled": true, "kind": "image|video|any",
#     "match": "URL 正则(可选)", "path_pattern": "路径正则(可选)",
#     "transform": "变换器名称" }
# 变换器按名称注册，安全、无 eval。首个命中并产生变化的变换器生效。
# ================================================================

RULES_FILE = os.environ.get("RESOURCES_HIGHRES_RULES",
                            os.path.join(PROJECT_DIR, "highres_rules.json"))


def _strip_size_suffix(path: str) -> str:
    """去掉 WordPress/WP 主题尺寸后缀：`foo-768x432.jpg` -> `foo.jpg`。"""
    m = re.search(
        r"^(.*)-(\d{2,4})x(\d{2,4})\.(jpg|jpeg|png|webp|gif|avif)$",
        path, re.IGNORECASE,
    )
    if m:
        return m.group(1) + "." + m.group(4)
    return path


def _pexels_dl(parsed) -> str:
    """pexels 原图直链：`photos/<id>/pexels-photo-<id>.jpeg?dl=...&fm=jpg`。"""
    pid = ""
    m = re.search(r"^/photos/(\d+)/", parsed.path)
    if m:
        pid = m.group(1)
    if not pid:
        return parsed
    return urlparse(
        f"https://images.pexels.com/photos/{pid}/pexels-photo-{pid}.jpeg"
        f"?cs=srgb&dl=pexels-photo-{pid}.jpg&fm=jpg"
    )


def _pixabay_1280(parsed) -> str:
    """pixabay 图片：`_640.jpg` -> `_1280.jpg`（高清变体）。"""
    new_path = re.sub(
        r"_(\d+)\.(jpg|jpeg|png|webp)$",
        lambda mm: f"_1280.{mm.group(2)}",
        parsed.path, flags=re.IGNORECASE,
    )
    return parsed._replace(path=new_path)


def _pixabay_video_large(parsed) -> str:
    """pixabay 视频：`_W_H_xxx.mp4` 升级为 `_large.mp4` 变体。"""
    for name in ("_tiny", "_small", "_medium", "_720", "_850"):
        if name in parsed.path:
            return parsed._replace(path=parsed.path.replace(name, "_large", 1))
    return parsed


def _bump_w_h_params(parsed, min_side: int) -> str:
    """通用：把 w/h 尺寸参数调大（值 <=500 时升到 min_side）。"""
    q = parse_qs(parsed.query)
    changed = False
    for key in ("w", "width", "h", "height"):
        if key in q:
            try:
                v = int(q[key][0])
            except (ValueError, TypeError):
                continue
            if 0 < v < min_side:
                q[key] = [str(min_side)]
                changed = True
    if not changed:
        return parsed
    return parsed._replace(query=urlencode(q, doseq=True))


def _apply_regex_sub(parsed, rule: dict):
    """通用正则路径替换：把 rule.search 替换为 rule.replace。

    供 LLM 生成规则时使用（transform="regex_sub"，字段 search/replace），
    与 strip_size_suffix 等价但自由度高，且 regex.sub 无 eval 风险。
    正则不合法或替换无变化时返回原 parsed。
    """
    search = rule.get("search", "")
    replace = rule.get("replace", "")
    if not search:
        return parsed
    try:
        new_path = re.sub(search, replace, parsed.path)
    except (re.error, TypeError):
        return parsed
    if new_path == parsed.path:
        return parsed
    return parsed._replace(path=new_path)


TRANSFORMS = {
    "strip_size_suffix": lambda parsed, min_side: parsed._replace(
        path=_strip_size_suffix(parsed.path)),
    "pexels_dl": lambda parsed, min_side: _pexels_dl(parsed),
    "pixabay_1280": lambda parsed, min_side: _pixabay_1280(parsed),
    "pixabay_video_large": lambda parsed, min_side: _pixabay_video_large(parsed),
    "bump_w_h_params": lambda parsed, min_side: _bump_w_h_params(parsed, min_side),
    # 通用正则路径替换：需规则另带 search / replace 字段。
    # 由 apply_rules 调用（这里保留占位 lambda，见 apply_rules 特判）。
    "regex_sub": lambda parsed, min_side: parsed,
}

_rules_cache: list[dict] | None = None


def load_rules() -> list[dict]:
    """加载规则表（带缓存；RESOURCES_HIGHRES_RULES 指向的 JSON 文件）。"""
    global _rules_cache
    if _rules_cache is not None:
        return _rules_cache
    try:
        with open(RULES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _rules_cache = [r for r in data.get("rules", []) if r.get("enabled")]
    except Exception:
        _rules_cache = []
    return _rules_cache


def reload_rules():
    """丢弃规则缓存，下次 load_rules 重新读文件（llm_rules 合并新规则后调用）。"""
    global _rules_cache
    _rules_cache = None


def apply_rules(url: str, kind: str = "image", min_side: int = 1200) -> str:
    """按规则表把 URL 变换为高清地址。无规则命中时原样返回。"""
    if not url or not url.startswith(("http://", "https://")):
        return url
    for rule in load_rules():
        if not rule.get("enabled"):
            continue
        rk = rule.get("kind", "any")
        if rk not in ("any", kind):
            continue
        m = rule.get("match")
        if m and not re.search(m, url):
            continue
        pp = rule.get("path_pattern")
        if pp and not re.search(pp, urlparse(url).path):
            continue
        try:
            parsed = urlparse(url)
            if rule.get("transform") == "regex_sub":
                new_parsed = _apply_regex_sub(parsed, rule)
            else:
                fn = TRANSFORMS.get(rule.get("transform", ""))
                if fn is None:
                    continue
                new_parsed = fn(parsed, min_side)
        except Exception:
            continue
        if new_parsed.geturl() != url:
            return new_parsed.geturl()
    return url


def highres_url(url: str, min_side: int = 1200) -> str:
    """把图片 URL 变换为高清地址。无规则命中时原样返回。"""
    return apply_rules(url, kind="image", min_side=min_side)


def video_highres_url(url: str) -> str:
    """把视频 URL 升级为更大变体（如 pixabay `_tiny.mp4` -> `_large.mp4`）。"""
    return apply_rules(url, kind="video", min_side=0)


# ================================================================
# 资源过滤
# ================================================================

def is_icon_url(url: str) -> bool:
    """判断 URL 是否为常见网站图标/附属小资源。"""
    if not config.FILTER_ICONS:
        return False
    path = urlparse(url).path.lower()
    last = path.rsplit("/", 1)[-1]
    if last.endswith(".ico"):
        return True
    return any(p in last or p in path for p in config.ICON_NAME_PATTERNS)


def is_tiny(res, min_size: int | None = None) -> bool:
    """体积过小的资源（几十 B~几百 B 的图标/占位图）。

    正常判定：0 < size < limit。当 size 未知（HEAD 探测失败保持 0）时，
    按 URL 特征兜底（/icon/、/thumb/、placeholder 等目录或文件名），
    避免极小图标在探测失败时漏网。
    """
    limit = config.MIN_RESOURCE_SIZE if min_size is None else min_size
    if limit <= 0:
        return False
    if res.size > 0:
        return res.size < limit
    path = urlparse(res.url).path.lower()
    last = path.rsplit("/", 1)[-1]
    if any(t in path for t in ("/icon", "/thumb", "/spacer", "/placeholder",
                               "/pixel", "/blank", "/loading", "/favicon")):
        return True
    if any(last == n or last.startswith(n + ".")
           for n in ("spacer", "blank", "pixel", "placeholder",
                     "favicon", "transparent", "loading", "dot")):
        return True
    return False


# ================================================================
# 文件头识别（防坏文件落盘 + 分辨率解析）
# ================================================================

def looks_like_image(path: str) -> bool:
    """校验图片文件的二进制 magic bytes，防止坏文件/非图片落盘。"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return False
    return (
        head[:3] == b"\xff\xd8\xff"                       # JPEG
        or head[:8] == b"\x89PNG\r\n\x1a\n"              # PNG
        or head[:6] in (b"GIF87a", b"GIF89a")            # GIF
        or head[:4] == b"RIFF" and head[8:12] == b"WEBP"  # WebP
        or head[:2] == b"BM"                              # BMP
        or head[:6] in (b"II*\x00", b"MM\x00*")          # TIFF
        or head[4:8] == b"ftyp" and head[8:12] in (
            b"avif", b"avis", b"av01", b"heic", b"heix", b"hevc", b"mif1")  # AVIF/HEIF
    )


def mp4_dimensions(data: bytes):
    """从 MP4/MOV 文件头解析 (width, height)；moov 在头部（faststart）才可解析。"""
    n = len(data)

    def boxes(buf: bytes, start: int, end: int):
        p = start
        while p + 8 <= end:
            size = int.from_bytes(buf[p:p + 4], "big")
            typ = buf[p + 4:p + 8]
            if size == 1 and p + 16 <= end:
                size = int.from_bytes(buf[p + 8:p + 16], "big")
                hdr = 16
            elif size == 0:
                size = end - p
                hdr = 8
            else:
                hdr = 8
            if size < hdr or p + size > end:
                break
            yield typ, p + hdr, p + size
            p += size

    for typ, bstart, bend in boxes(data, 0, n):
        if typ == b"moov":
            for t2, b2, e2 in boxes(data, bstart, bend):
                if t2 == b"trak":
                    for t3, b3, e3 in boxes(data, b2, e2):
                        if t3 == b"tkhd" and e3 - b3 >= 84:
                            ver = data[b3]
                            w_off = b3 + (76 if ver == 0 else 88)
                            if w_off + 8 <= e3:
                                w = int.from_bytes(data[w_off:w_off + 4], "big") >> 16
                                h = int.from_bytes(data[w_off + 4:w_off + 8], "big") >> 16
                                if w > 0 and h > 0:
                                    return w, h
    return None


def mkv_dimensions(data: bytes):
    """从 MKV/WebM 文件头解析 (width, height)。取第一个 TrackEntry 的像素尺寸。

    防御：对畸形文件（超长/自引用 vint size）有上限保护——解析窗口只扫
    MAX_MKV_SCAN 字节，且 vint size 超过窗口即放弃，避免退化 O(n^2)。
    """
    n = len(data)
    # 解析上限：EBML/段头 + 首个 TrackEntry 足够窄，超过即放弃
    MAX_MKV_SCAN = 64 * 1024

    def vint_size(buf: bytes, p: int) -> tuple:
        """EBML varint 长度字段：(vint 字节数, 值, vint 后位置)。

        标准编码：首字节高位的首个 1 位决定长度（0x80→1 字节、0x40→2 字节…），
        值 = 首字节去掉长度标记位后的低 7 位 + 后续字节。0xFF 为 unknown length。
        解析失败（越界/首位全 0）返回 (1, 0, p+1)，保证至少前进。
        """
        if p >= n:
            return 1, 0, p + 1
        first = buf[p]
        if first == 0xFF:
            return 1, 1, p + 1  # unknown length（跳过）
        length = 1
        for i in range(8):
            if first & (0x80 >> i):
                length = i + 1
                break
        else:
            return 1, 0, p + 1  # 首位全 0：非法 vint
        if p + length > n:
            return 1, 0, p + 1
        val = first & ((1 << (8 - length)) - 1)  # 首字节低 (8-length) 位
        for j in range(1, length):
            val = (val << 8) | buf[p + j]
        return length, val, p + length

    scan_end = min(n, MAX_MKV_SCAN)
    p = 0
    found_segment = False
    while p + 4 <= scan_end:
        if data[p:p + 4] == b"\x18S\x80g" or (data[p] == 0x18 and data[p + 1] == 0x53):
            found_segment = True
            _, seg_size, p = vint_size(data, p + 2 if data[p] == 0x18 else p + 4)
            # 声明 size 超窗口：直接在本窗口内找 TrackEntry（真实文件段头
            # 后面就是 Track，size 通常远小于窗口）
            break
        p += 1
    if not found_segment:
        return None
    w = h = 0
    while p + 2 <= scan_end:
        if data[p] == 0xAE:  # TrackEntry
            _, size, p = vint_size(data, p + 1)
            end = min(p + size, scan_end)
            wp = p
            while wp + 1 < end:
                if data[wp] == 0xB0:  # PixelWidth（值 1-4 字节裸 uint）
                    _, vs, nxt = vint_size(data, wp + 1)
                    vlen = nxt - (wp + 1)
                    start = wp + 1 + vlen  # 值字节在 vint 之后
                    if 1 <= vs <= 4 and start + vs <= end:
                        w = int.from_bytes(data[start:start + vs], "big")
                    wp = nxt + vs
                elif data[wp] == 0xBA:  # PixelHeight
                    _, vs, nxt = vint_size(data, wp + 1)
                    vlen = nxt - (wp + 1)
                    start = wp + 1 + vlen
                    if 1 <= vs <= 4 and start + vs <= end:
                        h = int.from_bytes(data[start:start + vs], "big")
                    wp = nxt + vs
                else:
                    # 未知元素：读 size 前进（size 异常时仍每步至少前进 1）
                    _, vs, nxt = vint_size(data, wp + 1)
                    if nxt <= wp + 1:
                        wp += 1
                    else:
                        wp = min(nxt + vs, scan_end)
            if w and h:
                return w, h
        else:
            p += 1
    return None


def avi_dimensions(data: bytes):
    """从 AVI 文件头（strf 块）解析 (width, height)。"""
    idx = data.find(b"strf")
    while idx != -1 and idx + 20 <= len(data):
        w = int.from_bytes(data[idx + 12:idx + 16], "little")
        h = int.from_bytes(data[idx + 16:idx + 20], "little")
        if w > 0 and h > 0:
            return w, abs(h)
        idx = data.find(b"strf", idx + 4)
    return None


def dimensions_from_head(data: bytes, kind: str):
    """根据文件头字节解析 (width, height)；解析不到返回 None。"""
    if not data:
        return None
    if kind == "image":
        try:
            from io import BytesIO
            from PIL import Image as _PILImage
            return _PILImage.open(BytesIO(data)).size
        except Exception:
            return None
    if kind == "video":
        return (mp4_dimensions(data) or mkv_dimensions(data)
                or avi_dimensions(data))
    return None


# ================================================================
# 视频变体挑选 / pexels 封面映射
# ================================================================


def pick_best_video(candidates: list[str]) -> str:
    """从候选视频 URL 里选最高清的一个（统一格式选择器的 best 语义）。

    排序规则（从优到劣）：
    1) 分辨率：文件名中的 `WxH` / `_W_H_` 强模式（宽高均限 3-4 位，
       排除 2026 这类年份/编号数字），乘积最大者
    2) 变体名：_large > _medium > _small > _tiny

    注意：不用裸 `(\\d+)[x_](\\d+)` —— 那会把 `abc_2026.mp4` 之类
    的文件名误判成分辨率。
    """
    from format_selector import pick_video_url
    return pick_video_url(candidates, "best")


_PEXELS_COVER_RE = re.compile(
    r"^/videos/(\d+)/(.+?)(?:-\d+)?\.(?:jpe?g|webp|png|avif)$", re.IGNORECASE)


def pexels_cover_to_video(url: str):
    """pexels 视频封面 -> 真视频直链：`/download/video/<id>/`。

    返回 (下载URL, 文件名) 或 None。封面无数字 id 时返回 None。
    """
    parsed = urlparse(url)
    if parsed.netloc.lower() != "images.pexels.com":
        return None
    m = _PEXELS_COVER_RE.match(parsed.path)
    if not m:
        return None
    vid, slug = m.group(1), m.group(2)
    return f"https://www.pexels.com/download/video/{vid}/", f"{slug}.mp4"


# ================================================================
# 详情页媒体提取（og:image / 真实视频直链），GUI 与 Scrapy 共用
# ================================================================

_VIDEO_ID_RE = re.compile(r"/video(?:s)?/([a-z0-9]+)/?", re.IGNORECASE)


def current_video_id_from_soup(soup):
    """从 og:url / canonical / 页面链接里提取当前视频 ID（若存在）。"""
    for m in soup.find_all("meta"):
        if m.get("property") == "og:url" and m.get("content"):
            hit = _VIDEO_ID_RE.search(m["content"])
            if hit:
                return hit.group(1)
    for link in soup.find_all("link"):
        if link.get("rel") and "canonical" in link.get("rel") and link.get("href"):
            hit = _VIDEO_ID_RE.search(link["href"])
            if hit:
                return hit.group(1)
    return ""


def extract_media_from_html(html: str, page_url: str = "") -> dict:
    """从详情页/页面 HTML 提取真实媒体资源。

    返回 dict：{title, video_url, image_url}。
    - video_url：og:video / video 标签 / HTML 内 CDN 直链中挑最高清的一个
      （能确定当前视频 id 时只取该视频，避免抓到推荐/相关视频）
    - image_url：og:image（已升级高清），无则页面最大的 img
    提取不到时对应字段为空字符串。
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    for name in ("h1", "title"):
        node = soup.find(name)
        if node and node.get_text(strip=True):
            title = node.get_text(strip=True)[:120]
            break
    if not title:
        for m in soup.find_all("meta"):
            prop = (m.get("property") or m.get("itemprop") or "").strip().lower()
            if prop in ("og:title", "twitter:title") and m.get("content", "").strip():
                title = m["content"].strip()[:120]
                break

    video_url = ""
    image_url = ""

    # 1) 视频：og:video / meta / video 标签 / source
    for m in soup.find_all("meta"):
        prop = (m.get("property") or m.get("itemprop") or "").strip().lower()
        content = m.get("content", "").strip()
        if not content or "http" not in content:
            continue
        if prop in ("og:video", "og:video:url", "og:video:secure_url",
                    "itemprop=video", "video", "embedurl", "contenturl") \
                and (".mp4" in content or "video" in content or "hls" in content):
            video_url = content
        elif "og:video" in prop and not video_url:
            video_url = content

    if not video_url:
        for v in soup.find_all("video"):
            src = v.get("src")
            if src and (".mp4" in src or "video" in src):
                video_url = src
                break
            if not src:
                for s in v.find_all("source"):
                    if s.get("src"):
                        video_url = s["src"]
                        break
                if video_url:
                    break
    if not video_url:
        # 从 HTML/JSON 里抓视频 CDN 直链（排除 canva 等第三方广告）
        candidates = re.findall(
            r'https?://[^\s"\'<>\\]+?\.(?:mp4|webm|mkv)(?:[?#][^\s"\'<>\\]*)?', html)
        candidates = [u.replace("\\/", "/").replace("&amp;", "&") for u in candidates]
        candidates = [u for u in candidates
                      if any(h in u for h in config.VIDEO_CDN_HINTS) and "canva.com" not in u]
        if candidates:
            # 若能确定当前详情页的视频 ID（og:url / canonical），优先只取该视频
            current_id = current_video_id_from_soup(soup)
            if current_id:
                own = [u for u in candidates if f"/{current_id}/" in u]
                if own:
                    candidates = own
            video_url = pick_best_video(candidates)

    if not video_url:
        # 2) 图片：og:image（可升级为高清），否则找页面最大的 img
        for m in soup.find_all("meta"):
            prop = (m.get("property") or m.get("itemprop") or "").strip().lower()
            if prop in ("og:image", "og:image:url", "og:image:secure_url", "image"):
                c = m.get("content", "").strip()
                if c.startswith("http"):
                    image_url = highres_url(c)
                    break
        if not image_url:
            best = ""
            for img in soup.find_all("img"):
                src = img.get("src") or img.get("data-src") or img.get("data-original")
                if src and src.startswith("http") and not is_icon_url(src):
                    best = src
                    break
            if best:
                image_url = highres_url(best)

    if not video_url:
        # B 站等：页面内嵌 window.__playinfo__（HTML 里带 DASH 流 JSON），
        # 静态抓取即可拿到直链，无需等 JS。
        play_url = playinfo_video_url(html)
        if play_url:
            video_url = play_url

    return {"title": title, "video_url": video_url, "image_url": image_url}


_PLAYINFO_RE = re.compile(
    r"window\.__playinfo__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL)


def playinfo_video_url(html: str) -> str:
    """从页面内嵌 __playinfo__ JSON 提取视频直链（B 站 DASH 流）。

    返回：dash.video 里按格式选择器（默认 best[height<=1080]）挑一条
    baseUrl；无 dash 时取 durl[0].url；都没有返回空串。
    """
    m = _PLAYINFO_RE.search(html or "")
    if not m:
        return ""
    try:
        import json
        data = json.loads(m.group(1))
    except ValueError:
        return ""
    d = (data or {}).get("data") or {}
    videos = (d.get("dash") or {}).get("video") or []
    items = [
        v for v in videos
        if isinstance(v, dict)
        and isinstance(v.get("baseUrl") or v.get("base_url"), str)
    ]
    if items:
        from format_selector import Format, select_formats
        fmts = [Format(url=v["baseUrl"], height=v.get("height") or 0,
                       width=v.get("width") or 0, size=v.get("bandwidth") or 0)
                for v in items]
        picked = (select_formats(fmts, "best[height<=1080]")
                  or select_formats(fmts, "best"))
        if picked:
            return picked.url
    durls = d.get("durl") or []
    for dv in durls:
        if isinstance(dv, dict) and isinstance(dv.get("url"), str) \
                and dv["url"].startswith("http"):
            return dv["url"]
    return ""


# ================================================================
# API 资源提取（「页面空壳 + 签名接口」的站：抖音/快手等）
# ================================================================
# 入口已迁移到 platform_adapters（平台适配器注册表），本模块仅保留转发，
# 兼容历史导入 `from discover_common import extract_media_from_api`。
# 新增平台改 platform_adapters，无需动此处。

from platform_adapters import extract_media_from_api  # noqa: F401
