"""Lazarus 子图切图入库 — ingest-spec §2 / IG-01~03。

流程：Parquet 校验 → 按 seed_address 切图 → 过滤（≥5 节点且混币器接触）
→ canonical JSON + content_hash + WL 指纹 → upsert patterns。
source=lazarus_confirmed, evidence_grade=A。

校验先于一切 DB 写入——损坏输入零写入（IG-16）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:  # 包内运行（python -m ingest.xxx）
    from .common import (
        EDGE_COLS,
        NODE_COLS,
        _int,
        new_pattern_row,
        passes_filter,
        read_validated_parquet,
        slice_by_seed,
        upsert_rows,
    )
except ImportError:  # 直接运行（python ingest/load_lazarus_subgraphs.py）
    from common import (
        EDGE_COLS,
        NODE_COLS,
        _int,
        new_pattern_row,
        passes_filter,
        read_validated_parquet,
        slice_by_seed,
        upsert_rows,
    )

SUBGRAPH_DIR = "results/step3_subgraph"


def _rows(table, cols: list[str]) -> list[dict]:
    return table.select(cols).to_pylist()


def slice_from_dir(base_dir: Path) -> tuple[list[dict], list[dict]]:
    """读取并校验 nodes/edges Parquet，返回行列表（纯数据，无 DB 副作用）。"""
    d = base_dir / SUBGRAPH_DIR
    nodes_tbl = read_validated_parquet(str(d / "subgraph_nodes.parquet"), NODE_COLS)
    edges_tbl = read_validated_parquet(str(d / "subgraph_edges.parquet"), EDGE_COLS)
    node_rows = _rows(nodes_tbl, list(NODE_COLS))
    edge_rows = _rows(edges_tbl, [
        c for c in [
            "src_address", "dst_address", "txid", "tx_layer",
            "total_num_inputs", "total_num_outputs", "dst_value_btc",
            "tx_total_input_btc", "tx_total_output_btc", "value_ratio",
            "src_value_btc", "is_stopped_expansion", "is_remixer",
            "is_crosschain", "op_return_protocol",
        ] if c in edges_tbl.column_names
    ])
    return node_rows, edge_rows


def build_patterns(node_rows: list[dict], edge_rows: list[dict],
                   labeled_addresses: set[str] | None = None,
                   coinjoin_txids: set[str] | None = None) -> list[dict]:
    """切图 + 过滤 + 组装 pattern 行（不含向量列）。"""
    subs = slice_by_seed(node_rows, edge_rows)
    kept = [s for s in subs if passes_filter(
        s, labeled_addresses=labeled_addresses, coinjoin_txids=coinjoin_txids)]

    rows = []
    for sub in kept:
        rows.append(new_pattern_row(
            name=_pattern_name(sub),
            source="lazarus_confirmed",
            grade="A",
            provenance="confirmed",
            sub=sub,
        ))
    return rows


def _pattern_name(sub) -> str:
    max_layer = max((n["first_layer"] for n in sub.nodes), default=0)
    return f"mixer_layering_{max_layer}hop_{len(sub.nodes)}nodes"


def upsert(session, rows: list[dict]) -> int:
    """(seed_address, content_hash) 冲突时覆盖内容、保留 id（IG-10/11）。"""
    from backend.models.knowledge import Pattern

    return upsert_rows(session, Pattern, rows, ["seed_address", "content_hash"])


def run(session, base_dir: str | None = None, verbose: bool = True) -> dict:
    from backend.core.config import get_settings
    from backend.models.knowledge import AddressMeta, CoinjoinTxid

    base = Path(base_dir or get_settings().lazarus_data_dir)

    # 标签上下文：run_all 保证 load_labels 先行；独立运行时空库则退化为仅按边属性判定
    labeled = {r.address for r in session.query(AddressMeta)}
    cj_txids = {r.txid for r in session.query(CoinjoinTxid)}

    node_rows, edge_rows = slice_from_dir(base)
    total_seeds = sum(1 for r in node_rows if _int(r.get("first_layer")) == 0)
    rows = build_patterns(node_rows, edge_rows, labeled, cj_txids)
    inserted = upsert(session, rows)

    if verbose:
        print(f"lazarus subgraphs: seeds={total_seeds} passed_filter={len(rows)} "
              f"upserted={inserted} "
              f"(label ctx: {len(labeled)} addrs, {len(cj_txids)} cj txids)")
    return {"seeds": total_seeds, "kept": len(rows), "upserted": inserted}


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.environ.setdefault("JWT_SECRET", "migration-placeholder")

    from common import get_engine
    from sqlalchemy.orm import Session

    from backend.models.knowledge import AddressMeta, CoinjoinTxid  # noqa: F401

    engine = get_engine()
    with Session(engine) as s:
        run(s)
