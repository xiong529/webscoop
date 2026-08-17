"""HLS（m3u8）流媒体下载器：纯 Python 实现，免 ffmpeg 依赖。

背景：很多视频站（尤其直播回放/点播平台）只给 m3u8 分片流，直链下载器
拿不到。本模块把「播放列表 + TS 分片」下载并顺序拼接成单个 .ts 文件
（视频/音频 TS 分片可被主流播放器直接播放，也可用 ffmpeg -i 转 mp4）。

能力与边界：
- 支持主播放列表（#EXT-X-STREAM-INF 多清晰度变速）→ 自动选最高带宽变体
- 支持相对/绝对分片 URI、#EXT-X-BYTERANGE 偏移分片、注释行
- 支持 AES-128 加密流（#EXT-X-KEY:METHOD=AES-128，pycryptodome 解密；
  IV 未声明时按分片序号作默认 IV；密钥按 URI 缓存复用）
- 分片并发下载（默认 8 线程）+ 单分片重试，失败即整体失败回退标准下载
- 不支持：SAMPLE-AES 等其它加密方式、加密分片的 BYTERANGE 变体、
  直播流（无 #EXT-X-ENDLIST 且数量无限），报明确错误而不是卡死

用法：
    from hls_downloader import download_hls, is_hls
    path = download_hls("https://example.com/master.m3u8",
                        dest_dir="videos", out_name="clip")
    # -> videos/clip.ts
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

_M3U8_EXTS = (".m3u8", ".m3u")
_M3U8_CTS = ("application/vnd.apple.mpegurl", "application/x-mpegurl",
             "audio/mpegurl", "audio/x-mpegurl")
_EXTINF_RE = re.compile(r"#EXTINF:\s*([\d.]+)")
_BYTERANGE_RE = re.compile(r"#EXT-X-BYTERANGE:\s*(\d+)(?:@(\d+))?")
_KEY_ATTR_RE = re.compile(r'(\w+)=(?:"([^"]+)"|([^,"\s]+))')
_KEY_CACHE: dict[str, bytes] = {}
_KEY_CACHE_LOCK = threading.Lock()


def is_hls(url: str, content_type: str = "") -> bool:
    """URL 或 Content-Type 是否指向 HLS 播放列表。"""
    if url:
        path = urlparse(url).path.lower()
        if path.endswith(_M3U8_EXTS):
            return True
    ct = (content_type or "").split(";")[0].strip().lower()
    return ct in _M3U8_CTS


class _HlsError(Exception):
    """HLS 下载失败（原因可读）。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _fetch(url: str, referer: str = "", timeout: int = 20) -> bytes:
    """拉取播放列表（含重试的常规 GET）。"""
    from gui_fetch import FetchSession
    hdrs = {"Referer": referer} if referer else {}
    last = None
    for _ in range(2):
        try:
            resp = FetchSession().get(url, headers=hdrs, timeout=timeout)
            if resp.status_code >= 400:
                raise _HlsError(f"HTTP {resp.status_code}: {url}")
            data = resp.content
            if not data:
                raise _HlsError(f"空响应: {url}")
            return data
        except _HlsError:
            raise
        except Exception as exc:
            last = exc
    raise _HlsError(f"拉取失败: {last}")


def _resolve_uri(base: str, uri: str) -> str:
    """分片 URI 相对/绝对/锚点解析。"""
    uri = uri.strip()
    if not uri:
        return ""
    if uri.startswith("//"):
        return f"{urlparse(base).scheme or 'https'}:{uri}"
    if "://" in uri:
        return uri
    return urljoin(base, uri)


def _pick_variant(master: bytes, master_url: str) -> str:
    """含 #EXT-X-STREAM-INF 的主列表：挑最高带宽变体，返回其子列表 URL。"""
    text = master.decode("utf-8", "replace")
    best_url, best_bw = "", -1
    pending: int | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH\s*=\s*(\d+)", line)
            pending = int(m.group(1)) if m else 0
        elif line and not line.startswith("#"):
            if pending is not None and pending > best_bw:
                best_bw, best_url = pending, line
            pending = None
    return _resolve_uri(master_url, best_url) if best_url else ""


