"""代理池管理：加载、轮换、按站绑定与失败吊销（GUI 与 Scrapy 共用）。

历史问题：旧实现只在 DEFAULT_PROXY 为空时才用代理池，且每次请求都重读
文件；Scrapy 的 meta["proxy"] 与 curl_cffi Session 的 proxies 是两条独立
路径，导致「配置了池也只用本机单代理」。

本模块提供一个进程内共享的 ProxyPool：

- load：从 proxies.txt / 环境变量 / Scrapy 设置一次性加载并缓存
- next()：轮换挑选（可绑定站点，同一站点尽量用同一出口，避免抖 IP）；
  已采样的代理按「响应时间 + 最近使用」加权，快代理优先
- revoke()：失败吊销，进入冷却，冷却期后重新可用（避免误杀临时抖动）
- health_check()：并发探活，死代理提前吊销（避免首个请求撞坏代理），
  成功者记录响应时间供加权挑选；ensure_health_monitor() 起后台线程
  定期探活（GUI / Scrapy 启动时各调一次，幂等）
- 与 config.DEFAULT_PROXY 的关系：DEFAULT_PROXY 是「本机中转」，仍优先生效；
  代理池在无默认代理或默认代理失败时接管（保持历史语义，但真正生效）
"""

from __future__ import annotations

import os
import random
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROBE_DEFAULT_URL = "http://www.gstatic.com/generate_204"

_PROXY_PATTERN = re.compile(
    r"^(?P<scheme>https?|socks[45]|http)://"
    r"(?:(?P<user>[^:/@]+):(?P<password>[^@/]+)@)?"
    r"(?P<host>[^:/\s]+)(?::(?P<port>\d+))?/?$"
)


