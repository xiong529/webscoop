"""LLM 规则生成器（主线 A）：把「一页 HTML + 资源直链样例」分析为一个高清规则。

用法：
    python llm_rules.py URL                # 只展示建议规则，不改文件
    python llm_rules.py URL --apply        # 合并进 highres_rules.json
    python llm_rules.py --file page.html   # 用本地 HTML（跳过网络抓取）

通过 OpenAI 兼容 /chat/completions 调用 LLM（DeepSeek/Ollama/OpenRouter/LiteLLM 均可）：
    RESOURCES_LLM_BASE=... RESOURCES_LLM_KEY=... RESOURCES_LLM_MODEL=... python llm_rules.py ...

原因与机制：
    静态规则表（highres_rules.json）的变换器白名单解决了两个问题——
    1) LLM 只能从白名单选 transform（含 regex_sub），无需 eval 任意代码；
    2) 用户可选（regex_sub）时 LLM 可给出 search/replace 正则，自由度足够又安全。
    一次分析入库后，后续抓取全走本地正则，零 token 成本（Kadoa/selfhealing 的同一思想）。

提示：LLM 分析前应给足样例。若目标页是 JS 动态渲染，先手动保存为 HTML：
    python llm_rules.py --file saved.html --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from functools import partial

import config
from discover_common import RULES_FILE, TRANSFORMS, is_icon_url, reload_rules

# 每类（图片/视频）最多送多少条样例 URL；太长会超模型上下文
MAX_SAMPLES_PER_KIND = 25


# ================================================================
# 0) LLM 配置存取（GUI 弹窗 / CLI 共用）
# ================================================================

def load_llm_config() -> dict:
    """读取本地 LLM 配置(LLM_CONFIG_FILE)。环境变量优先于文件值。"""
    cfg = {}
    try:
        with open(config.LLM_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg = {k: data.get(k, "") for k in ("base_url", "api_key", "model")}
    except (OSError, json.JSONDecodeError):
        pass
    cfg["base_url"] = os.environ.get("RESOURCES_LLM_BASE", cfg.get("base_url")
                                     or config.LLM_BASE_URL)
    cfg["api_key"] = os.environ.get("RESOURCES_LLM_KEY", cfg.get("api_key")
                                    or config.LLM_API_KEY)
    cfg["model"] = os.environ.get("RESOURCES_LLM_MODEL", cfg.get("model")
                                  or config.LLM_MODEL)
    return cfg


def save_llm_config(base_url: str = "", api_key: str = "", model: str = "") -> str:
    """把 LLM 配置写回 LLM_CONFIG_FILE。返回 (OK|错误信息)。"""
    cfg = load_llm_config()
    cfg.update({
        "base_url": (base_url or cfg.get("base_url") or "").strip() or config.LLM_BASE_URL,
        "api_key": (api_key or cfg.get("api_key") or "").strip(),
        "model": (model or cfg.get("model") or "").strip() or config.LLM_MODEL,
    })
    try:
        with open(config.LLM_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return ""
    except OSError as exc:
        return f"写入失败: {exc}"


def test_connection(base_url: str = "", api_key: str = "", model: str = "",
                    timeout: int = 30) -> tuple:
    """测试 LLM 连接。返回 (ok, 提示信息)。不发规则提示，仅验证鉴权+模型可达。"""
    import requests
    base = (base_url or config.LLM_BASE_URL).rstrip("/")
    key = api_key or config.LLM_API_KEY
    mdl = model or config.LLM_MODEL
    if not key:
        return False, "未填写 API Key"
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": mdl,
                  "messages": [{"role": "user", "content": "ping"}],
                  "max_tokens": 1},
            timeout=timeout,
        )
    except requests.exceptions.RequestException as exc:
        return False, f"请求失败: {exc}"
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message") or resp.text[:160]
        except Exception:
            detail = resp.text[:160]
        return False, f"HTTP {resp.status_code}: {detail}"
    return True, f"连接成功（{mdl}）"


# ================================================================
# 1) 从 HTML 提取资源直链样例
# ================================================================

_IMAGE_RE = re.compile(
    r"(https?://[^\s\"'<>()]+?\.(?:jpe?g|png|webp|gif|avif|svg)"
    r"(?:[?#][^\s\"'<>()]*)?)", re.IGNORECASE)
_VIDEO_RE = re.compile(
    r"(https?://[^\s\"'<>()]+?\.(?:mp4|webm|mkv|m3u8)(?:[?#][^\s\"'<>()]*)?)",
    re.IGNORECASE)
# 第三方合作/编辑外链（canva 等），不是本站资源，跳过
_AD_LINK_HINTS = ("canva.com", "content-partner", "editor.sunshine.ai")


def _is_ad_link(url: str) -> bool:
    return any(h in url for h in _AD_LINK_HINTS)


def _clean(url: str) -> str:
    url = url.replace("&amp;", "&").replace("\\/", "/")
    return url.strip(" .\"'")


def extract_url_samples(html: str, page_url: str = "") -> tuple:
    """从 HTML 提取图片/视频直链样例，返回 (images, videos) 去重列表。

    同时抓 meta og:image、img/video/source 标签与裸 URL 正则；
    过滤纯 http/www 外壳、图标与极小资源特征，截断到上限。
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    images, videos = [], []

    def add(found, bucket, cap):
        for u in found:
            if len(bucket) >= cap:
                return
            if not u:
                continue
            if u.lower() in ("http://", "https://"):
                continue
            if _is_ad_link(u):
                continue
            if is_icon_url(u):
                continue
            bucket.append(u)

    for m in soup.find_all("meta"):
        prop = (m.get("property") or m.get("itemprop") or "").strip().lower()
        if prop in ("og:image", "og:image:url", "og:image:secure_url", "image") \
                and m.get("content"):
            add([_clean(m["content"])], images, MAX_SAMPLES_PER_KIND)
    for tag, attrs, bucket in (
        ("img", ("src", "data-src", "data-original", "data-lazy-src"), images),
        ("source", ("src", "srcset"), videos),
        ("video", ("poster",), images),
        ("video", ("src",), videos),
    ):
        for node in soup.find_all(tag):
            for a in attrs:
                if not node.get(a):
                    continue
                val = node[a].strip()
                # srcset: 取第一个候选
                first = val.split(",")[0].strip().split(" ")[0]
                if first.startswith("http"):
                    add([_clean(first)], bucket, MAX_SAMPLES_PER_KIND)

    add([_clean(u) for u in _IMAGE_RE.findall(html or "")], images, MAX_SAMPLES_PER_KIND)
    add([_clean(u) for u in _VIDEO_RE.findall(html or "")], videos, MAX_SAMPLES_PER_KIND)

    seen_i, seen_v = set(), set()
    images = [u for u in images if not (u in seen_i or seen_i.add(u))]
    videos = [u for u in videos if not (u in seen_v or seen_v.add(u))]
    return images, videos


