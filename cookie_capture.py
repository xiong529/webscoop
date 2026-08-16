"""「浏览器手动登录抓 Cookie」帮助器（GUI 按钮的底层实现）。

流程：弹出真实浏览器（有头模式）→ 用户在页面内扫码/账号登录 →
   读取登录后获得的 Cookie → 输入框展示 → 保存进 cookies.txt（按域名）。

数据合规/隐私说明：
- 浏览器是完全独立的临时上下文（不导入用户日常 Chrome 数据）；
- Cookie 只写入本项目根目录 cookies.txt，不会自动上传/同步到其他服务。
"""

from __future__ import annotations

import os
import threading

from resources_reptile.utils.cookies import load_cookie


class CookieCaptureSession:
    """独立线程里运行一个有头 Playwright 浏览器供用户登录。

    用法：
        cc = CookieCaptureSession()
        cc.open(url)            # 弹出浏览器（非阻塞）
        cc.wait_login(60, cb)   # 轮询直到拿到比初始更多的 Cookie（可选）
        cookies = cc.snapshot() # {"host": "k=v; k=v"}（按域名归组）
        cc.save_to_file()       # 写 cookies.txt（与手动粘贴格式兼容）
        cc.close()              # 关浏览器
    """

    def __init__(self):
        self.lock = threading.Lock()
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._thread = None
        self._user_closed = False  # 用户手动关掉了浏览器窗口后置位

    # ---------------- 生命周期（在专用线程内运行） ----------------

    def _bg_run(self, url: str):
        from playwright.sync_api import sync_playwright
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=False)
        except Exception:
            return  # 启动失败：无浏览器可弹，保持状态不误导用户
        try:
            context = browser.new_context(
                viewport={"width": 1280, "height": 860}, locale="zh-CN")
            page = context.new_page()
            with self.lock:
                self._pw, self._browser, self._context, self._page = (
                    pw, browser, context, page)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass  # 首次加载失败不关浏览器：用户在窗口里手动导航/重试
            # 登录成功后用户可能关掉浏览器窗口：持续监测窗口是否还在
            deadline = 60 * 1000 * 30  # 最多等 30 分钟供用户操作
            elapsed = 0
            while elapsed < deadline:
                try:
                    if not browser.is_connected():
                        break
                    page.wait_for_timeout(500)
                    elapsed += 500
                except Exception:
                    break
            with self.lock:
                if self._browser is not None and not browser.is_connected():
                    self._user_closed = True  # 窗口被用户/系统关闭
        except Exception:
            pass
        finally:
            self.close()

    def open(self, url: str) -> bool:
        """弹出浏览器（立即返回）。返回是否成功启动。"""
        with self.lock:
            if self._thread and self._thread.is_alive():
                return True
            self._user_closed = False
        self._thread = threading.Thread(target=self._bg_run, args=(url,),
                                        name="cookie-capture", daemon=True)
        self._thread.start()
        return True

    @property
    def is_open(self) -> bool:
        with self.lock:
            return self._page is not None and self._browser is not None

    # ---------------- 读 Cookie ----------------

    def state(self) -> str:
        """捕获会话状态：'ok'（浏览器在线）/ 'closed'（窗口已关闭）/ 'none'。"""
        with self.lock:
            if self._browser is None:
                return "none" if not self._user_closed else "closed"
            try:
                connected = self._browser.is_connected()
            except Exception:
                connected = False
            if not connected:
                return "closed"
        return "ok"

    def snapshot(self) -> dict[str, str]:
        """把当前上下文的所有 Cookie 按主域归组：{"example.com": "a=1; b=2"}。"""
        if self.state() != "ok":
            return {}
        try:
            cookies = self._context.cookies()
        except Exception:
            return {}
        groups: dict[str, list[str]] = {}
        for c in cookies:
            if not c.get("name") or not c.get("value"):
                continue
            dom = (c.get("domain") or "").lstrip(".")
            if not dom:
                continue
            host = dom.split(":", 1)[0].lower()
            if host.startswith("www."):
                host = host[4:]
            groups.setdefault(host, []).append(f"{c['name']}={c['value']}")
        return {h: "; ".join(vs) for h, vs in groups.items()}

    def readable_candidates(self, url: str = "") -> list[str]:
        """供 GUI 展示/保存的候选行（含域名前缀，格式与 cookies.txt 一致）。"""
        snap = self.snapshot()
        out: list[str] = []
        for host, cookie in snap.items():
            # 会话类 cookie 优先、量大的放前面，方便挑选
            if cookie.strip():
                out.append(f"{host}:  {cookie}")
        if not out and url:
            from urllib.parse import urlparse
            h = (urlparse(url).hostname or "").lower()
            existing = load_cookie().get(h)
            out.append(f"{h}:  {existing or ''}".rstrip())
        return out

    def wait_login(self, timeout: float = 300, interval: float = 1.0) -> dict[str, str]:
        """轮询等待登录 Cookie 出现（用于 GUI 实时提示）。返回捕获到的分组。"""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = self.snapshot()
            if snap:
                return snap
            time.sleep(interval)
        return {}

    @staticmethod
    def save_paste_to_file(cookie_header: str, host: str,
                           path: str = "cookies.txt") -> tuple[int, str]:
        """手动粘贴兜底：把日常浏览器复制的 Cookie 头直接写入 cookies.txt。

        cookie_header 为 `name=value; ...`（自动剥掉 "Cookie: " 前缀与首尾空白）。
        host 为页面域名（不写时按注册域归一）。走与捕获完全相同的注入链路，
        不依赖捕获窗口。返回 (写入行数, 消息)。
        """
        from resources_reptile.utils.cookies import cookie_file_path, reload_cookie, registrable
        hd = (cookie_header or "").strip()
        for prefix in ("cookie:", "cookie :", "cookie:"):
            if hd.lower().startswith(prefix):
                hd = hd[len(prefix):].strip()
                break
        if not hd or "=" not in hd:
            return 0, "粘贴内容为空或不是 Cookie 格式（应为 name=value; name2=value2）"
        host = (host or "").strip().lower().lstrip("www.")
        if not host:
            return 0, "缺少域名（先填目标 URL）"
        # 子域写根域行，注入时按注册域家族归一
        row_host = registrable(host)
        # 相对路径按项目根目录解析，与注入读取同一文件（不依赖进程 CWD）
        path = cookie_file_path(path)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"\n# ===== 手动粘贴 {time_str()} =====\n")
                f.write(f"{row_host}:  {hd}\n")
            reload_cookie()
            return 1, f"已写入 {row_host}:  {hd[:40]}…（{path}）"
        except OSError as exc:
            return 0, f"写入失败: {exc}"

    def save_to_file(self, path: str = "cookies.txt") -> tuple[int, str]:
        """把当前捕获的 Cookie 合并写进 cookies.txt（保留原有手动行）。"""
        st = self.state()
        if st == "closed":
            return 0, ("浏览器窗口已关闭，无法读取 Cookie：请重新「弹出浏览器」，"
                       "登录后保持窗口开着直接点保存")
        if st == "none":
            return 0, "浏览器尚未弹出（启动失败？），请先「弹出浏览器」再登录"
        snap = self.snapshot()
        if not snap:
            return 0, ("浏览器开着但未捕获到 Cookie：请确认窗口内已登录成功"
                       "（页面右上角出现头像），保持窗口开着再点保存")
        lines = []
        for host, cookie in sorted(snap.items()):
            if cookie.strip():
                lines.append(f"{host}:  {cookie}")
        if not lines:
            return 0, "没有可保存的 Cookie"
        # 相对路径按项目根目录解析，与注入读取同一文件（不依赖进程 CWD）
        from resources_reptile.utils.cookies import cookie_file_path
        path = cookie_file_path(path)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n# ===== 浏览器登录抓取 %s =====\n" %
                        time_str())
                for ln in lines:
                    f.write(ln + "\n")
            # 让运行中的抓取进程立即生效（utils.cookies 首次加载后有缓存）
            try:
                from resources_reptile.utils.cookies import reload_cookie
                reload_cookie()
            except Exception:
                pass
            return len(lines), f"已写入 {len(lines)} 个域名的 Cookie：{path}"
        except OSError as exc:
            return 0, f"写入失败: {exc}"

    def close(self):
        with self.lock:
            pw, browser, context, page = (self._pw, self._browser,
                                          self._context, self._page)
            self._pw = self._browser = self._context = self._page = None
        for obj in (page, context, browser):
            try:
                if obj is not None and getattr(obj, "is_connected", None) \
                        and obj.is_connected():
                    obj.close()
            except Exception:
                pass
        try:
            if pw is not None:
                pw.stop()
        except Exception:
            pass


def time_str() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")