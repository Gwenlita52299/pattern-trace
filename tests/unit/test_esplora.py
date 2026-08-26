"""Esplora 客户端容错层单测 — GB-15 ~ GB-21。

全部使用注入替身（fake session/sleep/clock/redis），不发公网请求、不真实等待。
"""
import asyncio

import pytest

from backend.graph_builder.esplora import (
    BREAKER_COOLDOWN_S,
    CACHE_TTL_SECONDS,
    MEMPOOL_URL,
    CircuitBreaker,
    CircuitOpenError,
    EsploraClient,
)


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, hold=0):
        self.status = status
        self.payload = payload if payload is not None else {"ok": True}
        self.headers = headers or {}
        self.hold = hold
        self.inflight_counter = None  # 由 FakeSession.get 注入

    async def __aenter__(self):
        c = self.inflight_counter
        if c is not None:
            c.inflight += 1
            c.peak = max(c.peak, c.inflight)
        return self

    async def __aexit__(self, *exc):
        c = self.inflight_counter
        if c is not None:
            c.inflight -= 1
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        if self.hold:
            # 让出事件循环，制造并发重叠观测点（GB-20）
            await asyncio.sleep(self.hold)
        return self.payload


class FakeSession:
    """按脚本顺序回放响应；记录每次请求的 URL。"""

    def __init__(self, script):
        self.script = list(script)  # items: FakeResponse | Exception
        self.urls: list[str] = []
        self.inflight = 0
        self.peak = 0

    def get(self, url):
        self.urls.append(url)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        item.inflight_counter = self
        return item


class FakeSleep:
    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, seconds):
        self.delays.append(seconds)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeRedis:
    def __init__(self):
        self.store: dict[str, tuple[str, int | None]] = {}

    async def get(self, key):
        item = self.store.get(key)
        return item[0] if item else None

    async def set(self, key, value, ex=None):
        self.store[key] = (value, ex)


def run(coro):
    return asyncio.run(coro)


def deterministic_rng(lo, hi):  # 中点值 → 断言精确延迟
    return (lo + hi) / 2


# ---------------------------------------------------------------------------
# GB-15 缓存 key 含 base URL
# ---------------------------------------------------------------------------
class TestCacheKeyContainsBaseUrl:
    def test_same_url_second_call_hits_cache(self):
        session = FakeSession([FakeResponse(payload={"txid": "t1"})])
        client = EsploraClient(base_url="https://api-a.test", session=session,
                               fallback_url=None)

        async def flow():
            # 同一事件循环内两次调用——对应生产中单 loop 长驻的真实用法
            await client.get_tx("t1")
            await client.get_tx("t1")

        run(flow())
        assert len(session.urls) == 1

    def test_different_url_issues_new_request(self):
        s_a = FakeSession([FakeResponse(payload={"txid": "t1"})])
        s_b = FakeSession([FakeResponse(payload={"txid": "t1"})])

        async def flow():
            ca = EsploraClient(base_url="https://api-a.test", session=s_a, fallback_url=None)
            cb = EsploraClient(base_url="https://api-b.test", session=s_b, fallback_url=None)
            await ca.get_tx("t1")
            await cb.get_tx("t1")

        run(flow())
        assert len(s_b.urls) == 1  # B 未命中 A 的缓存，发出新请求


# ---------------------------------------------------------------------------
# GB-16 重试指数退避 + jitter（注入 rng 后断言精确值）
# ---------------------------------------------------------------------------
class TestRetryBackoff:
    def test_gb16_exponential_backoff_with_jitter(self):
        session = FakeSession([
            RuntimeError("boom"),
            RuntimeError("boom"),
            FakeResponse(payload={"txid": "t1", "vin": []}),
        ])
        sleeper = FakeSleep()

        async def flow():
            client = EsploraClient(session=session, sleep=sleeper,
                                   jitter_rng=deterministic_rng, fallback_url=None)
            return await client.get_tx("t1")

        tx = run(flow())
        assert tx == {"txid": "t1", "vin": []}
        assert len(session.urls) == 3
        # 指数增长 1s→2s；rng 取区间中点 → 无抖动偏移的基准值
        assert sleeper.delays == [1.0, 2.0]

    def test_429_honors_retry_after_header(self):
        session = FakeSession([
            FakeResponse(status=429, headers={"Retry-After": "7"}),
            FakeResponse(payload={"ok": 1}),
        ])
        sleeper = FakeSleep()

        async def flow():
            client = EsploraClient(session=session, sleep=sleeper, fallback_url=None)
            await client.get_tx("t1")

        run(flow())
        assert sleeper.delays[0] == 7.0

    def test_gives_up_after_max_retries(self):
        session = FakeSession([RuntimeError("down")] * 3)
        client = EsploraClient(session=session, sleep=FakeSleep(), fallback_url=None)
        with pytest.raises(RuntimeError):
            run(client.get_tx("t1"))


