"""LLM provider 抽象 — llm-judge-spec §2。

能力契约（spec §2 修正）：各 provider 原生 structured-output 能力不同
（OpenAI json_schema / Ollama format 参数 / Anthropic 无原生）。
统一约定为「尽力 structured output + 本地 Pydantic 硬校验兜底」——
无论 provider 是否支持 schema 参数，返回值都必须通过本地校验才算有效。

issue #76 重构：具体类只负责"怎么调用上游"；注册（registry）、能力声明
（capabilities）、并发闸门/重试/计量/脱敏（wrappers）由 backend.core.providers
统一提供——新增 provider 只需实现 complete() + 声明 CAPABILITIES + 注册。

Mock provider 是端到端测试的关键：不依赖公网或本地 Ollama 即可跑通
完整判断链路，scenario 由环境变量 LLM_MOCK_SCENARIO 控制，
每次调用前的固定延迟由 LLM_MOCK_DELAY_MS 控制（压测注入受控延迟用）。
它是测试替身，**不套统一包装**（避免重试/闸门干扰脚本化场景与 test
直接注入属性）。
"""
from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
import re
import time
from typing import TYPE_CHECKING

from backend.core.providers import (REGISTRY, ConcurrencyGate,
                                    ProviderCapabilities, ProviderError,
                                    ProviderErrorCode, ProviderMetrics,
                                    ProviderSpec, RetryPolicy, classify,
                                    gate_for, log_provider_event)

if TYPE_CHECKING:  # 注解引用 httpx 类型；运行时在各 complete() 内延迟导入
    import httpx


class LLMClient(abc.ABC):
    """所有 provider 的统一接口；返回原始文本（含可能的 think 块）。"""

    @abc.abstractmethod
    async def complete(
        self,
        messages: list[dict],
        json_schema: dict | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str: ...

    @property
    def model_name(self) -> str:
        return getattr(self, "model", self.__class__.__name__)


def _schema_prompt_text(json_schema: dict | None) -> str:
    """无原生 schema 能力的 provider 把 schema 说明内嵌进 prompt（LJ-17）。"""
    if not json_schema:
        return ""
    return (
        "\n\nThe response MUST be a single JSON object conforming to this "
        f"JSON Schema:\n{json.dumps(json_schema)}"
    )


def _embed_schema_in_messages(messages, json_schema):
    """把 schema 文本并入 system 消息末尾（非严格端点无 schema 约束解码时，
    json_object 只保证“输出是 JSON”，字段名要靠 prompt 自述）。"""
    if not json_schema:
        return messages
    out, appended = [], False
    for m in messages:
        if m.get("role") == "system" and not appended:
            out.append({"role": "system",
                        "content": m["content"] + _schema_prompt_text(json_schema)})
            appended = True
        else:
            out.append(m)
    if not appended:
        out.insert(0, {"role": "system", "content": _schema_prompt_text(json_schema)})
    return out


class _HTTPClient(LLMClient):
    """共享 httpx.AsyncClient 生命周期管理。"""

    timeout_seconds: float = 30.0

    async def close(self) -> None:
        pass  # 子类按需覆写；MVP 用短连接（每次请求独立 client）简化生命周期


def _malformed(client, what: str, exc: BaseException) -> ProviderError:
    """响应结构不符合预期 → invalid_response（issue #76 统一分类）。

    在解析处显式包装，好过依赖错误消息关键词启发式——上游换格式时
    能明确归因为"响应畸形"而不是让 KeyError 冒到编排层变成 TASK_FAILED。
    """
    return ProviderError(
        ProviderErrorCode.INVALID_RESPONSE,
        f"malformed {what} response: {exc}",
        provider=type(client).__name__,
        model=getattr(client, "model", ""), cause=exc)


class OpenAIClient(_HTTPClient):
    """OpenAI chat completions + 原生 response_format json_schema。

    strict_schema 能力开关：OpenAI 官端支持 json_schema 严格约束；部分兼容端点
    （如 DeepSeek）只认 json_object，接入方在子类置 False，schema 改由 prompt
    文本内嵌提供 + judge 层本地校验兜底（LJ-17 同一条路径）。
    """

    strict_schema: bool = True
    CAPABILITIES = ProviderCapabilities(
        kind="llm", structured_output=True, native_json_schema=True,
        requires_api_key=True)
    last_usage: dict | None = None   # 上游 usage（包装层计量读取）

    def __init__(self, base_url: str, api_key: str, model: str,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        # 测试注入点（httpx.MockTransport）；生产恒为 None
        self._transport = transport

    def _client(self):
        import httpx

        return httpx.AsyncClient(timeout=self.timeout_seconds,
                                 transport=self._transport)

    async def complete(self, messages, json_schema=None, temperature=0.0,
                       max_tokens=4096) -> str:

        if json_schema is not None and not self.strict_schema:
            # 非严格端点不支持 json_schema：schema 文本并入 system，
            # 仅用 json_object 兜底约束“输出为 JSON”，字段正确性靠本地校验
            messages = _embed_schema_in_messages(messages, json_schema)

        body: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_schema is not None:
            if self.strict_schema:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "judgment", "strict": True,
                                    "schema": json_schema},
                }
            else:
                body["response_format"] = {"type": "json_object"}
        async with self._client() as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
        self.last_usage = data.get("usage")
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:  # issue #76
            raise _malformed(self, "chat completion", exc) from exc


