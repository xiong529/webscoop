"""网站资源爬取图形界面（tkinter）

功能：
- 输入网址 -> 发现页面可爬取资源（图片/视频/文件）
- API 抓取弹窗：填 Pexels 接口地址 + API Key，按官方 API 直接获取资源（支持自动翻页）
- 展示区：图片显示缩略图+文件名，视频显示封面+文件名，文件显示文件名
- 勾选需要下载的内容，支持全选 / 全不选
- 抓取设置：浏览器指纹 / 代理开关 / 断点续载 / 并发数 / 下载目录
- 「刷新」换指纹重新抓取（每次结果可能不同）
- 便利功能：随机选 N / 复制链接 / 打开页面 / 去重
- 下载到 information/ 目录（按类型分子目录）
"""

from __future__ import annotations

import copy
import io
import os
import queue
import random
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from PIL import Image, ImageTk

from applog import log, setup_logging
import config
from api_discoverer import PRESETS, ApiDiscoverer, build_preset_url
from gui_crawler import (Discoverer, Downloader, Resource,
                         highres_url, load_failures)
from gui_fetch import FetchSession
from llm_rules import load_llm_config, save_llm_config, test_connection
from renderer import close_renderer

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
INFORMATION_DIR = config.INFORMATION_DIR
THUMB_SIZE = 96

KIND_LABELS = {"image": "图片", "video": "视频", "file": "文件"}
KIND_COLORS = {"image": "#2e7d32", "video": "#b26a00", "file": "#455a64"}


def pct_size(n: int) -> str:
    if n <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _bind_tooltip(widget, text: str):
    """给任意控件挂 Enter/Leave 悬浮提示（ttk 无原生 tooltip 选项）。"""
    tip = {"win": None}

    def _leave(_):
        w = tip["win"]
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass
            tip["win"] = None

    def _enter(_):
        if tip["win"] is not None:
            return
        try:
            w = tk.Toplevel(widget)
            w.wm_overrideredirect(True)
            w.wm_geometry(f"+{widget.winfo_rootx()}+{widget.winfo_rooty() + widget.winfo_height() + 4}")
            ttk.Label(w, text=text, background="#ffffe0", relief="solid",
                      padding=(6, 3)).pack()
            tip["win"] = w
        except Exception:
            pass

    widget.bind("<Enter>", _enter)
    widget.bind("<Leave>", _leave)


def _aspect_ratio(w: int, h: int) -> str:
    """宽高化简为最简整数比（如 1920x1080 -> 16:9）；比值过大返回空串。"""
    import math
    if w <= 0 or h <= 0:
        return ""
    g = math.gcd(w, h)
    sw, sh = w // g, h // g
    if sw > 100 or sh > 100:
        return ""
    return f"{sw}:{sh}"


def _fmt_size(b) -> str:
    """字节数转可读文本；None/0 显示 未知。"""
    if not b or int(b) <= 0:
        return "未知"
    return pct_size(int(b))


def fetch_image(res: Resource, max_box: tuple[int, int] | None = None):
    """拉取资源图片/封面（优先高清版），缩放至不超过 max_box。失败抛异常。"""
    # 缩略图预览：用页面原缩略地址（小、快）即可，避免拉原图
    if res.preview_url and res.preview_url != res.url and res.preview_url.startswith("http"):
        url = res.preview_url
    else:
        # 高清：真实媒体直链（备用解析提供）优先；否则按规则变换 URL
        raw = getattr(res, "raw_url", "") or ""
        if raw and raw.startswith("http"):
            url = raw
        elif res.kind == "image":
            url = highres_url(res.url)
        else:
            url = res.preview_url or res.url
    fs = FetchSession()
    try:
        r = fs.get(url, headers={"Referer": res.page_url}, timeout=config.REQUEST_TIMEOUT)
        r.raise_for_status()
        content = r.content
    finally:
        fs.close()
    if not content:
        raise ValueError("empty")
    img = Image.open(io.BytesIO(content))
    if max_box:
        img.thumbnail(max_box)
    return img


def load_thumb(res: Resource) -> ImageTk.PhotoImage:
    """尝试加载真实缩略图/封面，失败时生成占位图。"""
    try:
        img = fetch_image(res, (THUMB_SIZE, THUMB_SIZE))
        return ImageTk.PhotoImage(img)
    except Exception:
        return make_placeholder(res.kind)


def load_large_preview(res: Resource) -> ImageTk.PhotoImage | None:
    """加载预览区大图（最高约 800x800）。失败返回 None。"""
    try:
        img = fetch_image(res, (800, 800))
        return ImageTk.PhotoImage(img)
    except Exception:
        return None


