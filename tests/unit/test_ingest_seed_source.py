"""数据源切换单测 — cluster_k7 真实子图布局 + Lazarus 标签 CSV（ingest-spec §2/§4）。

全部使用临时 fixture（pyarrow 现场生成 / 临时 CSV），不依赖外部数据与 DB。
"""
import pyarrow as pa
import pytest

from ingest.common import IngestError
from ingest.load_labels import parse_lazarus_csv
from ingest.load_lazarus_subgraphs import (
    build_patterns,
    resolve_cluster_dir,
    slice_from_clusters,
)


def _write_parquet(path, rows, schema):
    import pyarrow.parquet as pq

    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


def _node_schema():
    # cluster_k7 真实布局：无 direct_related_to_lazarus 列（由标签表回填）
    return pa.schema([
        ("address", pa.string()),
        ("first_layer", pa.int64()),
        ("total_received_btc", pa.float64()),
        ("total_sent_btc", pa.float64()),
        ("utxo_count", pa.int64()),
    ])


def _edge_schema():
    return pa.schema([
        ("src_address", pa.string()),
        ("dst_address", pa.string()),
        ("txid", pa.string()),
        ("tx_layer", pa.string()),
        ("is_remixer", pa.bool_()),
    ])


def _nodes(seed, n_extra, first_layer_seed=0):
    rows = [{"address": seed, "first_layer": first_layer_seed,
             "total_received_btc": 1.0, "total_sent_btc": 0.9,
             "utxo_count": 1}]
    rows += [{"address": f"{seed}_x{i}", "first_layer": 1,
              "total_received_btc": 0.1, "total_sent_btc": 0.0,
              "utxo_count": 1} for i in range(n_extra)]
    return rows


def _edges(seed, n_extra, *, remixer=False):
    rows = [{"src_address": seed, "dst_address": f"{seed}_x{i}",
             "txid": f"t_{seed}_{i}", "tx_layer": "tx1",
             "is_remixer": remixer} for i in range(n_extra)]
    return rows


def _write_cluster(root, name, node_rows, edge_rows):
    d = root / name
    d.mkdir(parents=True)
    _write_parquet(d / "nodes.parquet", node_rows, _node_schema())
    _write_parquet(d / "edges.parquet", edge_rows, _edge_schema())


# ---------------------------------------------------------------------------
# slice_from_clusters · 多簇拼接 + IG-16 校验
# ---------------------------------------------------------------------------
class TestSliceFromClusters:
    def test_concatenates_all_cluster_dirs(self, tmp_path):
        _write_cluster(tmp_path, "C0", _nodes("S0", 1), _edges("S0", 1))
        _write_cluster(tmp_path, "C1", _nodes("S1", 1), _edges("S1", 1))
        node_rows, edge_rows = slice_from_clusters(tmp_path)
        assert [r["address"] for r in node_rows if r["first_layer"] == 0] == \
            ["S0", "S1"]  # sorted 目录序，拼接顺序确定
        assert len(edge_rows) == 2

    def test_missing_dir_raises(self, tmp_path):
        with pytest.raises(IngestError, match="不存在"):
            slice_from_clusters(tmp_path / "nope")

    def test_corrupt_cluster_rejects_all(self, tmp_path):
        _write_cluster(tmp_path, "C0", _nodes("S0", 1), _edges("S0", 1))
        bad = tmp_path / "C1"
        bad.mkdir()
        (bad / "nodes.parquet").write_bytes(b"garbage")
        with pytest.raises(IngestError, match="C1"):
            slice_from_clusters(tmp_path)

    def test_missing_required_column_reports_field(self, tmp_path):
        d = tmp_path / "C0"
        d.mkdir()
        schema = pa.schema([f for f in _node_schema() if f.name != "utxo_count"])
        _write_parquet(d / "nodes.parquet", _nodes("S0", 1), schema)
        with pytest.raises(IngestError, match="utxo_count"):
            slice_from_clusters(tmp_path)

    def test_resolve_cluster_dir_relative_to_repo_root(self):
        p = resolve_cluster_dir("ingest/seed/patterns/cluster_k7")
        assert p.is_absolute() and p.name == "cluster_k7"
        assert resolve_cluster_dir("/abs/dir") == __import__("pathlib").Path("/abs/dir")


# ---------------------------------------------------------------------------
# build_patterns · Lazarus 标签回填 direct_related_to_lazarus
# ---------------------------------------------------------------------------
class TestLazarusEnrichment:
    def _rows(self, seed):
        return _nodes(seed, 5), _edges(seed, 5, remixer=True)

    def test_lazarus_address_flagged_in_canonical(self):
        nodes, edges = self._rows("S")
        rows = build_patterns(nodes, edges, lazarus_addresses={"S"})
        assert len(rows) == 1
        by_id = {n["id"]: n for n in rows[0]["canonical_subgraph"]["nodes"]}
        assert by_id["addr:S"]["direct_related_to_lazarus"] is True
        assert by_id["addr:S_x0"]["direct_related_to_lazarus"] is False

    def test_no_lazarus_ctx_leaves_false(self):
        nodes, edges = self._rows("S")
        rows = build_patterns(nodes, edges)
        assert all(n.get("direct_related_to_lazarus") in (False, None)
                   for n in rows[0]["canonical_subgraph"]["nodes"])

    def test_flag_change_alters_content_hash(self):
        nodes, edges = self._rows("S")
        h_plain = build_patterns(nodes, edges)[0]["content_hash"]
        h_flagged = build_patterns(nodes, edges, lazarus_addresses={"S"})[0]["content_hash"]
        assert h_plain != h_flagged


# ---------------------------------------------------------------------------
# parse_lazarus_csv · 标签 CSV 解析
# ---------------------------------------------------------------------------
class TestParseLazarusCsv:
    def test_missing_file_returns_empty(self, tmp_path):
        assert parse_lazarus_csv(tmp_path / "nope.csv") == []

    def test_parses_rows_and_skips_blank_addresses(self, tmp_path):
        p = tmp_path / "lazarus.csv"
        p.write_text(
            "address,name,category,attribution_date\n"
            " bc1qabc ,Exploit A,STOLEN FUNDS,2025-02-22\n"
            ",,,\n"
            "bc1qdef,Exploit B,STOLEN FUNDS,2025-02-22\n",
            encoding="utf-8")
        rows = parse_lazarus_csv(p)
        assert [r["address"] for r in rows] == ["bc1qabc", "bc1qdef"]
        assert all(r["labels"] == ["lazarus"] for r in rows)
        assert all(r["source"] == "lazarus_stolen_csv" for r in rows)