# ================================================================
# 2) 构造 LLM 提示
# ================================================================

_TRANSFORM_HELP = {
    "strip_size_suffix": "去 WordPress 尺寸后缀：foo-768x432.jpg -> foo.jpg",
    "pexels_dl": "pexels 原图直链（photos/<id>/ -> dl 参数）",
    "pixabay_1280": "pixabay 图片 _640.jpg -> _1280.jpg",
    "pixabay_video_large": "pixabay 视频 _tiny/_small/_medium/_720/_850 -> _large",
    "bump_w_h_params": "调大 URL 里 ?w=/&h= 参数值",
    "regex_sub": "通用正则路径替换（需给 search/replace 字段）",
}

_SYSTEM_PROMPT = """你是网页资源抓取规则专家。用户给你一个网页里的图片/视频直链样例，
你的任务是为「把低清/缩略图 URL 变换为高清原图地址」设计一条规则。

现有变换器白名单（transform 只能选其一）：
{transform_help}

规则 JSON 结构（match 作用于完整 URL，path_pattern 作用于 URL 路径，二者都可省略为空串）：
{{
  "site": "一句话说明站点与规则用途",
  "enabled": true,
  "kind": "image | video | any",
  "match": "",
  "path_pattern": "",
  "transform": "白名单内的一个名字",
  "search": "",     # 仅 transform=regex_sub 时填写，Python 正则
  "replace": ""     # 仅 transform=regex_sub 时填写，替换文本
}}

要求：
1. 只输出一个 JSON 对象，不要任何解释文字或 markdown 代码块。
2. match/path_pattern 必须是合法 Python 正则，无法确定就不要填（用空串）。
3. 若样例 URL 不需要变换（已是原图直链）或看不出规律，输出
   {{"skip": true, "reason": "..."}}，不要瞎猜。
4. transform=regex_sub 时给 search（Python 正则，作用于 URL 路径）与 replace。
5. 以样例 URL 真实样式为准推断，不要臆造网站约定。"""