def load_proxies(source: str = "") -> list[str]:
    """从字符串 / 文件 / 环境变量加载代理列表（去重 + 格式校验）。"""
    proxies: list[str] = []

    if source:
        proxies.extend(s.strip() for s in re.split(r"[\s,]+", source) if s.strip())

    env = os.environ.get("RESOURCES_PROXY_URLS", "")
    if env:
        proxies.extend(s.strip() for s in re.split(r"[\s,]+", env) if s.strip())

    # 项目根目录 proxies.txt
    root = Path(__file__).resolve().parent.parent.parent
    txt = root / "proxies.txt"
    if txt.exists():
        with open(txt, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    proxies.append(line)

    # 去重并过滤非法格式
    seen, valid = set(), []
    for p in proxies:
        if p in seen:
            continue
        seen.add(p)
        if _PROXY_PATTERN.match(p):
            valid.append(p)
    return valid


def get_random_proxy(source: str = "") -> str | None:
    """随机返回一个代理 URL；无可用代理时返回 None（兼容旧调用）。"""
    return pool.proxy() if source == "" else _pick_random(load_proxies(source))


def _pick_random(proxies: list[str]) -> str | None:
    return random.choice(proxies) if proxies else None


def _probe_one(proxy: str, probe_url: str, timeout: float) -> bool:
    """走代理探测一次：2xx/3xx 视为可用。

    socks 代理无法用 urllib 探测，跳过视为可用（靠请求时失败吊销兜底）。
    """
    if proxy.startswith("socks"):
        return True
    try:
        entry = proxy if "://" in proxy else "http://" + proxy
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": entry, "https": entry}))
        req = urllib.request.Request(
            probe_url, headers={"User-Agent": "webscoop-health/1.0"})
        with opener.open(req, timeout=timeout) as resp:
            code = int(resp.status)
            return 200 <= code < 400
    except Exception:
        return False


class ProxyPool:
    """进程内共享代理池：轮换挑选 + 失败吊销（带冷却）。

    线程安全：GUI 多线程下载与 Scrapy 下载器可能并发取代理。
    容量上限：加载/重载时截断到 max_size（默认取 RESOURCES_PROXY_POOL_MAX，
    默认 200），防止代理文件被误写超长导致内存无限增长。
    """

    def __init__(self, cool_seconds: int = 300, max_fails: int = 2,
                 proxies: list[str] | None = None, cache_file: str = "",
                 max_size: int = 0):
        self._lock = threading.Lock()
        self._cache_file = cache_file  # 非空时从该文件加载（测试注入用）
        self._cool_seconds = cool_seconds      # 吊销后冷却时长（秒）
        self._max_fails = max_fails            # 连续失败多少次吊销
        self._max_size = max(1, int(max_size or os.environ.get(
            "RESOURCES_PROXY_POOL_MAX", "200")))
        self._fails: dict[str, int] = {}       # proxy -> 连续失败次数
        self._revoked_until: dict[str, float] = {}  # proxy -> 解除吊销时间
        self._last_used: dict[str, float] = {}  # proxy -> 最近使用时间
        self._latency: dict[str, float] = {}    # proxy -> 最近探活响应时间（秒）
        self._site_map: dict[str, str] = {}     # host -> 当前绑定的代理
        self._proxies: list[str] = list(proxies or [])[:self._max_size]
        self._loaded_at = time.time() if proxies else 0.0

    def _reload_locked(self) -> None:
        # 每 60s 才重读一次文件，避免高频 reload（文件是静态配置）
        if self._proxies and time.time() - self._loaded_at < 60:
            return
        if self._cache_file:
            loaded = self._load_from_file(self._cache_file)
        else:
            loaded = load_proxies()
        if loaded:
            self._proxies = loaded[:self._max_size]
            self._loaded_at = time.time()

    def reload_now(self) -> None:
        """立即重读代理来源（GUI「代理设置」保存后调用，跳过 60s 冷却缓存）。"""
        with self._lock:
            self._loaded_at = 0.0
            self._reload_locked()

    @staticmethod
    def _load_from_file(path: str) -> list[str]:
        from pathlib import Path
        p = Path(path)
        if not p.exists():
            return []
        out = []
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(line)
        return out

    def _candidates_locked(self) -> list[str]:
        """可用代理（未吊销或在冷却期外），按「响应时间 + 最近使用」加权排序。

        加权：已探活采样的代理里，响应快的优先（_latency 升序）；
        未采样过的一律视为同权（走 last_used 轮换），首次使用即被
        后台探活线程补采。排序键 = (latency 或同值, 最近使用升序)。
        """
        now = time.time()
        self._reload_locked()
        usable = [
            p for p in self._proxies
            if self._revoked_until.get(p, 0) < now
        ]
        if not usable:
            return []
        # 未采样的代理用 -1 键排在已采样之前：新代理优先试用并补采样，
        # 采样收敛后同一档内按「快代理优先 + 少用优先」轮换
        return sorted(
            usable,
            key=lambda p: (self._latency.get(p, -1),
                           self._last_used.get(p, 0.0)))

    def proxy(self, host: str = "") -> str | None:
        """取一个代理。host 非空时优先返回该站点已绑定的代理（保持出口稳定）。"""
        with self._lock:
            host = (host or "").lower().removeprefix("www.")
            if host and host in self._site_map:
                bound = self._site_map[host]
                if self._revoked_until.get(bound, 0) < time.time():
                    self._last_used[bound] = time.time()
                    return bound
                self._site_map.pop(host, None)  # 绑定代理被吊销，解除绑定
            cands = self._candidates_locked()
            if not cands:
                return None
            # 选最近最少使用的（轮换），避免并发取到同一个
            pick = cands[0]
            # 绑定站点：同一个站尽量同一个出口（防抖 IP 触发风控）
            if host:
                self._site_map[host] = pick
            self._last_used[pick] = time.time()
            return pick

    def revoke(self, proxy: str, reason: str = "", force: bool = False) -> None:
        """吊销一个代理：连续失败达阈值才进入冷却，冷却后可复用。

        force=True 用于连接级失败等强信号，一次即进入冷却。
        """
        if not proxy:
            return
        with self._lock:
            fails = self._fails.get(proxy, 0) + 1
            self._fails[proxy] = fails
            if force or fails >= self._max_fails:
                self._revoked_until[proxy] = time.time() + self._cool_seconds
                self._fails[proxy] = 0

    def success(self, proxy: str) -> None:
        """成功一次：清零失败计数（消除误伤）。"""
        if not proxy:
            return
        with self._lock:
            self._fails.pop(proxy, None)

    def health_check(self, probe_url: str = "", timeout: float = 5.0,
                     concurrency: int = 8) -> dict[str, bool]:
        """并发探活池内所有代理；不可用的立即吊销（force），返回 proxy -> ok。

        probe_url 为空用默认探针；socks 代理跳过（视为可用）。
        成功探活的代理记录响应时间（供 _candidates_locked 加权挑选）。
        """
        with self._lock:
            targets = list(self._proxies)
        if not targets:
            return {}
        results: dict[str, bool] = {}
        res_lock = threading.Lock()

        def probe(p: str) -> None:
            t0 = time.monotonic()
            ok = _probe_one(p, probe_url or PROBE_DEFAULT_URL, timeout)
            latency = time.monotonic() - t0
            with res_lock:
                results[p] = ok
                if ok:
                    self._latency[p] = latency
            if not ok:
                self.revoke(p, "health-fail", force=True)

        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            list(ex.map(probe, targets))
        return results

    def mark_used(self, proxy: str, ok: bool, latency: float = 0.0) -> None:
        """下载侧回传单次使用结果：成功采样响应时间，失败计入吊销计数。"""
        if not proxy:
            return
        if ok:
            with self._lock:
                self._latency[proxy] = latency if latency > 0 else self._latency.get(proxy, 0.0)
            self.success(proxy)
        else:
            self.revoke(proxy, "use-fail")

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._proxies)


# 进程内共享单例
pool = ProxyPool()


# 后台定期探活（幂等启动：GUI / Scrapy 各调一次不重复起线程）
_MONITOR_LOCK = threading.Lock()
_MONITOR_STARTED = False


def ensure_health_monitor(interval: float = 60.0, timeout: float = 5.0) -> None:
    """启动后台代理探活线程（幂等）。

    每 interval 秒对池内全部代理做一次并发探活：失败者立即吊销，
    成功者刷新响应时间样本（供加权挑选）。代理池为空时跳过。
    GUI 与 Scrapy 启动路径各调一次，内部保证只起一个线程。
    """
    global _MONITOR_STARTED
    with _MONITOR_LOCK:
        if _MONITOR_STARTED:
            return
        _MONITOR_STARTED = True

    def loop() -> None:
        while True:
            time.sleep(interval)
            try:
                if pool.size:
                    pool.health_check(timeout=timeout, concurrency=8)
            except Exception:
                pass  # 探活失败不干扰主流程，下轮再试

    threading.Thread(target=loop, daemon=True, name="proxy-health").start()


# 兼容旧接口：utils/proxy.py 曾导出这些函数
def current_pool() -> ProxyPool:
    return pool