def _parse_key(line: str, playlist_url: str) -> dict | None:
    """解析 #EXT-X-KEY 行 -> {uri, iv}；iv 为 bytes 或 None（用分片序号）。

    METHOD=NONE 或无 METHOD 表示清除密钥；AES-128 之外的方法明确报错。
    """
    attrs: dict[str, str] = {}
    for k, v1, v2 in _KEY_ATTR_RE.findall(line):
        attrs[k.upper()] = v1 or v2
    method = attrs.get("METHOD", "").strip().upper()
    if not method or method == "NONE":
        return None
    if method != "AES-128":
        raise _HlsError(f"暂不支持的加密方式: {method}")
    uri = (attrs.get("URI") or "").strip()
    iv: bytes | None = None
    iv_hex = (attrs.get("IV") or "").strip()
    if iv_hex:
        h = iv_hex[2:] if iv_hex.lower().startswith("0x") else iv_hex
        if len(h) % 2:
            h = "0" + h
        try:
            iv = bytes.fromhex(h)
        except ValueError:
            raise _HlsError(f"非法的 IV: {iv_hex}")
    return {"uri": _resolve_uri(playlist_url, uri) if uri else "", "iv": iv}


def _parse_segments(playlist: bytes, playlist_url: str) -> list[dict]:
    """解析播放列表为 [{uri, length, offset, key}, ...]。

    key 为当前生效的加密规格（#EXT-X-KEY），无加密时为 None。
    """
    text = playlist.decode("utf-8", "replace")
    segs: list[dict] = []
    key: dict | None = None
    pending_range: tuple[int, int] | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            if line.startswith("#EXT-X-KEY"):
                key = _parse_key(line, playlist_url)
                continue
            m = _BYTERANGE_RE.search(line)
            if m:
                pending_range = (int(m.group(1)), int(m.group(2) or 0))
            continue
        if not line:
            continue
        uri = _resolve_uri(playlist_url, line)
        if not uri:
            continue
        if pending_range:
            seg = {"uri": uri, "len": pending_range[0],
                   "offset": pending_range[1], "key": key}
            pending_range = None
        else:
            seg = {"uri": uri, "len": 0, "offset": 0, "key": key}
        if key and seg["len"]:
            raise _HlsError("加密分片暂不支持 BYTERANGE 变体")
        segs.append(seg)
    if not segs:
        raise _HlsError("播放列表无分片")
    return segs


def _fetch_key(key_uri: str) -> bytes:
    """获取 AES-128 密钥（按 URI 缓存复用；失败抛 _HlsError）。"""
    if not key_uri:
        raise _HlsError("缺少密钥 URI")
    with _KEY_CACHE_LOCK:
        if key_uri in _KEY_CACHE:
            return _KEY_CACHE[key_uri]
    key = _fetch(key_uri, referer="", timeout=20)
    if len(key) not in (16, 24, 32):
        raise _HlsError(f"密钥长度非法（{len(key)} 字节）")
    with _KEY_CACHE_LOCK:
        _KEY_CACHE[key_uri] = key
    return key


def _decrypt_segment(data: bytes, key_spec: dict, seq: int) -> bytes:
    """AES-128-CBC 解密单个分片（PKCS7 去填充）。"""
    try:
        from Crypto.Cipher import AES
    except ImportError:
        raise _HlsError("缺少 pycryptodome 库（pip install pycryptodome）")
    if len(data) % 16:
        raise _HlsError("分片长度非 16 对齐（加密流异常）")
    key = _fetch_key(key_spec["uri"])
    iv = key_spec["iv"] if key_spec["iv"] is not None else seq.to_bytes(16, "big")
    if len(iv) != 16:
        raise _HlsError("非法 IV 长度")
    plain = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    n = plain[-1]
    if n < 1 or n > 16 or plain[-n:] != bytes([n]) * n:
        raise _HlsError("分片 PKCS7 填充校验失败")
    return plain[:-n]


