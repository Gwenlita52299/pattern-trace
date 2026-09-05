"""LLM provider 抽象 — llm-judge-spec §2。

能力契约（spec §2 修正）：各 provider 原生 structured-output 能力不同
（OpenAI json_schema / Ollama format 参数 / Anthropic 无原生）。
统一约定为「尽力 structured output + 本地 Pydantic 硬校验兜底」——
无论 provider 是否支持 schema 参数，返回值都必须通过本地校验才算有效。

Mock provider 是端到端测试的关键：不依赖公网或本地 Ollama 即可跑通
完整判断链路，scenario 由环境变量 LLM_MOCK_SCENARIO 控制。
"""
from __future__ import annotations

import abc
import json
import os
import re
from typing import TYPE_CHECKING

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
        max_tokens: int = 1024,
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


class OpenAIClient(_HTTPClient):
    """OpenAI chat completions + 原生 response_format json_schema。

    strict_schema 能力开关：OpenAI 官端支持 json_schema 严格约束；部分兼容端点
    （如 DeepSeek）只认 json_object，接入方在子类置 False，schema 改由 prompt
    文本内嵌提供 + judge 层本地校验兜底（LJ-17 同一条路径）。
    """

    strict_schema: bool = True

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
                       max_tokens=1024) -> str:

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
            return resp.json()["choices"][0]["message"]["content"] or ""


class VLLMClient(OpenAIClient):
    """vLLM 暴露 OpenAI 兼容端点，仅默认 base_url 不同。"""


class DeepSeekClient(OpenAIClient):
    """DeepSeek OpenAI 兼容端点：API 形态相同，但不支持 response_format
    json_schema（只认 json_object），置 strict_schema=False 走 prompt 内嵌兜底。"""

    strict_schema = False


class AnthropicClient(_HTTPClient):
    """Anthropic messages API：无原生 json_schema 参数，
    schema 以文本形式并入 system prompt（spec §2 能力契约），本地硬校验兜底。"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._transport = transport

    async def complete(self, messages, json_schema=None, temperature=0.0,
                       max_tokens=1024) -> str:
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
            blocks = resp.json().get("content", [])
            return "".join(b.get("text", "") for b in blocks)


class OllamaClient(_HTTPClient):
    """Ollama /api/chat：format 参数传 JSON Schema 实现约束解码。"""

    def __init__(self, base_url: str, model: str,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._transport = transport

    async def complete(self, messages, json_schema=None, temperature=0.0,
                       max_tokens=1024) -> str:
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
            return resp.json()["message"]["content"] or ""


_ID_PATTERN = re.compile(r"(?:addr|tx|edge):[^\s\"'`,\]]+")
# issue #26：只提取 CANDIDATE PATTERNS 的 pattern_name 行——此前 ^[-\w]+: 的宽
# 正则会把 "seed: bc1q..." 等行也当候选名回显，新校验下这类引用会被拒绝
_NAME_PATTERN = re.compile(r"^pattern_name:\s*(.+)$", re.MULTILINE)


class MockLLMClient(LLMClient):
    """脚本化响应（LLM_PROVIDER=mock），场景由 scenario 参数/环境变量控制。

    valid_* 场景从用户消息里提取真实存在的子图 ID 作为 evidence，
    保证响应天然通过引用校验；invalid_* 场景用于触发重试/失败路径。
    """

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
        ids = _ID_PATTERN.findall(user_text)
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
            return json.dumps({
                "risk_level": "no_match", "matched_pattern": None,
                "confidence": 0.82, "evidence": [],
                "reasoning": "no structural correspondence with any known pattern",
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
        return json.dumps({
            "risk_level": "high", "matched_pattern": matched,
            "confidence": 0.91, "evidence": ids[:3],
            "reasoning": f"subgraph shows layering consistent with {matched or 'known mixing playbook'}",
            "recommended_action": "freeze",
        })


def get_llm_client(settings=None) -> LLMClient:
    """env/settings 切换 provider（LJ-01 集成冒烟：改环境变量即生效）。"""
    if settings is None:
        from backend.core.config import get_settings

        settings = get_settings()
    provider = settings.llm_provider
    if provider == "openai":
        return OpenAIClient(
            base_url=getattr(settings, "llm_base_url", "") or "https://api.openai.com/v1",
            api_key=getattr(settings, "llm_api_key", ""),
            model=settings.llm_model)
    if provider == "anthropic":
        return AnthropicClient(
            base_url=getattr(settings, "llm_base_url", "")
            or "https://api.anthropic.com",
            api_key=getattr(settings, "llm_api_key", ""),
            model=settings.llm_model)
    if provider == "vllm":
        return VLLMClient(
            base_url=settings.llm_base_url,
            api_key=getattr(settings, "llm_api_key", "") or "EMPTY",
            model=settings.llm_model)
    if provider == "deepseek":
        return DeepSeekClient(
            base_url=getattr(settings, "llm_base_url", "")
            or "https://api.deepseek.com",
            api_key=getattr(settings, "llm_api_key", ""),
            model=settings.llm_model)
    if provider == "ollama":
        return OllamaClient(base_url=settings.llm_base_url, model=settings.llm_model)
    if provider == "mock":
        return MockLLMClient(
            scenario=os.environ.get("LLM_MOCK_SCENARIO", "valid_high"))
    raise ValueError(f"unknown LLM_PROVIDER: {provider!r}")
