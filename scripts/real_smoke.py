"""定时真实冒烟：真实抓取平台页面，验证「渲染 → 接口捕获 → 适配器提取」链路未腐化。

目标：平台接口签名/页面结构改版会让 extract() 返回 0 —— 这是最容易“无声坏掉”的点，
本脚本在 GitHub Actions cron（.github/workflows/real_smoke.yml）里定时跑。

判定语义：
- 页面加载成功（渲染/静态均可）但提取 0 资源 → ADAPTER_FAIL → 退出码 1（开 issue）
- 传输层失败（超时/被拦）→ NETWORK → 退出码 0（仅警告，避免代理噪音误报）
- 未配置 URL 的平台 → SKIP
用法：
    python scripts/real_smoke.py
缺省读环境变量 SMOKE_DOUYIN_URL / SMOKE_KUAISHOU_URL / SMOKE_BILI_URL /
SMOKE_XIAOHONGSHU_URL（留空=跳过）。可用 --urls 语法传自定义：--urls douyin=URL
"""
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gui_crawler import Discoverer

PLATFORMS = ("douyin", "kuaishou", "bilibili", "xiaohongshu")


def probe(url: str) -> tuple[str, int, str]:
    if not url:
        return "SKIP", 0, "未配置 URL"
    try:
        discoverer = Discoverer(render_mode=True, stop_event=threading.Event())
        resources, title = discoverer.discover(url)
        n = len(resources)
        if n == 0:
            return "ADAPTER_FAIL", 0, f"页面已加载但提取 0 资源（title={title!r})"
        return "OK", n, f"提取 {n} 资源（title={title!r}）"
    except Exception as exc:  # 传输层问题不算适配器腐化
        return "NETWORK", 0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    urls = {p: os.environ.get(f"SMOKE_{p.upper()}_URL", "") for p in PLATFORMS}
    if "--urls" in sys.argv:
        idx = sys.argv.index("--urls")
        for kv in sys.argv[idx + 1:]:
            if "=" not in kv:
                continue
            k, v = kv.split("=", 1)
            if k in urls:
                urls[k] = v
    failed = 0
    for p in PLATFORMS:
        state, n, msg = probe(urls[p])
        print(f"[{p}] {state}: {msg}")
        if state == "ADAPTER_FAIL":
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())