"""Esplora async 客户端与容错层 — graph-builder-spec §7/§7.1（GB-15~21）。

分层：
    L1 进程内 async-lru（maxsize 默认 2048，key 含 base URL）
    L2 Redis（TTL 86400s，可选注入；生产由 compose 的 REDIS_URL 提供）
    HTTP aiohttp + Semaphore 并发预算 + 重试退避 + 熔断 + 备用 provider

可注入点：sleep / clock / rng / session——测试不真实等待、不发公网请求。
"""
from __future__ import annotations

import asyncio
import json
import random
import time

import aiohttp
from async_lru import alru_cache

BLOCKSTREAM_URL = "https://blockstream.info/api"
MEMPOOL_URL = "https://mempool.space/api"

# 公共 Blockstream API 限速较严：默认并发 5；自托管端点可配到 10
DEFAULT_CONCURRENCY = 5

CACHE_TTL_SECONDS = 86400  # Redis 二级缓存 24h
MAX_RETRIES = 3
BREAKER_THRESHOLD = 5      # 连续 5 次失败 → 打开
BREAKER_COOLDOWN_S = 30.0


class CircuitOpenError(RuntimeError):
    """熔断打开期间快速失败。"""


class _RateLimited(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"429 rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class CircuitBreaker:
    """连续失败计数熔断器。clock 可注入以在测试中即时推进时间。"""

    def __init__(
        self,
        threshold: int = BREAKER_THRESHOLD,
        cooldown_s: float = BREAKER_COOLDOWN_S,
        clock=time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._clock = clock
        self.consecutive_failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if self._clock() - self.opened_at >= self.cooldown_s:
            return "half_open"
        return "open"

    def before_request(self) -> None:
        if self.state == "open":
            raise CircuitOpenError(
                f"circuit open until {self.opened_at + self.cooldown_s:.0f}"
            )

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold and self.opened_at is None:
            self.opened_at = self._clock()


class EsploraClient:
    def __init__(
        self,
        base_url: str = BLOCKSTREAM_URL,
        fallback_url: str | None = MEMPOOL_URL,
        concurrency: int = DEFAULT_CONCURRENCY,
        cache_maxsize: int = 2048,
        redis_client=None,          # 注入 redis.asyncio.Redis 或兼容对象
        session=None,               # 注入 aiohttp.ClientSession 或测试替身
        sleep=None,                 # 默认 asyncio.sleep；测试注入记录型假 sleep
        clock=time.monotonic,
        jitter_rng=random.uniform,  # 测试注入确定性 rng
    ) -> None:
        self.base_url = base_url
        self.fallback_url = fallback_url
        self.redis = redis_client
        self.session = session
        self._sleep = sleep or asyncio.sleep
        self._clock = clock
        self._rng = jitter_rng
        self.semaphore = asyncio.Semaphore(concurrency)
        self.breaker = CircuitBreaker(clock=clock)
        # L1 缓存 key 显式含 base_url（GB-15）：两个不同 base URL 不共享条目
        self._l1 = alru_cache(maxsize=cache_maxsize)(self._fetch_path_uncached)
        self.http_call_count = 0  # 观测用：测试断言「未发 HTTP」

    # ------------------------------------------------------------------
    async def get_address_txs(self, address: str) -> list[dict]:
        return await self._l1(self.base_url, f"/address/{address}/txs")

    async def get_tx(self, txid: str) -> dict:
        return await self._l1(self.base_url, f"/tx/{txid}")

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()

    # ------------------------------------------------------------------
    async def _fetch_path_uncached(self, base_url: str, path: str):
        """L1 未命中后走 L2 → HTTP。base_url 参与 L1/L2 key（GB-15）。"""
        redis_key = f"esplora:{base_url}:{path}"

        if self.redis is not None:
            try:
                cached = await self.redis.get(redis_key)
                if cached is not None:
                    return json.loads(cached)
            except Exception:
                pass  # 缓存层故障不阻塞主流程

        data = await self._http_get_json(base_url, path)

        if self.redis is not None:
            try:
                await self.redis.set(redis_key, json.dumps(data), ex=CACHE_TTL_SECONDS)
            except Exception:
                pass
        return data

    async def _http_get_json(self, base_url: str, path: str):
        effective_base = base_url
        try:
            self.breaker.before_request()
        except CircuitOpenError:
            if self.fallback_url is None or base_url != self.base_url:
                raise
            # 熔断打开且配置了备用 provider → 切换（GB-18）
            effective_base = self.fallback_url

        url = f"{effective_base}{path}"
        delay = 1.0
        last_exc: Exception | None = None
        retry_after: float | None = None

        for attempt in range(MAX_RETRIES):
            try:
                self.http_call_count += 1
                session = self.session
                if session is None:
                    session = await self._ensure_session()
                async with self.semaphore, session.get(url) as resp:
                    if resp.status == 429:
                        ra = resp.headers.get("Retry-After")
                        raise _RateLimited(float(ra) if ra else delay)
                    resp.raise_for_status()
                    data = await resp.json()
                self.breaker.record_success()
                return data
            except _RateLimited as e:
                retry_after = e.retry_after
                last_exc = e
            except Exception as e:  # noqa: BLE001 — 网络/HTTP 错误统一走重试
                last_exc = e
            self.breaker.record_failure()
            if attempt < MAX_RETRIES - 1:
                wait = retry_after if retry_after is not None else delay * self._rng(0.7, 1.3)
                await self._sleep(wait)
                delay *= 2
                retry_after = None

        raise last_exc  # type: ignore[misc]

    async def _ensure_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return self.session
