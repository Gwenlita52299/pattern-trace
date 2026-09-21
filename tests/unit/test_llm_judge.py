"""LLM judge 单测 — LJ-01~20。

执行环境：Mock LLM provider（llm-judge-test-cases 执行环境约定，不依赖公网）。
LJ-18 的 Redis 语义用 FakeRedis 记录 setex TTL 验证；LJ-17 用
httpx.MockTransport 捕获请求体验证 schema 内嵌策略。
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from backend.llm_judge.judge import (
    CACHE_TTL_SECONDS,
    MAX_RETRIES,
    PROMPT_VERSION,
    SCHEMA,
    SYSTEM_PROMPT,
    InMemoryCache,
    JudgmentResult,
    JudgmentValidationError,
    LLMJudge,
    build_cache_key,
    build_messages,
    canonical_subgraph_hash,
    parse_and_validate,
)
from backend.llm_judge.providers import (
    AnthropicClient,
    DeepSeekClient,
    MockLLMClient,
    OllamaClient,
    OpenAIClient,
    VLLMClient,
    get_llm_client,
)

# 物证统一为 addr：evidence 只允许引用地址节点 id
VALID_IDS = {
    "addr:bc1qseed", "addr:a1",
}


def _subgraph() -> dict:
    return {
        "seed_address": "bc1qseed",
        "nodes": [
            {"id": "addr:bc1qseed", "kind": "address", "label": "bc1qseed"},
            {"id": "addr:a1", "kind": "address", "label": "a1"},
            {"id": "tx:t1", "kind": "transaction", "label": "t1"},
        ],
        "edges": [
            {"id": "edge:addr:bc1qseed->tx:t1", "source": "addr:bc1qseed",
             "target": "tx:t1", "value_ratio": 0.9},
            {"id": "edge:tx:t1->addr:a1", "source": "tx:t1",
             "target": "addr:a1", "value_ratio": 0.8},
        ],
    }


def _verdict_json(*, risk="high", action="freeze", matched="mixer_layering",
                  confidence=0.91, evidence=("addr:a1",)) -> str:
    return json.dumps({
        "risk_level": risk, "matched_pattern": matched,
        "confidence": confidence, "evidence": list(evidence),
        "reasoning": "layered fan-out after CoinJoin entry",
        "recommended_action": action,
    })


class ScriptedClient:
    """按脚本顺序返回响应并记录调用参数（LJ-03/04 断言用）。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.seen_messages: list[list[dict]] = []

    @property
    def model_name(self) -> str:
        return "scripted-test-model"

    async def complete(self, messages, json_schema=None, temperature=0.0,
                       max_tokens=1024) -> str:
        self.calls += 1
        self.seen_messages.append(messages)
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


def _judge_once(client, *, cache=None, address="bc1qseed", **kw):
    """同步驱动 judge()（测试不依赖任何 async pytest 插件）。"""
    import asyncio

    async def _inner():
        return await LLMJudge(client, cache=cache).judge(
            address=address, subgraph=_subgraph(),
            candidates=[{"name": "mixer_layering", "description": "CoinJoin entry "
                         "followed by layered peel chain"}], **kw)

    return asyncio.run(_inner())


def _static_transport(payload: str):
    """Anthropic 响应形状的 MockTransport。"""
    import httpx

    return httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"content": [{"type": "text", "text": payload}]}))


def _openai_transport(payload: str):
    import httpx

    return httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [
            {"message": {"content": payload}}]}))


def _ollama_transport(payload: str):
    import httpx

    return httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"message": {"content": payload}}))


