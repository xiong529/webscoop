"""渲染登录态注入测试：cookies.txt 规则 -> Playwright add_cookies / 头注入兜底。

覆盖：
1. 精确域名规则 -> cookie_list（domain=.host, path=/），无头注入
2. 父域规则命中子域（www.example.com <- example.com）
3. 无规则命中 -> 空
4. 全局 `*` 规则 -> 退回头注入（extra_http_headers["Cookie"]）
5. 多值/畸形分段解析（"a=1; b=2" / 空段跳过）
6. 同注册域家族：短链 v.douyin.com / iesdouyin.com 命中 www.douyin.com 规则，
   作用域提升到 .douyin.com（真实接口在 www.douyin.com，短链与接口域不同子域）
"""

import os
import sys
import unittest.mock as mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "resources_reptile"))

import config
import renderer

PASS = 0
FAIL = 0


def check(name, got, expected=True):
    global PASS, FAIL
    if isinstance(expected, bool) or expected is None:
        ok = (got is expected)
    else:
        ok = (got == expected)
    if ok:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name}: got={got!r} expected={expected!r}")


def _load(rules):
    with mock.patch("resources_reptile.utils.cookies.load_cookie",
                    return_value=dict(rules)):
        return renderer._cookie_ctx_for("https://www.example.com/path?q=1")


def _test():
    # 1. 父域规则命中子域：cookie_list 用 .example.com 作用域
    cl, hdrs = _load({"example.com": "session=x; token=y"})
    check("cookie: parent rule applied on subdomain",
          cl == [{"name": "session", "value": "x", "domain": ".example.com",
                  "path": "/"},
                 {"name": "token", "value": "y", "domain": ".example.com",
                  "path": "/"}])
    check("cookie: no header fallback", hdrs == {})
    # 2. 精确域名（子域规则：作用域提升到注册域，见家族注入设计）
    cl, _ = _load({"www.example.com": "a=1"})
    check("cookie: exact host match", cl and cl[0]["domain"] == ".example.com")
    # 3. 无命中
    cl, hdrs = _load({"other.com": "a=1"})
    check("cookie: no rule -> empty", cl == [] and hdrs == {})
    # 4. 全局 * -> 头注入兜底
    cl, hdrs = _load({"*": "sid=9"})
    check("cookie: wildcard -> header injection", cl == [] and hdrs == {"Cookie": "sid=9"})
    # 5. 全域留空规则
    cl, hdrs = _load({})
    check("cookie: empty rules -> empty", cl == [] and hdrs == {})
    # 6. 畸形分段
    cl, _ = _load({"example.com": "a=1; badseg; =x; b=2"})
    check("cookie: malformed segments skipped", len(cl) == 2)


def _test_family():
    import tempfile
    import resources_reptile.utils.cookies as cuk
    _old = config.COOKIE_FILE
    # 短链入口命中 www.douyin.com 规则：作用域提升到 .douyin.com
    with mock.patch("resources_reptile.utils.cookies.load_cookie",
                    return_value={"www.douyin.com": "sessionid=s1; ttwid=t9"}):
        cl, hdrs = renderer._cookie_ctx_for("https://v.douyin.com/xxxx/")
    check("family: v.douyin -> www rule matched",
          len(cl) == 2 and hdrs == {})
    check("family: scope lifted to .douyin.com",
          cl and all(c["domain"] == ".douyin.com" for c in cl))
    # www.douyin.com 目标命中 v.douyin.com 规则（登在短链、接口在 www 的镜像场景）
    with mock.patch("resources_reptile.utils.cookies.load_cookie",
                    return_value={"v.douyin.com": "sid=a"}):
        cl, _ = renderer._cookie_ctx_for("https://www.douyin.com/user/abc")
    check("family: www.douyin -> v rule matched", cl and cl[0]["domain"] == ".douyin.com")
    # 不同注册域不误命中
    with mock.patch("resources_reptile.utils.cookies.load_cookie",
                    return_value={"www.douyin.com": "sid=a"}):
        cl, hdrs = renderer._cookie_ctx_for("https://www.example.org/u")
    check("family: different registrable no match", cl == [] and hdrs == {})
    # utils.cookies.cookie_for 家族命中 + 保存后重载（用临时 cookies.txt 走真实解析链）
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        _path = f.name
    config.COOKIE_FILE = _path
    cuk._loaded = False
    try:
        check("reload: empty before write", cuk.load_cookie() == {})
        with open(_path, "a", encoding="utf-8") as f:
            f.write("www.douyin.com:  sid=z\n")
        rul = cuk.reload_cookie()
        check("reload: new rule visible", rul.get("www.douyin.com") == "sid=z")
        check("family: cookie_for v.douyin", cuk.cookie_for("v.douyin.com") == "sid=z")
        # iesdouyin.com 与 douyin.com 是不同注册域：不误命中（其页面接口本就跨站到 www.douyin.com）
        check("family: cookie_for iesdouyin no match",
              cuk.cookie_for("www.iesdouyin.com") is None)
    finally:
        config.COOKIE_FILE = _old
        cuk._loaded = False
        os.remove(_path)


