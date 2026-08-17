"""全量回归入口。一条命令跑所有测试套件:

    python tests/run_all.py            # 全部(单元 + 端到端)
    python tests/run_all.py unit       # 仅单元(不需浏览器/网络)
    python tests/run_all.py e2e        # 仅端到端(本地 HTTP 服务;e2e_render 需 chromium)
    python tests/run_all.py e2e_mime unit_common   # 按套件名子集

每个套件在独立子进程运行,互不污染全局配置;含失败/异常即非零退出码。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")

SUITES = {
    "unit_common": "发现核心逻辑:视频择优 / 极小文件 / 高清 / og / 文件头",
    "unit_format_selector": "格式选择器:spec 解析 / 条件过滤 / best-worst / URL 择优",
    "unit_download_archive": "下载存档/死链表:URL 归一 / 记录-命中闭环 / 持久化",
    "unit_hotsearch": "热搜榜:B站热榜解析 / 接口错误 / 截断",
    "unit_follow_list": "定时跟进:关注列表增删 / 去重 / 持久化",
    "unit_features": "新功能:去重 / MIME / robots / 重试 / failures.json / 渲染冒烟",
    "unit_netsuite": "网络层:代理池 / Cookie 注入 / 平台适配器 / mkv 上限 / stats",
    "unit_hls": "HLS m3u8 分片下载:变体选择 / 并发合并 / BYTERANGE / 失败路径",
    "unit_fallback": "Scrapling 第三层兜底接线(mock)",
    "unit_cookie_render": "渲染登录态注入:cookies.txt -> add_cookies / 头兜底",
    "unit_llm": "LLM 规则生成器:样例提取 / 提示 / 假 LLM 全流程 / 正则校验 / 合并",
    "e2e_stream": "Discoverer 流式回调 + 可打断(本地 HTTP,30 延迟图片)",
    "e2e_mime": "管道 MIME 嗅探(无扩展名链接按 Content-Type 归类)",
    "e2e_retry": "下载重试 + failures.json 持久化与清理",
    "e2e_scrapy_stats": "Scrapy CLI 统计闭环:发现/下载/失败分布 + stats.json 落盘",
    "e2e_render": "Playwright 渲染模式(需 chromium)",
    "e2e_douyin_api": "抖音空壳页 e2e:适配器命中强制渲染+接口捕获+提取(需 chromium)",
}


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    if not args:
        names = list(SUITES)
    elif args == ["unit"]:
        names = [n for n in SUITES if n.startswith("unit")]
    elif args == ["e2e"]:
        names = [n for n in SUITES if n.startswith("e2e")]
    else:
        names = []
        for a in args:
            if a in SUITES and a not in names:
                names.append(a)
    if not names:
        print(f"未知套件: {args}（可用: {' '.join(SUITES)} 或 unit / e2e）")
        return 2

    results = {}
    for name in names:
        print(f"\n===== {name} :: {SUITES[name]} =====")
        print(f"$ python tests/{name}.py")
        try:
            proc = subprocess.run(
                [sys.executable, "-X", "utf8", os.path.join(TESTS, f"{name}.py")],
                cwd=ROOT, timeout=600)
            results[name] = proc.returncode
        except subprocess.TimeoutExpired:
            print(f"[TIMEOUT] {name} 超过 600s")
            results[name] = 1
        except Exception as exc:
            print(f"[ERROR] {name}: {exc}")
            results[name] = 1

    print("\n" + "=" * 50)
    print("回归汇总:")
    bad = 0
    for name in names:
        ok = results.get(name) == 0
        bad += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"共 {len(names)} 套, 失败 {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())