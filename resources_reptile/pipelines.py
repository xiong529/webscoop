"""Item 管道：将资源文件下载到本地并按类型分类存放。

分类规则（按扩展名）：
- images/   图片
- videos/   视频
- audios/   音频
- docs/     文档
- software/ 软件安装包 / 可执行文件
- archives/ 压缩包
- others/   其他
"""

import os
import re
from urllib.parse import unquote, urlparse

import scrapy
from itemadapter import ItemAdapter
from scrapy.pipelines.files import FilesPipeline

# GUI 与 Scrapy 共用同一统计单例：下载成功/失败在此按文件精确计录
from stats import get_stats as _stats

EXTENSION_CATEGORY = {
    # 图片
    "jpg": "images", "jpeg": "images", "png": "images", "gif": "images",
    "webp": "images", "bmp": "images", "svg": "images", "ico": "images",
    "avif": "images", "tiff": "images", "heic": "images",
    # 视频
    "mp4": "videos", "mkv": "videos", "avi": "videos", "mov": "videos",
    "wmv": "videos", "flv": "videos", "webm": "videos", "m4v": "videos",
    "ts": "videos", "rmvb": "videos", "3gp": "videos",
    "m3u8": "videos",  # HLS 播放列表（下载时按分片合并为 .ts）
    # 音频
    "mp3": "audios", "wav": "audios", "flac": "audios", "aac": "audios",
    "ogg": "audios", "m4a": "audios", "wma": "audios", "mid": "audios",
    # 文档
    "pdf": "docs", "doc": "docs", "docx": "docs", "xls": "docs",
    "xlsx": "docs", "ppt": "docs", "pptx": "docs", "txt": "docs",
    "md": "docs", "epub": "docs", "mobi": "docs", "csv": "docs",
    # 软件安装包
    "exe": "software", "msi": "software", "apk": "software", "dmg": "software",
    "pkg": "software", "deb": "software", "rpm": "software", "bat": "software",
    "sh": "software", "jar": "software",
    # 压缩包
    "zip": "archives", "rar": "archives", "7z": "archives", "tar": "archives",
    "gz": "archives", "bz2": "archives", "xz": "archives", "zst": "archives",
    "iso": "archives",
}

_EXT_RE = re.compile(r"\.([a-z0-9]{2,6})$", re.IGNORECASE)

# Content-Type -> 扩展名（无扩展名 URL 下载时按响应头补全/归类，MIME 嗅探）
CT_EXTENSION = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "image/bmp": ".bmp", "image/avif": ".avif",
    "image/heic": ".heic", "image/svg+xml": ".svg", "image/x-icon": ".ico",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/x-matroska": ".mkv",
    "video/quicktime": ".mov", "video/x-msvideo": ".avi", "video/x-flv": ".flv",
    "video/mpeg": ".mpg", "video/3gpp": ".3gp", "video/x-ms-wmv": ".wmv",
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/flac": ".flac", "audio/aac": ".aac", "audio/ogg": ".ogg",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/webm": ".weba",
    "application/pdf": ".pdf",
    "application/zip": ".zip", "application/x-zip-compressed": ".zip",
    "application/x-rar-compressed": ".rar", "application/vnd.rar": ".rar",
    "application/x-7z-compressed": ".7z",
    "application/gzip": ".gz", "application/x-gzip": ".gz",
    "application/x-tar": ".tar", "application/x-bzip2": ".bz2",
    "application/x-xz": ".xz", "application/x-zstd": ".zst",
    "application/x-msdownload": ".exe", "application/x-msdos-program": ".exe",
    "application/vnd.microsoft.portable-executable": ".exe",
    "application/vnd.android.package-archive": ".apk",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/epub+zip": ".epub",
    "text/plain": ".txt", "text/csv": ".csv", "text/x-markdown": ".md",
    "application/octet-stream": ".bin",
}


