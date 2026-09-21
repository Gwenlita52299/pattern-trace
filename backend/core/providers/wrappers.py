"""Provider 通用包装组件 — issue #76。

四个可复用件，LLM 与 embedding 共用：
- ConcurrencyGate：进程级共享并发闸门（实例级 Semaphore 无意义——编排每次
  分析都会新建 client，只有跨实例共享才能真正限制对上游的并发压力）
- RetryPolicy：指数退避 + jitter，只对 retryable 错误码重试
- ProviderMetrics：调用次数/失败/重试/耗时/usage 计数（供日志与事件）
- 脱敏日志：统一输出格式，剔除 Authorization / api_key / 完整载荷

异步闸门按运行事件循环分桶（WeakKeyDictionary）：模块级共享的
asyncio.Semaphore 绑定 loop，跨 loop 复用（测试里的多次 asyncio.run）
会 RuntimeError；按 loop 分桶既保持"同 loop 内进程级共享"，又对多 loop
安全。
"""
from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
import weakref
from dataclasses import dataclass, field

from .errors import ProviderError, ProviderErrorCode

logger = logging.getLogger("backend.providers")

__all__ = [
    "ConcurrencyGate", "SyncConcurrencyGate", "gate_for", "sync_gate_for",
    "RetryPolicy", "ProviderMetrics", "redact_headers", "redact_url",
    "log_provider_event",
]


