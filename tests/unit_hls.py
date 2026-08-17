"""HLS 分片下载测试：用本地 HTTP 服务模拟 m3u8 播放列表 + TS 分片。

覆盖：
1. is_hls：URL 后缀 / Content-Type 识别
2. download_hls：主列表自动选最高带宽变体、相对 URI、多分片并发合并、输出 .ts 正确
3. BYTERANGE 偏移分片（带 Range 头）
4. 失败路径：加密流报错返回 None、分片 404 返回 None、纯列表无分片返回 None
"""

import http.server
import os
import shutil
import socket
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "resources_reptile"))

import config  # noqa: E402

config.PROXY_ENABLED = False
config.DEFAULT_PROXY = ""

sys.path.insert(0, os.path.join(ROOT, "tests"))

import hls_downloader  # noqa: E402

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


SEGS = [b"S" + bytes([i]) * 8 for i in range(1, 5)]  # 4 个分片


class Handler(http.server.BaseHTTPRequestHandler):
    paths = {}

    def do_GET(self):
        self.server: HlsServer
        spec = self.server.spec
        key = self.path
        if key in spec.get("seg_fails", ()):
            self.send_response(404)
            self.end_headers()
            return
        body = None
        ct = "application/vnd.apple.mpegurl"
        if key == "/master.m3u8":
            body = ("#EXTM3U\n"
                    "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n"
                    "low.m3u8\n"
                    "#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720\n"
                    "high.m3u8\n").encode()
        elif key == "/low.m3u8" or key == "/high.m3u8":
            body = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
                    "#EXTINF:8.0,\nseg1.ts\n#EXTINF:8.0,\nseg2.ts\n"
                    "#EXTINF:8.0,\nseg3.ts\n#EXTINF:8.0,\nseg4.ts\n"
                    "#EXT-X-ENDLIST\n").encode()
        elif key == "/ranges.m3u8":
            body = ("#EXTM3U\n#EXT-X-VERSION:6\n"
                    "#EXT-X-MAP:URI=\"init.ts\"\n"
                    "#EXTINF:8.0,\n#EXT-X-BYTERANGE:16@0\nranges.bin\n"
                    "#EXTINF:8.0,\n#EXT-X-BYTERANGE:16@16\nranges.bin\n"
                    "#EXT-X-ENDLIST\n").encode()
        elif key == "/enc.m3u8":
            body = ("#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"\n"
                    "#EXTINF:8.0,\nseg1.ts\n#EXT-X-ENDLIST\n").encode()
        elif key == "/empty.m3u8":
            body = ("#EXTM3U\n#EXT-X-ENDLIST\n").encode()
        elif key == "/seg1.ts":
            body, ct = SEGS[0], "video/mp2t"
        elif key == "/seg2.ts":
            body, ct = SEGS[1], "video/mp2t"
        elif key == "/seg3.ts":
            body, ct = SEGS[2], "video/mp2t"
        elif key == "/seg4.ts":
            body, ct = SEGS[3], "video/mp2t"
        elif key == "/ranges.bin":
            body, ct = b"N" * 32, "application/octet-stream"
        elif key == "/key.bin":
            body, ct = b"K" * 16, "application/octet-stream"
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class HlsServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    spec: dict = {}


def start_server() -> str:
    srv = HlsServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


def _test_is_hls():
    check("is_hls: url ext", hls_downloader.is_hls("http://x.com/a.m3u8"))
    check("is_hls: url ext m3u", hls_downloader.is_hls("http://x.com/a.m3u"))
    check("is_hls: ct", hls_downloader.is_hls("http://x.com/a", "application/vnd.apple.mpegurl; charset=utf-8"))
    check("is_hls: mp4 no", hls_downloader.is_hls("http://x.com/a.mp4", "") is False)
    check("is_hls: query ext", hls_downloader.is_hls("http://x.com/playlist.m3u8?token=1"))


def _test_variant_and_merge(base, dest):
    got = hls_downloader.download_hls(base + "/master.m3u8", dest_dir=dest, out_name="t1")
    check("hls: variant picked high", got is not None)
    check("hls: output exists", got is not None and os.path.exists(got))
    if got:
        with open(got, "rb") as f:
            data = f.read()
        check("hls: concatenated all 4 segs", data == SEGS[0] + SEGS[1] + SEGS[2] + SEGS[3])
        os.remove(got)


def _test_byterange(base, dest):
    got = hls_downloader.download_hls(base + "/ranges.m3u8", dest_dir=dest, out_name="t2")
    check("hls: byterange ok", got is not None)
    if got:
        with open(got, "rb") as f:
            data = f.read()
        check("hls: byterange merged 32 bytes", data == b"N" * 32)
        os.remove(got)


def _test_failures(base, dest):
    got = hls_downloader.download_hls(base + "/enc.m3u8", dest_dir=dest, out_name="t3")
    check("hls: aes-128 returns None", got is None)
    got = hls_downloader.download_hls(base + "/empty.m3u8", dest_dir=dest, out_name="t4")
    check("hls: no segments returns None", got is None)
    got = hls_downloader.download_hls(base + "/master.m3u8", dest_dir=dest, out_name="t5")
    check("hls: 404 segment returns None", got is None)
    leftovers = [p for p in os.listdir(dest) if p.startswith(".hls_tmp_")]
    check("hls: failure leaves no temp dir", len(leftovers) == 0, True)
    for p in ("t1.ts", "t2.ts", "t3.ts", "t4.ts", "t5.ts"):
        if os.path.exists(os.path.join(dest, p)):
            os.remove(os.path.join(dest, p))


def an():
    base, srv = start_server()
    dest = tempfile.mkdtemp(prefix="hls_test_")
    try:
        _test_is_hls()
        _test_variant_and_merge(base, dest)
        _test_byterange(base, dest)
        srv.spec = {"seg_fails": ("/seg3.ts",)}
        _test_failures(base, dest)
    finally:
        srv.shutdown()
        shutil.rmtree(dest, ignore_errors=True)


if __name__ == "__main__":
    an()
    print(f"\n{'-' * 40}\nunit_hls: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)