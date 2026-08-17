# -*- mode: python ; coding: utf-8 -*-
# webscoop 单文件 EXE 构建配置
# 构建: .venv\Scripts\python.exe -m PyInstaller --noconfirm webscoop.spec

from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ["gui.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("highres_rules.json", "."),
        ("webscoop.ico", "."),
    ] + collect_data_files("scrapling") + collect_data_files("curl_cffi")
    + collect_data_files("playwright") + collect_data_files("apify_fingerprint_datapoints"),
    hiddenimports=[
        # 平台适配器目录化热加载（pkgutil 扫描，静态分析不可见）
        "platform_adapters.douyin",
        "platform_adapters.kuaishou",
        "platform_adapters.xiaohongshu",
        "platform_adapters.bilibili",
        # 函数内懒加载的第三方库（playwright 顶层 import 触发官方 hook 收集 driver）
        "playwright",
        "playwright.sync_api",
        "curl_cffi",
        "curl_cffi.requests",
        "scrapling",
        "scrapling.fetchers",
        "gallery_dl",
        "gallery_dl.extractor",
        "vlc",
        # 本仓库根模块（全部显式列出，防动态引用漏收）
        "applog",
        "api_discoverer",
        "config",
        "cookie_capture",
        "dead_list",
        "discover_common",
        "download_archive",
        "follow_list",
        "format_selector",
        "gallery_backup",
        "gui_crawler",
        "gui_fetch",
        "hls_downloader",
        "hot_search",
        "llm_rules",
        "player_vlc",
        "renderer",
        "stats",
        # resources_reptile 子包
        "resources_reptile.pipelines",
        "resources_reptile.download_handlers",
        "resources_reptile.middlewares",
        "resources_reptile.dupefilters",
        "resources_reptile.utils.cookies",
        "resources_reptile.utils.proxy",
        # scrapy 深层依赖（pipeline 分类路径）
        "scrapy.http",
        "scrapy.utils.reqser",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tests",
        "pytest",
        "PIL.ImageShow",
        "PIL.ImageQt",
        "tkinter.test",
        "pydoc_data",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="webscoop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="webscoop.ico",
)