def ct_to_ext(content_type: str) -> str:
    """Content-Type -> 扩展名（带点），未知返回空串。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    return CT_EXTENSION.get(ct, "")


def classify_url(url: str, content_type: str = "") -> str:
    """根据 URL 的扩展名判断资源类型，返回分类目录名。

    URL 无扩展名时可用响应 Content-Type（MIME 嗅探）兜底分类，
    避免 /download?id=xxx 这类无扩展名资源全部落入 others。
    """
    path = urlparse(url).path
    match = _EXT_RE.search(path)
    if match:
        return EXTENSION_CATEGORY.get(match.group(1).lower(), "others")
    if content_type:
        return EXTENSION_CATEGORY.get(ct_to_ext(content_type).lstrip("."), "others")
    return "others"


def _safe_filename(name: str) -> str:
    """清除文件名中的非法字符。"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = unquote(name).strip(" .")
    return name or "download"


class ResourceFilesPipeline(FilesPipeline):
    """按资源类型分类下载，并提供更友好的文件名。"""

    def get_media_requests(self, item, info):
        adapter = ItemAdapter(item)
        url = adapter.get("url", "")
        names = adapter.get("file_names", [])
        for index, file_url in enumerate(adapter.get("file_urls", [])):  
            meta = {"referer": url}
            if names and index < len(names) and names[index]:
                meta["custom_name"] = names[index]
            yield scrapy.Request(file_url, meta=meta, headers={"Referer": url})

    def item_completed(self, results, item, info):
        """保持 FilesPipeline 默认行为：统计已在 media_downloaded/media_failed
        按文件精确记录（那里有请求 URL，失败原因/站点分布可归位）。"""
        return results

    async def media_downloaded(self, response, request, info, *, item=None):
        """下载成功：计入 downloaded（字节数）与已落盘目录的分类。"""
        file_info = await super().media_downloaded(response, request, info,
                                                   item=item)
        try:
            path = file_info.get("path", "")
            category = os.path.dirname(path).split(os.sep)[0] or "others"
            st = _stats()
            st.add_downloaded(1, len(response.body) if response.body else 0)
            st.add_category(category, 1)
        except Exception:
            pass
        return file_info

    def media_failed(self, failure, request, info):
        """下载失败：计入 failed（原因为错误消息，站点取请求 hostname）。

        与 GUI Downloader 共用 stats 单例，CLI Scrapy 跑完也能写 stats.json
        （落盘在 spider.closed 统一执行，见 resource_spider.py）。
        """
        try:
            reason = failure.getErrorMessage() or type(failure.value).__name__
            st = _stats()
            st.add_failed(
                1, reason=reason[:200],
                host=(urlparse(request.url).hostname or "") or "")
        except Exception:
            pass
        return super().media_failed(failure, request, info)

    def file_path(self, request, response=None, info=None, *, item=None):
        """生成保存路径：downloads/<分类>/<文件名>。

        无扩展名 URL（/download?id=xxx 等）下载后按响应 Content-Type
        嗅探补全扩展名并归类（MIME 嗅探，与 GUI 的 _reclassify_by_ct 对齐）。
        """
        ct = ""
        if response is not None:
            raw = response.headers.get("Content-Type")
            if raw:
                ct = raw.decode("latin-1", "replace")
        category = classify_url(request.url, ct)
        custom_name = request.meta.get("custom_name")
        parsed = urlparse(request.url)
        basename = ""
        if custom_name:
            basename = _safe_filename(os.path.basename(custom_name))
        else:
            base = os.path.basename(parsed.path)
            if base:
                basename = _safe_filename(base)
        # 无扩展名时按 Content-Type 补全（video/mp4 -> .mp4）
        if basename and not os.path.splitext(basename)[1]:
            ext = ct_to_ext(ct)
            if ext:
                basename = basename + ext
        if not basename:
            ext = ct_to_ext(ct)
            basename = (_safe_filename(request.url) + ext) if ext \
                else _safe_filename(request.url) + ".download"
        return os.path.join(category, basename)