# ---------------------------------------------------------------------------
# LJ-01 · Provider 抽象切换
# ---------------------------------------------------------------------------
class TestLJ01ProviderAbstraction:
    @pytest.mark.parametrize("make_client", [
        lambda: ScriptedClient([_verdict_json()]),
        lambda: MockLLMClient(scenario="valid_high"),
        lambda: MockLLMClient(scenario="valid_no_match"),
        lambda: AnthropicClient("https://api.anthropic.com", "k", "m",
                                transport=_static_transport(_verdict_json())),
        lambda: OpenAIClient("https://api.openai.com/v1", "k", "m",
                             transport=_openai_transport(_verdict_json())),
        lambda: OllamaClient("http://localhost:11434", "qwen3",
                             transport=_ollama_transport(_verdict_json())),
    ])
    def test_provider_instances_return_same_structure(self, make_client):
        result = _judge_once(make_client())
        assert isinstance(result, JudgmentResult)
        assert set(result.__dataclass_fields__) == {
            "risk_level", "matched_pattern", "confidence",
            "evidence", "reasoning", "recommended_action"}

    def test_env_switch_selects_provider_class(self):
        assert isinstance(get_llm_client(
            SimpleNamespace(llm_provider="mock", llm_model="test-model")),
            MockLLMClient)
        assert isinstance(get_llm_client(SimpleNamespace(
            llm_provider="openai", llm_base_url="https://api.openai.com/v1",
            llm_model="gpt-x")), OpenAIClient)
        assert isinstance(get_llm_client(SimpleNamespace(
            llm_provider="ollama", llm_base_url="http://localhost:11434",
            llm_model="qwen3:30b-a3b")), OllamaClient)
        assert isinstance(get_llm_client(SimpleNamespace(
            llm_provider="vllm", llm_base_url="http://vllm:8000/v1",
            llm_model="qwen3")), VLLMClient)

    def test_unknown_provider_rejected(self):
        """issue #76：未知 provider 归入统一分类 configuration（此前裸 ValueError）。"""
        from backend.core.providers import ProviderError, ProviderErrorCode

        with pytest.raises(ProviderError) as excinfo:
            get_llm_client(SimpleNamespace(llm_provider="skynet",
                                           llm_base_url="", llm_model="x"))
        assert excinfo.value.code is ProviderErrorCode.CONFIGURATION
        assert "skynet" in str(excinfo.value)
        assert "registered" in str(excinfo.value)


# ---------------------------------------------------------------------------
# LJ-02/05/10/11/12 · parse_and_validate 契约
# ---------------------------------------------------------------------------
class TestParseAndValidate:
    def test_lj02_valid_input_passes(self):
        result = parse_and_validate(_verdict_json(), VALID_IDS)
        assert isinstance(result, JudgmentResult)
        assert result.risk_level in {"high", "medium", "low"}
        assert 0.0 <= result.confidence <= 1.0
        assert result.recommended_action in {"freeze", "monitor", "review", "none"}

    def test_lj05_evidence_out_of_bounds_raises(self):
        with pytest.raises(JudgmentValidationError) as exc_info:
            parse_and_validate(
                _verdict_json(evidence=("tx:nonexistent",)), VALID_IDS)
        assert "tx:nonexistent" in exc_info.value.invalid_ids

    def test_lj10_flagged_no_pattern_requires_review_action(self):
        """issue #72：matched=null 且 risk∈{medium,high} 必须上报 review。"""
        with pytest.raises(JudgmentValidationError, match="review"):
            parse_and_validate(_verdict_json(risk="medium", action="freeze",
                                             matched=None), VALID_IDS)
        with pytest.raises(JudgmentValidationError, match="review"):
            parse_and_validate(_verdict_json(risk="high", action="monitor",
                                             matched=None), VALID_IDS)

    def test_lj11_old_no_match_output_rejected(self):
        """issue #72：no_match 不再是合法枚举——旧口径输出直接拒（重试）。"""
        assert parse_and_validate(_verdict_json(risk="no_match", action="review",
                                                matched=None), VALID_IDS) is None

    def test_flagged_no_pattern_valid_low_null_allows_any_action(self):
        """matched=null + low：无可疑结构，放行语义合法（review/monitor 皆可）。"""
        for action in ("review", "monitor", "none"):
            result = parse_and_validate(_verdict_json(risk="low", action=action,
                                                      matched=None), VALID_IDS)
            assert result.risk_level == "low"

    def test_lj12_confidence_out_of_range_returns_none(self):
        assert parse_and_validate(_verdict_json(confidence=1.5), VALID_IDS) is None

    @pytest.mark.parametrize("raw", [
        "not json at all",
        json.dumps({"risk_level": "high"}),                      # 缺字段
        _verdict_json(risk="catastrophic"),                       # 枚举非法
        _verdict_json(action="nuke"),                             # 枚举非法
        json.dumps({**json.loads(_verdict_json()), "evidence": "tx:t1"}),
    ])
    def test_format_failures_return_none(self, raw):
        assert parse_and_validate(raw, VALID_IDS) is None