def make_placeholder(kind: str) -> ImageTk.PhotoImage:
    """生成文字占位图（文件类型直接用缩略占位）。"""
    try:
        from PIL import ImageDraw
        palette = {"image": (240, 247, 240), "video": (255, 248, 225), "file": (236, 239, 241)}
        size = (THUMB_SIZE, THUMB_SIZE)
        img = Image.new("RGB", size, palette.get(kind, (236, 239, 241)))
        d = ImageDraw.Draw(img)
        label = KIND_LABELS.get(kind, "文件")
        d.text((size[0] // 2, size[1] // 2), label, fill=(120, 120, 120), anchor="mm")
        return ImageTk.PhotoImage(img)
    except Exception:
        img = Image.new("RGB", (2, 2), (236, 239, 241))
        return ImageTk.PhotoImage(img)


class ApiDialog(tk.Toplevel):
    """Pexels 官方 API 抓取弹窗（不与爬虫主界面混排）。"""

    def __init__(self, app):
        super().__init__(app.root)
        self.title("API 抓取 (Pexels 官方接口)")
        self.geometry("620x210")
        self.resizable(False, False)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.app = app

        body = ttk.Frame(self, padding=10)
        body.pack(fill=tk.BOTH, expand=True)

        r0 = ttk.Frame(body)
        r0.pack(fill=tk.X)
        self.api_preset_var = tk.StringVar(value=PRESETS["search"][0])
        self.api_preset_box = ttk.Combobox(
            r0, textvariable=self.api_preset_var,
            values=[PRESETS[k][0] for k in PRESETS] + ["自定义URL"],
            state="readonly", width=14)
        self.api_preset_box.pack(side=tk.LEFT)
        self.api_preset_box.bind("<<ComboboxSelected>>", self._on_preset_change)

        ttk.Label(r0, text="关键词:").pack(side=tk.LEFT, padx=(8, 2))
        self.api_kw_var = tk.StringVar()
        self.api_kw_entry = ttk.Entry(r0, textvariable=self.api_kw_var, width=16)
        self.api_kw_entry.pack(side=tk.LEFT)
        self.api_kw_entry.bind("<Return>", lambda e: self.api_fetch())
        self.api_kw_entry.bind("<KeyRelease>", lambda e: self._update_api_url())

        ttk.Label(r0, text="每页:").pack(side=tk.LEFT, padx=(8, 2))
        self.api_per_var = tk.StringVar(value="15")
        ttk.Spinbox(r0, from_=1, to=80, width=4, textvariable=self.api_per_var).pack(side=tk.LEFT)

        ttk.Label(r0, text="翻页:").pack(side=tk.LEFT, padx=(8, 2))
        self.api_pages_var = tk.StringVar(value=str(config.API_PAGE_LIMIT))
        ttk.Spinbox(r0, from_=1, to=30, width=4, textvariable=self.api_pages_var).pack(side=tk.LEFT)

        ttk.Label(r0, text="API Key:").pack(side=tk.LEFT, padx=(8, 2))
        self.api_key_var = tk.StringVar(value=self._load_api_key())
        ttk.Entry(r0, textvariable=self.api_key_var, width=32).pack(side=tk.LEFT)

        self.api_fetch_btn = ttk.Button(r0, text="API 获取", command=self.api_fetch)
        self.api_fetch_btn.pack(side=tk.LEFT, padx=(8, 0))

        r1 = ttk.Frame(body)
        r1.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(r1, text="接口地址:").pack(side=tk.LEFT)
        self.api_url_var = tk.StringVar(value=build_preset_url("search"))
        ttk.Entry(r1, textvariable=self.api_url_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        self.api_quota_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.api_quota_var, foreground="#666").pack(anchor="w", pady=(6, 0))

        self._on_preset_change()
        self.bind("<Escape>", lambda _e: self.destroy())

    def _api_preset_key(self) -> str:
        label = self.api_preset_var.get()
        for k, (name, _ep, _kw) in PRESETS.items():
            if name == label:
                return k
        return "custom"

    def _on_preset_change(self, _e=None):
        key = self._api_preset_key()
        needs_kw = key != "custom" and PRESETS[key][2]
        self.api_kw_entry.config(state=tk.NORMAL if needs_kw else tk.DISABLED)
        self._update_api_url()

    def _update_api_url(self, _e=None):
        key = self._api_preset_key()
        if key == "custom":
            return
        try:
            per = int(self.api_per_var.get() or 15)
        except ValueError:
            per = 15
        self.api_url_var.set(build_preset_url(key, self.api_kw_var.get(), per))

    def _load_api_key(self) -> str:
        key = os.environ.get("RESOURCES_API_KEY", "").strip()
        if key:
            return key
        try:
            with open(config.API_KEY_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _save_api_key(self, key: str):
        if not key:
            return
        try:
            with open(config.API_KEY_FILE, "w", encoding="utf-8") as f:
                f.write(key)
        except OSError:
            pass

    def api_fetch(self):
        """校验参数后交给主窗口启动抓取线程（结果经主队列回流）。"""
        if self.app.busy:
            return
        key = self._api_preset_key()
        if key != "custom" and PRESETS[key][2]:
            kw = self.api_kw_var.get().strip()
            if not kw:
                messagebox.showwarning("提示", "搜索类接口需要填写关键词")
                return
            try:
                per = int(self.api_per_var.get() or 15)
            except ValueError:
                per = 15
            url = build_preset_url(key, kw, per)
            self.api_url_var.set(url)
        else:
            url = self.api_url_var.get().strip()
            if not url:
                messagebox.showwarning("提示", "请填写 Pexels 接口地址")
                return
        if not url.startswith(("http://", "https://")):
            messagebox.showwarning("提示", "接口地址需要以 http(s):// 开头")
            return
        api_key = self.api_key_var.get().strip()
        if not api_key:
            messagebox.showwarning(
                "提示", "请填写 Pexels API Key（https://www.pexels.com/api/ 免费申请）")
            return
        try:
            max_pages = int(self.api_pages_var.get() or config.API_PAGE_LIMIT)
        except ValueError:
            max_pages = config.API_PAGE_LIMIT
        self._save_api_key(api_key)
        label = self.api_preset_var.get()
        if key != "custom":
            kw = self.api_kw_var.get().strip()
            if kw:
                label += f"「{kw}」"
        self.api_fetch_btn.config(state=tk.DISABLED)
        self.app.start_api_fetch(url, api_key, max_pages, label)

    def set_busy(self, busy: bool):
        state = tk.DISABLED if busy else tk.NORMAL
        self.api_fetch_btn.config(state=state)
        if not busy:
            self.api_quota_var.set("")

    def set_quota(self, quota: str):
        self.api_quota_var.set(f"配额 {quota}" if quota else "")


class LlmDialog(tk.Toplevel):
    """LLM 模型配置弹窗：接口地址 / API Key / 模型名 + 测试连接 + 保存。"""

    def __init__(self, app):
        super().__init__(app.root)
        self.title("LLM 模型配置")
        self.geometry("600x210")
        self.resizable(False, False)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.app = app

        body = ttk.Frame(self, padding=10)
        body.pack(fill=tk.BOTH, expand=True)

        cfg = load_llm_config()

        r0 = ttk.Frame(body)
        r0.pack(fill=tk.X)
        ttk.Label(r0, text="接口地址:").pack(side=tk.LEFT)
        self.base_var = tk.StringVar(value=cfg.get("base_url", ""))
        ttk.Entry(r0, textvariable=self.base_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        r1 = ttk.Frame(body)
        r1.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(r1, text="API Key:").pack(side=tk.LEFT)
        self.key_var = tk.StringVar(value=cfg.get("api_key", ""))
        key_entry = ttk.Entry(r1, textvariable=self.key_var, width=52)
        key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        r2 = ttk.Frame(body)
        r2.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(r2, text="模型名:").pack(side=tk.LEFT)
        self.model_var = tk.StringVar(value=cfg.get("model", ""))
        model_entry = ttk.Entry(r2, textvariable=self.model_var)
        model_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        model_entry.bind("<Return>", lambda _e: self._test_connection())

        r3 = ttk.Frame(body)
        r3.pack(fill=tk.X, pady=(10, 0))
        self.test_btn = ttk.Button(r3, text="测试连接", command=self._test_connection)
        self.test_btn.pack(side=tk.LEFT)
        self.save_btn = ttk.Button(r3, text="保存配置", command=self._save)
        self.save_btn.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(r3, text="关闭", command=self.destroy).pack(side=tk.RIGHT)

        self.status_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.status_var, foreground="#666",
                  wraplength=560).pack(anchor="w", pady=(8, 0))

        self.bind("<Escape>", lambda _e: self.destroy())

    def _values(self) -> tuple:
        return (self.base_var.get().strip(), self.key_var.get().strip(),
                self.model_var.get().strip())

    def _set_busy(self, busy: bool):
        state = tk.DISABLED if busy else tk.NORMAL
        self.test_btn.config(state=state)
        self.save_btn.config(state=state)

    def _test_connection(self):
        base, key, model = self._values()
        if not base or not key or not model:
            self.status_var.set("请填写接口地址 / API Key / 模型名")
            return
        self.status_var.set("正在测试连接…")
        self._set_busy(True)

        def worker():
            ok, msg = test_connection(base, key, model)
            self.after(0, lambda: (self.status_var.set(msg), self._set_busy(False)))

        threading.Thread(target=worker, daemon=True).start()

    def _save(self):
        base, key, model = self._values()
        if not base or not key or not model:
            self.status_var.set("请填写接口地址 / API Key / 模型名")
            return
        err = save_llm_config(base, key, model)
        if err:
            self.status_var.set(f"保存失败: {err}")
        else:
            self.status_var.set("已保存，llm_rules.py / GUI 将使用该配置")


class ResourceApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("网站资源爬取工具")
        root.geometry("1080x680")
        root.minsize(900, 560)

        self.queue = queue.Queue()
        self.thumb_queue = queue.Queue()
        self.thumb_results = queue.Queue()
        self.resources: dict[str, Resource] = {}
        self.thumb_cache: dict[int, ImageTk.PhotoImage] = {}
        self.current_preview_id: int | None = None
        self.busy = False
        self.session: FetchSession | None = None
        self.gallery: object | None = None
        self._backup_cancel = threading.Event()   # 备用下载/预览阶段取消标记
        self._stop_event = threading.Event()   # 发现/抓取阶段停止标记（「停止」按钮）
        self.seen_urls: set[str] = set()   # 本次会话已见过的 URL（去重）
        self.follow_active = False         # 定时跟进调度是否运行中
        self._follow_stop = threading.Event()
        self._impersonate_rot = 0          # 指纹轮换游标
        self._min_size_filter = 0          # 大小范围过滤（KB），未点「应用」前不预设
        self._max_size_filter = 0
        self._sel_anchor: str | None = None  # Shift 范围复选的锚点行

        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        threading.Thread(target=self._thumb_worker, daemon=True).start()

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill=tk.X)

        ttk.Label(top, text="网址:").pack(side=tk.LEFT)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(top, textvariable=self.url_var, width=70)
        self.url_entry.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        self.url_entry.bind("<Return>", lambda e: self.discover())

        self.discover_btn = ttk.Button(top, text="发现资源", command=self.discover)
        self.discover_btn.pack(side=tk.LEFT, padx=4)
        self.refresh_btn = ttk.Button(top, text="刷新(换指纹)", command=self.refresh)
        self.refresh_btn.pack(side=tk.LEFT, padx=4)
        self.download_btn = ttk.Button(top, text="下载勾选", command=self.download_selected, state=tk.DISABLED)
        self.download_btn.pack(side=tk.LEFT, padx=4)
        self.stop_btn = ttk.Button(top, text="停止", command=self.stop_current, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=4)
        _bind_tooltip(self.stop_btn, "停止正在进行的抓取/下载，已发现的资源保留并正常显示")
        self.backup_btn = ttk.Button(top, text="备用下载(gallery-dl)",
                                     command=self.backup_download)
        self.backup_btn.pack(side=tk.LEFT, padx=4)

        # ---- 抓取设置栏（两排：设置项 / 便利功能按钮） ----
        sbar = ttk.LabelFrame(self.root, text="抓取设置", padding=6)
        sbar.pack(fill=tk.X, padx=8)

        row1 = ttk.Frame(sbar)
        row1.pack(fill=tk.X)

        ttk.Label(row1, text="指纹:").pack(side=tk.LEFT)
        self.imp_var = tk.StringVar(value=config.IMPERSONATE)
        self.imp_box = ttk.Combobox(row1, textvariable=self.imp_var, width=14,
                                    values=config.IMPERSONATE_OPTIONS, state="readonly")
        self.imp_box.pack(side=tk.LEFT, padx=(2, 10))

        self.proxy_var = tk.BooleanVar(value=config.PROXY_ENABLED)
        ttk.Checkbutton(row1, text="启用代理", variable=self.proxy_var).pack(side=tk.LEFT, padx=6)

        self.render_var = tk.BooleanVar(value=config.RENDER_MODE)
        self.render_chk = ttk.Checkbutton(row1, text="渲染模式", variable=self.render_var)
        self.render_chk.pack(side=tk.LEFT, padx=6)
        _bind_tooltip(self.render_chk, "无头浏览器渲染页面后再发现资源（静态抓不到的 JS 站点勾选）")

        self.resume_var = tk.BooleanVar(value=config.RESUME_EXISTING)
        ttk.Checkbutton(row1, text="断点续载", variable=self.resume_var).pack(side=tk.LEFT, padx=6)

        ttk.Label(row1, text="并发:").pack(side=tk.LEFT, padx=(10, 2))
        self.workers_var = tk.StringVar(value=str(config.DOWNLOAD_WORKERS))
        self.workers_spin = ttk.Spinbox(row1, from_=1, to=config.DOWNLOAD_WORKERS_MAX,
                                        width=3, textvariable=self.workers_var)
        self.workers_spin.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(row1, text="下载目录:").pack(side=tk.LEFT)
        self.outdir_var = tk.StringVar(value=INFORMATION_DIR)
        self.outdir_entry = ttk.Entry(row1, textvariable=self.outdir_var, width=22)
        self.outdir_entry.pack(side=tk.LEFT, padx=4)
        ttk.Button(row1, text="浏览", command=self._choose_dir).pack(side=tk.LEFT)

        # ---- 文件大小范围过滤 ----
        ttk.Label(row1, text=" 大小范围:").pack(side=tk.LEFT, padx=(8, 2))
        self.min_size_var = tk.StringVar(value=str(config.MIN_RESOURCE_SIZE))
        self.min_size_entry = ttk.Entry(row1, textvariable=self.min_size_var, width=6)
        self.min_size_entry.pack(side=tk.LEFT)
        ttk.Label(row1, text="~").pack(side=tk.LEFT)
        self.max_size_var = tk.StringVar(value="0")
        self.max_size_entry = ttk.Entry(row1, textvariable=self.max_size_var, width=8)
        self.max_size_entry.pack(side=tk.LEFT)
        ttk.Label(row1, text="KB").pack(side=tk.LEFT, padx=(2, 4))
        ttk.Button(row1, text="按大小过滤", command=self.apply_size_filter).pack(side=tk.LEFT)

        # ---- 便利功能按钮栏（第二排，功能多不再挤一排） ----
        row2 = ttk.Frame(sbar)
        row2.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(row2, text="功能:").pack(side=tk.LEFT)
        ttk.Button(row2, text="随机选N", command=self.random_select).pack(side=tk.LEFT, padx=2)
        ttk.Button(row2, text="复制链接", command=self.copy_links).pack(side=tk.LEFT, padx=2)
        ttk.Button(row2, text="打开页面", command=self.open_page).pack(side=tk.LEFT, padx=2)
        ttk.Button(row2, text="关键词搜索…", command=self.open_search_dialog).pack(side=tk.LEFT, padx=2)
        ttk.Button(row2, text="热点模式…", command=self.open_hot_dialog).pack(side=tk.LEFT, padx=2)
        ttk.Button(row2, text="定时跟进…", command=self.open_follow_dialog).pack(side=tk.LEFT, padx=2)
        ttk.Button(row2, text="去重", command=self.dedupe).pack(side=tk.LEFT, padx=2)
        ttk.Button(row2, text="重试失败", command=self.retry_failed).pack(side=tk.LEFT, padx=2)
        self.api_btn = ttk.Button(row2, text="API 抓取…", command=self.open_api_dialog)
        self.api_btn.pack(side=tk.LEFT, padx=2)
        _bind_tooltip(self.api_btn, "Pexels 官方接口抓取（弹窗操作，避免与爬虫界面混排）")
        self.llm_btn = ttk.Button(row2, text="LLM 模型…", command=self.open_llm_dialog)
        self.llm_btn.pack(side=tk.LEFT, padx=2)
        _bind_tooltip(self.llm_btn, "配置 LLM（接口地址 / API Key / 模型名）并测试连接，供 llm_rules.py 生成高清规则")
        self.cookie_btn = ttk.Button(row2, text="登录抓 Cookie…", command=self.open_cookie_dialog)
        self.cookie_btn.pack(side=tk.LEFT, padx=2)
        _bind_tooltip(self.cookie_btn, "弹出真实浏览器手动登录（独立临时上下文，不碰日常浏览器数据），"
                                      "登录后把 Cookie 写入 cookies.txt 供需要登录态的站点注入")

        # ---- 下载文件名模板 ----
        tmpl = ttk.Frame(self.root, padding=(8, 0))
        tmpl.pack(fill=tk.X)
        ttk.Label(tmpl, text="文件名模板:").pack(side=tk.LEFT)
        self.tmpl_var = tk.StringVar(value=config.FILENAME_TEMPLATE)
        self.tmpl_entry = ttk.Entry(tmpl, textvariable=self.tmpl_var, width=52)
        self.tmpl_entry.pack(side=tk.LEFT, padx=4)
        _bind_tooltip(
            self.tmpl_entry,
            "定义保存路径/文件名，/ 表示子目录。可用：\n"
            "{category} 分类(images/videos/...)  {kind} 类型(image/video/file)\n"
            "{name} 原名  {stem} 无扩展名  {ext} 扩展名(.jpg)\n"
            "{site} 站点域名  {title} 页面标题  {size} 大小\n"
            "{width}x{height} 分辨率（如 1920x1080）\n"
            "默认：{category}/{name}（与旧行为一致）")
        ttk.Button(tmpl, text="还原默认",
                   command=lambda: self.tmpl_var.set(config.FILENAME_TEMPLATE)).pack(side=tk.LEFT)

        self._build_ops()
        self.root.after(150, self._poll_queue)
        self.root.after(150, self._poll_thumbs)

    def _build_ops(self):
        ops = ttk.Frame(self.root, padding=(8, 0, 8, 4))
        ops.pack(fill=tk.X)
        ttk.Button(ops, text="全选", command=lambda: self.set_all(True)).pack(side=tk.LEFT)
        ttk.Button(ops, text="全不选", command=lambda: self.set_all(False)).pack(side=tk.LEFT, padx=4)
        ttk.Button(ops, text="反选", command=self.invert_select).pack(side=tk.LEFT, padx=4)
        ttk.Label(ops, text="  展示:").pack(side=tk.LEFT, padx=(12, 0))
        self.filter_kind = tk.StringVar(value="all")
        for value, label in (("all", "全部"), ("image", "图片"), ("video", "视频"), ("file", "文件")):
            ttk.Radiobutton(ops, text=label, value=value, variable=self.filter_kind,
                            command=self.refresh_list).pack(side=tk.LEFT, padx=2)

        ttk.Label(ops, text="  类型:").pack(side=tk.LEFT, padx=(12, 0))
        self.filter_cat = tk.StringVar(value="all")
        for value, label in (("all", "全部"), ("images", "图片"), ("videos", "视频"),
                             ("audios", "音频"), ("docs", "文档"), ("software", "软件"),
                             ("archives", "压缩包"), ("others", "其他")):
            ttk.Radiobutton(ops, text=label, value=value, variable=self.filter_cat,
                            command=self.refresh_list).pack(side=tk.LEFT, padx=2)

        ttk.Label(ops, text="  排序:").pack(side=tk.LEFT, padx=(12, 0))
        self.sort_key = tk.StringVar(value="默认")
        sort_cb = ttk.Combobox(
            ops, textvariable=self.sort_key, state="readonly", width=9,
            values=("默认", "分辨率", "文件大小", "名称"))
        sort_cb.current(0)
        sort_cb.pack(side=tk.LEFT, padx=2)
        sort_cb.bind("<<ComboboxSelected>>", lambda _e: self.refresh_list())

        main = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        # 左侧列表（Tree 列内联缩略图 + 文件名）
        list_frame = ttk.Frame(main)
        main.add(list_frame, weight=3)
        cols = ("checked", "name", "info")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="预览")
        self.tree.column("#0", width=110, anchor="w", stretch=False)
        for cid, text, width in (
            ("checked", "☑", 44),
            ("name", "文件名", 320),
            ("info", "信息", 240),
        ):
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=width, anchor="w", stretch=(cid != "checked"))
        self.tree.column("name", stretch=True)
        self.tree.tag_configure("image", foreground=KIND_COLORS["image"])
        self.tree.tag_configure("video", foreground=KIND_COLORS["video"])
        self.tree.tag_configure("file", foreground=KIND_COLORS["file"])
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_tree_double)
        self.tree.bind("<Button-3>", self._on_tree_right)

        # 右侧预览
        preview_frame = ttk.Frame(main, width=260)
        main.add(preview_frame, weight=1)
        self.preview_label = ttk.Label(preview_frame, text="点击某行即勾选，Shift+点击范围复选，右键更多操作", anchor="center")
        self.preview_label.pack(fill=tk.BOTH, expand=True)
        self.preview_name = tk.StringVar()
        ttk.Label(preview_frame, textvariable=self.preview_name, wraplength=250).pack(fill=tk.X, pady=4)

        # 底部状态
        status = ttk.Frame(self.root, padding=8)
        status.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(status, textvariable=self.status_var).pack(side=tk.LEFT)
        self.progress = ttk.Progressbar(status, length=300)
        self.progress.pack(side=tk.RIGHT)

        self.root.after(150, self._poll_queue)
        self.root.after(150, self._poll_thumbs)

    # ---------------------------------------------------------------- API 抓取
    def open_api_dialog(self):
        """打开 Pexels 官方 API 抓取弹窗（已存在则前置）。"""
        dlg = getattr(self, "_api_dialog", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.lift()
            dlg.focus_force()
            return
        self._api_dialog = ApiDialog(self)

    def open_llm_dialog(self):
        """打开 LLM 模型配置弹窗（已存在则前置）。"""
        dlg = getattr(self, "_llm_dialog", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.lift()
            dlg.focus_force()
            return
        self._llm_dialog = LlmDialog(self)

    def open_cookie_dialog(self):
        """打开「浏览器手动登录抓 Cookie」弹窗（已存在则前置）。"""
        dlg = getattr(self, "_cookie_dialog", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.lift()
            dlg.focus_force()
            return
        CookieDialog(self)

    def open_search_dialog(self):
        """打开「关键词搜索」弹窗：按平台构造搜索页 URL → 走正常发现流程。"""
        dlg = getattr(self, "_search_dialog", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.lift()
            dlg.focus_force()
            return
        SearchDialog(self)

    def open_hot_dialog(self):
        """打开「热点模式」弹窗：热搜榜 → 勾选 → 批量发现（渲染+接口捕获）。"""
        dlg = getattr(self, "_hot_dialog", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.lift()
            dlg.focus_force()
            return
        if self.busy:
            messagebox.showinfo("提示", "当前有任务进行中，请先完成或停止")
            return
        HotDialog(self)

    def open_follow_dialog(self):
        """打开「定时跟进」弹窗：关注列表 + 轮询调度（后台线程）。"""
        dlg = getattr(self, "_follow_dialog", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.lift()
            dlg.focus_force()
            return
        FollowDialog(self)

    def start_follow(self, interval_min: int):
        """启动定时跟进（后台线程，不占用 busy；发现并入列表且按 URL 去重）。"""
        if self.busy:
            messagebox.showinfo("提示", "当前有任务进行中，请先完成或停止")
            return
        self.follow_active = True
        self._follow_stop = threading.Event()
        threading.Thread(target=self._follow_worker, args=(interval_min, False),
                         daemon=True).start()

    def run_follow_once(self):
        """立即跑一轮跟进（不循环）。"""
        if self.busy:
            messagebox.showinfo("提示", "当前有任务进行中，请先完成或停止")
            return
        self.follow_active = True
        self._follow_stop = threading.Event()
        threading.Thread(target=self._follow_worker, args=(0, True),
                         daemon=True).start()

    def stop_follow(self):
        """停止定时跟进（当前页探测完即止）。"""
        ev = getattr(self, "_follow_stop", None)
        self.follow_active = False
        if ev is not None:
            ev.set()

    def _follow_add_live(self, r: Resource):
        """跟进发现的资源：按 URL 去重后上屏（worker 线程调用）。"""
        if r.url in self.seen_urls:
            return
        self.seen_urls.add(r.url)
        self.queue.put(("res_item", r))

    def _follow_worker(self, interval_min: int, one_shot: bool):
        ev = self._follow_stop
        log.info("定时跟进启动 one_shot=%s 间隔=%s 分钟", one_shot, interval_min)
        try:
            from gui_crawler import Discoverer
            import follow_list
            while not ev.is_set():
                targets = follow_list.urls()
                if not targets:
                    self.queue.put(("follow_tick", "定时跟进：关注列表为空，等待添加"))
                else:
                    ok_pages = 0
                    self.queue.put(
                        ("follow_tick", f"定时跟进：开始扫 {len(targets)} 个页面…"))
                    log.info("跟进轮次开始 页面数=%d", len(targets))
                    for u in targets:
                        if ev.is_set():
                            break
                        d = Discoverer(session=self._make_session(),
                                       render_mode=True, stop_event=ev,
                                       on_resource=self._follow_add_live)
                        try:
                            d.discover(u)
                            ok_pages += 1
                        except Exception as exc:
                            log.warning("跟进页面失败 %s: %s", u[:120], exc)
                            self.queue.put(
                                ("follow_tick", f"[跟进] {u[:60]} 失败: {exc}"))
                    self.queue.put(("follow_done", ok_pages))
                    log.info("跟进轮次结束 成功页=%d", ok_pages)
                if one_shot:
                    break
                if ev.is_set():
                    break
                deadline = time.time() + interval_min * 60
                while time.time() < deadline and not ev.is_set():
                    time.sleep(0.5)
        except Exception as exc:
            log.exception("定时跟进异常")
            self.queue.put(("follow_tick", f"定时跟进异常: {exc}"))
        finally:
            self.follow_active = False
            log.info("定时跟进结束")
            self.queue.put(("follow_tick", "定时跟进已停止"))

    def _api_dialog_busy(self, busy: bool):
        """联动 API 弹窗的忙状态（发现/API 抓取互斥期间的按钮切换）。"""
        dlg = getattr(self, "_api_dialog", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.set_busy(busy)

    def _api_dialog_quota(self, quota: str):
        dlg = getattr(self, "_api_dialog", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.set_quota(quota)

    def start_api_fetch(self, url: str, api_key: str, max_pages: int, label: str):
        """API 弹窗触发：清空列表并启动抓取线程（结果经主队列回流）。"""
        if self.busy:
            return
        self.busy = True
        self.api_btn.config(state=tk.DISABLED)
        self.discover_btn.config(state=tk.DISABLED)
        self.refresh_btn.config(state=tk.DISABLED)
        self.download_btn.config(state=tk.DISABLED)
        self._clear_list()
        self.resources.clear()
        self.thumb_cache.clear()
        self.session = self._make_session()
        self.status_var.set(f"正在通过 Pexels API 获取（{label}，最多翻页 {max_pages} 页）...")
        self.progress.config(mode="indeterminate")
        self.progress.start(12)
        threading.Thread(target=self._api_worker,
                         args=(url, api_key, max_pages, label), daemon=True).start()

    def _api_worker(self, url, api_key, max_pages, label):
        try:
            d = ApiDiscoverer(session=self.session)

            def prog(done, total, text):
                self.queue.put(("probe", f"{text}: {done}/{total}"))

            resources, info = d.fetch(url, api_key, max_pages=max_pages, progress_cb=prog)
            info["label"] = label
            self.queue.put(("api_discovered", resources, info))
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self.queue.put(("api_failed", str(exc)))

    # ---------------------------------------------------------------- 发现
    def _make_session(self):
        """按设置栏参数新建 FetchSession（每次发现/下载前重建以保证配置生效）。"""
        return FetchSession(
            impersonate=self.imp_var.get() or config.IMPERSONATE,
            proxy_enabled=self.proxy_var.get(),
        )

    def discover(self, rotate_fingerprint: bool = False):
        if self.busy:
            return
        if self.follow_active:
            messagebox.showinfo("提示", "定时跟进运行中：请先到「定时跟进」弹窗停止，再手动发现")
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "请输入网址")
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
            self.url_var.set(url)
        if rotate_fingerprint:
            # 换一个指纹再抓，结果可能不同（每次轮换）
            opts = config.IMPERSONATE_OPTIONS
            self._impersonate_rot = (self._impersonate_rot + 1) % len(opts)
            self.imp_var.set(opts[self._impersonate_rot])
        self.busy = True
        self.discover_btn.config(state=tk.DISABLED)
        self.refresh_btn.config(state=tk.DISABLED)
        self.download_btn.config(state=tk.DISABLED)
        self.api_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self._stop_event.clear()
        self._api_dialog_busy(True)
        self._clear_list()
        self.resources.clear()
        self.thumb_cache.clear()
        self.session = self._make_session()
        self.status_var.set(f"正在发现 {url} （指纹: {self.imp_var.get()}, 代理: {'开' if self.proxy_var.get() else '关'}）...")
        self.progress.config(mode="indeterminate")
        self.progress.start(12)
        log.info("发现开始 url=%s 指纹=%s 代理=%s 渲染=%s",
                 url, self.imp_var.get(), self.proxy_var.get(), self.render_var.get())
        threading.Thread(target=self._discover_worker, args=(url,), daemon=True).start()

    def refresh(self):
        """换指纹重新抓取当前网址。"""
        self.discover(rotate_fingerprint=True)

    def stop_current(self):
        """停止正在进行的抓取：置位停止标记，已发现的资源保留。"""
        self._stop_event.set()
        self.stop_btn.config(state=tk.DISABLED)
        self.status_var.set("正在停止…（已发现的资源会保留）")

    def _discover_worker(self, url: str):
        try:
            d = Discoverer(session=self.session, render_mode=self.render_var.get(),
                           stop_event=self._stop_event,
                           on_resource=self._add_resource_live)

            def prog(done, total, msg):
                self.queue.put(("probe", f"{msg}: {done}/{total}"))

            resources, title = d.discover(url, progress_cb=prog)
            # 收集本次新 URL，供去重
            self.seen_urls = {r.url for r in resources}
            filtered = d.filtered_count + d.filtered_icons
            stopped = bool(self._stop_event.is_set())
            log.info("发现完成 url=%s 资源=%d 过滤=%d 停止=%s",
                     url, len(resources), filtered, stopped)
            self.queue.put(("discovered", resources, title, filtered, stopped))
            if not stopped:
                # 内置发现完成后，自动用 gallery-dl 补充（合并去重，无则静默）
                proxy = config.DEFAULT_PROXY if self.proxy_var.get() else None
                try:
                    self._backup_fallback_worker(url, proxy=proxy, merge_only=True)
                except Exception:
                    import traceback
                    traceback.print_exc()
        except Exception as exc:
            self.queue.put(("discover_failed", str(exc), url))

    def _add_resource_live(self, r: Resource):
        """探测完成一个就立即上屏一个（worker 线程调用，经队列回流主线程）。"""
        self.queue.put(("res_item", r))

    def discover_many(self, urls: list[str], label: str):
        """热点模式：批量发现多个页面（逐个渲染+接口捕获，结果聚合进主列表）。

        与 discover() 相同的前置/收尾约定（清空列表、busy 互斥、按钮联动）。
        """
        if self.busy or not urls:
            return
        self.busy = True
        self.discover_btn.config(state=tk.DISABLED)
        self.refresh_btn.config(state=tk.DISABLED)
        self.download_btn.config(state=tk.DISABLED)
        self.api_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self._stop_event.clear()
        self._api_dialog_busy(True)
        self._clear_list()
        self.resources.clear()
        self.thumb_cache.clear()
        self.session = self._make_session()
        self.status_var.set(f"热点模式 {label}：正在发现 {len(urls)} 个页面…")
        self.progress.config(mode="indeterminate")
        self.progress.start(12)
        threading.Thread(target=self._hot_worker, args=(urls, label),
                         daemon=True).start()

    def _hot_worker(self, urls: list[str], label: str):
        try:
            from gui_crawler import Discoverer
            all_res: list = []
            ok = 0
            for i, u in enumerate(urls, 1):
                if self._stop_event.is_set():
                    break
                self.queue.put(("probe", f"[{label}] {i}/{len(urls)} 正在发现 "
                                        f"{u[:60]}…"))
                d = Discoverer(session=self.session, render_mode=True,
                               stop_event=self._stop_event,
                               on_resource=self._add_resource_live)
                try:
                    resources, _title = d.discover(u)
                    ok += 1
                    all_res.extend(resources)
                except Exception as exc:
                    self.queue.put(("probe", f"[{label}] {u[:60]} 失败: {exc}"))
            stopped = bool(self._stop_event.is_set())
            self.queue.put(("discovered", all_res,
                            f"{label}（成功 {ok} 页）", 0, stopped))
        except Exception as exc:
            self.queue.put(("discover_failed", str(exc), label))

    def _backup_fallback_worker(self, url: str, proxy: str | None = None,
                                merge_only: bool = False):
        """内置发现失败/完毕后的补充：用 gallery-dl 适配器列文件并注入主列表。

        :param merge_only: True=合并模式（内置已成功，只补充差异项，失败静默）；
                           False=兜底模式（内置失败，作为唯一结果，失败报错）。
        """
        def note(text):
            self.queue.put(("backup_note", text))

        def fail(text):
            self.queue.put(("error", text))

        from urllib.parse import urlparse
        # gallery-dl 的 pexels 提取器不识别「搜索视频」页（会把 "videos" 当关键词
        # 返回无关图片）。这类页面结果由内置发现提供，跳过备用补充。
        try:
            pu = urlparse(url)
            if pu.netloc.lower().endswith("pexels.com") \
                    and "/search/videos/" in pu.path:
                (note("pexels 视频搜索页由内置解析处理，跳过 gallery-dl 补充")
                 if merge_only else
                 fail("pexels 视频搜索页不适用于备用解析器，请用「发现资源」按钮"))
                return
        except Exception:
            pass

        try:
            from gallery_backup import _IMAGE_EXTS, _VIDEO_EXTS, is_available, list_files
            if not is_available():
                (note("未安装 gallery-dl，跳过自动补充")
                 if merge_only else
                 fail("页面受反爬保护且备用解析器不可用，"
                      "请安装 gallery-dl：pip install gallery-dl"))
                return

            def prog(done, total, text=""):
                self.queue.put(("probe", f"备用解析器（gallery-dl）列文件: {done}"))

            items = list_files(url, limit=500, proxy=proxy, progress_cb=prog)
            if not items:
                (note("gallery-dl 未发现额外资源")
                 if merge_only else
                 fail("内置发现失败，备用解析器也未找到资源"))
                return
            resources: list[Resource] = []
            for it in items:
                ext = (it.get("ext") or "").lower()
                kind = "image" if ext in _IMAGE_EXTS else (
                    "video" if ext in _VIDEO_EXTS else "file")
                r = Resource(it["url"], page_url=url, title="(备用解析器)",
                             preview_url=it.get("preview") or "",
                             raw_url=it.get("page") or "",
                             name=it.get("name") or "", size=it.get("size") or 0)
                r.kind = kind
                r.category = {"image": "images", "video": "videos"}.get(kind, "others")
                r.width = it.get("width") or 0
                r.height = it.get("height") or 0
                r._backup = True
                resources.append(r)

            if merge_only:
                resources = self._dedupe_backup(resources)
                if not resources:
                    note("gallery-dl 补充完成：无新增资源")
                    return
                self.queue.put(("discovered_backup", resources, url, True))
            else:
                self.queue.put(("discovered_backup", resources, url, False))
        except Exception as exc:
            import traceback
            traceback.print_exc()
            if merge_only:
                note(f"gallery-dl 补充失败：{str(exc)[:120]}")
            else:
                fail(f"备用解析器失败：{exc}")

    @staticmethod
    def _id_token(url: str) -> str | None:
        """从 URL 尾部取数字段（≥5 位，作媒体 id 判重用）。"""
        parts = [p for p in url.split("/") if p]
        for p in reversed(parts):
            if p.isdigit() and len(p) >= 5:
                return p
        return None

    def _dedupe_backup(self, resources: list) -> list:
        """合并模式下按 URL / 文件名 / 媒体 id 去重，避免与内置结果重复。"""
        existing = list(self.resources.values())
        exist_urls = {r.url for r in existing}
        exist_names = {(os.path.basename(r.name or r.url or "") or "").lower()
                       for r in existing}
        exist_ids = {tok for r in existing if (tok := self._id_token(r.url or ""))}
        out: list[Resource] = []
        for r in resources:
            url = r.url or ""
            base = (os.path.basename(url) or "").lower()
            if url in exist_urls or base in exist_names:
                continue
            tok = self._id_token(url)
            if tok and tok in exist_ids:
                continue
            if base:
                exist_names.add(base)
            if tok:
                exist_ids.add(tok)
            out.append(r)
        return out

    # ---------------------------------------------------------------- 下载
    def download_selected(self):
        selected = [r for r in self.resources.values() if r._checked]
        selected = [r for r in selected if self._passes_size_filter(r)]
        if not selected:
            messagebox.showinfo("提示", "请先勾选要下载的内容")
            return
        outdir = self.outdir_var.get().strip() or INFORMATION_DIR
        self.busy = True
        self.download_btn.config(state=tk.DISABLED)
        self.discover_btn.config(state=tk.DISABLED)
        self.refresh_btn.config(state=tk.DISABLED)
        self.api_btn.config(state=tk.DISABLED)
        self._api_dialog_busy(True)
        self.status_var.set(f"正在下载 {len(selected)} 个资源 -> {outdir}")
        self.progress.config(mode="determinate", maximum=len(selected), value=0)
        backup_res = [r for r in selected if getattr(r, "_backup", False)]
        normal_res = [r for r in selected if not getattr(r, "_backup", False)]
        proxy = config.DEFAULT_PROXY if self.proxy_var.get() else None
        threading.Thread(
            target=self._mixed_download_worker,
            args=(backup_res, normal_res, outdir, proxy), daemon=True).start()

    def _mixed_download_worker(self, backup_res, normal_res, outdir, proxy):
        """混合下载：备用解析器项走 gallery-dl，普通项走内置分片下载。"""
        total_ok = 0
        if backup_res:
            try:
                from gallery_backup import GalleryDownload
            except ImportError:
                self.queue.put(("error", "缺少 gallery_backup 模块"))
                return
            urls = list(dict.fromkeys(r.url for r in backup_res))
            self.queue.put(("backup_indeterminate",))
            got: dict = {}
            ev = threading.Event()

            def dlog(line):
                self.queue.put(("backup_log", line))

            def ddone(saved: int, odir: str):
                got["saved"] = saved
                ev.set()

            def derr(m: str):
                got["err"] = m
                ev.set()

            gd = GalleryDownload(urls, outdir, log_cb=dlog, done_cb=ddone,
                                 error_cb=derr, proxy=proxy)
            gd.start()
            ev.wait(timeout=3600)
            total_ok += got.get("saved", 0)
            if got.get("err") and got.get("err") != "已取消":
                self.queue.put(("backup_log", f"备用下载出错：{got['err']}"))
        if normal_res:
            try:
                try:
                    workers = max(1, int(self.workers_var.get() or config.DOWNLOAD_WORKERS))
                except (ValueError, TypeError):
                    workers = config.DOWNLOAD_WORKERS
                if self.session is None:
                    self.session = FetchSession(
                        impersonate=self.imp_var.get() or config.IMPERSONATE,
                        proxy_enabled=self.proxy_var.get(),
                    )
                dl = Downloader(outdir, session=self.session, workers=workers,
                            filename_template=self.tmpl_var.get())
                failed_names: list[str] = []

                def prog(done, total, name, ok):
                    if not ok and name not in failed_names:
                        failed_names.append(name)
                    self.queue.put(("download", done, total, name, ok))
                dl.start(normal_res, progress_cb=prog)
                total_ok += dl.stat.downloaded
                msg = f"完成：{dl.stat.downloaded} 成功, {dl.stat.failed} 失败  -> {outdir}"
                if dl.failures and dl.stat.failed > 0:
                    reasons = sorted({str(v) for v in dl.failures.values()})
                    shown = reasons[:3]
                    more = f" 等 {len(reasons)} 类" if len(reasons) > 3 else ""
                    msg += "\n失败原因：" + " | ".join(shown) + more
                    msg += "\n失败列表已写入 failures.json，下次可点「重试失败」重新勾选"
                elif failed_names and dl.stat.failed > 0:
                    shown = failed_names[:3]
                    more = f" 等 {len(failed_names)} 个" if len(failed_names) > 3 else ""
                    msg += "\n失败原因示例：" + " | ".join(shown) + more
                self.queue.put(("done", msg, outdir, total_ok))
                log.info("下载完成 成功=%d 失败=%d 目录=%s",
                         dl.stat.downloaded, dl.stat.failed, outdir)
                if dl.failures:
                    log.warning("下载失败明细 %s",
                                {k: str(v)[:120] for k, v in dl.failures.items()})
                return
            except Exception as exc:
                import traceback
                traceback.print_exc()
                log.exception("下载流程异常 url=%s", outdir)
                self.queue.put(("error", f"下载出错：{exc}"))
                return
        self.queue.put(("done", f"完成：保存 {total_ok} 个文件 -> {outdir}",
                        outdir, total_ok))

    def backup_download(self):
        """备用下载：先用 gallery-dl 列出候选文件，弹出预览勾选，再下载选中项。

        覆盖 1400+ 网站（超出内置发现器范围时使用）。
        流程：设置弹窗(数量/最小大小/类型) → gallery-dl 列 URL + 探测大小
              → 预览勾选弹窗(文件名+大小) → 下载勾选项（可取消）。
        """
        if self.busy:
            return
        try:
            from gallery_backup import is_available
        except ImportError:
            messagebox.showerror("备用下载不可用", "缺少 gallery_backup 模块")
            return
        if not is_available():
            messagebox.showerror(
                "gallery-dl 未安装",
                "备用下载需要 gallery-dl。\n请先安装：pip install gallery-dl",
            )
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "请输入网址")
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
            self.url_var.set(url)
        outdir = self.outdir_var.get().strip() or INFORMATION_DIR
        opts = self._ask_backup_options()
        if opts is None:  # 用户取消
            return
        limit, min_kb, kind_filter = opts
        self._backup_cancel.clear()
        self.busy = True
        self.discover_btn.config(state=tk.DISABLED)
        self.refresh_btn.config(state=tk.DISABLED)
        self.download_btn.config(state=tk.DISABLED)
        self.api_btn.config(state=tk.DISABLED)
        self._api_dialog_busy(True)
        self.backup_btn.config(state=tk.NORMAL, text="取消备用下载",
                               command=self._cancel_backup)
        kind_txt = {"image": "仅图片", "video": "仅视频"}.get(kind_filter, "全部")
        limit_txt = f"最多 {limit} 个" if limit else "不限数量"
        probe_txt = f" / 最小 {min_kb}KB" if min_kb else ""
        self.status_var.set(
            f"正在解析 {url}（{kind_txt} / {limit_txt}{probe_txt}）...")
        self.progress.config(mode="determinate", maximum=100, value=0)

        proxy = config.DEFAULT_PROXY if self.proxy_var.get() else None
        threading.Thread(
            target=self._browse_worker,
            args=(url, limit, min_kb, kind_filter, proxy, outdir),
            daemon=True,
        ).start()

    def _browse_worker(self, url, limit, min_kb, kind_filter, proxy, outdir):
        """后台：gallery-dl 列 URL + 探测大小。完成后把候选列表交给 GUI 预览。"""
        from gallery_backup import list_files
        min_bytes = max(0, int(min_kb or 0)) * 1024

        def progress(done, total):
            self.queue.put(("backup_probe", done, total, f"正在探测文件大小 {done}/{total} ..."))

        try:
            items = list_files(
                url, limit=limit or 500, kind_filter=kind_filter,
                min_size=min_bytes, proxy=proxy, progress_cb=progress,
                cancel_event=self._backup_cancel, timeout=180,
            )
        except FileNotFoundError:
            self.queue.put(("backup_err", "未安装 gallery-dl"))
            return
        except RuntimeError as exc:
            self.queue.put(("backup_err", f"无法解析该页面：{exc}"))
            return
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self.queue.put(("backup_err", f"解析出错：{exc}"))
            return
        if self._backup_cancel.is_set():
            return
        self.queue.put(("backup_probed", items, outdir))

    def _ask_backup_options(self):
        """弹窗询问备用下载参数。返回 (数量上限, 最小大小KB, 类型)，取消返回 None。"""
        dlg = tk.Toplevel(self.root)
        dlg.title("备用下载设置")
        dlg.geometry("460x320")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        result: list = [None, None, None]

        ttk.Label(dlg, text="网址", padding=(16, 12, 0, 0)).pack(anchor="w")
        ttk.Label(dlg, text=self.url_var.get()[:80], wraplength=420,
                  foreground="#666").pack(anchor="w", padx=16)

        ttk.Label(dlg, text="最多下载数量（0 = 不限）:", padding=(16, 10, 0, 0)).pack(anchor="w")
        limit_var = tk.StringVar(value="30")
        ttk.Spinbox(dlg, from_=0, to=500, width=10, textvariable=limit_var).pack(
            anchor="w", padx=16, pady=(2, 0))

        ttk.Label(dlg, text="最小文件大小 KB（0 = 不限）:", padding=(16, 10, 0, 0)).pack(anchor="w")
        min_var = tk.StringVar(value="0")
        ttk.Spinbox(dlg, from_=0, to=100000, width=10, textvariable=min_var).pack(
            anchor="w", padx=16, pady=(2, 0))

        ttk.Label(dlg, text="文件类型:", padding=(16, 10, 0, 0)).pack(anchor="w")
        kind_var = tk.StringVar(value="all")
        row = ttk.Frame(dlg)
        row.pack(anchor="w", padx=16)
        for value, label in (("all", "全部"), ("image", "仅图片"), ("video", "仅视频")):
            ttk.Radiobutton(row, text=label, value=value, variable=kind_var).pack(
                side=tk.LEFT, padx=4)

        def on_ok():
            try:
                n = int(limit_var.get().strip() or 0)
                m = int(min_var.get().strip() or 0)
            except ValueError:
                messagebox.showerror("输入错误", "数量与大小必须是数字")
                return
            if n < 0 or n > 500:
                messagebox.showerror("输入错误", "数量范围为 0~500")
                return
            if m < 0:
                messagebox.showerror("输入错误", "大小范围需 ≥ 0")
                return
            result[0] = n or None
            result[1] = m or None
            result[2] = None if kind_var.get() == "all" else kind_var.get()
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.pack(side=tk.BOTTOM, fill=tk.X, pady=10)
        ttk.Button(btns, text="确定并开始解析", command=on_ok).pack(side=tk.RIGHT, padx=12)
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side=tk.RIGHT, padx=4)
        self.root.wait_window(dlg)
        if result[0] is None and result[1] is None and result[2] is None:
            return None
        return result[0], result[1], result[2]

    def _ask_backup_pick(self, items, outdir):
        """预览勾选弹窗。返回选中的 URL 列表；取消返回 None。"""
        dlg = tk.Toplevel(self.root)
        dlg.title("备用下载 - 选择要下载的文件")
        dlg.geometry("660x480")
        dlg.transient(self.root)
        dlg.grab_set()
        result: list = [None]

        seen_name: dict[str, int] = {}
        for it in items:
            seen_name[it["name"]] = seen_name.get(it["name"], 0) + 1

        cols = ("pick", "name", "size")
        tree = ttk.Treeview(dlg, columns=cols, show="headings", selectmode="extended")
        tree.heading("pick", text="勾选")
        tree.heading("name", text="文件名")
        tree.heading("size", text="大小")
        tree.column("pick", width=48, anchor="center")
        tree.column("name", width=440)
        tree.column("size", width=90, anchor="e")
        vsb = ttk.Scrollbar(dlg, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        dlg.rowconfigure(0, weight=1)
        dlg.columnconfigure(0, weight=1)

        for idx, it in enumerate(items):
            nm = it["name"]
            if seen_name[nm] > 1:
                nm = f"{nm}  (#{idx + 1})"
            checked = idx < 20
            tree.insert("", "end", iid=str(idx),
                        values=("\u2714" if checked else "", nm, _fmt_size(it["size"])))

        sel_set = {str(i) for i in range(min(20, len(items)))}  # 默认勾选前 20

        def total_b():
            return sum(int(items[int(i)]["size"] or 0)
                       for i in sel_set if i.isdigit() and int(i) < len(items))

        def refresh():
            cd.config(text=f"已勾选 {len(sel_set)} 个（共 {_fmt_size(total_b())}）")

        def toggle(iid):
            vals = list(tree.item(iid, "values"))
            if vals[0] == "\u2714":
                vals[0] = ""
                sel_set.discard(iid)
            else:
                vals[0] = "\u2714"
                sel_set.add(iid)
            tree.item(iid, values=vals)
            refresh()

        def on_click(e=None):
            iid = tree.identify_row(e.y)
            if iid:
                toggle(iid)

        def on_key(e=None):
            for iid in tree.selection():
                toggle(iid)
            return "break"

        def toggle_all():
            any_on = any(tree.item(i, "values")[0] == "\u2714" for i in tree.get_children())
            for i in tree.get_children():
                tree.item(i, values=("\u2714" if not any_on else "", *tree.item(i, "values")[1:]))
            sel_set.clear()
            if not any_on:
                sel_set.update(tree.get_children())
            refresh()

        tree.bind("<ButtonRelease-1>", on_click)
        tree.bind("<space>", on_key)

        info = tk.Frame(dlg)
        info.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=6)
        cd = ttk.Label(info, text="")
        cd.pack(side=tk.LEFT)
        ttk.Button(info, text="全选/清空", command=toggle_all).pack(side=tk.LEFT, padx=6)
        ttk.Label(info, text="单击勾选，Ctrl/Shift 多选",
                  foreground="#888").pack(side=tk.LEFT, padx=4)

        def on_download():
            urls = [items[int(i)]["url"] for i in sel_set if i.isdigit() and int(i) < len(items)]
            if not urls:
                messagebox.showwarning("提示", "请先勾选要下载的文件")
                return
            result[0] = urls
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Button(btns, text=f"开始下载（保存到 {outdir}）",
                   command=on_download).pack(side=tk.RIGHT, padx=12)
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side=tk.RIGHT, padx=4)
        refresh()

        self.root.wait_window(dlg)
        return result[0]

    def _cancel_backup(self):
        """取消进行中的备用下载（解析或下载阶段）。"""
        self._backup_cancel.set()
        if self.gallery is not None:
            self.gallery.cancel()
        self.status_var.set("正在取消备用下载…")
        self.backup_btn.config(state=tk.DISABLED)

    def _reset_backup_btn(self):
        """恢复备用下载按钮为初始状态。"""
        try:
            self.backup_btn.config(state=tk.NORMAL, text="备用下载(gallery-dl)",
                                   command=self.backup_download)
        except Exception:
            pass

    # ---------------------------------------------------------------- 列表
    def apply_size_filter(self):
        """按 UI 里的大小范围过滤当前列表（并支持重新发现时沿用）。"""
        try:
            self._min_size_filter = int(self.min_size_var.get().strip() or 0)
            self._max_size_filter = int(self.max_size_var.get().strip() or 0)
        except ValueError:
            messagebox.showerror("输入错误", "大小范围必须是数字（单位 KB）")
            return
        if self._min_size_filter < 0 or self._max_size_filter < 0:
            messagebox.showerror("输入错误", "大小范围不能为负数")
            return
        if self._max_size_filter and self._max_size_filter < self._min_size_filter:
            messagebox.showerror("输入错误", "最大值需 >= 最小值")
            return
        self.refresh_list()
        self.status_var.set(
            f"已按大小过滤：{self._min_size_filter}KB ~ "
            f"{self._max_size_filter if self._max_size_filter else '不限'}KB")

    def _passes_size_filter(self, res: Resource) -> bool:
        """判断资源是否通过 UI 大小范围过滤（0=不限）。"""
        lo = getattr(self, "_min_size_filter", 0)
        hi = getattr(self, "_max_size_filter", 0)
        size_kb = res.size / 1024 if res.size else 0
        if lo and size_kb and size_kb < lo:
            return False
        if hi and size_kb > hi:
            return False
        return True

    def _clear_list(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)

    def refresh_list(self):
        self._clear_list()
        want_kind = self.filter_kind.get()
        want_cat = self.filter_cat.get()
        items = [r for r in self.resources.values()
                 if (want_kind == "all" or r.kind == want_kind)
                 and (want_cat == "all" or r.category == want_cat)
                 and self._passes_size_filter(r)]
        for res in self._sorted(items):
            self._insert_row(res)

    def _add_resource_to_list(self, r: Resource):
        """探测完成的单个资源加入列表并立即显示（流式上屏）。

        资源先注册到 self.resources（下载/勾选依赖），再按当前过滤/排序插入一行；
        过滤视图下已注册但不可见的资源，等下次 refresh_list 时正常出现。
        """
        r._checked = False
        self.resources[str(id(r))] = r
        try:
            want_kind = self.filter_kind.get()
            want_cat = self.filter_cat.get()
        except Exception:
            want_kind = want_cat = "all"
        if (want_kind != "all" and r.kind != want_kind) \
                or (want_cat != "all" and r.category != want_cat) \
                or not self._passes_size_filter(r):
            return
        self._insert_row(r)

    def _sorted(self, items: list) -> list:
        """按当前排序方式排列表项（未知分辨率/大小排末尾）。"""
        mode = getattr(self, "sort_key", None)
        mode = mode.get() if mode else "默认"
        if mode == "分辨率":
            return sorted(items,
                          key=lambda r: (r.width * r.height if r.width and r.height else -1),
                          reverse=True)
        if mode == "文件大小":
            return sorted(items, key=lambda r: (r.size if r.size else -1), reverse=True)
        if mode == "名称":
            return sorted(items, key=lambda r: r.name.lower())
        return list(items)

    def _insert_row(self, res: Resource):
        iid = str(id(res))
        check = "☑" if getattr(res, "_checked", False) else "☐"
        size = pct_size(res.size)
        info = KIND_LABELS[res.kind]
        if size:
            info += f" · {size}"
        if res.width and res.height:
            info += f" · {res.width}×{res.height}"
            ratio = _aspect_ratio(res.width, res.height)
            if ratio:
                info += f" ({ratio})"
        self.tree.insert("", "end", iid=iid, text=res.name, values=(check, res.name, info),
                         tags=(res.kind,))

    def _on_tree_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        res = self.resources.get(iid)
        if not res:
            return
        state = event.state             # 0x1=Shift, 0x4=Ctrl
        if state & 0x1:                 # Shift：范围复选（锚点 -> 当前行）
            self._range_check(iid)
            return
        if state & 0x4:                 # Ctrl：单独反转该项，不动锚点
            self._toggle_check(iid, set_anchor=False)
            return
        self._toggle_check(iid)         # 普通点击即勾选（点整行都算，不用点小方框）

    def _toggle_check(self, iid, set_anchor: bool = True):
        res = self.resources.get(iid)
        if not res:
            return
        res._checked = not getattr(res, "_checked", False)
        self.tree.set(iid, column="checked", value="☑" if res._checked else "☐")
        if set_anchor:
            self._sel_anchor = iid

    def _range_check(self, iid):
        """Shift 点击：锚点到当前行的整段区间统一勾选/取消（按点击行状态）。"""
        rows = self.tree.get_children()
        if not rows:
            return
        anchor = self._sel_anchor if self._sel_anchor in rows else rows[0]
        try:
            a, b = rows.index(anchor), rows.index(iid)
        except ValueError:
            return
        lo, hi = (a, b) if a <= b else (b, a)
        res = self.resources.get(iid)
        target = not getattr(res, "_checked", False) if res else True
        for rid in rows[lo:hi + 1]:
            r = self.resources.get(rid)
            if r:
                r._checked = target
                self.tree.set(rid, column="checked", value="☑" if target else "☐")
        self._sel_anchor = iid

    def _on_tree_right(self, event):
        """右键菜单：预览/类型勾选/反选/复制等快捷操作。"""
        iid = self.tree.identify_row(event.y)
        res = self.resources.get(iid) if iid else None
        menu = tk.Menu(self.root, tearoff=0)
        if res:
            menu.add_command(
                label="预览",
                command=lambda: self._show_preview(res, iid))
            menu.add_command(
                label="放大/播放" if res.kind != "image" else "查看高清原图",
                command=(lambda: self._show_vlc_player(res)) if res.kind != "image"
                else (lambda: self._show_large_window(res)))
            menu.add_separator()
            menu.add_command(label="勾选此项", command=lambda: self._toggle_check(iid))
            menu.add_command(label="全选当前列表", command=lambda: self.set_all(True))
            menu.add_command(label="取消全选", command=lambda: self.set_all(False))
            menu.add_command(label="反选", command=self.invert_select)
            menu.add_separator()
            menu.add_command(label="按类型全选图片", command=lambda: self.select_kind("image"))
            menu.add_command(label="按类型全选视频", command=lambda: self.select_kind("video"))
            menu.add_command(label="按类型全选文件", command=lambda: self.select_kind("file"))
            menu.add_separator()
            menu.add_command(label="复制勾选链接", command=self.copy_links)
            menu.add_command(label="打开页面", command=self.open_page)
        else:
            menu.add_command(label="全选当前列表", command=lambda: self.set_all(True))
            menu.add_command(label="取消全选", command=lambda: self.set_all(False))
            menu.add_command(label="反选", command=self.invert_select)
            menu.add_separator()
            menu.add_command(label="按类型全选图片", command=lambda: self.select_kind("image"))
            menu.add_command(label="按类型全选视频", command=lambda: self.select_kind("video"))
            menu.add_command(label="按类型全选文件", command=lambda: self.select_kind("file"))
        menu.tk_popup(event.x_root, event.y_root)

    def invert_select(self):
        """反选：所有资源勾选状态取反。"""
        for res in self.resources.values():
            res._checked = not getattr(res, "_checked", False)
        self.refresh_list()
        self.status_var.set(f"已反选，{sum(1 for r in self.resources.values() if r._checked)} 项已勾选")

    def _on_tree_double(self, event):
        """双击：图片看原图，视频等用万能播放器在线播放。"""
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        res = self.resources.get(iid)
        if res:
            if res.kind == "image":
                self._show_large_window(res)
            else:
                self._show_vlc_player(res)
    def _thumb_worker(self):
        while True:
            res: Resource = self.thumb_queue.get()
            if res is None:
                break
            photo = load_thumb(res)
            self.thumb_results.put((id(res), photo))

    def _set_idle_buttons(self):
        """恢复所有主按钮为可用状态（发现/下载/API 获取共用）。"""
        self.busy = False
        self.discover_btn.config(state=tk.NORMAL)
        self.refresh_btn.config(state=tk.NORMAL)
        self.download_btn.config(state=tk.NORMAL)
        self.api_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self._api_dialog_busy(False)

    # ---------------------------------------------------------------- 轮询
    def _poll_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == "probe":
                    self.status_var.set(msg[1])
                elif kind == "follow_tick":
                    self.status_var.set(msg[1])
                elif kind == "follow_done":
                    _, ok_pages = msg
                    self.status_var.set(
                        f"定时跟进：本轮完成（成功 {ok_pages} 页），新资源已并入列表；"
                        f"存档命中的旧资源已自动跳过")
                elif kind == "res_item":
                    _, r = msg
                    self._add_resource_to_list(r)
                elif kind == "discover_failed":
                    _, err, url = msg
                    self.status_var.set(
                        f"发现失败（{err}），正在切换备用解析器（gallery-dl）...")
                    self.progress.config(mode="indeterminate")
                    self.progress.start(12)
                    proxy = config.DEFAULT_PROXY if self.proxy_var.get() else None
                    threading.Thread(
                        target=self._backup_fallback_worker, args=(url, proxy),
                        daemon=True).start()
                elif kind == "discovered_backup":
                    _, resources, url = msg[:3]
                    merged = len(msg) > 3 and bool(msg[3])
                    if not merged:
                        self.progress.stop()
                        self.progress.config(mode="determinate", value=0)
                        self._set_idle_buttons()
                        self.backup_btn.config(state=tk.NORMAL)
                    if merged:
                        self.seen_urls.update(r.url for r in resources)
                    else:
                        self.seen_urls = {r.url for r in resources}
                    for r in resources:
                        self.resources[str(id(r))] = r
                        r._checked = False
                    self.refresh_list()
                    if merged:
                        self.status_var.set(
                            f"发现共 {len(self.resources)} 个资源"
                            f"（gallery-dl 自动补充 {len(resources)} 个）")
                        self.progress.stop()
                        self.progress.config(mode="determinate", value=0)
                        self._set_idle_buttons()
                    else:
                        self.status_var.set(
                            f"发现 {len(resources)} 个资源（备用解析器 gallery-dl）")
                    self.thumb_queue_inner = [r for r in resources[:40]]
                elif kind == "backup_note":
                    self.status_var.set(msg[1] if len(msg) > 1 else "")
                elif kind == "discovered":
                    _, resources, title, filtered, stopped = msg[:5]
                    for r in resources:
                        if str(id(r)) not in self.resources:
                            self.resources[str(id(r))] = r
                            r._checked = False
                    self.refresh_list()
                    if stopped:
                        self.status_var.set(
                            f"已停止，保留已发现的 {len(self.resources)} 个资源（{title}）")
                    elif filtered:
                        self.status_var.set(f"发现 {len(resources)} 个资源（{title}），已自动过滤 {filtered} 个图标/极小文件")
                    else:
                        self.status_var.set(f"发现 {len(resources)} 个资源（{title}）")
                    self.progress.stop()
                    self.progress.config(mode="determinate", value=0)
                    self.stop_btn.config(state=tk.DISABLED)
                    self._set_idle_buttons()
                    # 首批缩略图（前 40 张，避免网络洪峰）
                    self.thumb_queue_inner = [r for r in resources[:40]]
                elif kind == "api_discovered":
                    _, resources, info = msg
                    self.seen_urls = {r.url for r in resources}
                    for r in resources:
                        self.resources[str(id(r))] = r
                        r._checked = False
                    self.refresh_list()
                    text = f"API 获取完成：{len(resources)} 个资源（{info.get('label', '')}"
                    if info.get("pages"):
                        text += f" · 共 {info['pages']} 页"
                    if info.get("total"):
                        text += f" · 全部结果 {info['total']} 条"
                    if info.get("quota"):
                        text += f" · 剩余配额 {info['quota']}"
                    self.status_var.set(text + "）")
                    self._api_dialog_quota(info.get("quota", "") or "")
                    self.progress.stop()
                    self.progress.config(mode="determinate", value=0)
                    self._set_idle_buttons()
                    self.thumb_queue_inner = [r for r in resources[:40]]
                elif kind == "api_failed":
                    self.status_var.set(f"API 获取失败：{msg[1]}")
                    self.progress.stop()
                    self.progress.config(mode="determinate", value=0)
                    self._set_idle_buttons()
                    messagebox.showerror("API 获取失败", msg[1])
                elif kind == "download":
                    _, done, total, name, ok = msg
                    self.progress.config(maximum=total, value=done)
                    self.status_var.set(f"下载中 {done}/{total}: {name} ({'成功' if ok else '失败'})")
                elif kind == "done":
                    _, msg, outdir, dl_count = msg
                    self.status_var.set(msg)
                    self.progress.config(mode="determinate", value=0)
                    self._set_idle_buttons()
                    self.backup_btn.config(state=tk.NORMAL)
                    if dl_count > 0:
                        self._show_download_done(outdir, dl_count)
                elif kind == "backup_log":
                    self.status_var.set(msg[1])
                elif kind == "backup_indeterminate":
                    self.progress.config(mode="indeterminate")
                    self.progress.start(12)
                elif kind == "backup_probe":
                    _, done_n, total_n, text = msg
                    self.status_var.set(text)
                    if total_n > 0:
                        self.progress.config(maximum=total_n, value=done_n)
                elif kind == "backup_probed":
                    _, items, odir = msg
                    self.progress.config(mode="determinate", value=0)
                    if not items:
                        self.status_var.set("没有找到可下载的文件")
                        self._set_idle_buttons()
                        self._reset_backup_btn()
                        break
                    picked = self._ask_backup_pick(items, odir)
                    if not picked or self._backup_cancel.is_set():
                        self.status_var.set("已取消")
                        self._set_idle_buttons()
                        self._reset_backup_btn()
                        break
                    self.status_var.set(
                        f"备用下载中：已勾选 {len(picked)} 个文件 -> {odir} ...")
                    self.progress.config(mode="indeterminate")
                    self.progress.start(12)

                    def log(line):
                        self.queue.put(("backup_log", line))

                    def done(saved: int, _odir: str):
                        self.queue.put(("backup_done", saved, _odir))

                    def err(msg2: str):
                        self.queue.put(("backup_err", msg2))

                    from gallery_backup import GalleryDownload
                    proxy = config.DEFAULT_PROXY if self.proxy_var.get() else None
                    self.gallery = GalleryDownload(
                        picked, odir, log_cb=log, done_cb=done, error_cb=err,
                        proxy=proxy,
                    )
                    self.gallery.start()
                elif kind == "backup_done":
                    _, saved, odir = msg
                    self.progress.stop()
                    self.progress.config(mode="determinate", value=0)
                    self._set_idle_buttons()
                    self._reset_backup_btn()
                    if saved > 0:
                        self._show_download_done(odir, saved)
                elif kind == "backup_err":
                    self.progress.stop()
                    self.progress.config(mode="determinate", value=0)
                    self._set_idle_buttons()
                    self._reset_backup_btn()
                    err_text = msg[1] if len(msg) > 1 else "解析失败"
                    self.status_var.set(f"备用下载失败：{err_text}")
                    messagebox.showerror("备用下载失败", err_text)
                elif kind == "error":
                    self.status_var.set(f"错误: {msg[1]}")
                    self.progress.stop()
                    self._set_idle_buttons()
                    self.backup_btn.config(state=tk.NORMAL)
                    messagebox.showerror("错误", msg[1])
        except queue.Empty:
            pass
        self.root.after(150, self._poll_queue)

    def _poll_thumbs(self):
        # 将新发现资源加入缩略图加载队列（限制初始数量避免拖慢界面）
        try:
            while True:
                res = self.thumb_queue_inner.pop(0)
                self.thumb_queue.put(res)
        except (AttributeError, IndexError):
            pass
        try:
            while True:
                obj_id, photo = self.thumb_results.get_nowait()
                self.thumb_cache[obj_id] = photo
                iid = str(obj_id)
                if self.tree.exists(iid):
                    self.tree.item(iid, image=photo)
                if obj_id == self.current_preview_id:
                    self.preview_label.config(image=photo, text="")
        except queue.Empty:
            pass
        self.root.after(150, self._poll_thumbs)

    def set_all(self, checked: bool):
        for res in self.resources.values():
            res._checked = checked
        self.refresh_list()

    def select_kind(self, kind: str):
        """勾选当前列表中所有指定类型资源（图片/视频/文件）。"""
        n = 0
        for res in self.resources.values():
            if res.kind == kind:
                res._checked = True
                n += 1
        self.refresh_list()
        self.status_var.set(f"已勾选 {n} 个{KIND_LABELS.get(kind, kind)}资源")

    def _show_preview(self, res, iid):
        self.current_preview_id = id(res)
        thumb = self.thumb_cache.get(id(res))
        self.preview_label.config(image=thumb if thumb else make_placeholder(res.kind), text="")
        kind_txt = KIND_LABELS[res.kind]
        self.preview_name.set(f"[{kind_txt}] {res.name}\n{res.url}\n（双击列表项可查看高清大图）")

    def _show_download_done(self, outdir: str, count: int):
        """下载完成后弹窗提示保存位置，并提供「打开文件夹」。"""
        box = messagebox.askyesno(
            "下载完成",
            f"已成功下载 {count} 个文件\n保存位置：\n{outdir}\n\n是否现在打开文件夹查看？",
            icon="info",
        )
        if box:
            try:
                os.startfile(outdir)
            except OSError:
                messagebox.showerror("提示", f"无法打开目录：{outdir}")

    def _show_vlc_player(self, res: Resource):
        """用 VLC 万能播放器播放视频/音频/文件。

        小视频（≤ PLAYER_CACHE_MB）先走内置代理下载到临时文件再本地播放
        （不卡顿、可拖动进度）；大视频流播（VLC 大缓冲 + 携带页面 Referer）。
        音频（mp3/flac/…）走纯音频窗口（无视频区）。
        """
        _AUDIO_EXTS = ("mp3", "wav", "flac", "m4a", "aac", "ogg", "opus",
                       "wma", "ape", "aiff", "mid", "midi")
        try:
            from player_vlc import VLCEmbeddedPlayer, VLCUnavailableError
        except ImportError:
            messagebox.showerror("播放器不可用", "未安装 VLC 播放组件（python-vlc）。\n请先安装：pip install python-vlc")
            return
        try:
            import tempfile
            raw_uri = res.raw_url or res.url
            audio = raw_uri.split("?", 1)[0].rsplit(".", 1)[-1].lower() in _AUDIO_EXTS
            win = tk.Toplevel(self.root)
            win.title(f"播放 - {res.name}")
            win.geometry("560x210" if audio else "880x560")
            container = ttk.Frame(win)
            container.pack(fill=tk.BOTH, expand=True)
            lbl = None
            if not audio:
                vframe = ttk.Frame(container)
                vframe.pack(fill=tk.BOTH, expand=True)
                lbl = ttk.Label(vframe, text="正在加载视频…", anchor="center")
                lbl.pack(fill=tk.BOTH, expand=True)

            st = {"cache_path": None, "progress": None, "playing": False}
            # 主线程预捕获配置（worker 线程禁止碰 tkinter 变量）
            proxy = config.DEFAULT_PROXY if self.proxy_var.get() else None
            play_imp = self.imp_var.get() or config.IMPERSONATE
            play_proxy_on = self.proxy_var.get()

            def _poll_cache_progress():
                """主线程轮询缓存进度（worker 线程禁止碰 tkinter）。"""
                p = st["progress"]
                if p and not st["playing"] and lbl and lbl.winfo_exists():
                    lbl.config(text=f"正在缓存视频 {pct_size(p[0])} / {pct_size(p[1])}"
                                    f"（缓存后本地播放更流畅）…")
                if not st["playing"] and win.winfo_exists():
                    win.after(200, _poll_cache_progress)
            if not audio:
                _poll_cache_progress()

            def cache_to_local(total: int, fs) -> str | None:
                """下载到临时文件；成功返回路径，失败返回 None。"""
                tmp = os.path.join(
                    tempfile.gettempdir(),
                    f"play_{os.getpid()}_{int(time.time() * 1000)}.cached")
                try:
                    got = 0
                    media = res.raw_url or res.url
                    with open(tmp, "wb") as f:
                        for chunk, _tl in fs.iter_content(
                                media, headers={"Referer": res.page_url},
                                chunk_size=262144, timeout=120):
                            if chunk:
                                f.write(chunk)
                                got += len(chunk)
                                st["progress"] = (got, total)
                except Exception:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    return None
                st["cache_path"] = tmp
                return tmp

            def prepare() -> str:
                """返回播放目标：缓存好的本地文件，或原始在线 URL。"""
                media = res.raw_url or res.url
                if res.kind != "video":
                    return media
                try:
                    fs = FetchSession(impersonate=play_imp,
                                      proxy_enabled=play_proxy_on)
                    _data, total, _ct = fs.read_prefix(media, 65536, timeout=15)
                    if total and total <= config.PLAYER_CACHE_MB * 1024 * 1024:
                        local = cache_to_local(total, fs)
                        if local:
                            return local
                except Exception:
                    return media
                return media

            def open_player():
                try:
                    player = VLCEmbeddedPlayer(None if audio else lbl, proxy=proxy,
                                               audio_only=audio)
                except VLCUnavailableError as exc:
                    win.after(0, lambda e=exc: messagebox.showerror("播放器不可用", str(e)))
                    return
                win._player = player  # 防止被 GC

                def _close():
                    player.release()
                    if st["cache_path"] and os.path.exists(st["cache_path"]):
                        try:
                            os.remove(st["cache_path"])
                        except OSError:
                            pass
                    win.destroy()

                win.protocol("WM_DELETE_WINDOW", _close)

                if audio:
                    meta = ttk.Label(container, text=res.name, anchor="center",
                                     wraplength=540)
                    meta.pack(fill=tk.X, padx=8, pady=(14, 2))
                    ttip = ttk.Label(container, text="加载中…", anchor="center")
                    ttip.pack(fill=tk.X, pady=2)
                    row = ttk.Frame(container)
                    row.pack(fill=tk.X, padx=8, pady=8)
                    btn_play = ttk.Button(row, text="暂停/继续", command=player.toggle)
                    btn_play.pack(side=tk.LEFT, padx=2)
                    ttk.Button(row, text="停止", command=player.stop).pack(side=tk.LEFT, padx=2)
                    scale = ttk.Scale(row, from_=0, to=1000,
                                      command=lambda v: player.set_position(float(v) / 1000))
                    scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
                    vol = ttk.Scale(row, from_=0, to=100,
                                    command=lambda v: player.volume(int(float(v))))
                    vol.set(80)
                    vol.pack(side=tk.RIGHT)

                    def _poll_audio():
                        try:
                            if player._duration > 0:
                                pos = player.position()
                                scale.set(int(pos * 1000))
                                ttip.config(
                                    text=f"{int(pos * player._duration)}s / {int(player._duration)}s"
                                         f"  -  {res.name}")
                        except Exception:
                            pass
                        if win.winfo_exists():
                            win.after(200, _poll_audio)

                    st["playing"] = True
                    win.after(0, lambda: player.play(raw_uri, referrer=res.page_url))
                    _poll_audio()
                    return

                uri = prepare()
                st["playing"] = True
                win.after(0, lambda: (lbl.config(text=""),
                                      player.play(uri, referrer=res.page_url)))
                controls = ttk.Frame(container)
                controls.pack(fill=tk.X, pady=4)
                btn_play = ttk.Button(controls, text="暂停/继续", command=player.toggle)
                btn_play.pack(side=tk.LEFT, padx=4)
                ttk.Button(controls, text="停止", command=player.stop).pack(side=tk.LEFT, padx=4)
                scale = ttk.Scale(controls, from_=0, to=1000, command=lambda v: player.set_position(float(v) / 1000))
                scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
                ttk.Button(controls, text="全屏", command=lambda: _fullscreen()).pack(side=tk.RIGHT, padx=4)

                def _poll():
                    try:
                        if player._duration > 0:
                            pos = player.position()
                            scale.set(int(pos * 1000))
                            win.title(f"播放中 {int(pos * player._duration)}s / {int(player._duration)}s - {res.name}")
                    except Exception:
                        pass
                    if win.winfo_exists():
                        win.after(200, _poll)
                _poll()

                def _on_resize():
                    try:
                        player.resize()
                    except Exception:
                        pass
                win.bind("<Configure>", lambda _e: _on_resize())

                def _fullscreen():
                    if win.attributes("-fullscreen"):
                        win.attributes("-fullscreen", False)
                    else:
                        win.attributes("-fullscreen", True)

            threading.Thread(target=open_player, daemon=True).start()
        except Exception as exc:
            messagebox.showerror("播放失败", str(exc))

    def _show_large_window(self, res: Resource):
        """弹出独立窗口显示原图：Ctrl+滚轮缩放，按住滚轮（中键）拖动平移。"""
        win = tk.Toplevel(self.root)
        win.title(f"大图预览 - {res.name}")
        win.geometry("760x600")
        canvas = tk.Canvas(win, bg="#1e1e1e", highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True)
        info_lbl = ttk.Label(win, text="加载中…", anchor="center")
        info_lbl.pack(side=tk.BOTTOM, fill=tk.X, pady=4)

        st = {"img": None, "photo": None, "iid": None, "zoom": 1.0,
              "pre_zoom": 1.0, "cx": 0.0, "cy": 0.0, "drag": None}

        def render(anchor=None):
            """按当前缩放渲染；anchor=(mx,my) 为缩放时保持不动的屏幕锚点。"""
            img = st["img"]
            w = max(1, int(round(img.width * st["zoom"])))
            h = max(1, int(round(img.height * st["zoom"])))
            try:
                photo = ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))
            except Exception:
                return
            st["photo"] = photo
            if anchor:
                mx, my = anchor
                ratio = st["zoom"] / st["pre_zoom"]
                st["cx"] = mx - (mx - st["cx"]) * ratio
                st["cy"] = my - (my - st["cy"]) * ratio
            else:
                st["cx"] = canvas.winfo_width() / 2
                st["cy"] = canvas.winfo_height() / 2
            if st["iid"] is None:
                st["iid"] = canvas.create_image(st["cx"], st["cy"], image=photo)
            else:
                canvas.itemconfig(st["iid"], image=photo)
                canvas.coords(st["iid"], st["cx"], st["cy"])
            info_lbl.config(
                text=f"原始 {img.width}x{img.height} · 显示 {w}x{h} · {st['zoom'] * 100:.0f}%"
                     f"  （Ctrl+滚轮缩放，按住滚轮拖动）")

        def fit():
            if not st["img"]:
                return
            cw, ch = canvas.winfo_width(), canvas.winfo_height()
            if cw <= 1 or ch <= 1:
                win.after(50, fit)
                return
            z = min(cw / st["img"].width, ch / st["img"].height, 1.0)
            st["zoom"] = max(z, 0.05)
            render()

        def load():
            try:
                rr = copy.copy(res)
                rr.preview_url = ""   # 大图窗口永远取高清原图，而非页面缩略图
                img = fetch_image(rr, None)
                st["img"] = img
                st["zoom"] = st["pre_zoom"] = 1.0
                win.after(0, fit)
            except Exception as exc:
                err = str(exc)
                win.after(0, lambda: info_lbl.config(text=f"加载失败：{err}"))

        def on_wheel(e):
            if not st["img"] or not (e.state & 0x0004):  # 仅 Ctrl+滚轮
                return
            st["pre_zoom"] = st["zoom"]
            st["zoom"] = min(max(st["zoom"] * (1.25 if e.delta > 0 else 0.8), 0.05), 20.0)
            render(anchor=(e.x, e.y))
            return "break"

        def on_press(e):
            if e.num == 2 or (e.num == 1 and e.state & 0x0002):  # 滚轮中键
                st["drag"] = (e.x, e.y)

        def on_drag(e):
            if st["drag"] and st["iid"] is not None:
                st["cx"] += e.x - st["drag"][0]
                st["cy"] += e.y - st["drag"][1]
                canvas.coords(st["iid"], st["cx"], st["cy"])
                st["drag"] = (e.x, e.y)

        def on_release(_e):
            st["drag"] = None

        canvas.bind("<MouseWheel>", on_wheel)
        canvas.bind("<ButtonPress-2>", on_press)
        canvas.bind("<B2-Motion>", on_drag)
        canvas.bind("<ButtonRelease-2>", on_release)
        threading.Thread(target=load, daemon=True).start()

    def _choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.outdir_var.get() or INFORMATION_DIR,
                                    title="选择下载目录")
        if d:
            self.outdir_var.set(os.path.normpath(d))

    def random_select(self):
        """随机勾选 N 个资源（弹窗输入 N）。"""
        if not self.resources:
            messagebox.showinfo("提示", "还没有资源，先「发现资源」")
            return
        n = max(1, len(self.resources) // 3)  # 默认勾 1/3，约 33%
        try:
            resp = messagebox.askinteger("随机选N", "要随机勾选多少个资源？", initialvalue=n,
                                         minvalue=1, maxvalue=len(self.resources))
        except Exception:
            resp = None
        if not resp:
            return
        for r in self.resources.values():
            r._checked = False
        picked = random.sample(list(self.resources.values()), min(resp, len(self.resources)))
        for r in picked:
            r._checked = True
        self.refresh_list()
        self.status_var.set(f"随机勾选 {len(picked)} 个资源，可点「下载勾选」")

    def copy_links(self):
        """复制勾选资源的 URL 到剪贴板（含页面地址）。"""
        checked = [r for r in self.resources.values() if r._checked]
        if not checked:
            messagebox.showinfo("提示", "请先勾选资源")
            return
        lines = [f"{r.name}\t{r.url}" for r in checked]
        text = "\n".join(lines)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set(f"已复制 {len(checked)} 条链接到剪贴板")

    def open_page(self):
        """在浏览器打开勾选资源的对应页面（无勾选则打开当前输入的网址）。"""
        checked = [r for r in self.resources.values() if r._checked]
        if checked:
            opened: set[str] = set()
            for r in checked:
                if not r.url.startswith("http"):
                    continue
                if r.url in opened:
                    continue
                opened.add(r.url)
                webbrowser.open(r.url)
            return
        url = self.url_var.get().strip()
        if url:
            webbrowser.open(url if url.startswith("http") else "https://" + url)

    def dedupe(self):
        """去掉当前列表中重复 URL 的资源，并在状态栏提示。"""
        seen: set[str] = set()
        dup = 0
        for r in list(self.resources.values()):
            if r.url in seen:
                del self.resources[str(id(r))]
                if self.tree.exists(str(id(r))):
                    self.tree.delete(str(id(r)))
                dup += 1
            else:
                seen.add(r.url)
        self.refresh_list()
        self.status_var.set(f"去重完成：移除 {dup} 个重复资源，剩余 {len(self.resources)} 个")

    def retry_failed(self):
        """勾选上次下载失败的资源（读下载目录 failures.json，重新勾选便于「下载勾选」重试）。"""
        outdir = self.outdir_var.get().strip() or INFORMATION_DIR
        try:
            entries = load_failures(outdir)
        except Exception:
            entries = {}
        if not entries:
            messagebox.showinfo(
                "重试失败", f"「{outdir}」没有失败记录\n（failures.json 不存在或为空）")
            return
        hit = missed = 0
        urls = {k for k in entries}
        for r in self.resources.values():
            if r.url in urls:
                r._checked = True
                hit += 1
            else:
                missed += 1
        self.refresh_list()
        self.status_var.set(
            f"已勾选 {hit} 个失败资源重试（记录 {len(entries)} 条，列表中 {missed} 条未找到），"
            "可点「下载勾选」")
        if hit == 0 and missed:
            messagebox.showinfo(
                "重试失败", "当前列表中没有匹配的失败资源。\n"
                "可重新「发现资源」让列表包含失败 URL，或先切换到对应下载目录。")

    def _on_close(self):
        try:
            if self.session is not None:
                self.session.close()
        except Exception:
            pass
        try:
            close_renderer()
        except Exception:
            pass
        try:
            if self.gallery is not None and hasattr(self.gallery, "cancel"):
                self.gallery.cancel()
        except Exception:
            pass
        try:
            dlg = getattr(self, "_api_dialog", None)
            if dlg is not None and dlg.winfo_exists():
                dlg._save_api_key(dlg.api_key_var.get().strip())
                dlg.destroy()
        except Exception:
            pass
        self.root.destroy()


def _set_app_icon(root):
    """设置窗口/任务栏图标（打包后取 exe 内嵌资源，源码运行取项目根 webscoop.ico）。"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    ico = os.path.join(base, "webscoop.ico")
    if not os.path.exists(ico):
        return
    try:
        root.iconbitmap(ico)
        img = Image.open(ico)
        root.iconphoto(True, ImageTk.PhotoImage(img))
    except Exception:
        pass


def _pool_health_bg(probe_url: str):
    """启动后台探活代理池：死代理提前吊销，不阻塞界面（失败静默）。"""
    try:
        from resources_reptile.utils.proxy import current_pool
        pool = current_pool()
        if pool.size:
            pool.health_check(probe_url=probe_url)
    except Exception:
        pass


def _doctor(run_code: int = 1) -> int:
    """无头自检（webscoop.exe --doctor）：验证懒加载依赖并按依赖就绪度返回退出码。

    检查项：平台适配器注册表 / Playwright(含 chromium 启动) / VLC / gallery-dl /
    Scrapling / curl_cffi 指纹会话。结果写入日志；缺失项只降级功能，不阻断 GUI。
    """
    from renderer import ensure_browsers_path
    ensure_browsers_path()
    played = 0
    failed = 0
    check = []

    def probe(name, fn):
        nonlocal played, failed
        played += 1
        try:
            fn()
            check.append(f"[OK]   {name}")
        except Exception as exc:
            failed += 1
            check.append(f"[FAIL] {name}: {type(exc).__name__}: {str(exc)[:140]}")

    import platform_adapters as pa
    probe("适配器注册表", lambda: None)
    log.info("适配器注册: %s", ", ".join(sorted(a.name for a in pa.PLATFORM_ADAPTERS)))
    probe("适配器插件(抖音/快手/小红书/B站)",
          lambda: [a.match_page("https://www.douyin.com/")
                   for a in pa.PLATFORM_ADAPTERS])

    def _curl_ok():
        from curl_cffi.requests import Session
        s = Session(impersonate="chrome")
        s.close()
        return True

    probe("curl_cffi TLS 指纹会话", lambda: _curl_ok() or None)
    probe("Scrapling 兜底",
          lambda: __import__("scrapling.fetchers", fromlist=["Fetcher"]))
    probe("gallery-dl 备用下载器",
          lambda: __import__("gallery_dl.extractor", fromlist=["extractor"]))
    probe("VLC 视频预览",
          lambda: __import__("vlc", fromlist=["MediaPlayer"]) or None)

    def _launch_chromium():
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            b.close()
    probe("Playwright + chromium 安装", _launch_chromium)

    for line in check:
        log.info("doctor %s", line)
    log.info("doctor 汇总: %d 项, 失败 %d", played, failed)
    return run_code if failed else 0


def main():
    if "--doctor" in sys.argv:
        sys.exit(_doctor(run_code=2))
    from renderer import ensure_browsers_path
    ensure_browsers_path()
    setup_logging()
    log.info("应用启动 工作目录=%s", os.getcwd())
    threading.Thread(target=_pool_health_bg,
                     args=(config.PROXY_HEALTH_PROBE,), daemon=True).start()
    root = tk.Tk()
    _set_app_icon(root)
    app = ResourceApp(root)
    try:
        root.mainloop()
    finally:
        log.info("应用退出 列表残留=%d", len(app.resources))


class SearchDialog:
    """「关键词搜索」弹窗：关键词 → 平台搜索页 URL → 复用「发现资源」全流程。

    支持平台：抖音（搜索页走 /aweme/v1/web/search/ 接口，渲染+捕获提取）、
    快手（/search/video 页，graphql 接口）、B站（搜索页渲染+捕获提取，
    详情页跟进时解析内嵌 __playinfo__ 拿 DASH 直链）。需要渲染模式
    （信息流懒加载由适配器 scroll_max 自动滚动）。"""

    SEARCH_PLATFORMS = {
        "抖音": ("https://www.douyin.com/search/{kw}",),
        "快手": ("https://www.kuaishou.com/search/video?searchKey={kw}",),
        "B站": ("https://search.bilibili.com/all?keyword={kw}",),
    }

    def __init__(self, app):
        self.app = app
        dlg = tk.Toplevel(app.root)
        self.dlg = dlg
        dlg.title("关键词搜索（抖音/快手）")
        dlg.geometry("420x150")
        dlg.transient(app.root)
        dlg.grab_set()
        app._search_dialog = self

        pad = {"padx": 8, "pady": 4}
        ttk.Label(dlg, text="按关键词抓取信息流（搜索页走渲染+接口捕获，建议勾选「渲染模式」）。",
                  justify=tk.LEFT).pack(anchor="w", **pad)
        row = ttk.Frame(dlg)
        row.pack(fill=tk.X, **pad)
        ttk.Label(row, text="平台:").pack(side=tk.LEFT)
        self.plat_var = tk.StringVar(value="抖音")
        ttk.Combobox(row, textvariable=self.plat_var, width=6, state="readonly",
                     values=list(self.SEARCH_PLATFORMS)).pack(side=tk.LEFT, padx=4)
        ttk.Label(row, text="关键词:").pack(side=tk.LEFT)
        self.kw_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.kw_var, width=24).pack(side=tk.LEFT, padx=4)

        btns = ttk.Frame(dlg)
        btns.pack(fill=tk.X, padx=8, pady=6)
        ttk.Button(btns, text="搜索", command=self._go).pack(side=tk.LEFT)
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side=tk.LEFT, padx=6)
        self.status_var = tk.StringVar(value="")
        ttk.Label(dlg, textvariable=self.status_var, foreground="#666").pack(anchor="w", **pad)

    def _go(self):
        from urllib.parse import quote
        from platform_adapters import api_filters_for
        plat = self.plat_var.get()
        kw = self.kw_var.get().strip()
        if not kw:
            self.status_var.set("请输入关键词")
            return
        url = (self.SEARCH_PLATFORMS[plat][0]).format(kw=quote(kw))
        if api_filters_for(url) is None:
            self.status_var.set("该平台未注册适配器")
            return
        self.app.url_var.set(url)
        self.app.render_var.set(True)  # 搜索页是 JS 信息流，自动开渲染
        self.status_var.set(f"构造搜索页: {url}")
        self.dlg.destroy()
        self.app.discover()


class FollowDialog:
    """「定时跟进」弹窗：管理关注列表 + 启动/停止后台调度。

    调度在后台线程跑（不占 busy）：每轮逐个「渲染+捕获」发现关注页面的新
    资源并入主列表（按 URL 去重）；配合全局下载存档，重复资源下载时自动跳过。
    关闭弹窗不停止调度，需点「停止」或退出程序。
    """

    def __init__(self, app):
        self.app = app
        self._running = False
        dlg = tk.Toplevel(app.root)
        self.dlg = dlg
        dlg.title("定时跟进")
        dlg.geometry("540x400")
        dlg.transient(app.root)
        dlg.grab_set()
        app._follow_dialog = self

        pad = {"padx": 8, "pady": 4}
        ttk.Label(dlg, text="关注列表：到点自动重新发现（存档跳过已下载，只列新增资源）。",
                  justify=tk.LEFT).pack(anchor="w", **pad)
        body = ttk.Frame(dlg)
        body.pack(fill=tk.BOTH, expand=True, **pad)
        self.lb = tk.Listbox(body, height=10)
        self.lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(body, command=self.lb.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.lb.config(yscrollcommand=sb.set)
        self.lb.bind("<Delete>", lambda e: self._remove())

        row = ttk.Frame(dlg)
        row.pack(fill=tk.X, **pad)
        self.url_var = tk.StringVar(value=app.url_var.get())
        ttk.Entry(row, textvariable=self.url_var, width=50).pack(side=tk.LEFT)
        ttk.Button(row, text="添加当前网址", command=self._add).pack(side=tk.LEFT, padx=4)
        ttk.Button(row, text="删除选中", command=self._remove).pack(side=tk.LEFT)

        ctrl = ttk.Frame(dlg)
        ctrl.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(ctrl, text="间隔(分钟):").pack(side=tk.LEFT)
        self.interval_var = tk.StringVar(value=str(config.FOLLOW_INTERVAL_MIN))
        ttk.Spinbox(ctrl, from_=5, to=1440, width=5,
                    textvariable=self.interval_var).pack(side=tk.LEFT)
        self.start_btn = ttk.Button(ctrl, text="开始定时", command=self._start)
        self.start_btn.pack(side=tk.LEFT, padx=6)
        self.once_btn = ttk.Button(ctrl, text="立即跑一轮", command=self._once)
        self.once_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(ctrl, text="停止", command=self._stop,
                                   state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=6)
        self.status_var = tk.StringVar(value="")
        ttk.Label(dlg, textvariable=self.status_var, foreground="#666") \
            .pack(anchor="w", **pad)

        self._reload()
        self.set_running(app.follow_active)

    def _reload(self):
        import follow_list
        self.lb.delete(0, tk.END)
        for it in follow_list.items():
            name = f"{it.get('name')} · " if it.get("name") else ""
            self.lb.insert(tk.END, f"{name}{it['url']}")

    def _add(self):
        import follow_list
        url = self.url_var.get().strip() or self.app.url_var.get().strip()
        if not url:
            self.status_var.set("请输入网址")
            return
        if follow_list.add(url):
            self._reload()
            self.status_var.set(f"已加入关注：{url}")
        else:
            self.status_var.set("已在关注列表中")

    def _remove(self):
        import follow_list
        sel = self.lb.curselection()
        if not sel:
            return
        it = follow_list.items()[sel[0]]
        follow_list.remove(it["url"])
        self._reload()

    def _start(self):
        try:
            interval = max(5, int(self.interval_var.get()))
        except ValueError:
            interval = config.FOLLOW_INTERVAL_MIN
        self.app.start_follow(interval)
        self.set_running(True)

    def _once(self):
        self.app.run_follow_once()
        self.set_running(True)

    def _stop(self):
        self.app.stop_follow()
        self.set_running(False)
        self.status_var.set("已请求停止（当前页面完成后停止）")

    def set_running(self, running: bool):
        self._running = running
        self.start_btn.config(state=tk.DISABLED if running else tk.NORMAL)
        self.once_btn.config(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL if running else tk.DISABLED)
        self.status_var.set(
            "调度运行中…（关闭弹窗不停止，点「停止」或退出程序结束）" if running else "已停止")


class HotDialog:
    """「热点模式」弹窗：抓热搜榜 → 勾选条目 → 批量发现（渲染 + 接口捕获）。

    - B站热榜：x/web-interface/ranking/v2 无需签名，列出 Top N 供勾选；
      视频直链仍由渲染捕获播放接口后经 bilibili 适配器提取。
    - 抖音热榜：热搜接口带签名（a_bogus），直接渲染热榜页 + 接口捕获，
      由 douyin 适配器提取作品（无需选择，一键跑）。

    注意：仅限个人学习使用，勿商用侵权。
    """

    def __init__(self, app):
        self.app = app
        self._items: list[dict] = []
        dlg = tk.Toplevel(app.root)
        self.dlg = dlg
        dlg.title("热点模式（B站热榜）")
        dlg.geometry("600x430")
        dlg.transient(app.root)
        dlg.grab_set()
        app._hot_dialog = self

        pad = {"padx": 8, "pady": 4}
        top = ttk.Frame(dlg)
        top.pack(fill=tk.X, **pad)
        ttk.Label(top, text="榜单:").pack(side=tk.LEFT)
        self.plat_var = tk.StringVar(value="B站热榜")
        self.plat_box = ttk.Combobox(top, textvariable=self.plat_var, width=10,
                                     state="readonly",
                                     values=["B站热榜", "抖音热榜"])
        self.plat_box.pack(side=tk.LEFT, padx=4)
        self.plat_box.bind("<<ComboboxSelected>>", lambda e: self._sync_ui())
        ttk.Label(top, text="条数:").pack(side=tk.LEFT)
        self.limit_var = tk.StringVar(value="30")
        ttk.Spinbox(top, from_=1, to=100, width=4,
                    textvariable=self.limit_var).pack(side=tk.LEFT)
        ttk.Button(top, text="抓取榜单", command=self._fetch).pack(side=tk.LEFT, padx=8)
        self.status_var = tk.StringVar(value="")
        ttk.Label(dlg, textvariable=self.status_var, foreground="#666") \
            .pack(anchor="w", **pad)

        body = ttk.Frame(dlg)
        body.pack(fill=tk.BOTH, expand=True, **pad)
        self.lb = tk.Listbox(body, selectmode=tk.EXTENDED, height=14)
        self.lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(body, command=self.lb.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.lb.config(yscrollcommand=sb.set)
        self.lb.bind("<Double-Button-1>", lambda e: self._go())

        btns = ttk.Frame(dlg)
        btns.pack(fill=tk.X, padx=8, pady=6)
        self.go_btn = ttk.Button(btns, text="发现勾选（渲染+捕获）",
                                 command=self._go, state=tk.DISABLED)
        self.go_btn.pack(side=tk.LEFT)
        ttk.Button(btns, text="全选", command=self._select_all).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="抓全部", command=self._go_all).pack(side=tk.LEFT)
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side=tk.RIGHT)

    def _fetch(self):
        if self.plat_var.get() == "抖音热榜":
            # 热搜接口带签名，浏览器渲染时自动计算；热榜页命中 douyin 适配器
            # → 强制渲染 + 捕获 /aweme/v1/web/ 接口 → 自动提取作品
            self.dlg.destroy()
            self.app.discover_many(["https://www.douyin.com/hot"], "抖音热榜")
            return
        from hot_search import bilibili_hot
        try:
            limit = min(max(int(self.limit_var.get() or 30), 1), 100)
        except ValueError:
            limit = 30
        self.status_var.set("正在抓取 B 站热榜…")

        def worker():
            try:
                items = bilibili_hot(limit=limit)
                self.dlg.after(0, lambda: self._fill(items))
            except Exception as exc:
                self.dlg.after(0, lambda e=exc: self.status_var.set(f"抓取失败：{e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _sync_ui(self):
        """切换榜单：抖音热榜无需勾选，直接一键跑。"""
        if self.plat_var.get() == "抖音热榜":
            self.status_var.set("抖音热榜带签名，直接渲染热榜页 + 接口捕获提取，点「抓取」开始")
        else:
            self.status_var.set("")

    def _fill(self, items):
        self._items = items
        self.lb.delete(0, tk.END)
        for it in items:
            view = f"{it['view']:,}"
            line = f"{it['rank']:>3}. {it['title'][:40]}  ·  {view}播放 · {it['author'][:10]}"
            self.lb.insert(tk.END, line)
        self.go_btn.config(state=tk.NORMAL if items else tk.DISABLED)
        self.status_var.set(f"热榜 {len(items)} 条；勾选后点「发现勾选」（自动强制渲染）")

    def _select_all(self):
        self.lb.selection_set(0, tk.END)

    def _selected(self) -> list[dict]:
        return [self._items[i] for i in self.lb.curselection()]

    def _go(self):
        sel = self._selected()
        if not sel:
            self.status_var.set("请先勾选要抓的条目")
            return
        urls = [it["url"] for it in sel]
        label = f"{self.plat_var.get()}·{len(urls)}条"
        self.dlg.destroy()
        self.app.discover_many(urls, label)

    def _go_all(self):
        if not self._items:
            self.status_var.set("请先「抓取榜单」")
            return
        self._select_all()
        self._go()


class CookieDialog:
    """「浏览器手动登录抓 Cookie」弹窗：弹出有头浏览器 → 预览 → 保存 cookies.txt。

    浏览器独立临时上下文（不导入用户日常数据），捕获的 Cookie 仅写入本项目
    cookies.txt，供登录态注入复用（接口站点渲染/下载时自动携带）。
    """

    def __init__(self, app):
        self.app = app
        self.cap = None
        dlg = tk.Toplevel(app.root)
        self.dlg = dlg
        dlg.title("浏览器手动登录抓 Cookie")
        dlg.geometry("460x380")
        dlg.transient(app.root)
        dlg.grab_set()
        dlg.protocol("WM_DELETE_WINDOW", self._on_close)
        app._cookie_dialog = self

        pad = {"padx": 8, "pady": 4}
        ttk.Label(dlg, text="步骤：1. 填目标站点地址 → 2. 点「弹出浏览器」手动登录 "
                            "→ 3. 登录后点「读取 Cookie」预览 → 4. 保存到 cookies.txt\n"
                            "浏览器为独立临时上下文，不导入日常浏览器数据，Cookie 仅写入本项目根目录。\n"
                            "抖音建议填 https://www.douyin.com 直接登录再回主页；保存后应能看到 "
                            "douyin.com 行（短链 v.douyin.com 与 www.douyin.com 已自动互通）。",
                 justify=tk.LEFT).pack(anchor="w", **pad)

        row = ttk.Frame(dlg)
        row.pack(fill=tk.X, **pad)
        ttk.Label(row, text="目标 URL：").pack(side=tk.LEFT)
        self.url_var = tk.StringVar(value="https://www.douyin.com")
        ttk.Entry(row, textvariable=self.url_var).pack(side=tk.LEFT,
                                                       fill=tk.X, expand=True)

        btns = ttk.Frame(dlg)
        btns.pack(fill=tk.X, **pad)
        self.open_btn = ttk.Button(btns, text="弹出浏览器", command=self._open_browser)
        self.open_btn.pack(side=tk.LEFT)
        self.read_btn = ttk.Button(btns, text="读取 Cookie", command=self._read)
        self.read_btn.pack(side=tk.LEFT, padx=4)
        self.save_btn = ttk.Button(btns, text="保存到 cookies.txt", command=self._save)
        self.save_btn.pack(side=tk.LEFT, padx=4)

        ttk.Label(dlg, text="捕获结果预览（按域名归组，可整行复制或直接保存）：",
                  ).pack(anchor="w", **pad)
        wrap = ttk.Frame(dlg)
        wrap.pack(fill=tk.BOTH, expand=True, **pad)
        self.text = tk.Text(wrap, height=7, wrap="char")
        sb = ttk.Scrollbar(wrap, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        ttk.Label(dlg, text="备用方案——日常浏览器复制粘贴（无需捕获窗口）：\n"
                            "在你自己浏览器里打开抖音并登录 → F12 → Network → 任选一个 "
                            "douyin.com 请求 → 复制 Cookie 请求头值 → 粘到下面 → 点保存",
                 justify=tk.LEFT).pack(anchor="w", **pad)
        paste_row = ttk.Frame(dlg)
        paste_row.pack(fill=tk.X, **pad)
        self.paste_text = tk.Text(paste_row, height=3, wrap="char")
        self.paste_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.paste_btn = ttk.Button(paste_row, text="保存粘贴内容",
                                    command=self._save_paste)
        self.paste_btn.pack(side=tk.LEFT, padx=4)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(dlg, textvariable=self.status_var, foreground="#666").pack(anchor="w", **pad)

        self._announced = False
        self._polling = False
        self.dlg.after(1200, self._poll_login)

    # ---------------- 操作 ----------------

    def _open_browser(self):
        from cookie_capture import CookieCaptureSession
        url = self.url_var.get().strip() or "https://www.douyin.com"
        if self.cap is None or not self.cap.is_open:
            self.cap = CookieCaptureSession()
        if not self.cap.open(url):
            self.status_var.set("浏览器启动失败（未安装 Chromium）")
            return
        # 等几秒，区分「启动失败」与「仍在弹出中」
        for _ in range(30):
            st = self.cap.state()
            if st in ("ok", "closed"):
                break
            time.sleep(0.2)
        if self.cap.state() == "none":
            self.status_var.set("浏览器启动失败（未安装 Chromium？）或仍在弹出中，稍候重试")
            return
        self.status_var.set("浏览器已弹出：请在窗口内登录（扫码/账号均可），"
                                     "登录后保持窗口开着，点「读取 Cookie」→「保存」；"
                                     "保存成功前不要关浏览器窗口")
        self.read_btn.config(state=tk.NORMAL)

    def _read(self):
        if self.cap is None:
            self.status_var.set("请先「弹出浏览器」")
            return
        st = self.cap.state()
        if st == "closed":
            self.text.delete("1.0", tk.END)
            self.text.insert(tk.END, "（浏览器窗口已关闭：请重新「弹出浏览器」，登录后保持窗口开着）")
            self.status_var.set("浏览器已关闭，重新弹出并登录后保持窗口开着")
            return
        rows = self.cap.readable_candidates(self.url_var.get())
        self.text.delete("1.0", tk.END)
        if not rows:
            self.text.insert(tk.END, "（未捕获到 Cookie，请确认窗口内已登录成功）")
            self.status_var.set("浏览器开着但未捕获到 Cookie")
            return
        for i, row in enumerate(rows, 1):
            self.text.insert(tk.END, f"#{i} {row}\n\n")
        self.status_var.set(f"捕获到 {len(rows)} 个域名：点「保存」全部写入 cookies.txt（保存后窗口可关）")

    def _save(self):
        if self.cap is None:
            self.status_var.set("请先「弹出浏览器」")
            return
        n, msg = self.cap.save_to_file()
        self.status_var.set(msg)

    def _poll_login(self):
        """每 1.2s 检测捕获窗口的登录态：一旦拿到 Cookie 就提示可保存。"""
        if self._polling or self.cap is None:
            self.dlg.after(1200, self._poll_login)
            return
        self._polling = True
        try:
            if self.cap.state() == "ok":
                snap = self.cap.snapshot()
                if snap and not self._announced:
                    self._announced = True
                    self.status_var.set(
                        f"✓ 已检测到 {len(snap)} 个域名的 Cookie，点「保存到 cookies.txt」")
                elif not snap and self._announced:
                    self._announced = False
        except Exception:
            pass
        finally:
            self._polling = False
        try:
            self.dlg.after(1200, self._poll_login)
        except Exception:
            pass  # 对话框已销毁

    def _save_paste(self):
        from cookie_capture import CookieCaptureSession
        from urllib.parse import urlparse
        hd = self.paste_text.get("1.0", tk.END).strip()
        host = urlparse(self.url_var.get().strip() or "https://www.douyin.com").hostname or ""
        n, msg = CookieCaptureSession.save_paste_to_file(hd, host)
        self.status_var.set(msg)
        if n:
            self.paste_text.delete("1.0", tk.END)

    def _on_close(self):
        if self.cap is not None:
            try:
                self.cap.close()
            except Exception:
                pass
        self.dlg.destroy()


if __name__ == "__main__":
    main()