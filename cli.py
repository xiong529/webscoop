"""webscoop 命令行入口（headless）：discover / download / follow / serve / gui / doctor。

tkinter 只是 GUI 宿主；本模块只在需要时懒加载 gui，保证无头环境
（服务器 / CI / Docker）里无需 tkinter 也能用全部命令。
"""
from __future__ import annotations

import argparse
import json
import sys
import time


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    args = _build_parser().parse_args()
    return args.fn(args)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="webscoop", description="资源采集与下载工具（无头 CLI）")
    ap.add_argument("--version", action="version",
                    version=_version())
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("gui", help="启动图形界面").set_defaults(fn=_cmd_gui)
    sub.add_parser("doctor", help="无头自检（依赖就绪度，失败非零退出）").set_defaults(fn=_cmd_doctor)

    p = sub.add_parser("discover", help="发现页面资源并打印（不下载）")
    p.add_argument("urls", nargs="+")
    p.add_argument("--render", action="store_true", help="强制无头浏览器渲染")
    p.add_argument("--json", action="store_true", help="以 JSON 输出（供脚本使用）")
    p.set_defaults(fn=_cmd_discover)

    p = sub.add_parser("download", help="发现并下载页内资源")
    p.add_argument("urls", nargs="+")
    p.add_argument("-o", "--outdir", default="")
    p.add_argument("--render", action="store_true")
    p.add_argument("-j", "--workers", type=int, default=None)
    p.set_defaults(fn=_cmd_download)

    p = sub.add_parser("follow", help="定时跟进关注列表")
    fsub = p.add_subparsers(dest="action", required=True)
    fsub.add_parser("list", help="列出关注").set_defaults(fn=FollowCli.list)
    f = fsub.add_parser("add", help="添加关注（去重）")
    f.add_argument("url")
    f.add_argument("-n", "--name", default="")
    f.set_defaults(fn=FollowCli.add)
    f = fsub.add_parser("remove", help="移除关注")
    f.add_argument("url")
    f.set_defaults(fn=FollowCli.remove)
    f = fsub.add_parser("clear", help="清空关注").set_defaults(fn=FollowCli.clear)
    f = fsub.add_parser("run", help="无头定时跟进：循环 发现→下载 关注列表")
    f.add_argument("-i", "--interval", type=int, default=0,
                   help="轮询间隔（分钟），缺省用配置 FOLLOW_INTERVAL_MIN")
    f.add_argument("-o", "--outdir", default="")
    f.add_argument("--render", action="store_true")
    f.add_argument("--once", action="store_true", help="只跑一轮即退出")
    f.set_defaults(fn=FollowCli.run)

    p = sub.add_parser("serve", help="启动 REST API（仅绑定 127.0.0.1）")
    p.add_argument("-p", "--port", type=int, default=8000)
    p.add_argument("--token", default="", help="访问令牌（缺省读 RESOURCES_API_TOKEN）")
    p.set_defaults(fn=_cmd_serve)

    return ap


class FollowCli:
    """follow 子命令实现（follow_list 为纯 JSON 读写，直接复用）。"""

    @staticmethod
    def count() -> int:
        from follow_list import items
        return len(items())
    def list(args) -> int:
        from follow_list import items
        rows = items()
        if not rows:
            print("（空列表）")
            return 0
        for it in rows:
            name = it.get("name") or ""
            print(f"{it['url']}\t{name}\t{it.get('added_at', '')}".rstrip("\t"))
        return 0

    @staticmethod
    def add(args) -> int:
        from follow_list import add
        ok = add(args.url, args.name)
        print("已添加" if ok else "已存在（跳过）")
        return 0 if ok else 1

    @staticmethod
    def remove(args) -> int:
        from follow_list import remove
        print("已移除" if remove(args.url) else "不在列表中")
        return 0

    @staticmethod
    def clear(args) -> int:
        from follow_list import clear
        clear()
        print("已清空")
        return 0

    @staticmethod
    def run(args) -> int:
        import config
        from follow_list import urls
        interval_min = args.interval or config.FOLLOW_INTERVAL_MIN
        print(f"定时跟进：每 {interval_min} 分钟一轮，Ctrl+C 退出，输出目录 "
              f"{args.outdir or config.INFORMATION_DIR}")
        try:
            while True:
                targets = urls()
                if not targets:
                    print("[round] 关注列表为空，等待下一轮…")
                for url in targets:
                    try:
                        _run_follow_url(url, args)
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[round] {url} 失败: {type(exc).__name__}: {exc}")
                print(f"[round] 完成，{interval_min} 分钟后下一轮…")
                if args.once:
                    return 0
                for _ in range(int(interval_min * 60 // 2)):
                    time.sleep(2)
        except KeyboardInterrupt:
            print("\n已停止")
            return 0


def _run_follow_url(url: str, args) -> None:
    from headless import discover_and_download
    stats = discover_and_download([url], args.outdir, render=bool(args.render))
    print(f"[round] {url}: 发现 {stats['found']}，下载 {stats['downloaded']}"
          f"，失败 {stats['failed']}")
    for e in stats["errors"]:
        print(f"  ! {e}")


def _cmd_discover(args) -> int:
    import threading as th
    from gui_crawler import Discoverer
    results: dict[str, dict] = {}
    for url in args.urls:
        try:
            d = Discoverer(render_mode=args.render, stop_event=th.Event())
            resources, title = d.discover(url)
            results[url] = {"title": title,
                            "resources": [r.to_dict() for r in resources]}
        except Exception as exc:
            results[url] = {"error": f"{type(exc).__name__}: {exc}"}
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    for url, out in results.items():
        print(f"## {url}")
        if "error" in out:
            print(f"  ! {out['error']}")
            continue
        print(f"  标题: {out['title']}")
        for r in out["resources"]:
            print(f"  - [{r['kind']}] {r['name']}\t{r['url']}")
    return 0


def _cmd_download(args) -> int:
    from headless import discover_and_download
    stats = discover_and_download(args.urls, args.outdir,
                                  render=args.render, workers=args.workers)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 1 if stats["errors"] else 0


def _cmd_gui(args) -> int:
    import gui  # 懒加载：无头环境不强制 tkinter
    return gui.main()


def _cmd_doctor(args) -> int:
    import gui  # 懒加载
    sys.argv = ["webscoop", "--doctor"]
    return gui.main()


def _cmd_serve(args) -> int:
    from server import serve
    print(f"API 服务: http://127.0.0.1:{args.port} （Ctrl+C 停止）")
    return serve(port=args.port, token=args.token)


def _version() -> str:
    try:
        from resources_reptile import __version__
        return __version__
    except Exception:
        return "unknown"


if __name__ == "__main__":
    sys.exit(main())