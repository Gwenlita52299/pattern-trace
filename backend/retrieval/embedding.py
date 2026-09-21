"""语义 embedding provider 抽象 — ingest-spec §5 / retrieval-spec §4。

stub（默认）：确定性 token-bag 哈希向量——每个 token 经 SHA-256 派生伪随机
分量后加权求和、归一化。相比整句哈希，共享词汇的文本向量真实相关，
使检索评估在无外部 API 时有意义；相同文本必得相同向量（幂等）。

openai_compat：OpenAI 兼容 /embeddings 端点（OpenRouter / SiliconFlow 等），
base_url + api_key 走配置。模型版本锁定由调用方写入 embedding_model 列
并在检索入口校验（RT-04）。

issue #76 重构：
- 连接复用：持久 httpx.Client（此前每次请求新建，TCP/TLS 握手每批都重来）
- 统一错误分类：ProviderError（timeout/rate_limited/auth_failed/
  invalid_response/unavailable/configuration）；EmbeddingAPIError 保留为
  其子类，兼容既有 ingest 故障注入的单参构造
- 并发闸门 + 退避重试 + 计量（与 LLM 共用 backend.core.providers 组件）
- embed_batch_async：async 调用方（健康检查/未来异步编排）用它避免
  阻塞事件循环；同步路径供 ingest 与线程隔离调用方使用
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time

from backend.core.providers import (REGISTRY, ProviderCapabilities,
                                    ProviderError, ProviderErrorCode,
                                    ProviderMetrics, ProviderSpec,
                                    RetryPolicy, SyncConcurrencyGate,
                                    classify, log_provider_event,
                                    sync_gate_for)

_TOKEN_RE = re.compile(r"[a-z_]+|\d+(?:\.\d+)?")


class EmbeddingAPIError(ProviderError):
    """上游故障（超时/429/响应畸形），供续跑路径与调用方处理。

    issue #76：并入统一 ProviderError（默认 unavailable，可重试语义），
    但保留单参构造——ingest 故障注入与既有调用方按 `EmbeddingAPIError(msg)`
    使用（IG-15）。
    """

    def __init__(self, message: str,
                 code: ProviderErrorCode = ProviderErrorCode.UNAVAILABLE,
                 **kwargs) -> None:
        super().__init__(code, message, kind="embedding", **kwargs)


def tokenize(text: str) -> list[str]:
    """小写分词；数字归一化为数量级桶——"12 addresses" 与 "15 addresses"
    共享同一 token，避免逐值哈希稀释相似度。"""
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text.lower()):
        if raw[0].isdigit():
            value = float(raw)
            magnitude = int(math.log10(value)) + 1 if value > 0 else 0
            out.append(f"num{min(magnitude, 9)}")
        else:
            out.append(raw[:24])
    return out


class StubEmbedding:
    """确定性 token-bag embedding（dim 维单位向量）。"""

    CAPABILITIES = ProviderCapabilities(
        kind="embedding", requires_api_key=False, max_batch=10_000)

    def __init__(self, model: str, dim: int):
        self.model = model
        self.dim = dim
        self.calls = 0  # provider 调用计数（IG-15 断言用）

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        tokens = tokenize(text) or ["<empty>"]
        acc = [0.0] * self.dim
        weight = 1.0 / math.sqrt(len(tokens))
        for tok in tokens:
            digest = hashlib.sha256(f"{self.model}:{tok}".encode()).digest()
            for i in range(0, 32, 4):
                idx = (i // 4) % self.dim
                acc[idx] += weight * (
                    int.from_bytes(digest[i:i + 4], "big") / 2**32 - 0.5)
        norm = math.sqrt(sum(x * x for x in acc)) or 1.0
        return [round(x / norm, 8) for x in acc]


class _ChunkedEmbeddingBase:
    """同步/异步共用的分批与响应归位逻辑。"""

    def _batches(self, texts: list[str]) -> list[list[str]]:
        size = self._max_batch
        if not size or size >= len(texts):
            return [texts]
        return [texts[i:i + size] for i in range(0, len(texts), size)]

    def _order_and_validate(self, data: list, count: int) -> list[list[float]]:
        """按 item["index"] 归位——容忍上游乱序；维度不符立即报错，
        严禁把维度漂移的向量写入库（RT-04 模型锁的数据一致性前提）。"""
        vectors: list[list[float] | None] = [None] * count
        for item in data:
            try:
                vectors[item["index"]] = item["embedding"]
            except (KeyError, IndexError, TypeError) as exc:
                raise EmbeddingAPIError(
                    f"embedding 响应项格式异常：{exc}",
                    ProviderErrorCode.INVALID_RESPONSE, model=self.model,
                    cause=exc) from exc
        if any(v is None for v in vectors):
            raise EmbeddingAPIError(
                "embedding 响应缺少向量项",
                ProviderErrorCode.INVALID_RESPONSE, model=self.model)
        for v in vectors:
            if len(v) != self.dim:  # type: ignore[arg-type]
                raise EmbeddingAPIError(
                    f"向量维度漂移：期望 {self.dim}，实际 {len(v)}",  # type: ignore[arg-type]
                    ProviderErrorCode.INVALID_RESPONSE, model=self.model)
        return vectors  # type: ignore[return-value]


class OpenAICompatEmbedding(_ChunkedEmbeddingBase):
    """OpenAI 兼容 /embeddings 端点客户端（OpenRouter / SiliconFlow / …）。"""

    CAPABILITIES = ProviderCapabilities(
        kind="embedding", requires_api_key=True, max_batch=256)

    def __init__(self, model: str, dim: int, base_url: str, api_key: str,
                 timeout: float = 60.0, transport=None,
                 max_batch: int | None = None,
                 gate: SyncConcurrencyGate | None = None,
                 retry: RetryPolicy | None = None,
                 metrics: ProviderMetrics | None = None):
        if not base_url or not api_key:
            raise EmbeddingAPIError(
                "embedding_base_url / embedding_api_key 未配置——"
                "provider=openai_compat 需要在 .env 中提供",
                ProviderErrorCode.CONFIGURATION, model=model)
        self.model = model
        self.dim = dim
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.calls = 0  # provider 调用计数（IG-15 断言用；= embed_batch 调用次数）
        self._transport = transport
        self._max_batch = max_batch
        self._gate = gate
        self._retry = retry or RetryPolicy(max_retries=0)
        self._metrics = metrics or ProviderMetrics(
            provider="openai_compat", model=model, kind="embedding")
        self._client = None  # 持久连接（惰性创建，close() 释放）

    @property
    def client(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self.timeout,
                                        transport=self._transport)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        out: list[list[float]] = []
        for batch in self._batches(texts):
            out.extend(self._request(batch))
        return out

    async def embed_batch_async(self, texts: list[str]) -> list[list[float]]:
        """async 调用方入口：在线程里跑同步实现，避免阻塞事件循环。

        （issue #76 发现：此前同步 httpx.post 被 async 编排直接调用，
        会在 embedding 请求期间卡住整个 worker 事件循环。）
        """
        return await asyncio.to_thread(self.embed_batch, texts)

    def _request(self, texts: list[str]) -> list[list[float]]:
        attempts = self._retry.max_retries + 1
        last_error: ProviderError | None = None
        for attempt in range(attempts):
            started = time.perf_counter()
            try:
                if self._gate is not None:
                    with self._gate:
                        data = self._post(texts)
                else:
                    data = self._post(texts)
                self._metrics.record_success(
                    (time.perf_counter() - started) * 1000)
                return self._order_and_validate(data, len(texts))
            except Exception as exc:  # noqa: BLE001 - 分类后决定重试或上抛
                err = classify(exc, provider="openai_compat",
                               model=self.model, kind="embedding")
                if err is None:
                    raise
                self._metrics.record_failure(
                    err.code, (time.perf_counter() - started) * 1000)
                if not err.retryable or attempt >= attempts - 1:
                    raise err from exc
                last_error = err
                self._metrics.record_retry()
                log_provider_event(self._metrics, "retry",
                                   level=logging.WARNING,
                                   attempt=attempt + 1,
                                   error_code=err.code.value)
            time.sleep(self._retry.delay_for(attempt))
        raise last_error or EmbeddingAPIError("embedding 调用失败")

    def _post(self, texts: list[str]) -> list:
        import httpx

        try:
            resp = self.client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": texts},
            )
        except httpx.HTTPError as exc:
            raise EmbeddingAPIError(f"embedding 请求失败：{exc}",
                                    model=self.model, cause=exc) from exc
        if resp.status_code != 200:
            raise EmbeddingAPIError(
                f"embedding endpoint {resp.status_code}: {resp.text[:200]}",
                _status_code(resp.status_code), model=self.model,
                status=resp.status_code)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise EmbeddingAPIError(
                f"embedding 响应非 JSON：{exc}",
                ProviderErrorCode.INVALID_RESPONSE, model=self.model,
                cause=exc) from exc
        data = payload.get("data")
        if not isinstance(data, list):
            raise EmbeddingAPIError(
                "embedding 响应缺少 data 数组",
                ProviderErrorCode.INVALID_RESPONSE, model=self.model)
        return data


def _status_code(status: int) -> ProviderErrorCode:
    if status in (401, 403):
        return ProviderErrorCode.AUTH_FAILED
    if status == 429:
        return ProviderErrorCode.RATE_LIMITED
    if 500 <= status < 600:
        return ProviderErrorCode.UNAVAILABLE
    return ProviderErrorCode.INVALID_RESPONSE


def _register_embedding_providers() -> None:
    """内置 embedding provider 自注册（issue #76）。"""

    def _stub(settings, **kw):
        return StubEmbedding(settings.embedding_model, settings.embedding_dim,
                             **kw)

    def _openai_compat(settings, **kw):
        name = settings.embedding_provider
        return OpenAICompatEmbedding(
            settings.embedding_model, settings.embedding_dim,
            settings.embedding_base_url, settings.embedding_api_key,
            max_batch=getattr(settings, "embedding_batch_size", 100),
            gate=sync_gate_for(
                f"embedding:{name}:{settings.embedding_model}",
                getattr(settings, "embedding_max_concurrency", 2),
                getattr(settings, "embedding_concurrency_policy", "wait")),
            retry=RetryPolicy(
                max_retries=getattr(settings, "embedding_max_retries", 2)),
            metrics=ProviderMetrics(provider=name,
                                    model=settings.embedding_model,
                                    kind="embedding"),
            **kw)

    REGISTRY.register(ProviderSpec(
        kind="embedding", name="stub", factory=_stub,
        capabilities=StubEmbedding.CAPABILITIES, wrap=False,
        display="StubEmbedding"))
    REGISTRY.register(ProviderSpec(
        kind="embedding", name="openai_compat", factory=_openai_compat,
        capabilities=OpenAICompatEmbedding.CAPABILITIES, wrap=False,
        display="OpenAICompatEmbedding"))


def build_provider(settings=None):
    """按配置构建 provider（issue #76：查注册表，不再 if-elif）。

    未接入的 provider 名一律显式报错（configuration 分类）——比
    NotImplementedError 更能被健康检查/编排层归因。
    """
    if settings is None:
        from backend.core.config import get_settings

        settings = get_settings()
    name = settings.embedding_provider
    return REGISTRY.get("embedding", name).factory(settings=settings)


_register_embedding_providers()
