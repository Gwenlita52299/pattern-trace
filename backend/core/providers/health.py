"""Provider 健康检查 — issue #76。

能区分配置错误 / 认证失败 / 限流 / 上游不可用——这四种在运维上的处置
完全不同（改配置 / 换 key / 等配额 / 排查网络）。检查用**未包装**的
raw client，避免污染计量与触发重试。

输出永远不含密钥：只回 provider/model/状态/错误码/耗时/简短说明。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .capabilities import ProviderCapabilities
from .errors import ProviderError, ProviderErrorCode, classify
from .registry import REGISTRY

__all__ = ["ProviderHealth", "check_provider", "check_all", "HEALTH_OK"]

HEALTH_OK = "ok"
HEALTH_ERROR = "error"

# 上游无关的本地配置缺失——不必发请求即可判定
_CONFIG_HINT = "缺少 API Key"


@dataclass
class ProviderHealth:
    kind: str
    provider: str
    model: str = ""
    status: str = HEALTH_ERROR
    code: str | None = None
    latency_ms: float | None = None
    detail: str = ""
    capabilities: ProviderCapabilities | None = None

    def to_dict(self) -> dict:
        out = {
            "kind": self.kind,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "code": self.code,
            "latency_ms": self.latency_ms,
            "detail": self.detail,
        }
        if self.capabilities is not None:
            out["capabilities"] = self.capabilities.as_dict()
        return out


def _missing_key(settings, kind: str, provider: str) -> bool:
    """云端 provider 缺 key 属于配置错误（不发请求即可判定）。"""
    attr = "llm_api_key" if kind == "llm" else "embedding_api_key"
    return not (getattr(settings, attr, "") or "").strip()


async def check_provider(settings, kind: str, provider: str | None = None,
                         *, timeout: float | None = None) -> ProviderHealth:
    """用一次极小调用探测 provider；分类错误而不泄露凭据。"""
    name = provider or (settings.llm_provider if kind == "llm"
                        else settings.embedding_provider)
    model = (settings.llm_model if kind == "llm" else settings.embedding_model)

    try:
        spec = REGISTRY.get(kind, name)
    except ProviderError as exc:
        return ProviderHealth(kind=kind, provider=name, model=model,
                              code=exc.code.value, detail=str(exc))

    health = ProviderHealth(kind=kind, provider=name, model=model,
                            capabilities=spec.capabilities)
    if spec.capabilities.requires_api_key and _missing_key(settings, kind, name):
        health.code = ProviderErrorCode.CONFIGURATION.value
        health.detail = f"{_CONFIG_HINT}（{kind}_api_key 为空）"
        return health

    limits = timeout or getattr(settings, "provider_health_timeout_seconds", 10.0)
    started = time.perf_counter()
    # 未包装 raw client：健康检查不应计入 provider 计量、也不该触发重试
    try:
        client = spec.factory(settings=settings)
    except Exception as exc:  # noqa: BLE001 - 构造失败也是配置问题
        err = classify(exc, provider=name, model=model)
        health.code = (err.code.value if err else
                       ProviderErrorCode.CONFIGURATION.value)
        health.detail = str(exc)[:200]
        return health

    try:
        if kind == "llm":
            await asyncio.wait_for(
                client.complete([{"role": "user", "content": "ping"}],
                                max_tokens=1),
                timeout=limits)
        else:
            await asyncio.wait_for(
                asyncio.to_thread(client.embed_batch, ["ping"]), timeout=limits)
    except Exception as exc:  # noqa: BLE001 - 分类后返回，不抛出
        err = classify(exc, provider=name, model=model)
        health.code = (err.code.value if err else
                       ProviderErrorCode.UNAVAILABLE.value)
        health.detail = str(exc)[:200]
        return health
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 - 关闭失败不影响健康结论
                pass

    health.status = HEALTH_OK
    health.latency_ms = round((time.perf_counter() - started) * 1000, 1)
    health.detail = "ok"
    return health


async def check_all(settings) -> list[ProviderHealth]:
    """当前配置的 LLM + embedding 的健康状况（并发探测）。"""
    return list(await asyncio.gather(
        check_provider(settings, "llm"),
        check_provider(settings, "embedding"),
    ))
