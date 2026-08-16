from dataclasses import dataclass, field


@dataclass
class ResourceItem:
    """一个待下载的网站资源（图片 / 视频 / 音频 / 文档 / 软件 / 压缩包）。

    - url: 资源所在页面 URL（用作防盗链 Referer）
    - title: 页面标题
    - file_urls: 资源文件的直接下载链接列表
    - file_names: 与 file_urls 一一对应的自定义文件名（可为空）
    """
    url: str = ""
    title: str = ""
    file_urls: list[str] = field(default_factory=list)
    file_names: list[str] = field(default_factory=list)