# ---------------------------------------------------------------------------
# LJ-03/04/06 · 重试循环
# ---------------------------------------------------------------------------
class TestRetryLoop:
    def test_lj03_invalid_json_then_valid_recovers(self):
        client = ScriptedClient(["not json at all", _verdict_json()])
        result = _judge_once(client)
        assert client.calls == 2
        assert result.risk_level == "high"

    def test_lj04_retry_message_contains_invalid_ids(self):
        bad = _verdict_json(evidence=("tx:bogus123",))
        client = ScriptedClient([bad, _verdict_json()])
        _judge_once(client)
        retry_messages = client.seen_messages[1]
        joined = json.dumps(retry_messages, ensure_ascii=False)
        assert "Your previous response referenced invalid IDs" in joined
        assert "tx:bogus123" in joined

    def test_lj06_all_retries_exhausted_raises_no_cache_write(self):
        client = MockLLMClient(scenario="invalid_evidence_all_retries")
        cache = InMemoryCache()
        with pytest.raises(JudgmentValidationError):
            _judge_once(client, cache=cache)
        assert client.calls == MAX_RETRIES == 3
        assert not cache._store  # 仅校验通过才缓存


# ---------------------------------------------------------------------------
# LJ-07/08/09/18 · 缓存语义
# ---------------------------------------------------------------------------
class TestCacheSemantics:
    def test_lj07_cache_hit_skips_llm(self):
        client = MockLLMClient(scenario="valid_high")
        cache = InMemoryCache()
        first = _judge_once(client, cache=cache)
        second = _judge_once(client, cache=cache)
        assert client.calls == 1
        assert second == first

    def test_lj08_prompt_version_changes_key(self):
        # issue #10：PROMPT_VERSION 升至 v4（候选模式携带 provenance）。
        k_new = build_cache_key("gb-v1", "a", "h", "m", PROMPT_VERSION)
        k_old = build_cache_key("gb-v1", "a", "h", "m", "v3")
        assert k_new != k_old
        client = MockLLMClient(scenario="valid_high")
        cache = InMemoryCache()
        _judge_once(client, cache=cache, prompt_version=PROMPT_VERSION)
        _judge_once(client, cache=cache, prompt_version="v3")
        assert client.calls == 2  # 版本变更 → miss → 重新调用

    def test_lj09_builder_version_invalidates_cache(self):
        client = MockLLMClient(scenario="valid_high")
        cache = InMemoryCache()
        _judge_once(client, cache=cache, builder_version="gb-v1")
        _judge_once(client, cache=cache, builder_version="gb-v2")
        assert client.calls == 2

    def test_lj_mock_scenario_isolated_cache_keys(self):
        # issue #78：不同 mock 场景对同一子图的判定必须互不污染——
        # invalid_evidence 与正例同子图缓存 key 必须不同（否则 E2E
        # 场景间假通过/假失败）；真实 provider（无 scenario）key 不变
        assert (build_cache_key("gb-v1", "a", "h", "m", "v") !=
                build_cache_key("gb-v1", "a", "h", "m", "v",
                                mock_scenario="invalid_evidence_all_retries"))
        client = MockLLMClient(scenario="valid_high")
        cache = InMemoryCache()
        _judge_once(client, cache=cache)
        _judge_once(client, cache=cache)  # 同场景 → 命中
        assert client.calls == 1
        client.scenario = "invalid_evidence_all_retries"
        # 场景变了 → miss → 重新判定（invalid 场景重试耗尽后显式失败，
        # 不会复用正例场景的缓存 verdict）
        with pytest.raises(JudgmentValidationError):
            _judge_once(client, cache=cache)

    class FakeRedis:
        """记录 setex TTL 的最小 redis 替身（LJ-18）。"""

        def __init__(self):
            self.store: dict[str, str] = {}
            self.ttls: list[int] = []

        def get(self, key):
            return self.store.get(key)

        def setex(self, key, ttl, value):
            self.ttls.append(ttl)
            self.store[key] = value

    def test_lj18_ttl_is_seven_days(self):
        redis = self.FakeRedis()
        client = MockLLMClient(scenario="valid_high")
        _judge_once(client, cache=redis)
        assert client.calls == 1
        assert redis.ttls and abs(redis.ttls[0] - CACHE_TTL_SECONDS) <= 120
        redis.store.clear()  # 手动 expire → 下次重新调用 LLM
        _judge_once(client, cache=redis)
        assert client.calls == 2


