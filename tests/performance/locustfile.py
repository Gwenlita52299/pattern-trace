"""STRESS-01/02/03 压测场景（stress-test-spec §4）— Locust。

用法（在 tests/performance/ 目录下或 -f 指定本文件）：
    STRESS_PROFILE=read  uv run locust -f tests/performance/locustfile.py \
        --headless --csv baseline_read -u 50 -r 10 -t 3m --host http://localhost:8000

STRESS_PROFILE 三档：
    read   STRESS-01 纯读基线（轮询 60% / subgraph 25% / cases 10% / patterns 5%）
    mixed  STRESS-02 读写混合（叠加 analyze 5%，验证幂等复用与 reclaim_zombies）
    anon   STRESS-03 限流边界（匿名 403/429 + 登录爆破 429，验证预期拒绝语义）

场景切换用环境变量而非 --tags：tags 过滤后可能出现空任务集的 User 类，
profile 门控让所有任务恒定义、按档位提前返回，行为可预期。

预期拒绝口径（spec §3.2）：429/403 用 catch_response 标记 success，
不污染错误率；只有 5xx 与超时计入失败。

账号：seed_volume.py 幂等灌入 stress@perf.local（backend 与本脚本共享同一 DB），
凭据可用 STRESS_EMAIL / STRESS_PASSWORD 覆盖，不硬编码到生产环境。
"""
from __future__ import annotations

import os
import random
import threading

from locust import HttpUser, between, task

PROFILE = os.environ.get("STRESS_PROFILE", "read")
STRESS_EMAIL = os.environ.get("STRESS_EMAIL", "stress@perf.local")
STRESS_PASSWORD = os.environ.get("STRESS_PASSWORD", "StressPerf!2026")

_POOL_LOCK = threading.Lock()
_JUDGMENT_POOL: list[str] = []   # STRESS-01 轮询 id 池：来自压测案件的 latest_judgment
_ADDR_POOL: list[str] = []       # 有 completed 分析的地址池（subgraph 任务数据源）
_DEMO_ADDR: list[str] = []       # analyze 种子地址（fixture 3 个，跨 VU 轮转）
_ADDR_SEQ = 0


def _claim_address() -> str | None:
    """跨 VU 轮转取种子地址；同地址会被并发撞到——这正是 STRESS-02 的幂等场景。"""
    global _ADDR_SEQ
    if not _DEMO_ADDR:
        return None
    with _POOL_LOCK:
        _ADDR_SEQ += 1
        return _DEMO_ADDR[_ADDR_SEQ % len(_DEMO_ADDR)]


