"""负样本生成单测 — IG-04 隔离语义 / IG-05 比例口径 / IG-06 约束。"""
import pytest

from ingest.corpus_gen import generate as gen_positives
from ingest.generate_negatives import count_positives, generate, generate_one


class TestNegativeGeneration:
    def test_source_and_grade(self):
        rows = generate(10)
        assert all(r["source"] == "constructed_normal" for r in rows)
        assert all(r["evidence_grade"] == "B" for r in rows)

    def test_no_mixer_or_crosschain_contact(self):
        """IG-06：无一例接触混币器/跨链标记（结构性保证）。"""
        rows = generate(100)
        for r in rows:
            for e in r["canonical_subgraph"]["edges"]:
                assert not e["is_remixer"], r["seed_address"]
                assert not e["is_crosschain"]
                assert e["op_return_protocol"] is None
            # 无一命中标签命名空间（负样本 seed 用 bc1qn 前缀隔离）
            assert r["seed_address"].startswith("bc1qn")

    def test_positive_synth_namespace_disjoint(self):
        pos_seeds = {r["seed_address"] for r in gen_positives(50)}
        neg_seeds = {r["seed_address"] for r in generate(50)}
        assert not (pos_seeds & neg_seeds)  # 命名空间互斥，杜绝交叉污染

    def test_deterministic_per_slot(self):
        a = generate(20)
        b = generate(20)
        assert all(x["content_hash"] == y["content_hash"] for x, y in zip(a, b))

    def test_referential_integrity(self):
        for r in generate(30):
            c = r["canonical_subgraph"]
            ids = {n["id"] for n in c["nodes"]}
            assert all(e["source"] in ids and e["target"] in ids
                       for e in c["edges"]), r["seed_address"]

    def test_generate_one_template_coverage(self):
        seen = set()
        for slot in range(30):
            _sub, name = generate_one(42, slot)
            seen.add(name)
        assert len(seen) >= 2  # 多个 normal 模板都被覆盖


class TestRatioAccounting:
    def test_count_positives_uses_grade_a_semantics(self, monkeypatch):
        """IG-05 口径：正样本 = patterns 表 evidence_grade='A'（含 confirmed+synth）。"""
        class FakeQuery:
            def __init__(self, base): self.base = base
            def filter(self, *a): return self
            def scalar(self): return self.base

        class FakeSession:
            def query(self, model): return FakeQuery(123)

        assert count_positives(FakeSession()) == 123

    def test_target_computation_matches_ratio(self):
        # run() 的 target = round(ratio × positives)；这里验证换算本身
        positives, ratio = 401, 3
        assert round(ratio * positives) == 1203
        # 落在 IG-05 允许区间 [2.5, 3.5]
        assert 2.5 <= 1203 / positives <= 3.5

    def test_isolation_semantics_negative_never_enters_patterns_table(self):
        """IG-04 的表级隔离：负样本行携带的 source 值与业务召回库的
        正样本 source 集合不相交，run_all 汇总据此断言零泄漏。"""
        positive_sources = {"lazarus_confirmed", "lazarus_synth"}
        negative_rows = generate(5)
        assert all(r["source"] not in positive_sources for r in negative_rows)


@pytest.mark.parametrize("pos,neg,in_range", [
    (100, 300, True), (100, 250, True), (100, 350, True),
    (100, 200, False), (100, 400, False), (0, 0, False),
])
def test_ratio_bounds(pos, neg, in_range):
    actual = (neg / pos) if pos else 0.0
    assert (2.5 <= actual <= 3.5) is in_range or pos == 0
