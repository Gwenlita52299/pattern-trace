"""issue #26：LLM matched_pattern 只能引用本次检索候选。

- 未知模式名 → JudgmentValidationError（invalid_ids 供重试提示）
- 候选为空时任何非 null 引用都拒绝
- 合法候选名通过；candidate_names=None 直调兼容（跳过检查）
- judge.judge 集成：未知模式重试后引用合法候选名可成功
"""
from __future__ import annotations

import json

import pytest

from backend.llm_judge.judge import (
    JudgmentResult,
    JudgmentValidationError,
    LLMJudge,
    parse_and_validate,
)

from tests.unit.test_llm_judge import VALID_IDS, _verdict_json

CANDIDATE_NAMES = {"mixer_layering", "peel_chain"}


class TestParseAndValidate:
    def test_unknown_pattern_rejected(self):
        with pytest.raises(JudgmentValidationError, match="not-a-known-pattern"):
            parse_and_validate(
                _verdict_json(matched="not-a-known-pattern"), VALID_IDS,
                candidate_names=CANDIDATE_NAMES)

    def test_unknown_pattern_lists_invalid_id_for_retry(self):
        with pytest.raises(JudgmentValidationError) as exc_info:
            parse_and_validate(
                _verdict_json(matched="phantom_pattern"), VALID_IDS,
                candidate_names=CANDIDATE_NAMES)
        assert exc_info.value.invalid_ids == ["phantom_pattern"]

    def test_empty_candidates_reject_any_reference(self):
        with pytest.raises(JudgmentValidationError, match="candidate"):
            parse_and_validate(
                _verdict_json(matched="mixer_layering"), VALID_IDS,
                candidate_names=set())

    def test_valid_candidate_accepted(self):
        result = parse_and_validate(
            _verdict_json(matched="mixer_layering"), VALID_IDS,
            candidate_names=CANDIDATE_NAMES)
        assert isinstance(result, JudgmentResult)
        assert result.matched_pattern == "mixer_layering"

    def test_null_pattern_accepted_with_candidates(self):
        result = parse_and_validate(
            _verdict_json(risk="medium", action="review", matched=None),
            VALID_IDS, candidate_names=CANDIDATE_NAMES)
        assert result.matched_pattern is None

    def test_none_candidate_names_skips_check(self):
        """candidate_names=None（直调/旧调用方）保持旧行为不收口。"""
        result = parse_and_validate(
            _verdict_json(matched="arbitrary-string"), VALID_IDS,
            candidate_names=None)
        assert result.matched_pattern == "arbitrary-string"


class TestJudgeIntegration:
    def test_unknown_pattern_retried_then_valid_candidate(self):
        """首轮回显未知模式 → 重试提示携带该名称 → 第二轮引用合法候选。"""
        responses = [
            _verdict_json(matched="hallucinated_pattern"),
            _verdict_json(matched="mixer_layering"),
        ]

        class ScriptedClient:
            model_name = "mock-model"

            def __init__(self, responses):
                self._responses = list(responses)
                self.calls: list[list[dict]] = []

            async def complete(self, messages, **kwargs):
                self.calls.append(messages)
                return self._responses.pop(0)

        client = ScriptedClient(responses)
        judge = LLMJudge(client=client)
        # 子图 ID 空间与 _verdict_json 的 evidence 引用对齐
        subgraph = {"nodes": [{"id": i} for i in sorted(VALID_IDS)],
                    "edges": []}

        import asyncio

        result = asyncio.run(judge.judge(
            address="bc1qtest", subgraph=subgraph,
            candidates=[type("C", (), {"name": "mixer_layering"})()],
            candidate_names=CANDIDATE_NAMES))
        assert result.matched_pattern == "mixer_layering"
        # 重试提示包含未知模式名（invalid_ids 注入）
        retry_content = client.calls[1][-1]["content"]
        assert "hallucinated_pattern" in retry_content

    def test_unknown_pattern_exhausts_retries(self):
        class AlwaysBadClient:
            model_name = "mock-model"

            async def complete(self, messages, **kwargs):
                return _verdict_json(matched="hallucinated_pattern")

        import asyncio

        judge = LLMJudge(client=AlwaysBadClient(), max_retries=2)
        subgraph = {"nodes": [{"id": i} for i in sorted(VALID_IDS)],
                    "edges": []}
        with pytest.raises(JudgmentValidationError, match="hallucinated_pattern"):
            asyncio.run(judge.judge(
                address="bc1qtest", subgraph=subgraph,
                candidate_names=CANDIDATE_NAMES))

    def test_null_pattern_with_medium_is_valid_flagged_no_pattern(self):
        """issue #72：matched=null + medium = flagged_no_pattern（合法落点）。"""
        result = parse_and_validate(
            _verdict_json(risk="medium", action="review", matched=None),
            VALID_IDS, candidate_names=CANDIDATE_NAMES)
        assert result.risk_level == "medium"
        assert result.matched_pattern is None


def test_schema_and_prompt_carry_candidate_rule():
    from backend.llm_judge.judge import SCHEMA, SYSTEM_PROMPT

    assert "CANDIDATE PATTERNS" in SYSTEM_PROMPT
    assert "matched_pattern" in json.dumps(SCHEMA)
