"""playbook 语料生成器单测 — 确定性、规模下限、IG-03 一致性、同名碰撞。"""
import collections

from ingest.common import MIN_PATTERN_NODES
from ingest.corpus_gen import CROSSCHAIN_PROTOCOLS, generate


class TestDeterminism:
    def test_same_seed_reproduces_identical_content(self):
        """幂等前提（IG-10/17）：内容只依赖 (seed_key, slot)。"""
        a = generate(30, seed_key=42)
        b = generate(30, seed_key=42)
        assert all(x["content_hash"] == y["content_hash"]
                   for x, y in zip(a, b))
        assert all(x["seed_address"] == y["seed_address"]
                   for x, y in zip(a, b))

    def test_different_seed_key_changes_everything(self):
        a = generate(10, seed_key=1)
        b = generate(10, seed_key=2)
        assert not ({x["seed_address"] for x in a}
                    & {y["seed_address"] for y in b})


class TestStructuralConstraints:
    def test_all_pass_positive_filter_line(self):
        rows = generate(100)
        assert all(r["canonical_subgraph"]["stats"]["node_count"]
                   >= MIN_PATTERN_NODES for r in rows)

    def test_mixer_entry_on_every_pattern(self):
        """IG-03 口径一致：synth 正样本必须全部有混币器接触。"""
        rows = generate(200)
        missing = [r for r in rows
                   if not any(e["is_remixer"] or e["is_crosschain"]
                              for e in r["canonical_subgraph"]["edges"])]
        assert not missing

    def test_referential_integrity(self):
        for r in generate(50):
            c = r["canonical_subgraph"]
            ids = {n["id"] for n in c["nodes"]}
            assert all(e["source"] in ids and e["target"] in ids
                       for e in c["edges"]), r["seed_address"]

    def test_topology_variety(self):
        rows = generate(120)
        families = collections.Counter(
            r["name"].rsplit("_f", 1)[0] for r in rows)
        assert len(families) >= 5          # 多模板 × 多层数组合
        assert min(families.values()) > 0

    def test_crosschain_edges_use_real_protocol_vocabulary(self):
        rows = generate(60)
        protos = {e["op_return_protocol"] for r in rows
                  for e in r["canonical_subgraph"]["edges"]
                  if e["op_return_protocol"]}
        assert protos <= set(CROSSCHAIN_PROTOCOLS)
        assert protos  # 有实际跨链边存在


class TestNameCollisions:
    def test_same_name_across_distinct_seeds(self):
        """IG-11 场景来源：形态学命名天然跨 seed 同名，
        upsert key (seed_address, content_hash) 下二者共存。"""
        rows = generate(80)
        by_name = collections.defaultdict(set)
        for r in rows:
            by_name[r["name"]].add(r["seed_address"])
        multi = [n for n, seeds in by_name.items() if len(seeds) >= 2]
        assert multi  # 至少存在一组同名不同 seed