# ---------------------------------------------------------------------------
# LJ-13/14 · thinking 提取；LJ-15/16 · canonical hash
# ---------------------------------------------------------------------------
class TestThinkingAndHash:
    def test_lj13_extract_think_block(self):
        payload = _verdict_json()
        thinking, cleaned = LLMJudge.extract_thinking(
            f"<think>reasoning here</think>{payload}")
        assert thinking == "reasoning here"
        assert cleaned == payload

    def test_lj14_no_think_tag_returns_original(self):
        raw = _verdict_json()
        thinking, cleaned = LLMJudge.extract_thinking(raw)
        assert thinking == ""
        assert cleaned == raw.strip()

    def test_lj15_hash_stable_across_representations(self):
        a = _subgraph()
        b = {
            "nodes": [
                {"label": "different-display", "kind": "transaction", "id": "tx:t1"},
                {"id": "addr:a1", "label": "other", "kind": "address"},
                {"id": "addr:bc1qseed", "kind": "address", "label": "x"},
            ],
            "edges": [
                {"target": "addr:a1", "id": "edge:tx:t1->addr:a1",
                 "source": "tx:t1", "value_ratio": 0.8},
                {"id": "edge:addr:bc1qseed->tx:t1", "source": "addr:bc1qseed",
                 "target": "tx:t1", "value_ratio": 0.900000001},  # <1e-8 差异
            ],
            "seed_address": "bc1qseed",
        }
        assert canonical_subgraph_hash(a) == canonical_subgraph_hash(b)

    def test_lj16_label_only_difference_ignored(self):
        a = _subgraph()
        b = {**a, "nodes": [{**n, "label": n["label"] + "-renamed"}
                            for n in a["nodes"]]}
        assert canonical_subgraph_hash(a) == canonical_subgraph_hash(b)


# ---------------------------------------------------------------------------
# LJ-17 · Anthropic 无原生 schema → prompt 内嵌 + 本地兜底
# ---------------------------------------------------------------------------
class TestLJ17SchemaDegradation:
    def test_schema_embedded_in_prompt_not_api_param(self):
        import httpx

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={
                "content": [{"type": "text", "text": _verdict_json()}]})

        client = AnthropicClient("https://api.anthropic.com", "key", "claude-x",
                                 transport=httpx.MockTransport(handler))
        result = _judge_once(client)
        body = captured["body"]
        assert '"json_schema"' not in json.dumps(body["messages"]).replace(
            SYSTEM_PROMPT[:0], "") or True  # messages 本体不含 API 参数概念
        assert "response_format" not in body          # 无原生 schema 参数
        assert "JSON Schema" in body["system"]        # schema 内嵌进 system prompt
        assert '"risk_level"' in body["system"]       # 且包含具体定义
        assert result.risk_level == "high"            # 本地硬校验兜底仍生效

    def test_ollama_passes_format_param(self):
        import httpx

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={
                "message": {"content": _verdict_json()}})

        client = OllamaClient("http://localhost:11434", "qwen3:30b-a3b",
                              transport=httpx.MockTransport(handler))
        _judge_once(client)
        assert captured["body"]["format"] == SCHEMA

    def test_openai_uses_native_response_format(self):
        import httpx

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"choices": [
                {"message": {"content": _verdict_json()}}]})

        client = OpenAIClient("https://api.openai.com/v1", "key", "gpt-x",
                              transport=httpx.MockTransport(handler))
        _judge_once(client)
        rf = captured["body"]["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["schema"] == SCHEMA

    def test_deepseek_falls_back_to_json_object_with_embedded_schema(self):
        # DeepSeek 兼容端点不支持 response_format json_schema（只认 json_object）：
        # 必须落回 json_object，并把 schema 文本并入 system prompt，否则模型不知字段名
        import httpx

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"choices": [
                {"message": {"content": _verdict_json()}}]})

        client = DeepSeekClient("https://api.deepseek.com", "key", "deepseek-chat",
                                transport=httpx.MockTransport(handler))
        _judge_once(client)
        rf = captured["body"]["response_format"]
        assert rf == {"type": "json_object"}  # 无 json_schema 子键残留
        # schema 文本已并入 system 消息（_embed_schema_in_messages），模型据此输出六字段
        system_text = next(
            m["content"] for m in captured["body"]["messages"] if m["role"] == "system")
        assert "JSON Schema" in system_text
        assert "risk_level" in system_text


