"""ingest 切图与 canonical 层单测 — IG-02/03/11/12/16。

全部使用临时 Parquet fixture（pyarrow 现场生成），不依赖外部数据与 DB。
"""
import math

import pyarrow as pa
import pytest

from ingest.common import (
    IngestError,
    Subgraph,
    content_hash,
    describe_subgraph,
    has_mixer_contact,
    new_pattern_row,
    passes_filter,
    read_validated_parquet,
    slice_by_seed,
    structural_features,
)

NODE_COLS = {
    "address": "string", "first_layer": "number",
    "total_received_btc": "number", "total_sent_btc": "number",
    "utxo_count": "number",
}
EDGE_COLS = {
    "src_address": "string", "dst_address": "string",
    "txid": "string", "tx_layer": "string",
}


def _write_parquet(path, rows: list[dict], schema) -> str:
    table = pa.Table.from_pylist(rows, schema=schema)
    import pyarrow.parquet as pq

    pq.write_table(table, path)
    return str(path)


def _node_schema():
    return pa.schema([
        ("address", pa.string()),
        ("first_layer", pa.int64()),
        ("total_received_btc", pa.float64()),
        ("total_sent_btc", pa.float64()),
        ("utxo_count", pa.int64()),
        ("direct_related_to_lazarus", pa.int64()),
    ])


def _edge_schema():
    return pa.schema([
        ("src_address", pa.string()),
        ("dst_address", pa.string()),
        ("txid", pa.string()),
        ("tx_layer", pa.string()),
        ("is_stopped_expansion", pa.bool_()),
        ("is_remixer", pa.bool_()),
        ("is_crosschain", pa.bool_()),
        ("op_return_protocol", pa.string()),
    ])


def _seed_node(addr):
    return {"address": addr, "first_layer": 0, "total_received_btc": 1.0,
            "total_sent_btc": 0.9, "utxo_count": 1,
            "direct_related_to_lazarus": 0}


def _extra_node(addr, layer=1):
    return {"address": addr, "first_layer": layer,
            "total_received_btc": 0.1, "total_sent_btc": 0.0,
            "utxo_count": 1, "direct_related_to_lazarus": 0}


def _edge(src, dst, txid, **kw):
    base = {"src_address": src, "dst_address": dst, "txid": txid,
            "tx_layer": "tx2", "is_stopped_expansion": dst is None,
            "is_remixer": False, "is_crosschain": False,
            "op_return_protocol": None}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# IG-16 · Parquet 损坏容错：清晰报错（含文件/字段定位）
# ---------------------------------------------------------------------------
class TestParquetValidation:
    def test_missing_column_reports_file_and_field(self, tmp_path):
        p = tmp_path / "nodes.parquet"
        # schema 本身缺列（而非仅行内缺值）才会触发 missing 校验
        schema = pa.schema([f for f in _node_schema() if f.name != "utxo_count"])
        _write_parquet(p, [_seed_node("a")], schema)
        with pytest.raises(IngestError, match="utxo_count"):
            read_validated_parquet(str(p), NODE_COLS)

    def test_type_drift_reports_expected_and_actual(self, tmp_path):
        p = tmp_path / "edges.parquet"
        schema = pa.schema([
            ("src_address", pa.string()), ("dst_address", pa.string()),
            ("txid", pa.int64()),  # 期望 string 实际 int64
            ("tx_layer", pa.string()),
        ])
        _write_parquet(p, [_edge("a", None, 123)], schema)
        with pytest.raises(IngestError, match="txid.*string"):
            read_validated_parquet(str(p), EDGE_COLS)

    def test_empty_file_rejected(self, tmp_path):
        p = tmp_path / "empty.parquet"
        _write_parquet(p, [], _node_schema())
        with pytest.raises(IngestError, match="0 行"):
            read_validated_parquet(str(p), NODE_COLS)

    def test_non_parquet_file_rejected(self, tmp_path):
        p = tmp_path / "garbage.parquet"
        p.write_bytes(b"not a parquet file")
        with pytest.raises(IngestError, match="无法解析"):
            read_validated_parquet(str(p), NODE_COLS)


# ---------------------------------------------------------------------------
# IG-02 · 按 seed_address 分组切图
# ---------------------------------------------------------------------------
class TestSliceBySeed:
    def test_each_seed_gets_isolated_subgraph(self):
        nodes = [
            _seed_node("SEED_A"), _seed_node("SEED_B"), _extra_node("mid1"),
            _extra_node("leafA", layer=2),
        ]
        edges = [
            _edge("SEED_A", "mid1", "tA1"),
            _edge("mid1", "leafA", "tA2"),
            _edge("SEED_B", "mid1", "tB1"),   # 共享 mid1（真实拓扑允许）
        ]
        subs = {s.seed_address: s for s in slice_by_seed(nodes, edges)}
        assert set(subs) == {"SEED_A", "SEED_B"}
        assert {e["txid"] for e in subs["SEED_A"].edges} == {"tA1", "tA2"}
        # B 可达共享节点 mid1 → 其全部下游同属 B 的真实链上拓扑
        assert {e["txid"] for e in subs["SEED_B"].edges} == {"tB1", "tA2"}

    def test_baseline_edge_expands_to_d3_pair(self):
        nodes = [_seed_node("S"), _extra_node("D")]
        edges = [_edge("S", "D", "T1")]
        sub = slice_by_seed(nodes, edges)[0]
        # 基线一条 addr→addr 边 → D3 的 addr→tx 与 tx→addr 两条
        assert len(sub.edges) == 2
        assert sub.edges[0]["target"] == "tx:T1"
        assert sub.edges[1]["source"] == "tx:T1"

    def test_stopped_edge_keeps_only_addr_to_tx(self):
        nodes = [_seed_node("S")]
        edges = [_edge("S", None, "T9", is_crosschain=True,
                       op_return_protocol="runes")]
        sub = slice_by_seed(nodes, edges)[0]
        assert len(sub.edges) == 1
        assert sub.edges[0]["is_stopped_expansion"] is True