class VLLMClient(OpenAIClient):
    """vLLM 暴露 OpenAI 兼容端点，仅默认 base_url 不同。"""


class DeepSeekClient(OpenAIClient):
    """DeepSeek OpenAI 兼容端点：API 形态相同，但不支持 response_format
    json_schema（只认 json_object），置 strict_schema=False 走 prompt 内嵌兜底。"""

    strict_schema = False
    CAPABILITIES = ProviderCapabilities(
        kind="llm", structured_output=True, native_json_schema=False,
        requires_api_key=True)


class AnthropicClient(_HTTPClient):
    """Anthropic messages API：无原生 json_schema 参数，
    schema 以文本形式并入 system prompt（spec §2 能力契约），本地硬校验兜底。"""

    CAPABILITIES = ProviderCapabilities(
        kind="llm", structured_output=True, native_json_schema=False,
        requires_api_key=True)

    def __init__(self, base_url: str, api_key: str, model: str,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._transport = transport

    async def complete(self, messages, json_schema=None, temperature=0.0,
                       max_tokens=4096) -> str:
        import httpx

        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        rest = [m for m in messages if m["role"] != "system"]
        if json_schema is not None:
            system_parts.append(_schema_prompt_text(json_schema))
        async with httpx.AsyncClient(timeout=self.timeout_seconds,
                                     transport=self._transport) as client:
            resp = await client.post(
                f"{self.base_url}/v1/messages",
                headers={"x-api-key": self.api_key,
                         "anthropic-version": "2023-06-01"},
                json={
                    "model": self.model,
                    "system": "\n\n".join(system_parts),
                    "messages": rest,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise _malformed(self, "anthropic messages",
                             ValueError(f"content is {type(blocks).__name__}"))
        return "".join(b.get("text", "") for b in blocks
                       if isinstance(b, dict))


class OllamaClient(_HTTPClient):
    """Ollama /api/chat：format 参数传 JSON Schema 实现约束解码。"""

    CAPABILITIES = ProviderCapabilities(
        kind="llm", structured_output=True, native_json_schema=True,
        requires_api_key=False)

    def __init__(self, base_url: str, model: str,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._transport = transport

    async def complete(self, messages, json_schema=None, temperature=0.0,
                       max_tokens=4096) -> str:
        import httpx

        body: dict = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        # Qwen3 系列：关闭思考模式降低延迟（spec §7 生产默认）
        if "qwen3" in self.model.lower():
            body["think"] = False
        if json_schema is not None:
            body["format"] = json_schema
        else:
            body["format"] = "json"
        async with httpx.AsyncClient(timeout=self.timeout_seconds,
                                     transport=self._transport) as client:
            resp = await client.post(f"{self.base_url}/api/chat", json=body)
            resp.raise_for_status()
            data = resp.json()
        try:
            return data["message"]["content"] or ""
        except (KeyError, TypeError) as exc:  # issue #76
            raise _malformed(self, "ollama chat", exc) from exc


_NODE_ID_PATTERN = re.compile(r"node id=(addr:[^\s\"'`,\]]+)")
# issue #26：只提取 CANDIDATE PATTERNS 的 pattern_name 行——此前 ^[-\w]+: 的宽
# 正则会把 "seed: bc1q..." 等行也当候选名回显，新校验下这类引用会被拒绝
_NAME_PATTERN = re.compile(r"^pattern_name:\s*(.+)$", re.MULTILINE)


class MockLLMClient(LLMClient):
    """脚本化响应（LLM_PROVIDER=mock），场景由 scenario 参数/环境变量控制。

    valid_* 场景从用户消息里提取真实存在的地址节点 ID 作为 evidence
    （物证统一为 addr），保证响应天然通过引用校验；invalid_* 场景用于
    触发重试/失败路径。
    """

    CAPABILITIES = ProviderCapabilities(
        kind="llm", structured_output=True, native_json_schema=True,
        requires_api_key=False)

    def __init__(self, scenario: str = "valid_high",
                 response_factory=None, fixed_delay_ms: int = 0):
        self.scenario = scenario
        self.response_factory = response_factory
        self.fixed_delay_ms = fixed_delay_ms
        self.calls = 0

    @property
    def model_name(self) -> str:
        return f"mock-{self.scenario}"

    async def complete(self, messages, json_schema=None, temperature=0.0,
                       max_tokens=1024) -> str:
        import asyncio

        self.calls += 1
        if self.fixed_delay_ms:
            await asyncio.sleep(self.fixed_delay_ms / 1000)
        if self.response_factory is not None:
            return self.response_factory(messages)

        user_text = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        # 物证统一为 addr：合法场景只引用地址节点 id（从 node id= 行精确提取，
        # 避免把 addr:A->tx:T 这类边 id 当作节点引用）
        ids = _NODE_ID_PATTERN.findall(user_text)
        names = _NAME_PATTERN.findall(user_text)
        matched = names[0].strip() if names else None

        if self.scenario == "invalid_evidence_all_retries":
            return json.dumps({
                "risk_level": "high", "matched_pattern": matched,
                "confidence": 0.9,
                "evidence": ["tx:nonexistent0000000000000000000000000000000000000000000000000000"],
                "reasoning": "referencing an ID outside the subgraph",
                "recommended_action": "freeze",
            })
        if self.scenario == "invalid_json_then_valid" and self.calls == 1:
            return "not json at all"
            # 其余调用落入下方 valid 分支

        if self.scenario in ("valid_no_match", "no_match"):
            # issue #72：枚举重构后 no_match 档已删除——场景语义变为
            # flagged_no_pattern（matched=null + medium + review）
            return json.dumps({
                "risk_level": "medium", "matched_pattern": None,
                "confidence": 0.55, "evidence": [],
                "reasoning": "suspicious structure but no KB correspondence",
                "recommended_action": "review",
            })
        if self.scenario == "valid_low":
            return json.dumps({
                "risk_level": "low", "matched_pattern": None,
                "confidence": 0.31,
                "evidence": ids[:2],
                "reasoning": "plain spending pattern; no mixer contact, "
                             "shallow fan-out",
                "recommended_action": "monitor",
            })
        if self.scenario == "timeout":
            import httpx

            raise httpx.TimeoutException("simulated provider timeout")
        if self.scenario == "rate_limited":
            import httpx

            resp = httpx.Response(429, request=httpx.Request(
                "POST", "mock://llm"))
            raise httpx.HTTPStatusError(
                "simulated provider rate limit", request=resp.request,
                response=resp)
        # issue #72：无候选 → matched=null → high 必须 review
        # （flagged_no_pattern 校验强制）；有匹配才可 freeze
        return json.dumps({
            "risk_level": "high", "matched_pattern": matched,
            "confidence": 0.91, "evidence": ids[:3],
            "reasoning": f"subgraph shows layering consistent with {matched or 'known mixing playbook'}",
            "recommended_action": "freeze" if matched else "review",
        })


class MeteredLLMClient(LLMClient):
    """统一包装：进程级并发闸门 + 退避重试 + 计量 + 脱敏日志 + 错误分类。

    - 闸门与计量按 provider+model 共享（跨 client 实例、跨 judgment 生效；
      编排每次分析都新建 client，实例级限制没有意义）
    - 只重试 retryable 码（timeout/rate_limited/unavailable）；退避等待
      放在闸门之外，避免占着并发额度空转
    - usage 取内层 provider 的 last_usage（不提供该属性的 provider 记 0）
    - provider 层异常统一收敛为 ProviderError；代码缺陷原样上抛

    `wrap_llm_client()` 会为每个内层类型生成动态子类，使
    `isinstance(client, OpenAIClient)` 等既有契约仍然成立。
    """

    def __init__(self, inner: LLMClient, *, capabilities: ProviderCapabilities,
                 gate: ConcurrencyGate, retry: RetryPolicy,
                 metrics: ProviderMetrics) -> None:
        self.inner = inner
        self.capabilities = capabilities
        self.metrics = metrics
        self._gate = gate
        self._retry = retry

    @property
    def model_name(self) -> str:
        return self.inner.model_name

    async def complete(self, messages, json_schema=None, temperature=0.0,
                       max_tokens=4096) -> str:
        attempts = self._retry.max_retries + 1
        last_error: ProviderError | None = None
        for attempt in range(attempts):
            started = time.perf_counter()
            try:
                async with self._gate:
                    raw = await self.inner.complete(
                        messages, json_schema=json_schema,
                        temperature=temperature, max_tokens=max_tokens)
                self.metrics.record_success(
                    (time.perf_counter() - started) * 1000,
                    getattr(self.inner, "last_usage", None))
                return raw
            except Exception as exc:  # noqa: BLE001 - 分类后决定重试或上抛
                err = classify(exc, provider=self.metrics.provider,
                               model=self.metrics.model)
                if err is None:
                    raise  # 非 provider 层错误（编程缺陷）不伪装、不重试
                self.metrics.record_failure(
                    err.code, (time.perf_counter() - started) * 1000)
                if not err.retryable or attempt >= attempts - 1:
                    raise err from exc
                last_error = err
                self.metrics.record_retry()
                log_provider_event(
                    self.metrics, "retry", level=logging.WARNING,
                    attempt=attempt + 1, error_code=err.code.value)
            await asyncio.sleep(self._retry.delay_for(attempt))
        raise last_error or ProviderError(
            ProviderErrorCode.UNAVAILABLE, "provider call failed",
            provider=self.metrics.provider, model=self.metrics.model)


def wrap_llm_client(inner: LLMClient, *, capabilities: ProviderCapabilities,
                    gate: ConcurrencyGate, retry: RetryPolicy,
                    metrics: ProviderMetrics) -> MeteredLLMClient:
    """生成动态子类（同时继承 MeteredLLMClient 与内层类型）。

    这样 `isinstance(get_llm_client(...), OpenAIClient)` 这类既有断言、
    以及 `judge.client.model_name` 等直接属性访问都不受影响。
    """
    cls = type(f"Metered{type(inner).__name__}",
               (MeteredLLMClient, type(inner)), {})
    obj = cls.__new__(cls)
    MeteredLLMClient.__init__(obj, inner, capabilities=capabilities,
                              gate=gate, retry=retry, metrics=metrics)
    return obj


def _register_llm_providers() -> None:
    """内置 LLM provider 自注册（issue #76：新增 provider 只需在此加一行）。"""

    def _openai(settings, **kw):
        return OpenAIClient(
            base_url=getattr(settings, "llm_base_url", "")
            or "https://api.openai.com/v1",
            api_key=getattr(settings, "llm_api_key", ""),
            model=settings.llm_model, **kw)

    def _anthropic(settings, **kw):
        return AnthropicClient(
            base_url=getattr(settings, "llm_base_url", "")
            or "https://api.anthropic.com",
            api_key=getattr(settings, "llm_api_key", ""),
            model=settings.llm_model, **kw)

    def _vllm(settings, **kw):
        return VLLMClient(
            base_url=settings.llm_base_url,
            api_key=getattr(settings, "llm_api_key", "") or "EMPTY",
            model=settings.llm_model, **kw)

    def _deepseek(settings, **kw):
        return DeepSeekClient(
            base_url=getattr(settings, "llm_base_url", "")
            or "https://api.deepseek.com",
            api_key=getattr(settings, "llm_api_key", ""),
            model=settings.llm_model, **kw)

    def _ollama(settings, **kw):
        return OllamaClient(base_url=settings.llm_base_url,
                            model=settings.llm_model, **kw)

    def _mock(settings, **kw):
        # LLM_MOCK_DELAY_MS：压测 W1 专线用受控延迟把队列堆积拉到可观测区间
        # （stress-test-spec §2.4）。仅 mock 生效，真实 provider 不受影响。
        delay_ms = int(os.environ.get("LLM_MOCK_DELAY_MS", "0") or 0)
        return MockLLMClient(
            scenario=os.environ.get("LLM_MOCK_SCENARIO", "valid_high"),
            fixed_delay_ms=delay_ms, **kw)

    for name, factory, cls, wrap in (
            ("openai", _openai, OpenAIClient, True),
            ("anthropic", _anthropic, AnthropicClient, True),
            ("vllm", _vllm, VLLMClient, True),
            ("deepseek", _deepseek, DeepSeekClient, True),
            ("ollama", _ollama, OllamaClient, True),
            # mock 是测试替身：脚本化场景 + 测试直接注入属性，不套包装
            ("mock", _mock, MockLLMClient, False)):
        REGISTRY.register(ProviderSpec(
            kind="llm", name=name, factory=factory,
            capabilities=cls.CAPABILITIES, wrap=wrap,
            display=cls.__name__))


_register_llm_providers()


def get_llm_client(settings=None) -> LLMClient:
    """env/settings 切换 provider（LJ-01 集成冒烟：改环境变量即生效）。

    issue #76：工厂不再 if-elif 分发——查注册表拿 spec（含能力声明与
    是否包装），真实 provider 一律套统一包装层。
    """
    if settings is None:
        from backend.core.config import get_settings

        settings = get_settings()
    provider = settings.llm_provider
    spec = REGISTRY.get("llm", provider)
    inner = spec.factory(settings=settings)
    if not spec.wrap:
        return inner
    metrics = ProviderMetrics(provider=provider, model=settings.llm_model,
                              kind="llm")
    return wrap_llm_client(
        inner, capabilities=spec.capabilities,
        gate=gate_for(f"llm:{provider}:{settings.llm_model}",
                      getattr(settings, "llm_max_concurrency", 4),
                      getattr(settings, "llm_concurrency_policy", "wait")),
        retry=RetryPolicy(
            max_retries=getattr(settings, "llm_max_retries", 2),
            base_delay=getattr(settings, "llm_retry_base_delay", 0.5)),
        metrics=metrics)
