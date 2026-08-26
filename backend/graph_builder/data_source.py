"""分析管线的 tx_provider 数据源 — graph-builder-spec §4 Esplora 接入。

双模式：
- fixture：确定性演示数据（infra/fixtures/demo_txs.json），端到端测试与
  离线演示不依赖公网 Esplora；含夹具自带的 coinjoin/crosschain 标记集，
  使演示子图呈现与真实 Lazarus 场景一致的 mixer/crosschain 终止语义。
- live：同步 httpx 访问 Esplora API（mempool.space 兼容），带重试退避/熔断/
  备用端点/Redis 缓存容错层（与 esplora.EsploraClient 共享熔断器与缓存键规范）。

builder 的 provider 契约是同步可调用对象返回 tx 列表
（.txid/.inputs/.outputs/.block_time/.unspent_outputs，见 builder._process_entry）；
EsploraClient（异步+熔断）服务于异步调用方。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from .esplora import CircuitBreaker, CircuitOpenError

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "infra" / "fixtures" / "demo_txs.json"


class FixtureTxProvider:
    def __init__(self, data: dict):
        self.data = data
        self.txs_by_address: dict[str, list] = data["txs_by_address"]
        self.seed_addresses: list[str] = data.get("seed_addresses", [])
        self.coinjoin_txids: set[str] = set(data.get("coinjoin_txids", []))
        self.crosschain_tx_set: dict[str, str] = dict(data.get("crosschain_tx_set", {}))

    def seed_block_time(self, address: str) -> float | None:
        times = [t.get("block_time") for t in self.txs_by_address.get(address, [])
                 if t.get("block_time") is not None]
        return max(times) if times else None

    def __call__(self, address: str) -> list:
        return [
            SimpleNamespace(
                txid=tx["txid"], inputs=tx["inputs"], outputs=tx["outputs"],
                block_time=tx.get("block_time"),
                unspent_outputs=set(tx.get("unspent_outputs", [])),
            )
            for tx in self.txs_by_address.get(address, [])
        ]

    @classmethod
    def load(cls, path: str | Path | None = None) -> FixtureTxProvider:
        return cls(json.loads(Path(path or FIXTURE_PATH).read_text()))


class LiveEsploraProvider:
    """同步容错层：重试退避 + 429 Retry-After + 熔断 + 备用端点 + Redis L2。

    与 esplora.EsploraClient（异步调用方用）共享 CircuitBreaker 类和
    esplora:{base}:{path} 缓存键规范；同步形态是 builder provider 契约决定的。
    """

    CACHE_TTL_SECONDS = 86400
    MAX_RETRIES = 3
    FALLBACKS = {
        "https://mempool.space/api": "https://blockstream.info/api",
        "https://blockstream.info/api": "https://mempool.space/api",
    }

    def __init__(self, base_url: str, timeout_seconds: float = 10.0,
                 redis_client=None):
        self.base_url = base_url.rstrip("/")
        self.fallback_base = self.FALLBACKS.get(self.base_url)
        self.timeout_seconds = timeout_seconds
        self.breaker = CircuitBreaker()
        self.redis = redis_client
        self.httpx_client = None  # 惰性创建，进程内复用连接池

    def __call__(self, address: str) -> list:
        data = self._fetch(f"/address/{address}/txs")
        return [self._map_tx(tx) for tx in data]

    def seed_block_time(self, address: str) -> float | None:
        """时间窗基准（spec §3 out_of_range）：种子地址最近一次上链活动。"""
        times = [t.block_time for t in self(address)
                 if t.block_time is not None]
        return max(times) if times else None

    # ------------------------------------------------------------------
    def _fetch(self, path: str) -> object:
        redis_key = f"esplora:{self.base_url}:{path}"
        cached = self._redis_get(redis_key)
        if cached is not None:
            return cached
        data = self._http_with_fault_tolerance(path)
        self._redis_set(redis_key, data)
        return data

    def _http_with_fault_tolerance(self, path: str) -> object:
        import random
        import time as _time

        effective = self.base_url
        delay = 1.0
        last_exc: Exception | None = None
        retry_after: float | None = None
        for attempt in range(self.MAX_RETRIES):
            try:
                self.breaker.before_request()
            except CircuitOpenError:
                if self.fallback_base is None or effective != self.base_url:
                    raise
                effective = self.fallback_base  # 熔断切换备用端点（GB-18 同语义）
            try:
                data = self._http_get(effective, path)
                self.breaker.record_success()
                return data
            except Exception as exc:  # noqa: BLE001 — 网络/HTTP 错误统一重试
                last_exc = exc
                ra = getattr(exc, "retry_after", None)
                retry_after = float(ra) if ra is not None else None
                self.breaker.record_failure()
            if attempt < self.MAX_RETRIES - 1:
                rng_wait = delay * random.uniform(0.7, 1.3)
                _time.sleep(retry_after if retry_after is not None else rng_wait)
                delay *= 2
        raise last_exc  # type: ignore[misc]

    def _http_get(self, base_url: str, path: str) -> object:
        import httpx

        resp = (self.httpx_client or httpx).get(
            f"{base_url}{path}", timeout=self.timeout_seconds)
        if resp.status_code == 429:
            ra = resp.headers.get("Retry-After")
            raise _SyncRateLimited(float(ra) if ra else 1.0)
        resp.raise_for_status()
        return resp.json()

    def _redis_get(self, key: str):
        if self.redis is None:
            return None
        try:
            raw = self.redis.get(key)
            return json.loads(raw) if raw is not None else None
        except Exception:  # noqa: BLE001 — 缓存层故障不阻塞主流程
            return None

    def _redis_set(self, key: str, data) -> None:
        if self.redis is None:
            return
        try:
            self.redis.set(key, json.dumps(data), ex=self.CACHE_TTL_SECONDS)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _map_tx(tx: dict) -> SimpleNamespace:
        return SimpleNamespace(
            txid=tx["txid"],
            inputs=[{
                "address": (vin.get("prevout") or {}).get("scriptpubkey_address"),
                "value": (vin.get("prevout") or {}).get("value", 0) / 1e8,
            } for vin in tx.get("vin", [])],
            outputs=[{
                "address": vout.get("scriptpubkey_address"),
                "value": vout.get("value", 0) / 1e8,
            } for vout in tx.get("vout", [])],
            block_time=tx.get("block_time"),
            # Esplora 响应自带每个 vout 的 spent 状态：未花输出即 unspent 终止依据
            unspent_outputs={
                f"{tx['txid']}:{i}" for i, v in enumerate(tx.get("vout", []))
                if not (v.get("status") or {}).get("spent", True)
            },
        )


class _SyncRateLimited(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"429 rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


def build_provider(settings) -> tuple[object, list[str]]:
    """按 settings.graph_data_mode 返回 (provider, demo_seed_addresses)。"""
    if settings.graph_data_mode == "live":
        return LiveEsploraProvider(settings.esplora_api_url,
                                   redis_client=_sync_redis()), []
    fixture = FixtureTxProvider.load()
    seeds = settings.demo_seeds_list or fixture.seed_addresses
    return fixture, seeds


def _sync_redis():
    """Redis 可达则复用为 Esplora L2 缓存；不可达是合法形态（无缓存）。"""
    try:
        from ..core.config import get_settings

        import redis

        client = redis.Redis.from_url(get_settings().redis_url,
                                      socket_connect_timeout=1)
        client.ping()
        return client
    except Exception:  # noqa: BLE001 — 与 orchestration._judgment_cache 同口径
        return None