def _download_segment(url: str, referer: str, offset: int, length: int,
                      key: dict | None = None, seq: int = 0) -> bytes:
    """下载单个分片（BYTERANGE 时带 Range 头；加密流解密后返回明文）。"""
    from gui_fetch import FetchSession
    hdrs = {"Referer": referer} if referer else {}
    if length > 0:
        hdrs["Range"] = f"bytes={offset}-{offset + length - 1}"
    resp = FetchSession().get(url, headers=hdrs, timeout=30)
    if resp.status_code >= 400:
        raise _HlsError(f"分片 HTTP {resp.status_code}: {url}")
    data = resp.content
    if not data:
        raise _HlsError(f"分片空响应: {url}")
    if length > 0:
        # 服务器若不支持 Range（仍回 200 全文），截断到请求长度防重复
        data = data[:length]
    if key:
        data = _decrypt_segment(data, key, seq)
    return data


def download_hls(url: str, dest_dir: str, out_name: str = "stream",
                 referer: str = "", workers: int = 8,
                 max_segments: int = 15_000) -> str | None:
    """下载 m3u8 流。成功返回最终 .ts 文件绝对路径；失败返回 None。

    - 主列表自动选最高带宽变体
    - 每分片下载到独立临时文件（BYTERANGE 带 Range 头），并发执行；
      单分片失败即整体失败（调用方可回退直链）
    - 成功后按播放列表顺序拼接 -> dest_dir/out_name.ts
    """
    dest: str | None = None
    work_dir: str | None = None
    try:
        os.makedirs(dest_dir, exist_ok=True)
        master = _fetch(url, referer=referer)
        playlist_url = _pick_variant(master, url) or url
        playlist = _fetch(playlist_url, referer=referer) if playlist_url != url \
            else master
        segs = _parse_segments(playlist, playlist_url)
        if len(segs) > max_segments:
            raise _HlsError(
                f"分片过多({len(segs)}>{max_segments})，疑似直播流，跳过")
        work_dir = os.path.join(dest_dir, ".hls_tmp_" + uuid.uuid4().hex[:8])
        os.makedirs(work_dir, exist_ok=True)
        fail_reason: list[str] = []
        lock = threading.Lock()

        def work(idx: int, seg: dict):
            if fail_reason:
                return
            tmp = os.path.join(work_dir, f"{idx:06d}.ts")
            for _ in range(3):
                try:
                    data = _download_segment(seg["uri"], referer, seg["offset"],
                                             seg["len"], seg.get("key"), idx)
                    with open(tmp, "wb") as f:
                        f.write(data)
                    return
                except Exception as exc:
                    last = exc
            with lock:
                if not fail_reason:
                    fail_reason.append(str(last) or type(last).__name__)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = [pool.submit(work, i, s) for i, s in enumerate(segs)]
            for _fut in as_completed(futs):
                if fail_reason:
                    for f in futs:
                        f.cancel()
                    break
        if fail_reason:
            raise _HlsError(f"分片下载失败: {fail_reason[0]}")
        dest = os.path.join(dest_dir, out_name + ".ts")
        with open(dest, "wb") as out:
            for i in range(len(segs)):
                tmp = os.path.join(work_dir, f"{i:06d}.ts")
                if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
                    raise _HlsError(f"分片缺失: {i}")
                with open(tmp, "rb") as f:
                    shutil.copyfileobj(f, out)
        shutil.rmtree(work_dir, ignore_errors=True)
        return dest
    except _HlsError as exc:
        print(f"[HLS] {exc.reason}")
        if dest and os.path.exists(dest):
            try:
                os.remove(dest)
            except OSError:
                pass
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
        return None