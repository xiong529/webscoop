"""llm_rules 规则生成器单测:样例提取 / 提示构造 / 假 LLM 全流程 / 正则校验 / 合并落盘。

独立进程运行:python tests/unit_llm.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import llm_rules  # noqa

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name}: got={got!r} want={want!r}")


HTML = """<html><head><title>Gallery</title>
<meta property="og:image" content="https://cdn.example.com/photos/og-768x432.jpg"/>
</head><body>
<img src="https://cdn.example.com/photos/a-1024x576.jpg">
<img data-src="https://cdn.example.com/photos/b-single.jpg">
<img src="https://cdn.example.com/icons/favicon.ico">
<video poster="https://cdn.example.com/vid/cover-800.jpg"
       src="https://cdn.example.com/vid/clip_small.mp4"></video>
<source src="https://cdn.example.com/vid/other_medium.mp4">
</body></html>"""

# --- 样例提取 ---
im, vd = llm_rules.extract_url_samples(HTML, "https://example.com/gallery")
check("og image extracted", "https://cdn.example.com/photos/og-768x432.jpg" in im, True)
check("img src extracted", "https://cdn.example.com/photos/a-1024x576.jpg" in im, True)
check("img data-src extracted", "https://cdn.example.com/photos/b-single.jpg" in im, True)
check("favicon filtered", any("favicon" in u for u in im), False)
check("video poster to images", "https://cdn.example.com/vid/cover-800.jpg" in im, True)
check("video src to videos", "https://cdn.example.com/vid/clip_small.mp4" in vd, True)
check("source src to videos", "https://cdn.example.com/vid/other_medium.mp4" in vd, True)

# --- 空页 / 无样例页 ---
check("empty html yields nothing",
      llm_rules.extract_url_samples("", "https://x.com/"), ([], []))
im2, vd2 = llm_rules.extract_url_samples(
    "<html><body><p>no media</p></body></html>", "https://x.com/")
check("no media page", (im2, vd2), ([], []))
im3, vd3 = llm_rules.extract_url_samples(
    "<html><body><a href='https://canva.com/content-partner/?file-url=https%3A"
    "//cdn.pixabay.com/video/1_large.mp4'>edit</a>"
    "<img src='https://cdn.pixabay.com/video/2_tiny.jpg'/></body></html>",
    "https://x.com/")
check("canva ad link filtered", any("canva" in u for u in vd3), False)

# --- 提示构造 ---
msgs = llm_rules.build_messages("https://x.com/p", im, vd, "image")
check("system prompt has white-list", "regex_sub" in msgs[0]["content"], True)
check("system prompt explains rules", "path_pattern" in msgs[0]["content"], True)
check("user msg has page", "https://x.com/p" in msgs[1]["content"], True)
check("user msg has kind", "期望规则类型: image" in msgs[1]["content"], True)

# --- parse_rule: 兼容 markdown 代码块 & 裸 JSON ---
r1, e1 = llm_rules.parse_rule('{"site":"x","transform":"regex_sub","search":"a","replace":"b"}')
check("parse bare json", (r1 is not None and r1["site"] == "x", e1), (True, None))
r2, e2 = llm_rules.parse_rule('```json\n{"site":"y","transform":"regex_sub"}\n```')
check("parse fenced json", (r2 is not None and r2["site"] == "y", e2), (True, None))
r3, e3 = llm_rules.parse_rule("这不是 JSON")
check("parse garbage yields error", (r3, bool(e3)), (None, True))
r4, e4 = llm_rules.parse_rule("")
check("parse empty yields error", (r4, bool(e4)), (None, True))
r5, e5 = llm_rules.parse_rule('前缀废话 {"kind":"image"} 后缀')
check("parse json amid prose", (r5["kind"], e5), ("image", None))

# --- validate_rule ---
ok, err = llm_rules.validate_rule({"skip": True, "reason": "none"})
check("skip rule valid", (ok, err), (True, None))
ok, err = llm_rules.validate_rule({"transform": "regex_sub", "search": "a", "replace": "b"})
check("valid regex_sub", (ok, err), (True, None))
ok, err = llm_rules.validate_rule({"transform": "unknown_thing"})
check("unknown transform rejected", ok, False)
ok, err = llm_rules.validate_rule({"transform": "regex_sub", "search": "a"})
check("regex_sub without replace rejected", ok, False)
ok, err = llm_rules.validate_rule({"transform": "regex_sub", "search": "[", "replace": "b"})
check("bad regex rejected", ok, False)
ok, err = llm_rules.validate_rule({"transform": "regex_sub", "search": "a", "replace": "b",
                                   "kind": "audio"})
check("bad kind rejected", ok, False)

# --- 假 LLM 全流程 ---
def fake_llm(messages, **kw):
    return json.dumps({
        "site": "example cdn thumb -> full",
        "enabled": True,
        "kind": "image",
        "match": "cdn.example.com",
        "path_pattern": "",
        "transform": "regex_sub",
        "search": r"\-(\d{3,4})x(\d{3,4})\.jpg",
        "replace": ".jpg",
    })


def fake_llm_skip(messages, **kw):
    return json.dumps({"skip": True, "reason": "已是原图"})


rule, page, (ims, vds), err = llm_rules.analyze_page(HTML, "https://example.com/gallery",
                                                     "image", llm=fake_llm)
check("analyze returns clean rule", err, None)
check("analyze rule kind", rule.get("kind"), "image")
check("analyze samples summary", (len(ims), len(vds)), (4, 2))

rule_s, page_s, _, err_s = llm_rules.analyze_page(
    "<html><img src='https://u.example.com/orig-100.jpeg'/></html>",
    "https://example.com/", "image", llm=fake_llm_skip)
check("analyze skip", (rule_s.get("skip", False), err_s), (True, None))

# --- 合并落盘 + 规则实际生效 ---
tmp = os.path.join(tempfile.gettempdir(), "llm_rules_test.json")
with open(tmp, "w", encoding="utf-8") as f:
    json.dump({"version": 1, "comment": "test", "rules": []}, f, ensure_ascii=False)

idx, err = llm_rules.merge_rule(rule, rules_file=tmp)
check("merge index 0", (idx, err), (0, None))
idx2, err2 = llm_rules.merge_rule(rule, rules_file=tmp)
check("dedupe returns -1", (idx2, "已存在" in (err2 or "")), (-1, True))

data = json.load(open(tmp, encoding="utf-8"))
check("rule persisted", len(data["rules"]), 1)

# 复用临时规则文件,验证 highres_url 能应用新规则
import discover_common  # noqa
discover_common.RULES_FILE = tmp
discover_common.reload_rules()
check("new regex_sub rule works",
      discover_common.highres_url("https://cdn.example.com/photos/hello-768x432.jpg"),
      "https://cdn.example.com/photos/hello.jpg")
check("non-matching site untouched",
      discover_common.highres_url("https://other.example.com/pic-768x432.jpg"),
      "https://other.example.com/pic-768x432.jpg")

os.remove(tmp)

# --- 配置存取 roundtrip（临时文件） ---
import config  # noqa
import tempfile
_cfg_tmp = os.path.join(tempfile.gettempdir(), "unit_llm_cfg.json")
_old_cfg = config.LLM_CONFIG_FILE
config.LLM_CONFIG_FILE = _cfg_tmp
os.environ.pop("RESOURCES_LLM_BASE", None)
os.environ.pop("RESOURCES_LLM_KEY", None)
os.environ.pop("RESOURCES_LLM_MODEL", None)
check("save returns empty on success",
      llm_rules.save_llm_config("https://api.test.com/v1", "sk-unit", "m1"), "")
cfg = llm_rules.load_llm_config()
check("config roundtrip base", cfg["base_url"], "https://api.test.com/v1")
check("config roundtrip key", cfg["api_key"], "sk-unit")
check("config roundtrip model", cfg["model"], "m1")
# 环境变量应覆盖文件值
os.environ["RESOURCES_LLM_MODEL"] = "env-model"
check("env overrides file model", llm_rules.load_llm_config()["model"], "env-model")
os.environ.pop("RESOURCES_LLM_MODEL", None)
config.LLM_CONFIG_FILE = _old_cfg
if os.path.exists(_cfg_tmp):
    os.remove(_cfg_tmp)

# --- test_connection 的请求路径（不真发网络：用假 requests 注入不了，走错误分支） ---
os.environ["RESOURCES_LLM_KEY"] = ""
# 未配置 key 时直接拒绝
ok, msg = llm_rules.test_connection()
check("test_connection without key", (ok, "API Key" in str(msg)), (False, True))

print(f"DONE  PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)