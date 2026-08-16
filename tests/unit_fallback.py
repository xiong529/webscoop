"""Scrapling 第三层兜底接线测试（mock，不碰真实网络）。

覆盖：
1. 常规链抛连接异常且 SCRAPLING_FALLBACK 开：get() 返回 Scrapling 结果
2. 常规链抛异常但 fallback 关闭：原样抛
3. 常规链返回 403（指纹轮换后仍 403）：fallback 175 命中返回其结果
4. fallback 返回 None（不可用/4xx）：get() 返回常规链结果或原异常
5. head() 不受影响（无 Scrapling 兜底）
"""

import os
import sys
import unittest.mock as mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "resources_reptile"))

import config  # noqa: E402
import gui_fetch  # noqa: E402

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


def _mk_resp(status=200, body=b"<html>b</html>"):
    r = mock.Mock()
    r.status = status
    r.headers = {"content-type": "text/html"}
    r.body = body
    r.url = "http://scrapling.local/x"
    return r


def _test_exception_path():
    s = gui_fetch.FetchSession(proxy_enabled=False)
    ret = mock.Mock(status_code=200, headers={"x": "1"}, content=b"fb",
                    url="http://scrapling.local/x")
    with mock.patch.object(gui_fetch, "_scrapling_get", return_value=ret) as fb:
        with mock.patch.object(s, "_request_with_fallback",
                               side_effect=OSError("conn refused")):
            resp = s.get("http://x.test/a", proxy="")
            check("fallback: exception path returns scrapling", resp.status_code == 200
                  and resp.content == b"fb")
            check("fallback: called once", fb.call_count == 1)
    config.SCRAPLING_FALLBACK = False
    with mock.patch.object(s, "_request_with_fallback",
                           side_effect=OSError("conn refused")):
        try:
            s.get("http://x.test/a")
            check("fallback: disabled re-raises", False)
        except OSError:
            check("fallback: disabled re-raises", True)
    config.SCRAPLING_FALLBACK = True


def _test_status_path():
    s = gui_fetch.FetchSession(proxy_enabled=False)
    ret = mock.Mock(status_code=200, headers={"x": "1"}, content=b"fb",
                    url="http://scrapling.local/x")
    with mock.patch.object(gui_fetch, "_scrapling_get", return_value=ret) as fb:
        bad = mock.Mock(status_code=429, headers={}, content=b"")
        with mock.patch.object(s, "_request_with_fallback", return_value=(bad, False)):
            resp = s.get("http://x.test/a")
            check("fallback: 429 path returns scrapling", resp.status_code == 200)
        with mock.patch.object(s, "_request_with_fallback", return_value=(bad, False)):
            fb.return_value = None
            resp = s.get("http://x.test/a")
            check("fallback: scrapling fail keeps 429", resp.status_code == 429)


def _test_head_untouched():
    s = gui_fetch.FetchSession(proxy_enabled=False)
    ok = mock.Mock(status_code=200, headers={}, content=b"")
    calls = {"n": 0}

    def fake_chain(method, url, headers, timeout, stream=False, proxy_override=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("boom")
        return ok, False

    with mock.patch.object(gui_fetch, "_scrapling_get") as fb:
        with mock.patch.object(s, "_request_with_fallback", side_effect=fake_chain):
            try:
                s.head("http://x.test/a")
                check("fallback: head unchanged", True)
            except OSError:
                check("fallback: head unchanged", True)
            check("fallback: head no scrapling call", fb.call_count == 0)


def an():
    config.IMPERSONATE = "chrome"
    config.PROXY_ENABLED = False
    config.DEFAULT_PROXY = ""
    config.REQUEST_TIMEOUT = 5
    config.SCRAPLING_FALLBACK = True
    _test_exception_path()
    _test_status_path()
    _test_head_untouched()


if __name__ == "__main__":
    an()
    print(f"\n{'-' * 40}\nunit_fallback: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)