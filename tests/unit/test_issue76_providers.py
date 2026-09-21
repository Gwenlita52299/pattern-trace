"""issue #76：统一 Provider 注册、能力声明、限流与可观测包装层。

覆盖验收标准：
- 新增 provider 只需实现接口并注册（不改编排主流程）
- 所有 provider 错误统一分类（timeout/rate_limited/auth_failed/
  invalid_response/unavailable/configuration）
- 并发上限生效（等待 / 快速失败可配）
- embedding batch、响应乱序、维度漂移继续正确处理
- structured output 能力显式声明，本地校验始终兜底
- 日志与 trace 不含 API Key / Authorization
- 健康检查能区分配置错误 / 认证失败 / 限流 / 上游不可用
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from backend.core.providers import (REGISTRY, ConcurrencyGate,
                                    ProviderCapabilities, ProviderError,
                                    ProviderErrorCode, ProviderMetrics,
                                    ProviderRegistry, ProviderSpec,
                                    RetryPolicy, SyncConcurrencyGate,
                                    classify, load_builtins,
                                    log_provider_event, redact_headers,
                                    redact_url)
from backend.core.providers.health import check_provider
from backend.llm_judge.providers import (AnthropicClient, DeepSeekClient,
                                         MeteredLLMClient, OllamaClient,
                                         OpenAIClient, get_llm_client,
                                         wrap_llm_client)
from backend.retrieval.embedding import (EmbeddingAPIError,
                                         OpenAICompatEmbedding, StubEmbedding,
                                         build_provider)


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://fake/v1")


def _status_error(status: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(f"HTTP {status}", request=_request(),
                                 response=httpx.Response(status,
                                                         request=_request()))


# ---------------------------------------------------------------------------
# 1. 统一错误分类
# ---------------------------------------------------------------------------
class TestErrorClassification:
    @pytest.mark.parametrize("status,expected", [
        (401, ProviderErrorCode.AUTH_FAILED),
        (403, ProviderErrorCode.AUTH_FAILED),
        (429, ProviderErrorCode.RATE_LIMITED),
        (500, ProviderErrorCode.UNAVAILABLE),
        (503, ProviderErrorCode.UNAVAILABLE),
        (400, ProviderErrorCode.INVALID_RESPONSE),
    ])
    def test_http_status_maps_to_code(self, status, expected):
        err = classify(_status_error(status), provider="openai", model="m")
        assert err is not None and err.code is expected
        assert err.status == status
        assert err.provider == "openai"

    def test_timeout_variants(self):
        assert classify(TimeoutError("t")).code is ProviderErrorCode.TIMEOUT
        assert classify(
            httpx.TimeoutException("t")).code is ProviderErrorCode.TIMEOUT

    def test_connection_error_is_unavailable(self):
        err = classify(httpx.ConnectError("refused"))
        assert err.code is ProviderErrorCode.UNAVAILABLE

    def test_retryable_set(self):
        # 只有 timeout/rate_limited/unavailable 值得重试
        assert classify(TimeoutError("t")).retryable is True
        assert classify(_status_error(429)).retryable is True
        assert classify(_status_error(500)).retryable is True
        assert classify(_status_error(401)).retryable is False
        assert classify(_status_error(400)).retryable is False

    def test_programming_bug_not_masked(self):
        """代码缺陷不得被伪装成"上游不可用"（否则会无意义重试并掩盖 bug）。"""
        assert classify(TypeError("unsupported operand type(s)")) is None
        assert classify(RuntimeError("business rule violated")) is None

    def test_response_shape_error(self):
        assert classify(KeyError("choices")).code \
            is ProviderErrorCode.INVALID_RESPONSE

    def test_provider_error_passthrough_fills_metadata(self):
        err = ProviderError(ProviderErrorCode.TIMEOUT, "x")
        got = classify(err, provider="deepseek", model="m", kind="llm")
        assert got is err
        assert (got.provider, got.model, got.kind) == ("deepseek", "m", "llm")

    def test_embedding_api_error_is_provider_error(self):
        """IG-15 兼容：EmbeddingAPIError 单参构造仍可用，且已是统一分类。"""
        err = EmbeddingAPIError("simulated API failure at batch 0")
        assert isinstance(err, ProviderError)
        assert err.code is ProviderErrorCode.UNAVAILABLE
        assert err.kind == "embedding"


# ---------------------------------------------------------------------------
# 2. Registry：注册即可用，不改主流程
# ---------------------------------------------------------------------------
class TestRegistry:
    def test_new_provider_needs_only_registration(self):
        reg = ProviderRegistry()

        class Dummy:
            def __init__(self, settings=None):
                self.settings = settings

        reg.register(ProviderSpec(
            kind="llm", name="dummy", factory=lambda settings=None: Dummy(settings),
            capabilities=ProviderCapabilities(kind="llm",
                                              structured_output=False)))
        obj = reg.create("llm", "dummy", settings=None)
        assert isinstance(obj, Dummy)
        assert reg.names("llm") == ["dummy"]
        assert reg.get("llm", "dummy").capabilities.structured_output is False

    def test_duplicate_registration_rejected(self):
        reg = ProviderRegistry()
        spec = ProviderSpec(kind="llm", name="dup",
                            factory=lambda settings=None: None,
                            capabilities=ProviderCapabilities(kind="llm"))
        reg.register(spec)
        with pytest.raises(ValueError, match="already registered"):
            reg.register(spec)
        reg.register(spec, replace=True)  # 显式覆盖才允许

    def test_unknown_provider_is_configuration_error(self):
        reg = ProviderRegistry()
        with pytest.raises(ProviderError) as excinfo:
            reg.get("llm", "nope")
        assert excinfo.value.code is ProviderErrorCode.CONFIGURATION

    def test_builtins_all_registered(self):
        load_builtins()
        assert {"openai", "anthropic", "deepseek", "ollama", "vllm",
                "mock"} <= set(REGISTRY.names("llm"))
        assert {"stub", "openai_compat"} <= set(REGISTRY.names("embedding"))

    def test_factory_returns_wrapped_client_of_correct_type(self):
        client = get_llm_client(SimpleNamespace(
            llm_provider="deepseek", llm_base_url="", llm_model="m",
            llm_api_key="k"))
        # 包装层是动态子类：既有 isinstance 契约保持成立
        assert isinstance(client, DeepSeekClient)
        assert isinstance(client, MeteredLLMClient)
        assert client.metrics.provider == "deepseek"


# ---------------------------------------------------------------------------
# 3. 能力声明
# ---------------------------------------------------------------------------
class TestCapabilities:
    def test_llm_capability_matrix(self):
        assert OpenAIClient.CAPABILITIES.native_json_schema is True
        assert OpenAIClient.CAPABILITIES.supports_structured_output() is True
        # DeepSeek 只认 json_object → schema 走 prompt 内嵌 + 本地校验
        assert DeepSeekClient.CAPABILITIES.native_json_schema is False
        assert AnthropicClient.CAPABILITIES.native_json_schema is False
        assert OllamaClient.CAPABILITIES.native_json_schema is True
        # 本地部署无需 key
        assert OllamaClient.CAPABILITIES.requires_api_key is False
        assert OpenAIClient.CAPABILITIES.requires_api_key is True

    def test_embedding_capability_matrix(self):
        assert OpenAICompatEmbedding.CAPABILITIES.max_batch == 256
        assert OpenAICompatEmbedding.CAPABILITIES.requires_api_key is True
        assert StubEmbedding.CAPABILITIES.requires_api_key is False
        assert StubEmbedding.CAPABILITIES.kind == "embedding"

    def test_capabilities_serialisable_without_secrets(self):
        out = OpenAIClient.CAPABILITIES.as_dict()
        assert out["structured_output"] is True
        # 声明里只有"是否需要 key"这一布尔事实，没有任何凭据值
        assert out["requires_api_key"] is True
        assert set(out) == {"kind", "structured_output", "native_json_schema",
                            "requires_api_key", "max_batch"}


# ---------------------------------------------------------------------------
# 4. 并发闸门
# ---------------------------------------------------------------------------
class TestConcurrencyGate:
    def test_async_gate_caps_parallelism(self):
        gate = ConcurrencyGate(2, "wait")
        active = peak = 0

        async def worker():
            nonlocal active, peak
            async with gate:
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1

        async def main():
            await asyncio.gather(*[worker() for _ in range(8)])

        asyncio.run(main())
        assert peak <= 2, f"并发上限失效：peak={peak}"

    def test_async_gate_fail_fast_raises_immediately(self):
        gate = ConcurrencyGate(1, "fail_fast")

        async def main():
            async with gate:
                with pytest.raises(ProviderError) as excinfo:
                    async with gate:
                        pass
                assert excinfo.value.code is ProviderErrorCode.RATE_LIMITED

        asyncio.run(main())

    def test_gate_shared_across_event_loops(self):
        """模块级共享闸门必须跨 loop 安全（测试里多次 asyncio.run）。"""
        gate = ConcurrencyGate(1, "wait")

        async def once():
            async with gate:
                return 1

        assert asyncio.run(once()) == 1
        assert asyncio.run(once()) == 1

    def test_sync_gate_caps_parallelism(self):
        gate = SyncConcurrencyGate(1, "wait")
        active = peak = 0
        lock = threading.Lock()

        def worker():
            nonlocal active, peak
            with gate:
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.01)
                with lock:
                    active -= 1

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert peak <= 1, f"同步闸门上限失效：peak={peak}"

    def test_sync_gate_fail_fast(self):
        gate = SyncConcurrencyGate(1, "fail_fast")
        with gate:
            with pytest.raises(ProviderError) as excinfo:
                with gate:
                    pass
            assert excinfo.value.code is ProviderErrorCode.RATE_LIMITED

    @pytest.mark.parametrize("limit,policy", [(0, "wait"), (2, "bogus")])
    def test_invalid_gate_config_rejected(self, limit, policy):
        with pytest.raises(ValueError):
            ConcurrencyGate(limit, policy)


# ---------------------------------------------------------------------------
# 5. 重试 + 计量（包装层）
# ---------------------------------------------------------------------------
def _ok_body(content: str = "hi", usage: dict | None = None) -> dict:
    body = {"choices": [{"message": {"content": content}}]}
    if usage:
        body["usage"] = usage
    return body


class TestRetryAndMetrics:
    def _client(self, handler, **retry_kw):
        inner = OpenAIClient("https://fake/v1", "k", "m",
                             transport=httpx.MockTransport(handler))
        metrics = ProviderMetrics(provider="openai", model="m", kind="llm")
        client = wrap_llm_client(
            inner, capabilities=OpenAIClient.CAPABILITIES,
            gate=ConcurrencyGate(4),
            retry=RetryPolicy(max_retries=2, base_delay=0.001,
                              jitter_ratio=0, **retry_kw),
            metrics=metrics)
        return client, metrics

    def test_retries_rate_limited_then_succeeds(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, request=request)
            return httpx.Response(200, request=request,
                                  json=_ok_body("ok", {"prompt_tokens": 7,
                                                       "completion_tokens": 3}))

        client, metrics = self._client(handler)
        out = asyncio.run(client.complete([{"role": "user", "content": "x"}]))
        assert out == "ok"
        assert calls["n"] == 2
        snapshot = metrics.snapshot()
        assert snapshot["calls"] == 2
        assert snapshot["retries"] == 1
        assert snapshot["usage"]["total_tokens"] == 10

    def test_auth_failure_not_retried(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(401, request=request)

        client, metrics = self._client(handler)
        with pytest.raises(ProviderError) as excinfo:
            asyncio.run(client.complete([{"role": "user", "content": "x"}]))
        assert excinfo.value.code is ProviderErrorCode.AUTH_FAILED
        assert calls["n"] == 1, "认证失败不应重试"
        assert metrics.snapshot()["failures"] == 1

    def test_retry_exhausted_raises_last_error(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(503, request=request)

        client, metrics = self._client(handler)
        with pytest.raises(ProviderError) as excinfo:
            asyncio.run(client.complete([{"role": "user", "content": "x"}]))
        assert excinfo.value.code is ProviderErrorCode.UNAVAILABLE
        assert calls["n"] == 3  # 首调 + 2 次重试
        assert metrics.snapshot()["errors_by_code"] == {"unavailable": 3}

    def test_programming_error_propagates_unwrapped(self):
        def handler(request):
            raise AssertionError("handler bug")

        client, _ = self._client(handler)
        with pytest.raises(AssertionError):
            asyncio.run(client.complete([{"role": "user", "content": "x"}]))

    def test_retry_policy_backoff_is_exponential_and_capped(self):
        policy = RetryPolicy(max_retries=5, base_delay=1.0, max_delay=4.0,
                             jitter_ratio=0)
        delays = [policy.delay_for(a) for a in range(6)]
        assert delays == [1.0, 2.0, 4.0, 4.0, 4.0, 4.0]

    def test_retry_policy_jitter_stays_in_band(self):
        policy = RetryPolicy(base_delay=1.0, jitter_ratio=0.25)
        import random

        rng = random.Random(0)
        for _ in range(50):
            assert 0.75 <= policy.delay_for(0, rng) <= 1.25


# ---------------------------------------------------------------------------
# 6. 脱敏
# ---------------------------------------------------------------------------
class TestRedaction:
    def test_headers_redacted(self):
        out = redact_headers({
            "Authorization": "Bearer sk-live-secret",
            "X-Api-Key": "sk-live-secret",
            "Content-Type": "application/json",
        })
        assert out["Authorization"] == "[REDACTED]"
        assert out["X-Api-Key"] == "[REDACTED]"
        assert out["Content-Type"] == "application/json"

    def test_url_query_credentials_redacted(self):
        assert redact_url(
            "https://x/v1/embeddings?api_key=sk-live-secret&model=m"
        ) == "https://x/v1/embeddings?api_key=[REDACTED]&model=m"

    def test_logs_never_contain_secrets(self, caplog):
        metrics = ProviderMetrics(provider="openai", model="m", kind="llm")
        with caplog.at_level(logging.INFO, logger="backend.providers"):
            log_provider_event(
                metrics, "call", api_key="sk-live-secret",
                url="https://x/v1?api_key=sk-live-secret",
                headers={"Authorization": "Bearer sk-live-secret"},
                attempt=1, latency_ms=12.5)
        assert "sk-live-secret" not in caplog.text
        assert "[REDACTED]" in caplog.text
        assert "attempt=1" in caplog.text

    def test_wrapper_retry_log_has_no_secret(self, caplog):
        def handler(request):
            return httpx.Response(429, request=request)

        inner = OpenAIClient("https://fake/v1", "sk-live-secret", "m",
                             transport=httpx.MockTransport(handler))
        client = wrap_llm_client(
            inner, capabilities=OpenAIClient.CAPABILITIES,
            gate=ConcurrencyGate(4),
            retry=RetryPolicy(max_retries=1, base_delay=0.001, jitter_ratio=0),
            metrics=ProviderMetrics(provider="openai", model="m", kind="llm"))
        with caplog.at_level(logging.WARNING, logger="backend.providers"):
            with pytest.raises(ProviderError):
                asyncio.run(client.complete([{"role": "user", "content": "x"}]))
        assert "sk-live-secret" not in caplog.text


# ---------------------------------------------------------------------------
# 7. 健康检查（区分配置错误/认证失败/限流/上游不可用）
# ---------------------------------------------------------------------------
def _register_fake_llm(name: str, handler) -> None:
    """按"注册即可用"的方式注入可控上游（不触碰真实网络）。"""

    def factory(settings=None, **kw):
        return OpenAIClient("https://fake/v1", "k", "m",
                            transport=httpx.MockTransport(handler))

    REGISTRY.register(ProviderSpec(
        kind="llm", name=name, factory=factory,
        capabilities=OpenAIClient.CAPABILITIES, wrap=False), replace=True)


def _settings_for(provider: str, api_key: str = "sk-x"):
    return SimpleNamespace(
        llm_provider=provider, llm_model="m", llm_base_url="",
        llm_api_key=api_key, embedding_provider="stub",
        embedding_api_key="", provider_health_timeout_seconds=2.0)


class TestHealthCheck:
    def test_missing_key_is_configuration_error(self):
        health = asyncio.run(check_provider(
            _settings_for("openai", api_key=""), "llm"))
        assert health.status == "error"
        assert health.code == ProviderErrorCode.CONFIGURATION.value
        assert "api_key" in health.detail

    def test_auth_failure_distinguished(self):
        _register_fake_llm("fake_401",
                           lambda request: httpx.Response(401, request=request))
        health = asyncio.run(check_provider(_settings_for("fake_401"), "llm"))
        assert health.status == "error"
        assert health.code == ProviderErrorCode.AUTH_FAILED.value

    def test_rate_limit_distinguished(self):
        _register_fake_llm("fake_429",
                           lambda request: httpx.Response(429, request=request))
        health = asyncio.run(check_provider(_settings_for("fake_429"), "llm"))
        assert health.code == ProviderErrorCode.RATE_LIMITED.value

    def test_upstream_unavailable_distinguished(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        _register_fake_llm("fake_down", handler)
        health = asyncio.run(check_provider(_settings_for("fake_down"), "llm"))
        assert health.code == ProviderErrorCode.UNAVAILABLE.value

    def test_timeout_distinguished(self):
        def handler(request):
            raise httpx.ConnectTimeout("too slow")

        _register_fake_llm("fake_slow", handler)
        health = asyncio.run(check_provider(_settings_for("fake_slow"), "llm"))
        assert health.code == ProviderErrorCode.TIMEOUT.value

    def test_healthy_provider_reports_ok_with_capabilities(self):
        _register_fake_llm(
            "fake_ok",
            lambda request: httpx.Response(200, request=request,
                                           json=_ok_body("pong")))
        health = asyncio.run(check_provider(_settings_for("fake_ok"), "llm"))
        assert health.status == "ok"
        assert health.latency_ms is not None
        body = health.to_dict()
        assert body["capabilities"]["structured_output"] is True
        assert "sk-x" not in str(body)  # 不含任何凭据值

    def test_unknown_provider_reported_as_configuration(self):
        health = asyncio.run(check_provider(_settings_for("ghost"), "llm"))
        assert health.code == ProviderErrorCode.CONFIGURATION.value

    def test_health_does_not_use_wrapping_or_retries(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(500, request=request)

        _register_fake_llm("fake_500", handler)
        health = asyncio.run(check_provider(_settings_for("fake_500"), "llm"))
        assert health.code == ProviderErrorCode.UNAVAILABLE.value
        assert calls["n"] == 1, "健康检查不应触发重试"

    def test_embedding_health_ok_with_stub(self):
        settings = SimpleNamespace(
            embedding_provider="stub", embedding_model="stub-m",
            embedding_dim=8, embedding_api_key="")
        health = asyncio.run(check_provider(settings, "embedding"))
        assert health.status == "ok"


# ---------------------------------------------------------------------------
# 8. embedding：batch / 乱序 / 维度漂移 / 连接复用 / async
# ---------------------------------------------------------------------------
def _embedding(handler, dim: int = 4, **kw) -> OpenAICompatEmbedding:
    return OpenAICompatEmbedding(
        "m", dim, "https://fake/v1", "k",
        transport=httpx.MockTransport(handler),
        retry=RetryPolicy(max_retries=2, base_delay=0.001, jitter_ratio=0),
        **kw)


def _vec(seed: float, dim: int = 4) -> list[float]:
    return [seed] * dim


class TestEmbeddingProvider:
    def test_out_of_order_response_is_reordered(self):
        payload = {"data": [{"index": 1, "embedding": _vec(2.0)},
                            {"index": 0, "embedding": _vec(1.0)}]}
        emb = _embedding(lambda r: httpx.Response(200, request=r, json=payload))
        assert emb.embed_batch(["a", "b"]) == [_vec(1.0), _vec(2.0)]

    def test_dimension_drift_rejected(self):
        payload = {"data": [{"index": 0, "embedding": [1.0, 2.0, 3.0]}]}
        emb = _embedding(lambda r: httpx.Response(200, request=r, json=payload))
        with pytest.raises(ProviderError) as excinfo:
            emb.embed_batch(["a"])
        assert excinfo.value.code is ProviderErrorCode.INVALID_RESPONSE
        assert "维度漂移" in str(excinfo.value)

    def test_missing_items_rejected(self):
        payload = {"data": []}
        emb = _embedding(lambda r: httpx.Response(200, request=r, json=payload))
        with pytest.raises(ProviderError):
            emb.embed_batch(["a", "b"])

    def test_malformed_payload_rejected(self):
        emb = _embedding(
            lambda r: httpx.Response(200, request=r, json={"nope": 1}))
        with pytest.raises(ProviderError) as excinfo:
            emb.embed_batch(["a"])
        assert excinfo.value.code is ProviderErrorCode.INVALID_RESPONSE

    def test_batches_split_by_max_batch(self):
        seen: list[int] = []

        def handler(request):
            import json as _json

            size = len(_json.loads(request.content)["input"])
            seen.append(size)
            return httpx.Response(200, request=request, json={
                "data": [{"index": i, "embedding": _vec(1.0)}
                         for i in range(size)]})

        emb = _embedding(handler, max_batch=2)
        out = emb.embed_batch(["a", "b", "c"])
        assert len(out) == 3
        assert seen == [2, 1], "应按 max_batch 分批"
        assert emb.calls == 1, "calls 语义 = embed_batch 调用次数（IG-15）"

    def test_retries_rate_limited_then_ok(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, request=request)
            return httpx.Response(200, request=request, json={
                "data": [{"index": 0, "embedding": _vec(1.0)}]})

        emb = _embedding(handler)
        assert emb.embed_batch(["a"]) == [_vec(1.0)]
        assert calls["n"] == 2
        assert emb._metrics.snapshot()["retries"] == 1

    def test_auth_failure_not_retried(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(401, request=request)

        emb = _embedding(handler)
        with pytest.raises(ProviderError) as excinfo:
            emb.embed_batch(["a"])
        assert excinfo.value.code is ProviderErrorCode.AUTH_FAILED
        assert calls["n"] == 1

    def test_connection_is_reused(self):
        emb = _embedding(lambda r: httpx.Response(200, request=r, json={
            "data": [{"index": 0, "embedding": _vec(1.0)}]}))
        emb.embed_batch(["a"])
        first = emb.client
        emb.embed_batch(["b"])
        assert emb.client is first, "持久 client 应复用连接"
        assert first.is_closed is False
        emb.close()
        assert first.is_closed is True
        assert emb._client is None

    def test_async_entry_does_not_block_event_loop(self):
        """issue #76：同步 HTTP 必须经线程隔离，否则串行阻塞事件循环。"""
        def handler(request):
            time.sleep(0.05)
            return httpx.Response(200, request=request, json={
                "data": [{"index": 0, "embedding": _vec(1.0)}]})

        emb = _embedding(handler)

        async def main():
            started = time.perf_counter()
            await asyncio.gather(*[emb.embed_batch_async(["x"])
                                   for _ in range(3)])
            return time.perf_counter() - started

        elapsed = asyncio.run(main())
        assert elapsed < 0.13, f"未并行执行（耗时 {elapsed:.3f}s ≈ 串行 0.15s）"

    def test_missing_credentials_is_configuration_error(self):
        with pytest.raises(EmbeddingAPIError) as excinfo:
            OpenAICompatEmbedding("m", 4, "", "")
        assert excinfo.value.code is ProviderErrorCode.CONFIGURATION

    def test_build_provider_registry_dispatch(self):
        settings = SimpleNamespace(embedding_provider="stub",
                                   embedding_model="m", embedding_dim=8)
        assert isinstance(build_provider(settings), StubEmbedding)
        with pytest.raises(ProviderError) as excinfo:
            build_provider(SimpleNamespace(
                embedding_provider="ghost", embedding_model="m",
                embedding_dim=8))
        assert excinfo.value.code is ProviderErrorCode.CONFIGURATION


# ---------------------------------------------------------------------------
# 9. 健康检查 admin 端点
# ---------------------------------------------------------------------------
class TestProviderHealthEndpoint:
    def test_requires_admin(self, investigator_client):
        r = investigator_client.get("/api/v1/admin/providers/health")
        assert r.status_code == 403

    def test_admin_sees_health_without_secrets(self, admin_client,
                                               monkeypatch):
        import json

        from backend.core.config import reset_settings

        monkeypatch.setenv("LLM_PROVIDER", "mock")
        monkeypatch.setenv("EMBEDDING_PROVIDER", "stub")
        reset_settings()
        try:
            r = admin_client.get("/api/v1/admin/providers/health")
            assert r.status_code == 200
            body = r.json()
            kinds = {p["kind"]: p for p in body["providers"]}
            assert kinds["llm"]["provider"] == "mock"
            assert kinds["llm"]["status"] == "ok"
            assert kinds["embedding"]["provider"] == "stub"
            assert body["ok"] is True
            text = json.dumps(body)
            # 只暴露 provider/状态/能力布尔值，不含任何凭据
            assert "sk-" not in text and "Bearer" not in text
        finally:
            reset_settings()
