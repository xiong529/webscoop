"""CLI 无头层测试：任务注册表 + follow 子命令 + 纯解析。

    python tests/unit_cli.py
注意：不触网、不启 tkinter；discover/download 的网络路径留待 e2e。
"""
import os
import sys
import tempfile
import time
from argparse import Namespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from headless import TaskRegistry

passed = 0


def check(name: str, cond: bool):
    global passed
    passed += 1
    print("PASS" if cond else "FAIL", name)
    assert cond, name


# ---------- 任务注册表 ----------
reg = TaskRegistry(max_workers=2)

def _slow_ok(task):
    time.sleep(0.2)
    task.progress["ok"] = 3

def _boom(task):
    raise RuntimeError("boom")


t_ok = reg.submit("discover", {"urls": ["a"]}, _slow_ok)
check("registry: 提交后为 queued/running 之一", t_ok.state in ("queued", "running"))
detail = reg.describe(t_ok.id)
check("registry: describe 返回 id/kind/state", detail is not None
      and detail["id"] == t_ok.id and detail["kind"] == "discover")
for _ in range(50):
    if t_ok.done:
        break
    time.sleep(0.05)
check("registry: 正常任务 done 且进度保留", t_ok.state == "done"
      and t_ok.progress["ok"] == 3)

t_bad = reg.submit("download", {}, _boom)
for _ in range(50):
    if t_bad.done:
        break
    time.sleep(0.05)
check("registry: 异常任务 failed 且带错误信息",
      t_bad.state == "failed" and "boom" in t_bad.error)

check("registry: snapshot 包含两个任务",
      len(reg.snapshot()) == 2 and reg.get("nope") is None)

# 并发限流：max_workers=2 时第三个任务排队
t3 = reg.submit("discover", {}, lambda t: time.sleep(0.15))
time.sleep(0.05)
check("registry: 队列控制（第 3 个任务不立刻 running）",
      t3.state in ("queued", "running"))
time.sleep(0.5)
check("registry: 排队任务最终 done", t3.state == "done")


# ---------- follow 子命令（写入临时文件） ----------
_tmp = tempfile.mkdtemp(prefix="ws_cli_")
os.environ["RESOURCES_FOLLOW_FILE"] = os.path.join(_tmp, "follow.json")

from follow_list import clear as fl_clear
from cli import FollowCli

fl_clear()
check("cli follow: add", FollowCli.add(Namespace(url="https://a.example/u", name="甲")) == 0)
check("cli follow: add 去重返回 1", FollowCli.add(Namespace(url="https://a.example/u", name="")) == 1)
check("cli follow: list 非空", FollowCli.list(Namespace()) == 0 and FollowCli.count() == 1)
check("cli follow: remove", FollowCli.remove(Namespace(url="https://a.example/u")) == 0)
check("cli follow: remove 后为空", FollowCli.count() == 0)
check("cli follow: clear", FollowCli.clear(Namespace()) == 0)


def _parse(argv: list[str]):
    import cli
    old = sys.argv
    sys.argv = ["webscoop", *argv]
    try:
        ap = cli._build_parser()
        return ap.parse_args()
    finally:
        sys.argv = old


ns = _parse(["follow", "add", "https://a.example/u", "-n", "x"])
check("cli parse: follow add url+name",
      ns.cmd == "follow" and ns.action == "add" and ns.name == "x")
ns = _parse(["discover", "https://a.example/u", "--render", "--json"])
check("cli parse: discover flags",
      ns.render is True and ns.json is True and len(ns.urls) == 1)
ns = _parse(["download", "u1", "u2", "-o", "o", "-j", "2"])
check("cli parse: download outdir/workers",
      ns.outdir == "o" and ns.workers == 2 and len(ns.urls) == 2)
ns = _parse(["follow", "run", "-i", "5", "--once"])
check("cli parse: follow run once/interval",
      ns.action == "run" and ns.interval == 5 and ns.once is True)
ns = _parse(["serve", "-p", "9999", "--token", "t"])
check("cli parse: serve port/token",
      ns.port == 9999 and ns.token == "t")
try:
    _parse(["--version"])
    check("cli parse: version 动作 SystemExit", False)
except SystemExit as exc:
    check("cli parse: version 动作 SystemExit(0)", exc.code == 0)
try:
    _parse([])
    check("cli parse: 空命令应报错", False)
except SystemExit as exc:
    check("cli parse: 空命令报错退出码 2", exc.code == 2)

print(f"DONE  PASS={passed} FAIL=0")