# ---------------------------------------------------------------------------
# GB-17 熔断器
# ---------------------------------------------------------------------------
class TestCircuitBreaker:
    def test_gb17_opens_after_consecutive_failures_and_fast_fails(self):
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        for _ in range(5):
            breaker.record_failure()
        assert breaker.state == "open"
        opened_at = breaker.opened_at
        assert opened_at == pytest.approx(clock.t)

        with pytest.raises(CircuitOpenError):
            breaker.before_request()

        clock.advance(BREAKER_COOLDOWN_S + 1)
        assert breaker.state == "half_open"
        breaker.before_request()  # half_open 放行试探请求

        breaker.record_success()
        assert breaker.state == "closed"

    def test_client_fast_fails_when_open_without_fallback(self):
        # 每次调用内部消耗 3 次 HTTP 失败 → 2 次调用累计 6 次 ≥ 阈值 5
        session = FakeSession([RuntimeError("down")] * 6)
        client = EsploraClient(session=session, sleep=FakeSleep(), fallback_url=None)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                run(client.get_tx("t1"))
        calls_after_open = len(session.urls)
        with pytest.raises(CircuitOpenError):
            run(client.get_tx("t1"))
        assert len(session.urls) == calls_after_open  # 未发 HTTP，快速失败


# ---------------------------------------------------------------------------
# GB-18 备用 provider 切换
# ---------------------------------------------------------------------------
class TestFallbackProvider:
    def test_switches_to_mempool_when_breaker_open(self):
        session = FakeSession([])  # 打开后不应有任何主 provider 调用

        async def flow():
            client = EsploraClient(
                base_url="https://blockstream.info/api",
                fallback_url=MEMPOOL_URL,
                session=session,
            )
            for _ in range(5):
                client.breaker.record_failure()
            session.script.append(FakeResponse(payload={"txid": "t1"}))
            await client.get_tx("t1")

        run(flow())
        assert session.urls[-1].startswith(MEMPOOL_URL)


# ---------------------------------------------------------------------------
# GB-20 Semaphore 并发预算
# ---------------------------------------------------------------------------
class TestConcurrencyBudget:
    def test_peak_inflight_respects_semaphore(self):
        session = FakeSession([FakeResponse(payload={"i": i}, hold=0.01) for i in range(12)])
        client = EsploraClient(session=session, concurrency=5, fallback_url=None)
        async def burst():
            await asyncio.gather(*(client.get_tx(f"t{i}") for i in range(12)))
        run(burst())
        assert session.peak <= 5


# ---------------------------------------------------------------------------
# GB-21 Redis 二级缓存 TTL 24h
# ---------------------------------------------------------------------------
class TestRedisSecondLevelCache:
    def test_l2_hit_avoids_http_and_sets_ttl(self):
        session = FakeSession([FakeResponse(payload={"txid": "t1"})])
        redis = FakeRedis()

        async def flow():
            client = EsploraClient(session=session, redis_client=redis, fallback_url=None)
            await client.get_tx("t1")                   # HTTP miss → 写 L2
            assert len(session.urls) == 1
            key = f"esplora:{client.base_url}:/tx/t1"
            _, ttl = redis.store[key]
            assert ttl == CACHE_TTL_SECONDS == 86400

            client._l1.cache_clear()                    # 排除一级缓存干扰
            await client.get_tx("t1")
            assert len(session.urls) == 1               # 第二次未发 HTTP（L2 命中）

        run(flow())