class ConcurrencyGate:
    """异步并发闸门；`policy=fail_fast` 时超额立即抛（不排队）。"""

    def __init__(self, limit: int, policy: str = "wait") -> None:
        if limit < 1:
            raise ValueError(f"concurrency limit must be >= 1, got {limit}")
        if policy not in ("wait", "fail_fast"):
            raise ValueError(f"unknown concurrency policy: {policy!r}")
        self.limit = limit
        self.policy = policy
        self._sems: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._guard = threading.Lock()
        self.max_observed = 0   # 观测：历史同时持有数（测试断言用）

    def _sem(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        with self._guard:
            sem = self._sems.get(loop)
            if sem is None:
                sem = asyncio.Semaphore(self.limit)
                self._sems[loop] = sem
            return sem

    async def __aenter__(self) -> "ConcurrencyGate":
        sem = self._sem()
        if self.policy == "fail_fast" and sem.locked():
            raise ProviderError(
                ProviderErrorCode.RATE_LIMITED,
                f"local concurrency gate full (limit={self.limit})")
        await sem.acquire()
        held = self.limit - sem._value  # noqa: SLF001 - 仅用于观测
        self.max_observed = max(self.max_observed, held)
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._sem().release()


class SyncConcurrencyGate:
    """同步闸门（threading.Semaphore）——embedding 同步路径用。"""

    def __init__(self, limit: int, policy: str = "wait") -> None:
        if limit < 1:
            raise ValueError(f"concurrency limit must be >= 1, got {limit}")
        if policy not in ("wait", "fail_fast"):
            raise ValueError(f"unknown concurrency policy: {policy!r}")
        self.limit = limit
        self.policy = policy
        self._sem = threading.Semaphore(limit)
        self._guard = threading.Lock()
        self._active = 0
        self.max_observed = 0

    def __enter__(self) -> "SyncConcurrencyGate":
        if self.policy == "fail_fast":
            if not self._sem.acquire(blocking=False):
                raise ProviderError(
                    ProviderErrorCode.RATE_LIMITED,
                    f"local concurrency gate full (limit={self.limit})")
        else:
            self._sem.acquire()
        with self._guard:
            self._active += 1
            self.max_observed = max(self.max_observed, self._active)
        return self

    def __exit__(self, *exc_info) -> None:
        with self._guard:
            self._active -= 1
        self._sem.release()


# 进程级共享：同一 key 复用同一闸门（首次配置的 limit/policy 生效）
_ASYNC_GATES: dict[str, ConcurrencyGate] = {}
_SYNC_GATES: dict[str, SyncConcurrencyGate] = {}
_GATES_LOCK = threading.Lock()


def gate_for(key: str, limit: int, policy: str = "wait") -> ConcurrencyGate:
    """取进程级共享的异步闸门（按 provider+model 分 key）。"""
    with _GATES_LOCK:
        gate = _ASYNC_GATES.get(key)
        if gate is None:
            gate = ConcurrencyGate(limit, policy)
            _ASYNC_GATES[key] = gate
        return gate


def sync_gate_for(key: str, limit: int,
                  policy: str = "wait") -> SyncConcurrencyGate:
    with _GATES_LOCK:
        gate = _SYNC_GATES.get(key)
        if gate is None:
            gate = SyncConcurrencyGate(limit, policy)
            _SYNC_GATES[key] = gate
        return gate


def reset_gates() -> None:
    """测试用：清空进程级闸门缓存。"""
    with _GATES_LOCK:
        _ASYNC_GATES.clear()
        _SYNC_GATES.clear()


@dataclass(frozen=True)
class RetryPolicy:
    """指数退避 + 抖动。只重试 retryable 错误码（见 errors.RETRYABLE_CODES）。"""

    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter_ratio: float = 0.25

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
        raw = min(self.base_delay * (2 ** attempt), self.max_delay)
        if self.jitter_ratio:
            rnd = rng or random
            raw *= 1.0 + rnd.uniform(-self.jitter_ratio, self.jitter_ratio)
        return max(raw, 0.0)


@dataclass
class ProviderMetrics:
    """provider 调用计量。线程安全（同步 embedding 在线程池中跑）。"""

    provider: str = ""
    model: str = ""
    kind: str = ""
    calls: int = 0
    failures: int = 0
    retries: int = 0
    latency_ms_total: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors_by_code: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_success(self, latency_ms: float,
                       usage: dict | None = None) -> None:
        with self._lock:
            self.calls += 1
            self.latency_ms_total += latency_ms
            if usage:
                self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                self.completion_tokens += int(usage.get("completion_tokens") or 0)

    def record_failure(self, code: ProviderErrorCode | None,
                       latency_ms: float = 0.0) -> None:
        with self._lock:
            self.calls += 1
            self.failures += 1
            self.latency_ms_total += latency_ms
            key = code.value if code else "unknown"
            self.errors_by_code[key] = self.errors_by_code.get(key, 0) + 1

    def record_retry(self) -> None:
        with self._lock:
            self.retries += 1

    @property
    def avg_latency_ms(self) -> float:
        with self._lock:
            return (self.latency_ms_total / self.calls) if self.calls else 0.0

    def snapshot(self) -> dict:
        with self._lock:
            out = {
                "provider": self.provider,
                "model": self.model,
                "kind": self.kind,
                "calls": self.calls,
                "failures": self.failures,
                "retries": self.retries,
                "avg_latency_ms": round(
                    (self.latency_ms_total / self.calls) if self.calls else 0.0, 1),
                "errors_by_code": dict(self.errors_by_code),
            }
        if self.prompt_tokens or self.completion_tokens:
            out["usage"] = {"prompt_tokens": self.prompt_tokens,
                            "completion_tokens": self.completion_tokens,
                            "total_tokens": self.prompt_tokens
                            + self.completion_tokens}
        return out


_SENSITIVE_KEYS = frozenset({
    "authorization", "x-api-key", "api_key", "apikey", "api-key",
    "cookie", "set-cookie", "proxy-authorization", "access_token",
    "refresh_token", "password", "secret",
})


def redact_headers(headers) -> dict:
    """返回脱敏后的 headers 副本（保留键名便于排障，值一律打码）。"""
    out = {}
    for key, value in dict(headers or {}).items():
        out[key] = "[REDACTED]" if key.lower() in _SENSITIVE_KEYS else value
    return out


def redact_url(url: str) -> str:
    """剥离 URL query 中的凭据参数（部分兼容端点用 ?api_key= 传参）。"""
    if not url or "?" not in url:
        return url
    base, _, query = url.partition("?")
    parts = []
    for pair in query.split("&"):
        name, sep, _ = pair.partition("=")
        parts.append(f"{name}=[REDACTED]" if sep
                     and name.lower() in _SENSITIVE_KEYS else pair)
    return f"{base}?{'&'.join(parts)}"


def log_provider_event(metrics: ProviderMetrics, event: str, *,
                       level: int = logging.INFO, **fields) -> None:
    """统一脱敏日志：只输出白名单字段，敏感键一律打码。

    绝不记录 messages / 请求体 / 响应体（可能含用户地址与链上数据）。
    """
    safe = {}
    for key, value in fields.items():
        if key.lower() in _SENSITIVE_KEYS:
            safe[key] = "[REDACTED]"
        elif key in ("url", "base_url"):
            safe[key] = redact_url(str(value))
        elif key in ("headers",):
            safe[key] = redact_headers(value)
        else:
            safe[key] = value
    safe.setdefault("provider", metrics.provider)
    safe.setdefault("model", metrics.model)
    detail = " ".join(f"{k}={v}" for k, v in safe.items())
    logger.log(level, "provider.%s %s", event, detail)


def monotonic_ms() -> float:
    return time.perf_counter() * 1000.0
