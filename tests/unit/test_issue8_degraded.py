"""issue #8 —— 保留部分子图 + degraded 数据质量（partial graph with degraded quality）。

覆盖验收标准：
  1. 部分 Esplora 分支失败时保留已获取子图；
  2. 部分失败标记 data_quality=degraded；
  3. 部分失败标记 requires_manual_review=true；
  4. 保存缺失分支数量与上游错误摘要；
  5. LLM Prompt 明确说明图不完整；
  6. 前端/报告可展示数据质量状态（payload+渲染）；
  7. 根节点完全不可用仍走 ESPLORA_UNAVAILABLE（builder 前置：degraded + 仅种子节点）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.graph_builder.builder import GraphBuilder
from backend.graph_builder.id_contract import node_id
from backend.llm_judge.judge import DEGRADED_NOTE, build_messages
from backend.retrieval.retriever import subgraphresult_to_canonical
from backend.services.report_service import _render_html

from test_builder import (
    SEED,
    MockTx,
    OutspendProvider,
    out,
    vin,
)


class PartialFailOutspend(OutspendProvider):
    """除 (tx_fund2,0) 外都成功；该分支 outspend 抛错 → 部分失败。"""

    def outspend(self, txid, vout):
        if (txid, vout) == ("tx_fund2", 0):
            raise ConnectionError("esplora unreachable")
        return super().outspend(txid, vout)


def _partial_fail_result():
    funding1 = MockTx(txid="tx_fund1", outputs=[out(SEED, 0.5)])
    funding2 = MockTx(txid="tx_fund2", outputs=[out(SEED, 0.5)])
    spender1 = MockTx(txid="sp1", inputs=[vin(SEED, 0.5, "tx_fund1", 0)],
                      outputs=[out("bc1qnew")], block_time=1700000000.0)
    provider = PartialFailOutspend(
        {SEED: [funding1, funding2]},
        {("tx_fund1", 0): "sp1"},
        {"sp1": spender1},
    )
    return GraphBuilder().build(SEED, provider, hops=1)


# ---------------------------------------------------------------------------
# 部分失败：保留子图 + degraded 元数据
# ---------------------------------------------------------------------------
class TestPartialFailureDegrades:
    def test_degraded_metadata_and_surviving_branch(self):
        result = _partial_fail_result()
        assert result.stats.degraded is True
        assert result.stats.data_quality == "degraded"
        assert result.stats.requires_manual_review is True
        assert result.stats.missing_branches >= 1
        assert result.stats.source_errors, "应记录上游错误摘要"
        err = result.stats.source_errors[0]
        assert err["stage"] == "esplora"
        assert err["address"] == SEED
        assert err["error_code"] == "UPSTREAM_ERROR"
        # 存活分支仍保留（子图不整体失败）
        assert node_id("transaction", "sp1") in result.node_ids()
        assert node_id("address", "bc1qnew") in result.node_ids()

    def test_canonical_subgraph_carries_metadata(self):
        canon = subgraphresult_to_canonical(_partial_fail_result())
        stats = canon["stats"]
        assert stats["data_quality"] == "degraded"
        assert stats["requires_manual_review"] is True
        assert stats["missing_branches"] >= 1
        assert stats["source_errors"]
        assert stats["source_errors"][0]["stage"] == "esplora"


class TestCompleteFlowUnchanged:
    def test_complete_data_quality(self):
        funding = MockTx(txid="tx_fund", outputs=[out(SEED, 0.5)])
        provider = OutspendProvider({SEED: [funding]}, {}, {"tx_fund": funding})
        result = GraphBuilder().build(SEED, provider, hops=1)
        assert result.stats.degraded is False
        assert result.stats.data_quality == "complete"
        assert result.stats.requires_manual_review is False
        assert result.stats.missing_branches == 0
        assert result.stats.source_errors == []
        canon = subgraphresult_to_canonical(result)
        assert canon["stats"]["data_quality"] == "complete"

    def test_build_messages_omits_degraded_note_when_complete(self):
        subgraph = {"nodes": [{"id": "addr:seed", "kind": "address"}], "edges": []}
        content = build_messages(subgraph, [])[-1]["content"]
        assert "INCOMPLETE" not in content
        assert "DATA QUALITY: degraded" not in content


# ---------------------------------------------------------------------------
# 全部失败（根不可用）：degraded + 仅种子节点（orchestration 据此外抛 ESPLORA_UNAVAILABLE）
# ---------------------------------------------------------------------------
class TestRootUnavailable:
    def test_root_only_seed_node_and_degraded(self):
        def dead_provider(addr):
            raise ConnectionError("esplora unreachable")

        result = GraphBuilder().build(SEED, dead_provider, hops=1)
        assert result.stats.degraded is True
        assert result.stats.data_quality == "degraded"
        assert len(result.nodes) == 1  # 仅种子节点 → orchestration 判定 ESPLORA_UNAVAILABLE


# ---------------------------------------------------------------------------
# LLM Prompt：degraded 说明
# ---------------------------------------------------------------------------
class TestDegradedPrompt:
    def test_degraded_note_injects_incomplete_warning(self):
        subgraph = {"nodes": [{"id": "addr:seed", "kind": "address"}], "edges": []}
        content = build_messages(subgraph, [], degraded=True,
                                 missing_branches=3)[-1]["content"]
        assert "DATA QUALITY: degraded (missing_branches=3)" in content
        assert "INCOMPLETE" in content
        assert DEGRADED_NOTE in content
        assert "cautious" in content.lower()

    def test_notes_stack_with_retry(self):
        subgraph = {"nodes": [{"id": "addr:seed", "kind": "address"}], "edges": []}
        msgs = build_messages(subgraph, [], retry_note="retry-me",
                              degraded=True, missing_branches=1)
        content = msgs[-1]["content"]
        assert "retry-me" in content
        assert "DATA QUALITY: degraded" in content


# ---------------------------------------------------------------------------
# API payload + 报告渲染：暴露/展示数据质量
# ---------------------------------------------------------------------------
class TestPayloadAndRender:
    def _completed_judgment(self, quality="degraded"):
        return SimpleNamespace(
            id="j-8", address="addr:x", hops=2, time_window_days=90,
            status="completed", created_at=None, concluded_at=None,
            data_as_of=None, data_quality=quality,
            requires_manual_review=(quality == "degraded"),
            subgraph_snapshot={
                "nodes": [], "edges": [],
                "stats": {"missing_branches": 2,
                          "source_errors": [{"stage": "esplora", "error_code": "TIMEOUT"}]},
            },
            subgraph_hash="h" * 64,
            risk_level="medium", matched_pattern_id=None,
            matched_pattern_name=None, confidence=0.6,
            evidence=["addr:x"], reasoning="r", recommended_action="review",
            model="m", prompt_version="v1", builder_version="v1",
            latency_ms=50, error_code=None, error_message=None,
            retry_count=0, failed_at=None,
        )

    def test_judgment_payload_exposes_quality(self):
        from backend.api.app import _judgment_payload

        payload = _judgment_payload(self._completed_judgment())
        assert payload["data_quality"] == "degraded"
        assert payload["requires_manual_review"] is True
        assert payload["missing_branches"] == 2
        assert payload["source_errors"][0]["error_code"] == "TIMEOUT"

    def test_report_html_includes_quality(self):
        case = SimpleNamespace(title="T", status="open")
        j = self._completed_judgment()
        html = _render_html(case, "c", [{"address": "addr:x", "judgment": j}])
        assert "data_quality:degraded" in html
        assert "复核:是" in html  # requires_manual_review=True → 需人工复核

    def test_complete_payload_quality_flag(self):
        from backend.api.app import _judgment_payload

        payload = _judgment_payload(self._completed_judgment(quality="complete"))
        assert payload["data_quality"] == "complete"
        assert payload["requires_manual_review"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
