"""graphormer_v2 候选闭包入库 — 业务级 unseen-similarity 知识库数据源。

数据源（settings.graphormer_data_dir，默认 ingest/seed/graphormer_v2/）：
- derived/confirmed_candidates.parquet：9,346 个含 confirmed_downstream
  节点的候选闭包（seed_address / members / confirmed_members）
- derived/graphormer_pooled.npz：每个候选的 784 维 Graphormer pooled 向量
- subgraph_nodes.parquet / subgraph_edges.parquet：全局节点/边表

闭包由 members 列表驱动（与基准 closure_edges 的 MAX_DEPTH=4 / NODE_CAP=200
语义一致，无需重跑 BFS）：nodes 取成员地址的节点表行，edges 取 src ∈ 成员
的全部出边（stopped/悬挂边由 _edge_dicts 语义自然保留）。

入库口径（2026-09 与 benchmark 对齐决策）：
- 仅 self-confirmed 闭包（业务真值池），source=lazarus_confirmed、grade=A
- 不过滤 <5 节点最小闭包：KB 索引必须与基准 confirmed 池一致
  （unseen_sim_report.md §5 的过滤建议适用于候选库构建，不适用于本迁移）
- canonical 直写 graphormer_embedding（784 维），semantic_embedding 留空——
  召回走 Graphormer cosine，文本描述 embedding 不参与（P2 评估中）
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

try:  # 包内运行（python -m ingest.xxx）
    from .common import (
        Subgraph,
        _edge_dicts,
        _node_dict,
        new_pattern_row,
        to_canonical,
        upsert_rows,
    )
except ImportError:  # 直接运行（python ingest/load_graphormer_candidates.py）
    from common import (
        Subgraph,
        _node_dict,
        _edge_dicts,
        new_pattern_row,
        to_canonical,
        upsert_rows,
    )

UPSERT_CHUNK = 100  # canonical JSONB 单行可达数百 KB，控制语句体积
MAX_CLOSURE_EDGES = 2500  # 闭包边行封顶（内部边确定性截断，防 11MB 级 canonical）
DANGLING_EDGE_CAP = 200  # 每闭包保留的悬挂边（dst∈union 但 ∉members）上限


def _resolve_dir(base: str | None) -> Path:
    if base:
        p = Path(base)
        return p if p.is_absolute() else Path(__file__).resolve().parents[1] / p
    return Path(__file__).resolve().parents[1] / "ingest/seed/graphormer_v2"


def build_pattern_rows(base_dir: Path):
    """逐闭包产出 pattern 行（generator，内存按行释放）。"""
    import pyarrow.parquet as pq

    cand = pq.read_table(str(base_dir / "derived/confirmed_candidates.parquet"))
    seeds = cand.column("seed_address").to_pylist()
    members_list = cand.column("members").to_pylist()
    conf_counts = cand.column("confirmed_members").to_pylist()
    pooled_npz = np.load(str(base_dir / "derived/graphormer_pooled.npz"))
    pooled = pooled_npz["pooled"]
    npz_seeds = pooled_npz["seeds"].tolist()
    pooled_by_seed = {s: pooled[i] for i, s in enumerate(npz_seeds)}

    wanted = set(seeds)
    union: set[str] = set()
    for lst in members_list:
        union.update(lst)

    # 节点行：先列化整批，只对命中 union 的地址物化 dict（167 万行级）
    rows_by_addr: dict[str, dict] = {}
    nodes_path = base_dir / "subgraph_nodes.parquet"
    pf = pq.ParquetFile(str(nodes_path))
    node_cols = [c for c in pq.read_schema(str(nodes_path)).names
                 if c in {"address", "first_layer", "total_received_btc",
                          "total_sent_btc", "utxo_count",
                          "direct_related_to_lazarus", "confirmed_downstream",
                          "probably_lazarus_related", "is_censored"}]
    for b in pf.iter_batches(columns=node_cols, batch_size=500_000):
        cols = {c: b.column(c).to_pylist() for c in node_cols}
        for i, a in enumerate(cols["address"]):
            if a in union:
                rows_by_addr[a] = {c: cols[c][i] for c in node_cols}

    # 边行预过滤：src∈union 且 (dst None 或 dst∈union)。巨型 hub 地址
    # （单一 src 最高 56.7 万出边）指向 union 之外的边在闭包视角全部是
    # 悬挂边，预过滤在源头截断，避免逐闭包重复扫全量出边
    out_by_src: dict[str, list[dict]] = {}
    edges_path = base_dir / "subgraph_edges.parquet"
    pf = pq.ParquetFile(str(edges_path))
    edge_cols = [c for c in pq.read_schema(str(edges_path)).names
                 if c in {"src_address", "dst_address", "txid", "tx_layer",
                          "total_num_inputs", "total_num_outputs",
                          "dst_value_btc", "tx_total_input_btc",
                          "tx_total_output_btc", "value_ratio", "src_value_btc",
                          "is_stopped_expansion", "is_remixer", "is_crosschain",
                          "op_return_protocol"}]
    for b in pf.iter_batches(columns=edge_cols, batch_size=1_000_000):
        cols = {c: b.column(c).to_pylist() for c in edge_cols}
        srcs = cols["src_address"]
        dsts = cols["dst_address"]
        for i, src in enumerate(srcs):
            if src not in union:
                continue
            dst = dsts[i]
            if dst is not None and dst not in union:
                continue
            out_by_src.setdefault(src, []).append(
                {c: cols[c][i] for c in edge_cols})

    rows = []
    for k, seed in enumerate(seeds):
        members = members_list[k]
        members_set = set(members)
        sub_nodes = []
        seen = set()
        for a in members:
            if a in seen or a not in rows_by_addr:
                continue
            seen.add(a)
            sub_nodes.append(_node_dict(rows_by_addr[a]))
        # 闭包边集：内部边（dst∈members）+ stopped（dst None）全保留；
        # 跨闭包悬挂边（dst∈union∖members）确定性排序后封顶保留。
        # 基准中它们全部入 canonical（对 56.7 万出边 hub 闭包不可存储）。
        # 悬挂边降级为 stopped 形态（只留 addr→tx）：dst 不是本闭包成员，
        # 保留 tx→dst 会违反 canonical 引用完整性（基准中悬挂边无节点）
        internal: list[dict] = []
        dangling: list[dict] = []
        for a in members:
            for er in out_by_src.get(a, []):
                dst = er["dst_address"]
                if dst is None:
                    internal.append(er)
                elif dst in members_set:
                    internal.append(er)
                else:
                    dangling.append(er)
        internal.sort(key=lambda e: (e["txid"], e["src_address"],
                                     e.get("dst_address") or ""))
        internal = internal[:MAX_CLOSURE_EDGES]
        dangling.sort(key=lambda e: (e["txid"], e.get("dst_address") or ""))
        dangling = dangling[:DANGLING_EDGE_CAP]
        edge_rows = internal + dangling
        sub = Subgraph(seed_address=seed)
        sub.nodes = sub_nodes
        sub.edges = []
        for er in edge_rows:
            if er.get("dst_address") is not None and er["dst_address"] not in members_set:
                er = {**er, "dst_address": None, "is_stopped_expansion": True}
            sub.edges.extend(_edge_dicts(er))
        canon = to_canonical(sub)
        row = new_pattern_row(
            name=f"campaign_{seed[:12]}_{conf_counts[k]}conf",
            source="lazarus_confirmed",
            grade="A",
            provenance="confirmed",
            sub=sub,
            canon=canon,
        )
        # 检索指纹：rerank 通道预计算（rerank 只拉指纹列，
        # 大闭包 canonical 单行 MB 级不可池化回传）
        from backend.retrieval.channels import build_fingerprint

        row["retrieval_fingerprint"] = build_fingerprint(sub_nodes, sub.edges)
        vec = pooled_by_seed.get(seed)
        if vec is not None:
            row["graphormer_embedding"] = [float(x) for x in vec]
        yield row
        if (k + 1) % 1000 == 0:
            print(f"    built {k + 1}/{len(seeds)} closures", flush=True)

    # 校验用统计（逐行算，避免攒全量行）
    stats = {"candidates": len(seeds), "member_union": len(union)}


def run(session, base_dir: str | None = None, verbose: bool = True) -> dict:
    from backend.models.knowledge import Pattern

    base = _resolve_dir(base_dir)
    total = with_vec = 0
    chunk: list[dict] = []
    for row in build_pattern_rows(base):
        chunk.append(row)
        total += 1
        with_vec += 1 if "graphormer_embedding" in row else 0
        if len(chunk) >= UPSERT_CHUNK:
            upsert_rows(session, Pattern, chunk,
                        ["seed_address", "content_hash"],
                        chunk_size=UPSERT_CHUNK)
            chunk = []
    if chunk:
        upsert_rows(session, Pattern, chunk, ["seed_address", "content_hash"],
                    chunk_size=UPSERT_CHUNK)
    stats = {"candidates": total, "with_vector": with_vec}
    if verbose:
        print(f"graphormer_v2 candidates: {stats}")
    return stats


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import os

    os.environ.setdefault("JWT_SECRET", "migration-placeholder")

    from common import get_engine
    from sqlalchemy.orm import Session

    engine = get_engine()
    with Session(engine) as s:
        run(s)