def build_messages(page_url: str, images: list[str], videos: list[str],
                   kind: str = "") -> list[dict]:
    """构造 chat 消息：system + 一条含样例的 user。"""
    transform_help = "\n".join(f"- {k}: {v}" for k, v in _TRANSFORM_HELP.items())
    lines = [f"页面: {page_url or '(本地 HTML)'}", "图片直链样例:"]
    lines += [f"  {u}" for u in images] or ["  (无)"]
    lines += ["视频直链样例:"]
    lines += [f"  {u}" for u in videos] or ["  (无)"]
    if kind:
        lines.append(f"期望规则类型: {kind}")
    system = _SYSTEM_PROMPT.replace("{transform_help}", transform_help)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(lines)},
    ]


# ================================================================
# 3) 调 LLM（OpenAI 兼容）
# ================================================================

def llm_complete(messages: list[dict], base_url: str = "", api_key: str = "",
                 model: str = "", timeout: int = 0) -> str:
    """调用 OpenAI 兼容 /chat/completions，返回首个回复文本。"""
    import requests
    base = (base_url or config.LLM_BASE_URL).rstrip("/")
    key = api_key or config.LLM_API_KEY
    mdl = model or config.LLM_MODEL
    if not key:
        raise RuntimeError("未配置 LLM API Key（RESOURCES_LLM_KEY）")
    resp = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        json={"model": mdl, "messages": messages, "temperature": 0.2},
        timeout=timeout or config.LLM_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"LLM 响应结构异常: {data}")


def parse_rule(text: str) -> tuple:
    """解析 LLM 输出的 JSON。返回 (rule, None) 或 (None, 错误信息)。

    兼容 markdown 代码块包裹与首尾噪音。
    """
    if not text:
        return None, "LLM 回复为空"
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1)
    text = text.strip()
    if not text.startswith("{"):
        s = text.find("{")
        e = text.rfind("}")
        if s == -1 or e == -1 or e <= s:
            return None, f"不是 JSON 对象: {text[:200]!r}"
        text = text[s:e + 1]
    try:
        rule = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败: {exc}"
    if not isinstance(rule, dict) or not rule.get("skip"):
        if "skip" in rule and not isinstance(rule.get("skip"), bool):
            rule["skip"] = bool(rule["skip"])
    return rule, None


def validate_rule(rule: dict) -> tuple:
    """校验 LLM 建议的规则（调试正则合法性）。返回 (ok, 错误信息或 None)。"""
    if not isinstance(rule, dict):
        return False, "不是字典"
    if rule.get("skip"):
        return True, None
    transform = rule.get("transform", "")
    if transform not in TRANSFORMS:
        return False, f"未知 transform: {transform!r}（可用: {', '.join(TRANSFORMS)}）"
    for field in ("match", "path_pattern", "search"):
        val = rule.get(field)
        if not val:
            continue
        if not isinstance(val, str):
            return False, f"{field} 必须是字符串"
        try:
            re.compile(val)
        except re.error as exc:
            return False, f"{field} 不是合法正则: {exc}"
    if transform == "regex_sub" and not (rule.get("search") and rule.get("replace")):
        return False, "regex_sub 必须提供 search 与 replace"
    kind = rule.get("kind", "any")
    if kind not in ("image", "video", "any"):
        return False, f"kind 非法: {kind!r}"
    return True, None


# ================================================================
# 4) 合并进规则表
# ================================================================