def _test_capture_session():
    """CookieCaptureSession 状态机与保存消息（mock 浏览器对象，不弹真实窗口）。"""
    import tempfile
    from cookie_capture import CookieCaptureSession
    cc = CookieCaptureSession()
    # 1. 未启动
    n, msg = cc.save_to_file()
    check("capture: not started message", "浏览器尚未弹出" in msg)
    # 2. 浏览器已关闭（窗口被用户关掉）
    fake = mock.Mock()
    fake.is_connected.return_value = False
    cc._browser = fake
    cc._context = mock.Mock()
    check("capture: state closed", cc.state() == "closed")
    n, msg = cc.save_to_file()
    check("capture: closed message", "窗口已关闭" in msg)
    # 3. 浏览器在线但无 cookie
    fake.is_connected.return_value = True
    cc._context.cookies.return_value = []
    check("capture: state ok", cc.state() == "ok")
    n, msg = cc.save_to_file()
    check("capture: open-no-cookie message", "未捕获到 Cookie" in msg)
    # 4. 正常保存：写文件 + 重载缓存
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        _p3 = f.name
    cc._context.cookies.return_value = [
        {"name": "sid", "value": "s9", "domain": "www.douyin.com"},
        {"name": "ttwid", "value": "t8", "domain": ".douyin.com"},
    ]
    n, msg = cc.save_to_file(_p3)
    check("capture: saved 1 domain", n == 1 and "已写入 1 个域名" in msg)
    import resources_reptile.utils.cookies as cuk
    _oldf = config.COOKIE_FILE
    config.COOKIE_FILE = _p3
    cuk._loaded = False
    try:
        check("capture: saved rule readable", cuk.cookie_for("v.douyin.com") == "sid=s9; ttwid=t8")
    finally:
        config.COOKIE_FILE = _oldf
        cuk._loaded = False
        os.remove(_p3)


# 5. 手动粘贴兜底：Cookie 头 + 域名 -> cookies.txt + 重载
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        _p4 = f.name
    n, msg = cc.save_paste_to_file("Cookie: sessionid=p1; ttwid=p2",
                                   "v.douyin.com", _p4)
    check("paste: saved 1 row",
          n == 1 and "douyin.com:" in msg and "sessionid=p1" in msg)
    config.COOKIE_FILE = _p4
    cuk._loaded = False
    try:
        check("paste: rule readable via family",
              cuk.cookie_for("www.douyin.com") == "sessionid=p1; ttwid=p2")
    finally:
        config.COOKIE_FILE = _oldf
        cuk._loaded = False
        os.remove(_p4)
    n, msg = cc.save_paste_to_file("not-a-cookie", "www.example.com", _p4)
    check("paste: garbage rejected", n == 0)
    # 6. 相对路径解析到项目根目录（模块三级上溯），与注入读取同一文件，
    #    不依赖进程 CWD——曾因少上溯一级导致「保存的文件读不到」。
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
        cuk.__file__)))
    check("path: relative resolves to repo root",
          cuk.cookie_file_path("cookies.txt") ==
          os.path.join(repo_root, "cookies.txt"))
    check("path: absolute kept", cuk.cookie_file_path(r"C:\x\c.txt") ==
          r"C:\x\c.txt")
    with tempfile.TemporaryDirectory() as _td6:
        _fake = os.path.join(_td6, "fake.txt")
        with mock.patch("resources_reptile.utils.cookies.cookie_file_path",
                        return_value=_fake):
            n, msg = cc.save_paste_to_file("Cookie: sid=q1", "v.douyin.com")
        check("paste: save uses cookie_file_path", n == 1 and
              os.path.exists(_fake))


if __name__ == "__main__":
    _test()
    _test_family()
    _test_capture_session()
    print(f"\n{'-' * 40}\nunit_cookie_render: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)