"""STRESS 场景（stress-test-spec §4）— Locust。

用法（推荐经单一入口 `run_stress.py`，它负责环境守卫/池/归档）：
    STRESS_PROFILE=read STRESS_ID_POOL_FILE=output/stress/pools/dev-pool.json \
      uv run locust -f tests/performance/locustfile.py \
      --headless --csv baseline_read -u 50 -r 10 -t 3m --host http://localhost:8000

STRESS_PROFILE 四档（queue-* 专线为 spec P1，本文件先留位）：
    read   STRESS-01 纯读基线（轮询 60% / subgraph 25% / cases 10% / patterns 5%）
    mixed  STRESS-02 读写混合（叠加 analyze 5%，验证幂等复用与 reclaim_zombies）
    anon   STRESS-03 限流边界（匿名 403/429 + 登录爆破 429，验证预期拒绝语义）

关键环境变量：
    STRESS_ID_POOL_FILE   spec §2.3 分层抽样池（run_stress.py pool 产出）。
                          缺省时退化为 API 派生池，输出标注 derived(hot-only)，
                          该退化只允许冒烟、不得作为基线。
    STRESS_POOL_TIER      hot | cold，轮询任务用哪一档（默认 hot），两档不合并统计。
    STRESS_MOCK_SCENARIO  可选；仅 mock provider 生效，注入失败路径（如 timeout）。
    STRESS_EXPECT_FAILURE 1 = 期望任务落到 failed 终态（配合上面那项）。

判定口径（spec §3.2，不可只看 5xx）：非预期 4xx、以及 2xx 但缺关键字段都计失败；
仅 429/403 与脚本化失败路径算预期拒绝，不计入错误率。

账号：seed_volume.py 幂等灌入 stress@perf.local（backend 与本脚本共享同一 DB），
凭据可用 STRESS_EMAIL / STRESS_PASSWORD 覆盖，不硬编码到生产环境。
"""
from __future__ import annotations

import importlib.util
import os
import random
import threading
from pathlib import Path

from locust import HttpUser, between, task


