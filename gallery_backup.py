"""gallery-dl 备用下载器（覆盖 1400+ 网站的兜底方案）。

当内置发现器在陌生网站找不到资源时，可把网址交给 gallery-dl
批量下载（支持 Instagram/Pinterest/Reddit 等大量站点）。
通过子进程调用 gallery-dl CLI，逐行解析进度输出，避免阻塞 GUI。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading

import config

# pexels 站内 v3 API 需该密钥头，且发送空的 X-Forwarded-* 头会触发 520，
# gallery-dl 内置的 PexelsAPI 固定带空头导致失败，见 _PATCHED_PEXELS_API。

# gallery-dl 可执行文件（虚拟环境优先，其次 PATH）
_GDL = None
_VENV_GDL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         ".venv", "Scripts", "gallery-dl.exe")


def gallery_dl_path() -> str | None:
    global _GDL
    if _GDL is not None:
        return _GDL
    for cand in (_VENV_GDL,):
        if os.path.exists(cand):
            _GDL = cand
            return _GDL
    _GDL = shutil.which("gallery-dl")
    return _GDL


def is_available() -> bool:
    return gallery_dl_path() is not None


# ---- 通用 URL 变体处理 -------------------------------------------------
# 很多网站带语言/地区段（/zh-cn/、/en-us/、/zh-hans/）或子域（zh-cn.example.com），
# gallery-dl 的提取器往往不识别这些写法。无法解析时自动生成变体再重试。

_LOCALE_SEG_RE = re.compile(r"^[a-z]{2,3}(?:[-_][a-z]{2,4}){1,2}$", re.I)
_LOCALE_SUBDOMAIN_RE = re.compile(r"^[a-z]{2,3}(?:[-_][a-z]{2,4}){1,2}$", re.I)


def url_variants(url: str) -> list[str]:
    """生成去掉语言/地区前缀的候选网址（原地址排第一，去重保序）。"""
    from urllib.parse import urlsplit, urlunsplit

    variants = [url]
    try:
        p = urlsplit(url)
        netloc, path = p.netloc, p.path
        if not netloc:
            return variants

        # 1) 路径开头的语言段：/zh-cn/、/en-us/、/zh-hans/ ...（最多剥 2 层）
        segs = [s for s in path.split("/") if s != ""]
        for _ in range(2):
            if segs and _LOCALE_SEG_RE.match(segs[0]):
                segs.pop(0)
                new_path = "/" + "/".join(segs) + ("/" if path.endswith("/") else "")
                variants.append(urlunsplit((p.scheme, netloc, new_path, p.query, p.fragment)))

        # 2) 语言子域：zh-cn.example.com -> www.example.com
        host = netloc.split(":", 1)[0]
        port = netloc[len(host):]
        if "." in host and not host.startswith("www."):
            sub, dot, rest = host.partition(".")
            if dot and rest and _LOCALE_SUBDOMAIN_RE.match(sub):
                variants.append(urlunsplit(
                    (p.scheme, "www." + rest + port, path, p.query, p.fragment)))
    except Exception:
        pass

    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def pick_supported_url(url: str) -> str:
    """返回第一个能被 gallery-dl 识别的变体；进程内识别不可用时退回原网址。"""
    try:
        from gallery_dl import extractor
    except Exception:
        return url
    for cand in url_variants(url):
        try:
            if extractor.find(cand) is not None:
                return cand
        except Exception:
            continue
    return url


def gallery_dl_cmd(url: str) -> list[str]:
    """返回调用 gallery-dl 的前缀命令。

    pexels 域名走本文件的 CLI 包装模式（去掉 PexelsAPI 的空 X-Forwarded-*
    请求头，否则其站内 API 返回 520），其余站点直接用 gallery-dl。
    """
    gdl = gallery_dl_path()
    if not gdl:
        raise FileNotFoundError("未安装 gallery-dl")
    if "pexels.com" in url:
        return [sys.executable, os.path.abspath(__file__)]
    return [gdl]


# 进度行示例：
#  "[danbooru][download] Downloading 12.jpg to C:/.../12.jpg"
#  "[][download] Saved 3 files to ..."  (完成汇总)
#  "[pixiv][download] Saving xyz to ..."
# 管道模式下 gallery-dl 也可能只输出纯路径行：C:\dir\file.jpg
# 站点名可能有多段 []，且间距不定，用 (?:\[\w+\]\s*)+ 匹配任意多段
_PROGRESS_RE = re.compile(r"(?:\[\w+\]\s*)+(Downloading|Saving)\s+(.+?) to (.+)")
_DONE_RE = re.compile(r"\[\[\]\]|Saved (\d+) (?:files?|images?)")
_ERROR_RE = re.compile(r"(?:\[\w+\]\s*)*\s*(?:[Ee]rror:?\s+|\[[\w]+\]\[error\])(.*)")
_FILE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg",
             ".avif", ".jfif", ".tif", ".tiff", ".mp4", ".webm", ".mkv",
             ".mov", ".avi", ".flv", ".m4v", ".wmv", ".ts", ".mp3")


def _count_files(dirpath: str) -> int:
    n = 0
    try:
        for entry in os.scandir(dirpath):
            if entry.is_file():
                n += 1
    except OSError:
        pass
    return n


_IMAGE_EXTS = ("jpg", "jpeg", "png", "gif", "webp", "bmp", "svg",
               "avif", "jfif", "tif", "tiff")
_VIDEO_EXTS = ("mp4", "webm", "mkv", "mov", "avi", "flv", "m4v", "wmv", "ts")


def _kind_filter_arg(kind_filter: str | None) -> str | None:
    # 注意：filter 里不能访问 i['...'] 或 url（gallery-dl 1.32.9 会卡死），
    # 使用文档推荐的顶层 extension 变量
    if kind_filter == "image":
        return "extension in ('" + "','".join(_IMAGE_EXTS) + "')"
    if kind_filter == "video":
        return "extension in ('" + "','".join(_VIDEO_EXTS) + "')"
    return None


def list_files(url: str, limit: int = 100, kind_filter: str | None = None,
               min_size: int = 0, proxy: str | None = None,
               progress_cb=None, cancel_event=None, workers: int = 8,
               timeout: float = 180.0) -> list[dict]:
    """列出目标页可下载的文件候选（用 gallery-dl 的 -j 输出元数据）。

    返回 [{"url", "name", "size", "ext", "page"} ...]：
      - url: 下载目标（多数站点为文件直链；danbooru 等直链被反爬拦截的
        站点自动改用可正常下载的「页面」URL）。
      - size: 字节数（来自站点元数据，精确）；探测不到为 None。
      - page: 来源页面 URL（有则给，便于排查）。
    类型/最小大小过滤在 Python 端完成；注意不把 --filter 交给 gallery-dl，
    否则当过滤结果为空时其会放弃 --range 上限、遍历整站（gallery-dl 行为）。
    :raises RuntimeError: gallery-dl 无法解析该 URL。
    """
    import json
    from urllib.parse import urlparse

    gdl = gallery_dl_cmd(url)
    if not gdl:
        raise FileNotFoundError("未安装 gallery-dl")
    original_url = url
    url = pick_supported_url(url)
    env = dict(os.environ)
    if proxy:
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
    # 按类型过滤时放大抓取量，靠 Python 端再收敛，保证能拿满该类型
    eff_limit = limit if (limit and not kind_filter) else (
        min(int(limit or 500) * 25, 2000) if limit else 2000)
    cmd = gdl + ["-j", "--range", f"1-{max(1, int(eff_limit))}"]
    cmd.append(url)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"解析超时（{timeout:.0f}s），请减小数量或稍后再试")
    if proc.returncode != 0 and not proc.stdout.strip():
        msg = (proc.stderr or "").strip()[:300] or "无法解析该页面"
        if url != original_url:
            msg += f"\n（已自动尝试去掉语言前缀：{url}）"
        raise RuntimeError(msg)
    host = urlparse(url if url.startswith(("http://", "https://")) else "http://" + url).netloc
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        urls = [ln.strip() for ln in proc.stdout.splitlines()
                if ln.strip().lower().startswith("http")]
        return [{"url": u, "name": os.path.basename(urlparse(u).path) or u,
                 "size": None, "ext": "", "page": None} for u in urls[:limit]]

    results: list[dict] = []
    seen: set[str] = set()
    for entry in data if isinstance(data, list) else []:
        meta = None
        for part in entry[1:]:
            if isinstance(part, dict):
                meta = part
                break
        if not meta:
            continue
        file_url = meta.get("file_url") or meta.get("url") or ""
        # gallery-dl 的 Url 消息为 [3, url, meta]，URL 在字符串元素里
        if not file_url and len(entry) > 1 and isinstance(entry[1], str) \
                and entry[1].startswith(("http://", "https://")):
            file_url = entry[1]
        if not file_url:
            continue
        key = meta.get("md5") or file_url
        if key in seen:
            continue
        seen.add(key)
        extension = (meta.get("extension") or meta.get("file_ext") or "").lower()
        if kind_filter == "image" and extension not in _IMAGE_EXTS:
            continue
        if kind_filter == "video" and extension not in _VIDEO_EXTS:
            continue
        filename = meta.get("filename") or os.path.basename(file_url)
        if filename.endswith("." + extension):
            filename = filename[: -(len(extension) + 1)]
        name = f"{filename}.{extension}" if extension and not filename.endswith("." + extension) else filename
        size = meta.get("file_size")
        if size is not None and min_size > 0 and size < min_size:
            continue
        download_url = file_url
        # danbooru 等站直链被 Cloudflare 拦截，改用「单帖页」URL（已实测可下）
        if "danbooru" in host and meta.get("id") is not None:
            download_url = f"https://{host}/posts/{meta['id']}"
        preview = (meta.get("preview_file_url") or meta.get("preview_url")
                   or meta.get("sample_url") or meta.get("thumb_url") or "")
        results.append({
            "url": download_url,
            "name": name,
            "size": size,
            "ext": extension,
            "page": file_url,
            "preview": preview,
            "width": meta.get("width") or meta.get("image_width") or 0,
            "height": meta.get("height") or meta.get("image_height") or 0,
        })
        if len(results) >= (limit or len(results) + 1):
            break
        if cancel_event is not None and cancel_event.is_set():
            break
    if progress_cb:
        progress_cb(len(results), len(results))
    return results


class GalleryDownload:
    """在后台线程运行 gallery-dl，通过回调回传进度/完成/错误。

    :param limit: 最多下载的文件数（None 不限）。
    :param kind_filter: "image" | "video" | None（只下图片/只下视频/不限）。
    """

    def __init__(self, urls, outdir: str, log_cb=None, done_cb=None,
                 error_cb=None, proxy: str | None = None,
                 limit: int | None = None, kind_filter: str | None = None):
        self.urls = [urls] if isinstance(urls, str) else list(urls)
        self.outdir = outdir
        self.log_cb = log_cb or (lambda line: None)
        self.done_cb = done_cb
        self.error_cb = error_cb
        self.proxy = proxy
        self.limit = limit
        self.kind_filter = kind_filter
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancel.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def _run(self):
        try:
            gdl = gallery_dl_cmd(self.urls[0] if self.urls else "")
        except FileNotFoundError:
            gdl = None
        if not gdl:
            self.log_cb("[备用下载] 未找到 gallery-dl，请先安装：pip install gallery-dl")
            if self.error_cb:
                self.error_cb("gallery-dl 未安装")
            return
        env = dict(os.environ)
        if self.proxy:
            env["http_proxy"] = self.proxy
            env["https_proxy"] = self.proxy
        cmd = list(gdl) + [
            "--directory", self.outdir,
            "--retries", "3",
        ]
        if self.limit and self.limit > 0:
            cmd += ["--range", f"1-{self.limit}"]
        farg = _kind_filter_arg(self.kind_filter)
        if farg:
            cmd += ["--filter", farg]
        cmd += [pick_supported_url(u) for u in self.urls]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            self.log_cb(f"[备用下载] 启动失败: {exc}")
            if self.error_cb:
                self.error_cb(str(exc))
            return
        assert self._proc.stdout is not None
        start_count = _count_files(self.outdir)
        saved = 0
        for line in self._proc.stdout:
            line = line.rstrip("\n\r")
            if not line.strip():
                continue
            m = _PROGRESS_RE.search(line)
            if m:
                self.log_cb(f"[备用下载] {m.group(2)} -> {os.path.basename(m.group(3))}")
                saved += 1
                continue
            m = _DONE_RE.search(line)
            if m and "Saved" in line:
                continue
            tail = line.rstrip()
            if tail.lower().endswith(_FILE_EXT):
                self.log_cb(f"[备用下载] {os.path.basename(tail)}")
                saved += 1
                continue
            if "error" in line.lower() and "traceback" not in line.lower():
                self.log_cb(f"[备用下载] {line.strip()[:200]}")
                continue
            self.log_cb(f"[备用下载] {line.strip()[:200]}")
        self._proc.wait()
        if self._cancel.is_set():
            self.log_cb("[备用下载] 已取消")
            if self.error_cb:
                self.error_cb("已取消")
            return
        if self._proc.returncode == 0:
            new_files = _count_files(self.outdir) - start_count
            saved = max(saved, new_files)
            self.log_cb(f"[备用下载] 完成：保存 {saved} 个文件 -> {self.outdir}")
            if self.done_cb:
                self.done_cb(saved, self.outdir)
        else:
            self.log_cb(f"[备用下载] 退出码 {self._proc.returncode}，可能没有可下载的资源")
            if self.error_cb:
                self.error_cb(f"退出码 {self._proc.returncode}")


if __name__ == "__main__":
    # gallery-dl CLI 包装模式：pexels 站内 v3 API 的空 X-Forwarded-* 头
    # 会触发 520，这里去掉后把命令行原样转发给 gallery-dl。
    def _patch_pexels_api():
        try:
            from gallery_dl.extractor.pexels import PexelsAPI
        except ImportError:
            return
        _orig = PexelsAPI.__init__

        def _patched(self, extractor):
            _orig(self, extractor)
            for key in ("X-Forwarded-CF-Connecting-IP",
                        "X-Forwarded-HTTP_CF_IPCOUNTRY",
                        "X-Forwarded-CF-IPRegionCode"):
                self.headers.pop(key, None)

        PexelsAPI.__init__ = _patched

    _patch_pexels_api()
    import gallery_dl as _gdl
    sys.exit(_gdl.main())
