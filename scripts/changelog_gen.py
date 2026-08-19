"""生成按约定式提交前缀分组的 CHANGELOG 章节。

用法：
    python scripts/changelog_gen.py --version v1.0.8 [--from v1.0.7] [--to HEAD] [--out FILE]
区间为 (from, to]（不含 from 本身，from 缺省/为空则取全量历史）。
release 工作流中 --out 到临时文件，作 gh release 的 notes-file；
本地验证亦可 --out 追加进 CHANGELOG.md。
"""
import argparse
import re
import subprocess
import sys

_PREFIX_RE = re.compile(r"(?i)^([a-z]+)\s*[:：]\s*")
_GROUPS = [
    ("feat", "\u65b0\u529f\u80fd"),
    ("fix", "\u4fee\u590d"),
    ("perf", "\u6027\u80fd"),
    ("refactor", "\u91cd\u6784"),
    ("docs", "\u6587\u6863"),
    ("build", "\u6784\u5efa/CI"),
    ("ci", "\u6784\u5efa/CI"),
    ("chore", "\u5176\u4ed6"),
    ("test", "\u5176\u4ed6"),
]
_TITLE_BY_PREFIX = dict(_GROUPS)


def _group_of(subject: str) -> str:
    m = _PREFIX_RE.match(subject)
    if m:
        return _TITLE_BY_PREFIX.get(m.group(1).lower(), "\u5176\u4ed6")
    return "\u5176\u4ed6"


def _strip_prefix(subject: str) -> str:
    return _PREFIX_RE.sub("", subject, count=1)


def render(version: str, start: str, to: str) -> str:
    rng = f"{start}..{to}" if start else to
    proc = subprocess.run(
        ["git", "log", rng, "--no-merges", "--pretty=format:%s"],
        capture_output=True, text=True, encoding="utf-8",
    )
    subjects = [line for line in proc.stdout.splitlines() if line.strip()]
    version = version.lstrip("v")
    if not subjects:
        return f"## {version}\n\n(\u65e0\u63d0\u4ea4\u3002)\n"
    grouped: dict[str, list[str]] = {}
    for subject in subjects:
        grouped.setdefault(_group_of(subject), []).append(_strip_prefix(subject))
    out = [f"## {version}"]
    seen: set[str] = set()
    for _, title in _GROUPS:
        if title in seen:
            continue
        seen.add(title)
        items = grouped.get(title)
        if items:
            out.append(f"\n### {title}")
            out.extend(f"- {item}" for item in items)
    return "\n".join(out) + "\n"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--from", dest="start", default="")
    ap.add_argument("--to", default="HEAD")
    ap.add_argument("--out", default="", help="UTF-8 文件输出（缺省 stdout）")
    args = ap.parse_args()
    text = render(args.version, args.start, args.to)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())