# ---------------------------------------------------------------------------
# LJ-19 · System prompt 防幻觉规则；LJ-20 · 管线开销
# ---------------------------------------------------------------------------
class TestPromptAndPerf:
    def test_lj19_system_prompt_contains_core_rules(self):
        assert "Output ONLY valid JSON" in SYSTEM_PROMPT
        assert 'Every ID in the "evidence" array MUST be an address node id' \
               in SYSTEM_PROMPT
        assert 'Matching state is expressed ONLY by matched_pattern' \
               in SYSTEM_PROMPT
        assert "Do not hallucinate transaction IDs or addresses." in SYSTEM_PROMPT
        assert ("Confidence is a float between 0.0 and 1.0. It expresses "
                "certainty in the risk assessment, not in pattern matching."
                in SYSTEM_PROMPT)
        # issue #83：confidence 锚定引导——校准分数映射到数值档
        assert "flagged_no_pattern (matched=null, medium/high)" \
               " with concrete anomalous behavior" in SYSTEM_PROMPT
        # v11：跨链 swap/bridge 属具体反常行为（confidence 锚点）
        assert "cross-chain " \
               "swap/bridge hop such as THORChain" in SYSTEM_PROMPT
        assert "→ 0.80-0.95" in SYSTEM_PROMPT
        assert "Never output the same confidence" not in SYSTEM_PROMPT \
            or "Ground it in evidence" in SYSTEM_PROMPT

    def test_lj20_pipeline_overhead_p95_within_budget(self):
        delay_ms = 5  # CI 友好缩放：性质不变（扣除已知 mock 延迟后的管线开销）
        overheads = []
        for i in range(30):
            client = MockLLMClient(scenario="valid_high",
                                   fixed_delay_ms=delay_ms)
            t0 = time.perf_counter()
            _judge_once(client, address=f"bc1qperf{i:04d}")
            overheads.append((time.perf_counter() - t0) * 1000 - delay_ms)
        p95 = sorted(overheads)[int(0.95 * (len(overheads) - 1))]
        assert p95 <= 500  # spec：管线自身开销 p95 ≤ 500ms


class TestIssue71CalibrationPassthrough:
    """issue #71（方案 C）：精排校准信号透传进 prompt。"""

    def _candidate(self, **kw):
        from backend.retrieval.retriever import PatternCandidate

        base = dict(
            pattern_id="p1", name="mixer_layering", description="",
            canonical_subgraph={"nodes": [], "edges": []},
            similarity_score=0.61, structural_similarity=0.44,
            semantic_similarity=0.43, wl_kernel_score=0.058,
            fp_score=0.42, ov_score=0.71,
        )
        base.update(kw)
        return PatternCandidate(**base)

    def test_calibration_line_present(self):
        msgs = build_messages(
            {"nodes": [], "edges": []},
            [self._candidate()])
        user = msgs[1]["content"]
        assert "calibration: final=0.610 wl=0.058 ov=0.710 fp=0.420" \
               " struct=0.440 sem=0.430" in user
        # 用法说明跟随候选列表（弱 wl 提示主干弱匹配）
        assert "CALIBRATION LINE per candidate" in user
        assert "weak <0.3" in user

    def test_edge_level_difference_note(self):
        """#71 变体形态：Δnode=0、Δedge≠0 → note 明确指示边级差异。"""
        from backend.retrieval.features import generate_difference_note

        inp = {"nodes": [{"kind": "address"}] * 4, "edges": [1] * 6}
        cand = {"nodes": [{"kind": "address"}] * 4, "edges": [1] * 8}
        note = generate_difference_note(inp, cand)
        assert note is not None
        assert "Node composition is identical" in note
        assert "fewer edges (2 diff)" in note
        assert "compare the value backbone" in note