def _load_pool_module():
    """按文件路径加载同目录 pool.py。

    不走 sys.path：locust 以文件方式加载本模块，同目录模块不保证在搜索路径上，
    直接按路径加载既确定又避免与其他名为 pool 的第三方模块撞名。
    """
    path = Path(__file__).resolve().parent / "pool.py"
    spec = importlib.util.spec_from_file_location("stress_perf_pool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pool_mod = _load_pool_module()

PROFILE = os.environ.get("STRESS_PROFILE", "read")
STRESS_EMAIL = os.environ.get("STRESS_EMAIL", "stress@perf.local")
STRESS_PASSWORD = os.environ.get("STRESS_PASSWORD", "StressPerf!2026")
POOL_FILE = os.environ.get("STRESS_ID_POOL_FILE", "")
POOL_TIER = os.environ.get("STRESS_POOL_TIER", pool_mod.HOT)
MOCK_SCENARIO = os.environ.get("STRESS_MOCK_SCENARIO", "")
EXPECT_FAILURE = os.environ.get("STRESS_EXPECT_FAILURE", "0") == "1"

_POOL_LOCK = threading.Lock()
_POOL: dict = {}
_POOL_ORIGIN = "derived(hot-only)"
_DEMO_ADDR: list[str] = []       # analyze 种子地址（fixture 3 个，跨 VU 轮转）
_ADDR_SEQ = 0

if POOL_FILE:
    _POOL = pool_mod.load_pool(POOL_FILE)
    _POOL_ORIGIN = f"file:{Path(POOL_FILE).name}"
    print(f"[pool] {_POOL_ORIGIN} hot={len(_POOL.get(pool_mod.HOT, []))} "
          f"cold={len(_POOL.get(pool_mod.COLD, []))} tier={POOL_TIER}")
else:
    print("[pool] derived(hot-only): STRESS_ID_POOL_FILE 未设置——"
          "该口径只允许冒烟，不得作为基线（spec §2.3）")


def _claim_address() -> str | None:
    """跨 VU 轮转取种子地址；同地址会被并发撞到——这正是 STRESS-02 的幂等场景。"""
    global _ADDR_SEQ
    if not _DEMO_ADDR:
        return None
    with _POOL_LOCK:
        _ADDR_SEQ += 1
        return _DEMO_ADDR[_ADDR_SEQ % len(_DEMO_ADDR)]


def _expect(response, required: tuple[str, ...] = ()):
    """2xx + 关键字段齐全才算成功（spec §3.2）。返回 payload 或 None。"""
    if response.status_code != 200:
        response.failure(f"unexpected {response.status_code}: {response.text[:120]}")
        return None
    try:
        payload = response.json()
    except Exception:
        response.failure("non-JSON body")
        return None
    if not isinstance(payload, dict):
        response.failure("non-object body")
        return None
    missing = [f for f in required if not payload.get(f)]
    if missing:
        response.failure(f"missing fields {missing}")
        return None
    return payload


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
        """池来源二选一：优先 spec §2.3 的池文件；否则 API 派生（仅冒烟）。

        派生路径优先从压测案件关联取真实 judgment id，空库兜底走一次 analyze。
        """
        if _POOL.get(pool_mod.HOT):
            return
        with _POOL_LOCK:
            if _POOL.get(pool_mod.HOT):
                return
        bucket: list[str] = []
        addrs: list[str] = []
        r = self.client.get("/api/v1/cases", name="warmup:cases",
                            params={"page_size": 10})
        if r.status_code == 200:
            for item in r.json().get("items", []):
                detail = self.client.get(f"/api/v1/cases/{item['id']}",
                                         name="warmup:case_detail")
                for a in detail.json().get("addresses", []):
                    lj = (a.get("latest_judgment") or {}).get("id")
                    if lj:
                        bucket.append(lj)
                    if a.get("address"):
                        addrs.append(a["address"])
        if not bucket:
            self._fetch_demo_addresses()
            addr = _claim_address()
            if addr:
                r = self.client.post("/api/v1/addresses/analyze",
                                     name="warmup:analyze",
                                     json={"address": addr})
                jid = (r.json() or {}).get("judgment_id")
                if r.status_code in (200, 202) and jid:
                    bucket.append(jid)
        with _POOL_LOCK:
            if not _POOL.get(pool_mod.HOT):
                _POOL.setdefault(pool_mod.HOT, []).extend(bucket)
                _POOL.setdefault(pool_mod.COLD, [])
                _POOL.setdefault(pool_mod.ADDRESSES, []).extend(addrs)

    def _subgraph_addresses(self) -> list[str]:
        with _POOL_LOCK:
            from_pool = list(_POOL.get(pool_mod.ADDRESSES) or [])
        return from_pool or list(_DEMO_ADDR)

    # ---- STRESS-01 纯读基线 ----
    @task(60)
    def poll_judgment(self):
        if PROFILE not in ("read", "mixed"):
            return
        jid = pool_mod.sample(_POOL, POOL_TIER)
        if not jid:
            self._ensure_pool()
            jid = pool_mod.sample(_POOL, POOL_TIER)
        if not jid:
            return
        with self.client.get(f"/api/v1/judgments/{jid}",
                             name=f"GET /api/v1/judgments/[id] ({POOL_TIER} pool)",
                             catch_response=True) as r:
            _expect(r, ("status",))

    @task(25)
    def subgraph(self):
        if PROFILE not in ("read", "mixed"):
            return
        addrs = self._subgraph_addresses()
        if not addrs:
            self._fetch_demo_addresses()
            addrs = self._subgraph_addresses()
        if not addrs:
            return
        with self.client.get(f"/api/v1/addresses/{random.choice(addrs)}/subgraph",
                             catch_response=True) as r:
            _expect(r, ("nodes",))

    @task(10)
    def cases_list(self):
        if PROFILE not in ("read", "mixed"):
            return
        with self.client.get("/api/v1/cases", params={"page": random.randint(1, 5)},
                             catch_response=True) as r:
            _expect(r, ("items",))

    @task(5)
    def patterns(self):
        if PROFILE not in ("read", "mixed"):
            return
        with self.client.get("/api/v1/patterns", catch_response=True) as r:
            _expect(r, ("items",))

    # ---- STRESS-02 读写混合：analyze + 轮询至终态 ----
    @task(5)
    def analyze_and_poll(self):
        if PROFILE != "mixed":
            return
        self._fetch_demo_addresses()
        addr = _claim_address()
        if not addr:
            return
        body: dict = {"address": addr}
        if MOCK_SCENARIO:
            body["mock_scenario"] = MOCK_SCENARIO
        with self.client.post("/api/v1/addresses/analyze", json=body,
                              catch_response=True) as r:
            # 202=新建 / 200=幂等复用：两者都合法（BE-12/46）；
            # 429/403 预期拒绝不计错误率（spec §3.2）；其余判定失败
            if r.status_code not in (200, 202, 429, 403):
                r.failure(f"unexpected {r.status_code}: {r.text[:120]}")
                return
            jid = (r.json() or {}).get("judgment_id")
        if r.status_code == 202 and jid:
            self._poll_to_terminal(jid)

    def _poll_to_terminal(self, jid: str) -> None:
        """轮询至终态；终态与预期不符即判失败（附着在真实请求上）。"""
        expected = {"failed"} if (EXPECT_FAILURE or MOCK_SCENARIO) else {"completed"}
        for attempt in range(20):
            with self.client.get(f"/api/v1/judgments/{jid}", name="poll:analyze",
                                 catch_response=True) as r:
                if r.status_code != 200:
                    r.failure(f"poll unexpected {r.status_code}")
                    return
                status = (r.json() or {}).get("status")
                if status in ("completed", "failed"):
                    if status not in expected:
                        r.failure(f"terminal={status} expected={sorted(expected)}")
                    return
                if attempt == 19:
                    r.failure(f"not terminal after 20 polls: {status}")
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
