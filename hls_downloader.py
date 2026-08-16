"""HLS（m3u8）流媒体下载器：纯 Python 实现，免 ffmpeg 依赖。

背景：很多视频站（尤其直播回放/点播平台）只给 m3u8 分片流，直链下载器
拿不到。本模块把「播放列表 + TS 分片」下载并顺序拼接成单个 .ts 文件
（视频/音频 TS 分片可被主流播放器直接播放，也可用 ffmpeg -i 转 mp4）。

能力与边界：
- 支持主播放列表（#EXT-X-STREAM-INF 多清晰度变速）→ 自动选最高带宽变体
- 支持相对/绝对分片 URI、#EXT-X-BYTERANGE 偏移分片、注释行
- 分片并发下载（默认 8 线程）+ 单分片重试，失败即整体失败回退标准下载
- 不支持：AES-128 加密流（#EXT-X-KEY:METHOD=AES-128，需密钥解密）、
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
        except Exception as exc:  # noqa: BLE001
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


def _parse_segments(playlist: bytes, playlist_url: str) -> list[dict]:
    """解析播放列表为 [{uri, length, offset}, ...]（BYTERANGE 分片带偏移）。"""
    text = playlist.decode("utf-8", "replace")
    if "#EXT-X-KEY:METHOD=AES-128" in text:
        raise _HlsError("该流为 AES-128 加密，暂不支持（需密钥解密）")
    segs: list[dict] = []
    pending_range: tuple[int, int] | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
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
            segs.append({"uri": uri, "len": pending_range[0],
                         "offset": pending_range[1]})
            pending_range = None
        else:
            segs.append({"uri": uri, "len": 0, "offset": 0})
    if not segs:
        raise _HlsError("播放列表无分片")
    return segs


def _download_segment(url: str, referer: str, offset: int, length: int) -> bytes:
    """下载单个分片（BYTERANGE 时带 Range 头），返回原始字节。"""
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
                                             seg["len"])
                    with open(tmp, "wb") as f:
                        f.write(data)
                    return
                except Exception as exc:  # noqa: BLE001
                    last = exc
            with lock:
                if not fail_reason:
                    fail_reason.append(str(last) or type(last).__name__)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = [pool.submit(work, i, s) for i, s in enumerate(segs)]
            for fut in as_completed(futs):
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
        return None