class StressUser(HttpUser):
    wait_time = between(0.2, 1.0)  # 近似前端 ~200ms 轮询节奏 + 页面停留

    def on_start(self):
        # CSRF：写请求强制 X-Requested-With，登录本身也是 POST
        self.client.headers.update({"X-Requested-With": "XMLHttpRequest"})
        if PROFILE != "anon":
            r = self.client.post("/api/v1/auth/login", name="warmup:login",
                                 json={"email": STRESS_EMAIL,
                                       "password": STRESS_PASSWORD})
            token = r.json().get("access_token") if r.status_code == 200 else None
            if token:
                self.client.headers.update(
                    {"Authorization": f"Bearer {token}"})
            self._ensure_pool()

    def _fetch_demo_addresses(self) -> None:
        global _DEMO_ADDR
        if _DEMO_ADDR:
            return
        r = self.client.get("/api/v1/demo/addresses", name="warmup:demo")
        _DEMO_ADDR = r.json().get("addresses", []) if r.status_code == 200 else []

    def _ensure_pool(self) -> None:
        """轮询 id 池：优先从压测案件关联取真实 judgment id；空库兜底走一次 analyze。"""
        with _POOL_LOCK:
            if _JUDGMENT_POOL:
                return
        r = self.client.get("/api/v1/cases", name="warmup:cases",
                            params={"page_size": 10})
        if r.status_code != 200:
            return
        for item in r.json().get("items", []):
            detail = self.client.get(f"/api/v1/cases/{item['id']}",
                                     name="warmup:case_detail")
            for a in detail.json().get("addresses", []):
                lj = (a.get("latest_judgment") or {}).get("id")
                with _POOL_LOCK:
                    if lj:
                        _JUDGMENT_POOL.append(lj)
                    if a.get("address") and (lj or not _JUDGMENT_POOL):
                        # demo 种子在空库无 completed 分析会 404，优先用案件关联地址
                        _ADDR_POOL.append(a["address"])
        if not _JUDGMENT_POOL:
            self._fetch_demo_addresses()
            addr = _claim_address()
            if addr:
                r = self.client.post("/api/v1/addresses/analyze",
                                     name="warmup:analyze",
                                     json={"address": addr})
                jid = (r.json() or {}).get("judgment_id")
                if r.status_code in (200, 202) and jid:
                    with _POOL_LOCK:
                        _JUDGMENT_POOL.append(jid)

    # ---- STRESS-01 纯读基线 ----
    @task(60)
    def poll_judgment(self):
        if PROFILE not in ("read", "mixed"):
            return
        with _POOL_LOCK:
            pool = list(_JUDGMENT_POOL)
        if not pool:
            return
        with self.client.get(f"/api/v1/judgments/{random.choice(pool)}",
                             catch_response=True) as r:
            if r.status_code >= 500:
                r.failure(f"5xx leak: {r.status_code}")

    @task(25)
    def subgraph(self):
        if PROFILE not in ("read", "mixed"):
            return
        with _POOL_LOCK:
            addrs = _ADDR_POOL or list(_DEMO_ADDR)
        if not addrs:
            self._fetch_demo_addresses()
            with _POOL_LOCK:
                addrs = list(_DEMO_ADDR)
        if not addrs:
            return
        with self.client.get(f"/api/v1/addresses/{random.choice(addrs)}/subgraph",
                             catch_response=True) as r:
            if r.status_code >= 500:
                r.failure(f"5xx leak: {r.status_code}")

    @task(10)
    def cases_list(self):
        if PROFILE not in ("read", "mixed"):
            return
        with self.client.get("/api/v1/cases", params={"page": random.randint(1, 5)},
                             catch_response=True) as r:
            if r.status_code >= 500:
                r.failure(f"5xx leak: {r.status_code}")

    @task(5)
    def patterns(self):
        if PROFILE not in ("read", "mixed"):
            return
        with self.client.get("/api/v1/patterns", catch_response=True) as r:
            if r.status_code >= 500:
                r.failure(f"5xx leak: {r.status_code}")

    # ---- STRESS-02 读写混合：analyze + 轮询至终态 ----
    @task(5)
    def analyze_and_poll(self):
        if PROFILE != "mixed":
            return
        self._fetch_demo_addresses()
        addr = _claim_address()
        if not addr:
            return
        with self.client.post("/api/v1/addresses/analyze",
                              json={"address": addr},
                              catch_response=True) as r:
            # 202=新建 / 200=幂等复用：两者都合法（BE-12/46）；
            # 429/403 预期拒绝不计错误率（spec §3.2）；其余判定失败
            if r.status_code not in (200, 202, 429, 403):
                r.failure(f"unexpected {r.status_code}: {r.text[:120]}")
                return
            jid = (r.json() or {}).get("judgment_id")
        if r.status_code == 202 and jid:
            # 轮询至终态：fixture+mock 下任务秒级完成，上限防异常挂死。
            # 普通请求即可：locust 对 4xx/5xx 默认判失败，5xx 泄漏自然进错误率
            for _ in range(20):
                payload = self.client.get(f"/api/v1/judgments/{jid}").json()
                if payload.get("status") in ("completed", "failed"):
                    break
                self.wait()

    # ---- STRESS-03 限流边界（匿名）----
    @task(8)
    def anon_analyze_restricted(self):
        if PROFILE != "anon":
            return
        # 非白名单地址：匿名一律 403
        with self.client.post("/api/v1/addresses/analyze",
                              json={"address": "anon-not-allowed-stress"},
                              catch_response=True) as r:
            if r.status_code != 403:
                r.failure(f"expected 403, got {r.status_code}")

    @task(6)
    def anon_analyze_quota(self):
        if PROFILE != "anon":
            return
        self._fetch_demo_addresses()
        addr = _claim_address()
        if not addr:
            return
        with self.client.post("/api/v1/addresses/analyze",
                              json={"address": addr},
                              catch_response=True) as r:
            # 配额内 202、超配额 429 + Retry-After：两者皆预期
            if r.status_code not in (202, 200, 429):
                r.failure(f"expected 202/429, got {r.status_code}")
            elif r.status_code == 429 and not r.headers.get("Retry-After"):
                r.failure("429 without Retry-After")

    @task(4)
    def anon_login_bruteforce(self):
        if PROFILE != "anon":
            return
        with self.client.post("/api/v1/auth/login",
                              json={"email": "bruteforce@stress.local",
                                    "password": "wrong"},
                              catch_response=True) as r:
            # 首次 401（INVALID_CREDENTIALS），SEC-05 计数超限后 429
            if r.status_code not in (401, 429):
                r.failure(f"expected 401/429, got {r.status_code}")
