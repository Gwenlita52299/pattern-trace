"""统一 Provider 基础设施 — issue #76。

- errors：统一错误码与分类（timeout/rate_limited/auth_failed/
  invalid_response/unavailable/configuration）
- capabilities：能力声明（structured output / batch / 维度 / 上下文）
- registry：provider 注册与工厂（新增 provider 不改编排主流程）
- wrappers：并发闸门、退避重试、计量、脱敏日志
- health：健康检查（区分配置错误/认证失败/限流/上游不可用）
"""
from __future__ import annotations

from .capabilities import ProviderCapabilities
from .errors import (RETRYABLE_CODES, ProviderError, ProviderErrorCode,
                     classify)
from .registry import REGISTRY, ProviderRegistry, ProviderSpec, load_builtins
from .wrappers import (ConcurrencyGate, ProviderMetrics, RetryPolicy,
                       SyncConcurrencyGate, gate_for, log_provider_event,
                       redact_headers, redact_url, reset_gates, sync_gate_for)

__all__ = [
    "ProviderCapabilities", "ProviderError", "ProviderErrorCode",
    "RETRYABLE_CODES", "classify",
    "ProviderRegistry", "ProviderSpec", "REGISTRY", "load_builtins",
    "ConcurrencyGate", "SyncConcurrencyGate", "gate_for", "sync_gate_for",
    "RetryPolicy", "ProviderMetrics", "redact_headers", "redact_url",
    "log_provider_event", "reset_gates",
]