def read_rules(rules_file: str = "") -> dict:
    """读规则表 JSON（文件缺失时返回空表）。"""
    path = rules_file or RULES_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("rules"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": 1, "comment": "", "rules": []}


def merge_rule(rule: dict, rules_file: str = "", dedupe: bool = True) -> tuple:
    """把建议规则追加进规则表。返回 (index, None) 或 (None, 错误信息)。

    同已存规则完全一致时跳过（返回 -1）。成功后调用 reload_rules()。
    """
    if not isinstance(rule, dict) or rule.get("skip"):
        return -1, "skip 规则不需要写入"
    data = read_rules(rules_file)
    for i, existing in enumerate(data["rules"]):
        if dedupe and existing == rule:
            return -1, f"已存在相同规则（第 {i + 1} 条），跳过"
    data["rules"].append(rule)
    path = rules_file or RULES_FILE
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError as exc:
        return None, f"写入失败: {exc}"
    reload_rules()
    return len(data["rules"]) - 1, None


# ================================================================
# 5) 主流程
# ================================================================

def analyze_page(html: str, page_url: str = "", kind: str = "",
                 llm: callable = None) -> tuple:
    """本地 HTML -> LLM -> (建议规则, page, samples)。llm 可注入以便测试。

    返回 (rule: dict|None, page: str, (images, videos), error: str|None)。
    llm 注入签名 llm(messages)->str;默认走 llm_complete。
    """
    images, videos = extract_url_samples(html or "", page_url)
    if not images and not videos:
        return None, page_url or "", ([], []), "HTML 中没找到图片/视频直链样例"
    messages = build_messages(page_url, images[:MAX_SAMPLES_PER_KIND],
                              videos[:MAX_SAMPLES_PER_KIND], kind)
    if llm is None:
        llm = partial(llm_complete, base_url=config.LLM_BASE_URL,
                      api_key=config.LLM_API_KEY, model=config.LLM_MODEL)
    try:
        text = llm(messages)
    except Exception as exc:
        return None, page_url or "", (images, videos), f"LLM 调用失败: {exc}"
    rule, err = parse_rule(text)
    if err:
        return None, page_url or "", (images, videos), err
    ok, verr = validate_rule(rule)
    if not ok:
        return None, page_url or "", (images, videos), f"规则不合法: {verr}"
    return rule, page_url or "", (images, videos), None


def fetch_page(url: str):
    """抓页面 HTML，返回 (html, final_url)。失败抛 RuntimeError。"""
    from gui_fetch import FetchSession
    session = FetchSession()
    try:
        try:
            resp = session.get(url)
        except Exception as exc:
            raise RuntimeError(f"抓取失败: {exc}")
        if resp.status_code >= 400:
            raise RuntimeError(f"抓取失败 HTTP {resp.status_code}: {url}")
        return resp.text, resp.url
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="LLM 规则生成器：分析一页资源直链并建议一条高清规则")
    ap.add_argument("url", nargs="?", help="目标网页 URL")
    ap.add_argument("--file", "-f", help="本地 HTML 文件路径（跳过网络抓取）")
    ap.add_argument("--apply", action="store_true",
                    help="把建议规则合并进 highres_rules.json")
    ap.add_argument("--kind", default="", choices=("", "image", "video", "any"),
                    help="提示 LLM 规则类型（默认由样例自动决定）")
    ap.add_argument("--base", default="", help="LLM base URL（默认取配置）")
    ap.add_argument("--key", default="", help="LLM API Key（默认取配置/环境变量）")
    ap.add_argument("--model", default="", help="LLM 模型（默认取配置）")
    args = ap.parse_args(argv)

    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                html = f.read()
        except OSError as exc:
            print(f"[错误] 读文件失败: {exc}")
            return 2
        page_url = f"file://{os.path.abspath(args.file)}"
    elif args.url:
        try:
            html, page_url = fetch_page(args.url)
            print(f"[抓取] {page_url} (bytes={len(html)})")
        except RuntimeError as exc:
            print(f"[错误] {exc}")
            return 2
    else:
        ap.print_help()
        return 2

    if args.base or args.key or args.model:
        llm = partial(llm_complete, base_url=args.base, api_key=args.key,
                      model=args.model)
    else:
        llm = None
    rule, page, (images, videos), err = analyze_page(html, page_url, args.kind, llm=llm)
    if err:
        print(f"[错误] {err}")
        return 2
    if not rule or rule.get("skip"):
        print(f"[跳过] 无需规则（{reason or '原因未知'}）" if (reason := rule.get("reason"))
              else "[跳过] LLM 判断无需变换")
        return 0

    print(f"\n建议规则（页面 {page}）:")
    print(json.dumps(rule, ensure_ascii=False, indent=2))
    print(f"\n样例: 图片 {len(images)} 条, 视频 {len(videos)} 条")
    for u in images[:5]:
        print(f"  img {u}")
    for u in videos[:5]:
        print(f"  vid {u}")

    if not args.apply:
        print("\n[提示] 加 --apply 才会写入 highres_rules.json")
        return 0
    idx, err = merge_rule(rule)
    if err:
        print(f"[错误] 未写入: {err}")
        return 2
    print(f"[合并] 已写入第 {idx + 1} 条: {RULES_FILE}")
    print("       下次发现该站资源时自动生效，无需任何 token 成本。")
    return 0


if __name__ == "__main__":
    sys.exit(main())