# ---------------------------------------------------------------------------
# IG-03 · 过滤：≥5 节点且混币器接触
# ---------------------------------------------------------------------------
class TestFilter:
    def _sub_with(self, n_addr_nodes: int, *, remixer=False, cj_txid=None):
        nodes = [{"id": f"addr:a{i}", "kind": "address", "label": f"a{i}",
                  "first_layer": 0} for i in range(n_addr_nodes)]
        edges = []
        if remixer:
            edges.append({"id": "edge:x", "source": "addr:a0",
                          "target": "tx:mix", "txid": "mix_tx",
                          "is_remixer": True})
        if cj_txid:
            edges.append({"id": "edge:y", "source": "addr:a0",
                          "target": "tx:cj", "txid": cj_txid,
                          "is_remixer": False})
        s = Subgraph(seed_address="a0")
        s.nodes = nodes if nodes else [{"id": "addr:a0", "kind": "address",
                                        "label": "a0", "first_layer": 0}]
        s.edges = edges
        return s

    def test_small_subgraph_rejected(self):
        assert not passes_filter(self._sub_with(2),
                                 labeled_addresses=set(), coinjoin_txids=set())

    def test_no_mixer_contact_rejected_even_if_large(self):
        assert not passes_filter(self._sub_with(10),
                                 labeled_addresses=set(), coinjoin_txids=set())

    def test_remixer_edge_qualifies(self):
        sub = self._sub_with(6, remixer=True)
        assert has_mixer_contact(sub)
        assert passes_filter(sub)

    def test_coinjoin_txid_qualifies(self):
        sub = self._sub_with(6, cj_txid="known_cj")
        assert passes_filter(sub, coinjoin_txids={"known_cj"})

    def test_labeled_address_qualifies(self):
        sub = self._sub_with(6)
        assert passes_filter(sub, labeled_addresses={"a3"})


# ---------------------------------------------------------------------------
# IG-11/12 + canonical 一致性
# ---------------------------------------------------------------------------
class TestCanonicalAndHash:
    def _row_for(self, seed, txids=("t1", "t2")):
        nodes = [_seed_node(seed)]
        edges = [_edge(seed, None, t, is_remixer=(i == 0))
                 for i, t in enumerate(txids)]
        for i in range(5):  # 凑过过滤线的规模
            nodes.append(_extra_node(f"x{i}"))
            edges.append(_edge(f"x{i}", None, f"tx{i}"))
        sub = slice_by_seed(nodes, edges)[0]
        return new_pattern_row(name="same_name", source="lazarus_confirmed",
                               grade="A", sub=sub)

    def test_same_name_different_seeds_both_kept(self):
        """IG-11：同名 pattern 因 seed/hash 不同而共存（弃用 name+source key）。"""
        r1 = self._row_for("SEED_1")
        r2 = self._row_for("SEED_2")
        assert r1["name"] == r2["name"] == "same_name"
        assert r1["content_hash"] != r2["content_hash"]
        assert r1["seed_address"] != r2["seed_address"]
        assert len(r1["content_hash"]) == 64

    def test_hash_deterministic_and_content_sensitive(self):
        r = self._row_for("SEED_1")
        h1 = content_hash(r["canonical_subgraph"])
        h2 = content_hash(r["canonical_subgraph"])
        assert h1 == h2 == r["content_hash"]
        mutated = dict(r["canonical_subgraph"])
        mutated["stats"] = dict(mutated["stats"], node_count=99)
        assert content_hash(mutated) != h1

    def test_canonical_referential_integrity_and_kinds(self):
        c = self._row_for("SEED_1")["canonical_subgraph"]
        kinds = {n["id"].split(":", 1)[0] for n in c["nodes"]}
        assert kinds == {"addr", "tx"}
        ids = {n["id"] for n in c["nodes"]}
        assert all(e["source"] in ids and e["target"] in ids for e in c["edges"])
        ids_sorted = [n["id"] for n in c["nodes"]]
        assert ids_sorted == sorted(ids_sorted)  # 序列化与构建顺序无关

    def test_wl_fingerprint_structure(self):
        """IG-12：非空、多重集 hash 列表结构、同图稳定。"""
        fp = self._row_for("SEED_1")["wl_fingerprint"]
        rounds = fp["rounds"]
        assert len(rounds) >= 2
        assert all(isinstance(h, str) and len(h) == 40 for rd in rounds for h in rd)
        fp2 = self._row_for("SEED_1")["wl_fingerprint"]
        assert fp == fp2

    def test_description_mentions_seed(self):
        c = self._row_for("SEED_1")["canonical_subgraph"]
        desc = describe_subgraph(c)
        assert "SEED_1" in desc and "nodes" in desc

    def test_structural_features_dim_and_finiteness(self):
        feats = structural_features(self._row_for("SEED_1")["canonical_subgraph"])
        assert len(feats) == 20
        assert all(math.isfinite(v) for v in feats)
