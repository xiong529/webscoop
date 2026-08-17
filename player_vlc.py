"""VLC 通用媒体播放器（嵌入式，支持在线 URL 与本地文件播放）。

将官方 VLC win64 便携版放在 ``third_party/vlc/<版本>/`` 后即可使用，
无需安装到系统。支持几乎所有格式（视频/音频/图片/网络流），并嵌入 tkinter 窗口。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request
import zipfile

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
THIRD_PARTY_DIR = os.path.join(PROJECT_DIR, "third_party")

VLC_VERSION = "3.0.23"
VLC_ZIP_NAME = f"vlc-{VLC_VERSION}-win64.zip"
VLC_DOWNLOAD_URL = f"https://get.videolan.org/vlc/last/win64/{VLC_ZIP_NAME}"


def _has_libvlc() -> bool:
    """third_party/vlc/* 下是否存在 libvlc.dll。"""
    base = os.path.join(THIRD_PARTY_DIR, "vlc")
    if not os.path.isdir(base):
        return False
    for entry in os.listdir(base):
        if os.path.isfile(os.path.join(base, entry, "libvlc.dll")):
            return True
    return False


def ensure_vlc() -> int:
    """首次运行时自动获取 VLC 便携版（放入 third_party/vlc/）。

    仅在 libvlc.dll 缺失时下载一次（约 80MB）。返回 0 表示就绪。
    """
    if _has_libvlc():
        print("VLC 播放器就绪。")
        return 0
    os.makedirs(THIRD_PARTY_DIR, exist_ok=True)
    zip_path = os.path.join(THIRD_PARTY_DIR, VLC_ZIP_NAME)
    if not (os.path.isfile(zip_path) and os.path.getsize(zip_path) > 50_000_000):
        print(f"下载 VLC 播放器组件（约 80MB，仅首次需要）：\n  {VLC_DOWNLOAD_URL}")
        req = urllib.request.Request(VLC_DOWNLOAD_URL,
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=600) as resp, open(zip_path, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        print(f"下载完成：{os.path.getsize(zip_path) / 1e6:.1f} MB")
    target = os.path.join(THIRD_PARTY_DIR, "vlc", f"vlc-{VLC_VERSION}")
    if not os.path.isfile(os.path.join(target, "libvlc.dll")):
        print("解压 VLC…")
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(os.path.join(THIRD_PARTY_DIR, "vlc"))
    if _has_libvlc():
        print("VLC 播放器就绪。")
        return 0
    print("VLC 初始化失败，请手动安装 VLC 播放器。", file=sys.stderr)
    return 1


class VLCUnavailableError(RuntimeError):
    """找不到可用的 VLC 运行时。"""


def find_vlc_dir() -> str | None:
    """在项目 third_party 下查找 VLC 便携版目录（或系统安装的 VLC）。"""
    candidates: list[str] = []
    # 1) 项目内置便携版：third_party/vlc/*（内含 libvlc.dll 的最高版本目录）
    base = os.path.join(THIRD_PARTY_DIR, "vlc")
    if os.path.isdir(base):
        for entry in sorted(os.listdir(base), reverse=True):
            cand = os.path.join(base, entry)
            if os.path.isfile(os.path.join(cand, "libvlc.dll")):
                candidates.append(cand)
    # 2) 系统安装版（注册表 / 常见路径）
    import winreg
    for hive, key in (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\VideoLAN\VLC"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\VideoLAN\VLC"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\VideoLAN\VLC"),
    ):
        try:
            with winreg.OpenKey(hive, key) as k:
                inst = winreg.QueryValueEx(k, "InstallDir")[0]
                candidates.append(inst)
        except OSError:
            pass
    for cand in candidates:
        if os.path.isfile(os.path.join(cand, "libvlc.dll")):
            return cand
    return None


_vlc_dir_loaded: str | None = None


def _setup_libvlc() -> None:
    """把 VLC 目录加入 DLL 搜索路径，使 ``import vlc`` 可加载。"""
    global _vlc_dir_loaded
    if _vlc_dir_loaded:
        return
    vlc_dir = find_vlc_dir()
    if not vlc_dir:
        raise VLCUnavailableError(
            "未找到 VLC 运行时。请将官方 VLC win64 便携版放入 "
            "third_party/vlc/ 目录，或安装 VLC 桌面版。"
        )
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(vlc_dir)
    os.environ["PATH"] = vlc_dir + os.pathsep + os.environ.get("PATH", "")
    os.environ["VLC_PLUGIN_PATH"] = os.path.join(vlc_dir, "plugins")
    _vlc_dir_loaded = vlc_dir


def import_vlc():
    """懒加载 python-vlc，返回 vlc 模块。找不到运行时抛 VLCUnavailableError。"""
    try:
        _setup_libvlc()
        import vlc  # noqa: WPS433
    except VLCUnavailableError:
        raise
    except Exception as exc:
        raise VLCUnavailableError(f"python-vlc 加载失败：{exc}") from exc
    return vlc


class VLCEmbeddedPlayer:
    """嵌入式 VLC 播放器：可挂到任意 tkinter 控件（Frame/Canvas/Label）里播。

    支持 URL 直链与本地文件，自动处理画布尺寸变化，附带简易控制（播放/暂停/停止/
    进度/音量）。关闭宿主时调用 :meth:`stop` 释放资源。
    """

    def __init__(self, host_widget, repeat: bool = False, proxy: str | None = None):
        self.host = host_widget
        self._vlc = import_vlc()
        args = [
            "--no-video-title-show",
            "--verbose=0", "--no-osd",
            "--input-repeat=99999" if repeat else "--input-repeat=0",
            # 在线播放优化：加大网络缓冲（默认 1000ms 容易卡），断流自动重连
            "--network-caching=5000",
            "--http-reconnect",
        ]
        if proxy:
            args.append(f"--http-proxy={proxy}")
        self._instance = self._vlc.Instance(*args)
        if self._instance is None:
            # 参数/初始化失败时退化为最小配置
            self._instance = self._vlc.Instance()
        if self._instance is None:
            raise VLCUnavailableError("无法初始化 VLC 实例。")
        self.player = self._instance.media_player_new()
        self._duration = 0.0
        self._polling = False
        # 在 host 上开一个子 Frame 作为 VLC 渲染目标
        self._wrapper = None
        if hasattr(host_widget, "winfo_id"):
            self._wrapper = host_widget
        self._hwnd = None
        self._bind_after()

    def _bind_after(self):
        # host 可能是 ttk.Frame，拿它的内部 tk.Frame
        frame = self._wrapper
        if frame is not None:
            try:
                frame.update_idletasks()
            except Exception:
                pass
            self._attach(frame.winfo_id())

    def _attach(self, hwnd: int):
        self._hwnd = hwnd
        self.player.set_hwnd(hwnd)

    def resize(self):
        """外部调用：宿主尺寸变化后重设渲染区域。"""
        if self._wrapper is not None and self._hwnd:
            try:
                self._wrapper.update_idletasks()
                self.player.set_hwnd(self._hwnd)
            except Exception:
                pass

    def play(self, uri: str):
        """播放 URL 或本地路径。uri 支持 http(s):// 直链（含 rtmp/rtsp 等由 VLC 处理）。"""
        if isinstance(uri, os.PathLike):
            uri = os.fspath(uri)
        if os.path.isfile(uri):
            uri = os.path.abspath(uri)
        media = self._instance.media_new(uri)
        self.player.set_media(media)
        self.player.play()
        self._polling = True
        threading.Thread(target=self._poll_position, daemon=True).start()

    def _poll_position(self):
        while self._polling:
            try:
                dur = self.player.get_length() / 1000.0
                if dur > 0:
                    self._duration = dur
            except Exception:
                pass
            time.sleep(0.5)

    def toggle(self):
        try:
            if self.player.is_playing():
                self.player.pause()
            else:
                self.player.play()
        except Exception:
            pass

    def stop(self):
        self._polling = False
        try:
            self.player.stop()
        except Exception:
            pass

    def release(self):
        self.stop()
        try:
            self.player.release()
            self._instance.release()
        except Exception:
            pass

    def position(self) -> float:
        """0..1 播放进度。"""
        try:
            return self.player.get_position()
        except Exception:
            return 0.0

    def set_position(self, pos: float):
        try:
            self.player.set_position(max(0.0, min(1.0, pos)))
        except Exception:
            pass

    def is_playing(self) -> bool:
        try:
            return bool(self.player.is_playing())
        except Exception:
            return False

    def volume(self, level: int):
        try:
            self.player.audio_set_volume(max(0, min(100, level)))
        except Exception:
            pass

    @property
    def duration(self) -> float:
        return self._duration


if __name__ == "__main__":
    if "--ensure" in sys.argv:
        sys.exit(ensure_vlc())
    print("用法：python player_vlc.py --ensure   （首次获取 VLC 便携版）")
    sys.exit(2)