"""本地敏感文件加密存储（Windows DPAPI / POSIX 权限收紧）。

用途：cookies.txt、llm_config.json、pexels_api_key.txt 等含登录态/API Key
的文件。原则：

- Windows：DPAPI（CryptProtectData）加密，密文只能由「加密时的同一 Windows
  用户、同一机器」解出；换用户/拷走文件都无法解密。
- POSIX（Linux/Mac）：无等价系统级方案，权限收紧到 0600 + 明文（保持
  文件可被其它工具编辑）。
- 兼容：文件头加标记 ``WSENC1:`` 判定加密；无标记的旧明文文件照常读取
  （升级无缝，旧文件不会被破坏）。
- 开关：环境变量 RESOURCES_SECRET_PLAINTEXT=1 强制明文写入（测试/沙箱用）。

用法：
    write_secret(path, "k=v; ...")
    text = read_secret(path)     # 无文件/失败返回 ""

注意：写文件用临时文件 + 原子替换，避免读到半截密文。
"""

from __future__ import annotations

import base64
import os
import sys

_WIN = sys.platform == "win32"
_HEADER = "WSENC1:"
_PLAINTEXT_ENV = "RESOURCES_SECRET_PLAINTEXT"


def _dpapi_available() -> bool:
    if not _WIN:
        return False
    if os.environ.get(_PLAINTEXT_ENV) == "1":
        return False
    try:
        import ctypes
        return hasattr(getattr(ctypes, "windll", None), "crypt32")
    except Exception:
        return False


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    """Windows DPAPI 加密/解密（CryptProtectData / CryptUnprotectData）。"""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = getattr(ctypes, "windll").crypt32  # noqa: B009 仅 Windows 运行（_WIN 守卫）
    kernel32 = ctypes.cdll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0,
              ctypes.byref(blob_out)):
        raise OSError("DPAPI 失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, wintypes.HANDLE))


def protect(text: str) -> str:
    """加密文本；非 Windows 或禁用时原样返回（降级明文）。"""
    if not _dpapi_available():
        return text
    return _HEADER + base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")


def unprotect(data: str) -> str:
    """解密文本；无加密标记或解密失败时原样返回（兼容旧明文文件）。"""
    if not data.startswith(_HEADER):
        return data
    try:
        raw = base64.b64decode(data[len(_HEADER):])
        return _dpapi(raw, False).decode("utf-8")
    except Exception:
        return data.removeprefix(_HEADER)  # 无法解密：降级展示密文本身，不崩溃


def _chmod_600(path: str) -> None:
    if not _WIN:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def write_secret(path: str, text: str) -> None:
    """加密写文件（临时文件 + 原子替换；POSIX 补 0600 权限）。"""
    payload = (protect(text) if text else text).encode("utf-8")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _chmod_600(path)


def read_secret(path: str) -> str:
    """读文件并解密；文件缺失返回 ""。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    return unprotect(raw.decode("utf-8", "replace"))