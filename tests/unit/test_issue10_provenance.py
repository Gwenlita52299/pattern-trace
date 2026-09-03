"""issue #10 — 合成模式来源性质（provenance）与真实确认证据分离的契约测试。

覆盖验收标准：
- inception：source→provenance 映射、new_pattern_row 携带 provenance；
- ingest：合成样本不是 Grade A、负样本 provenance=negative；
- retrieval：PatternCandidate 携带 source/provenance；
- LLM prompt：标记 synthetic provenance，并声明非真实链上证据；
- report：knowledge_base_provenance_metrics 拆分 confirmed/synthetic/negative。
"""
from __future__ import annotations

from ingest.common import Subgraph, new_pattern_row, provenance_of
from ingest.corpus_gen import generate as gen_synth
from ingest.generate_negatives import generate as gen_neg


def _sub(seed="bc1qseed"):
    """最小合法 Subgraph（to_canonical 不强制 MIN_PATTERN_NODES）。"""
    return Subgraph(
        seed_address=seed,
        nodes=[
            {"id": f"addr:{seed}", "kind": "address", "label": seed,
             "first_layer": 0, "total_received_btc": 1.0},
            {"id": "addr:bc1qa", "kind": "address", "label": "bc1qa",
             "first_layer": 1, "total_received_btc": 0.5},
            {"id": "tx:t1", "kind": "transaction", "first_layer": 0},
        ],
        edges=[
            {"id": "edge:addr:bc1qseed->tx:t1", "source": f"addr:{seed}",
             "target": "tx:t1", "txid": "t1", "is_remixer": True},
            {"id": "edge:tx:t1->addr:bc1qa", "source": "tx:t1",
             "target": "addr:bc1qa", "txid": "t1"},
        ],
    )


# -- source→provenance 映射 ------------------------------------------------
class TestProvenanceMapping:
    def test_source_to_provenance(self):
        assert provenance_of("lazarus_confirmed") == "confirmed"
        assert provenance_of("lazarus_synth") == "synthetic"
        assert provenance_of("constructed_normal") == "negative"

    def test_unknown_source_falls_back_to_synthetic_not_confirmed(self):
        # 未知 source 保守回退 synthetic，绝不误标为真实确认证据
        assert provenance_of("something_new") == "synthetic"


# -- new_pattern_row 携带 provenance / 等级 ---------------------------------
class TestNewPatternRowProvenance:
    def test_confirmed_row(self):
        r = new_pattern_row(name="m", source="lazarus_confirmed", grade="A",
                            sub=_sub())
        assert r["provenance"] == "confirmed"
        assert r["evidence_grade"] == "A"

    def test_explicit_provenance_wins(self):
        r = new_pattern_row(name="m", source="foo", grade="S",
                            provenance="synthetic", sub=_sub())
        assert r["provenance"] == "synthetic"


# -- ingest 合成样本不伪装 Grade A ------------------------------------------
class TestIngestSyntheticProvenance:
    def test_synth_rows_are_synthetic_grade_s(self):
        rows = gen_synth(40)
        assert all(r["source"] == "lazarus_synth" for r in rows)
        assert all(r["provenance"] == "synthetic" for r in rows)
        assert all(r["evidence_grade"] == "S" for r in rows)

    def test_negative_rows_are_negative(self):
        rows = gen_neg(20)
        assert all(r["source"] == "constructed_normal" for r in rows)
        assert all(r["provenance"] == "negative" for r in rows)
        assert all(r["evidence_grade"] == "B" for r in rows)


# -- LLM prompt 标记 synthetic provenance ----------------------------------
class TestJudgePromptProvenance:
    def _subgraph(self):
        return {
            "seed_address": "bc1qseed",
            "nodes": [{"id": "addr:bc1qseed", "kind": "address"},
                      {"id": "tx:t1", "kind": "transaction"}],
            "edges": [{"id": "edge:addr:bc1qseed->tx:t1",
                       "source": "addr:bc1qseed", "target": "tx:t1",
                       "is_remixer": True}],
        }

    def test_synthetic_candidate_carries_provenance_and_note(self):
        from backend.llm_judge.judge import build_messages

        cands = [
            {"name": "mixer_layering_3hop",
             "description": "confirmed peel chain",
             "source": "lazarus_confirmed", "provenance": "confirmed"},
            {"name": "synth_peel_chain_l3",
             "description": "template",
             "source": "lazarus_synth", "provenance": "synthetic"},
        ]
        msgs = build_messages(self._subgraph(), cands)
        user = msgs[1]["content"]
        assert "provenance: synthetic" in user
        assert "evidence_status: not_real_on_chain_evidence" in user
        assert "provenance: confirmed" in user
        assert "SYNTHETIC structural templates" in user
        assert "Do not cite the candidate itself as blockchain evidence" in user

    def test_all_confirmed_has_no_disclaimer(self):
        from backend.llm_judge.judge import build_messages

        cands = [{"name": "m", "description": "", "source": "lazarus_confirmed",
                  "provenance": "confirmed"}]
        msgs = build_messages(self._subgraph(), cands)
        user = msgs[1]["content"]
        assert "provenance: confirmed" in user
        assert "SYNTHETIC structural templates" not in user


# -- retrieval 候选携带 provenance -----------------------------------------
class TestRetrievalCandidateProvenance:
    def test_candidate_carries_source_and_provenance(self):
        from backend.retrieval.retriever import PatternCandidate

        c = PatternCandidate(pattern_id="p1", name="synth_peel_chain_l3",
                             source="lazarus_synth", provenance="synthetic")
        assert c.provenance == "synthetic"
        assert c.source == "lazarus_synth"
        # 默认回落：未显式设置时不误当作 synthetic
        c2 = PatternCandidate(pattern_id="p2", name="m")
        assert c2.provenance == "confirmed"


# -- report 拆分 confirmed/synthetic/negative ------------------------------
class TestReportProvenanceMetrics:
    def test_metrics_split_by_provenance(self):
        from backend.services.report_service import knowledge_base_provenance_metrics

        class _Q:
            def __init__(self, values): self._values = values  # 共享同一可变列表

            def filter(self, *a):
                return self

            def scalar(self):
                return self._values.pop(0) if self._values else 0

        class _Session:
            def __init__(self, values): self._values = values

            def query(self, model):
                return _Q(self._values)

        m = knowledge_base_provenance_metrics(_Session([2, 3, 9]))
        assert m["confirmed"] == 2
        assert m["synthetic"] == 3
        assert m["negative"] == 9
        assert m["total_positives"] == 5
        assert m["synthetic_share"] == 0.6  # 3/5
        assert m["negative_to_positive_ratio"] == 1.8  # 9/5
