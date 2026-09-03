"""ingest 公共层 — 切图、canonical 序列化、content_hash、WL 指纹、描述生成。

数据流（ingest-spec §2）：
    Parquet → 校验(IG-16) → 按 seed_address 切图(IG-02) → 过滤(IG-03)
    → canonical_subgraph → content_hash / wl_fingerprint / 描述文本
校验全部发生在任何 DB 写入之前——损坏输入零写入（IG-16）。

ID 规范沿用 graph-builder D3：addr:<address> / tx:<txid> / edge:<src>-><dst>。
与基线 parquet 的差异：基线边是 addr→addr（tx 为边属性），这里按 D3 展开
为 addr→tx→addr 三元组，stopped 边（dst_address 为空）只保留 addr→tx。
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field

import pyarrow.parquet as pq

from backend.graph_builder.id_contract import edge_id, node_id
from backend.retrieval.features import describe_subgraph, extract_features  # noqa: F401

FLOAT_PRECISION = 8

# 混币器接触判定：地址标签命中或 CoinJoin txid 命中（§2 过滤条件）。
# coinjoin 产出地址直接来自混币会话，AML 口径视为 mixer contact。
MIXER_LABELS = {"mixer", "coinjoin"}

NODE_COLS = {  # col -> 合法类型类别
    "address": "string",
    "first_layer": "number",
    "total_received_btc": "number",
    "total_sent_btc": "number",
    "utxo_count": "number",
}
EDGE_COLS = {
    "src_address": "string",
    "dst_address": "string",  # 可空：stopped 边无目标地址
    "txid": "string",
    "tx_layer": "string",
}

MIN_PATTERN_NODES = 5  # §2 过滤：节点数 ≥ 5

# issue #10：source → provenance。合成样本只作检索参考，不代表真实链上交易；
# 负样本仅用于阈值校准与误报评估。evidence_grade 与 provenance 分离：
# confirmed 可用真实等级(A/B)，synthetic 用 S，negative 用 B。
PROVENANCE_BY_SOURCE: dict[str, str] = {
    "lazarus_confirmed": "confirmed",
    "lazarus_synth": "synthetic",
    "constructed_normal": "negative",
}

# source → 证据等级（issue #10：synthetic 不再伪装成 Grade A）
GRADE_BY_SOURCE: dict[str, str] = {
    "lazarus_confirmed": "A",
    "lazarus_synth": "S",
    "constructed_normal": "B",
}


def provenance_of(source: str) -> str:
    """由 source 推导来源性质；未知 source 保守回退为 synthetic，避免误标为 confirmed。"""
    return PROVENANCE_BY_SOURCE.get(source, "synthetic")


class IngestError(Exception):
    """带文件/字段定位的输入错误（IG-16 要求清晰报错）。"""


# ---------------------------------------------------------------------------
# Parquet 读取与校验
# ---------------------------------------------------------------------------

def _check_kind(table, col: str, kind: str, path: str) -> None:
    import pyarrow as pa

    dtype = table.column(col).type
    if pa.types.is_null(dtype):
        return  # 全空列按缺数据处理，不在此报错
    ok = {
        "string": pa.types.is_string(dtype) or pa.types.is_large_string(dtype),
        "number": pa.types.is_integer(dtype) or pa.types.is_floating(dtype)
        or pa.types.is_decimal(dtype),
    }[kind]
    if not ok:
        raise IngestError(f"{path}: 字段 {col} 类型漂移（期望 {kind}，实际 {dtype}）")


def read_validated_parquet(path: str, required: dict[str, str]):
    """读 Parquet 并校验必需字段；任何问题抛 IngestError（指出文件与字段）。"""
    try:
        table = pq.read_table(path)
    except Exception as exc:  # 文件级损坏（非 parquet 格式等）
        raise IngestError(f"{path}: 文件无法解析为 Parquet（{exc}）") from exc

    missing = [c for c in required if c not in table.column_names]
    if missing:
        raise IngestError(f"{path}: 缺少必需字段 {missing}")
    if table.num_rows == 0:
        raise IngestError(f"{path}: 空文件（0 行），无法切图")
    for col, kind in required.items():
        _check_kind(table, col, kind, path)
    return table


# ---------------------------------------------------------------------------
# 切图（IG-02）：first_layer==0 为 seed，可达性 BFS 收集每棵子树
# ---------------------------------------------------------------------------

@dataclass
class Subgraph:
    seed_address: str
    nodes: list[dict] = field(default_factory=list)   # canonical node dicts
    edges: list[dict] = field(default_factory=list)   # canonical edge dicts

    @property
    def txids(self) -> set[str]:
        return {e["txid"] for e in self.edges if e.get("txid")}


def slice_by_seed(node_rows: list[dict], edge_rows: list[dict]) -> list[Subgraph]:
    """把混合数据集切成 per-seed 子图。不同 seed 的记录互不混淆（IG-02）。

    共享中间节点（多个 seed 都可达）会出现在多个子图中——这是真实拓扑，
    各 pattern 记录自己的视图即可。
    """
    rows_by_addr: dict[str, dict] = {}
    for r in node_rows:
        rows_by_addr[r["address"]] = r

    out_by_src: dict[str, list[dict]] = {}
    for e in edge_rows:
        out_by_src.setdefault(e["src_address"], []).append(e)

    seeds = [r["address"] for r in node_rows if _int(r.get("first_layer")) == 0]

    subgraphs: list[Subgraph] = []
    for seed in seeds:
        sub = Subgraph(seed_address=seed)
        seen_nodes: set[str] = set()
        queue = deque([seed])
        while queue:
            addr = queue.popleft()
            if addr in seen_nodes or addr not in rows_by_addr:
                continue
            seen_nodes.add(addr)
            sub.nodes.append(_node_dict(rows_by_addr[addr]))
            for e in out_by_src.get(addr, []):
                sub.edges.extend(_edge_dicts(e))
                dst = e.get("dst_address")
                if dst and dst not in seen_nodes:
                    queue.append(dst)

        # parquet 边可能引用节点表之外的端点：补占位节点，
        # 保证子图自身引用完整（WL kernel / 前端渲染依赖）。
        # tx: 前缀端点是交易节点（D3），不能误标为 address
        known = {n["id"] for n in sub.nodes}
        for e in sub.edges:
            for end in (e["source"], e["target"]):
                if end in known:
                    continue
                if end.startswith("tx:"):
                    sub.nodes.append({
                        "id": end, "kind": "transaction",
                        "label": end.split(":", 1)[1][:16], "first_layer": 0,
                    })
                else:
                    sub.nodes.append({
                        "id": end, "kind": "address",
                        "label": end.split(":", 1)[1], "first_layer": -1,
                        "total_received_btc": 0.0, "total_sent_btc": 0.0,
                        "utxo_count": 0, "direct_related_to_lazarus": False,
                    })
                known.add(end)
        subgraphs.append(sub)
    return subgraphs


def _node_dict(row: dict) -> dict:
    addr = row["address"]
    return {
        "id": node_id("address", addr),
        "kind": "address",
        "label": addr,
        "first_layer": _int(row.get("first_layer")),
        "total_received_btc": _num(row.get("total_received_btc")),
        "total_sent_btc": _num(row.get("total_sent_btc")),
        "utxo_count": _int(row.get("utxo_count")),
        "direct_related_to_lazarus": bool(row.get("direct_related_to_lazarus")),
    }


def _edge_dicts(row: dict) -> list[dict]:
    src = row["src_address"]
    txid = row["txid"]
    dst = row.get("dst_address")
    stopped = dst is None
    total_in = _num(row.get("tx_total_input_btc"))
    total_out = _num(row.get("tx_total_output_btc"))
    src_value = _num(row.get("src_value_btc"))
    dst_value = _num(row.get("dst_value_btc"))

    base = {
        "txid": txid,
        "tx_layer": row.get("tx_layer") or "",
        "total_num_inputs": _int(row.get("total_num_inputs")),
        "total_num_outputs": _int(row.get("total_num_outputs")),
        "is_stopped_expansion": bool(row.get("is_stopped_expansion")) or stopped,
        "is_remixer": bool(row.get("is_remixer")),
        "is_crosschain": bool(row.get("is_crosschain")),
        "op_return_protocol": row.get("op_return_protocol"),
    }
    # addr→tx：金额口径 = 该笔交易的总量（证据面板显示交易规模）
    parts = [
        {
            **base,
            "id": edge_id(node_id("address", src), node_id("transaction", txid)),
            "source": node_id("address", src),
            "target": node_id("transaction", txid),
            "value_ratio": src_value / total_in if total_in > 0 else 0.0,
            "dst_value_btc": total_out,
        }
    ]
    # tx→dst：仅非 stopped 边存在
    if not stopped:
        parts.append({
            **base,
            "id": edge_id(node_id("transaction", txid), node_id("address", dst)),
            "source": node_id("transaction", txid),
            "target": node_id("address", dst),
            "value_ratio": dst_value / total_in if total_in > 0 else 0.0,
            "dst_value_btc": dst_value,
        })
    return parts


def _num(v) -> float:
    if v is None:
        return 0.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def _int(v) -> int:
    # NaN 会穿透 `x or 0`（NaN 为真值），必须先经 _num 的 isfinite 过滤
    return int(_num(v))


# ---------------------------------------------------------------------------
# 过滤（IG-03）：节点数 ≥ 5 且存在混币器接触
# ---------------------------------------------------------------------------

def has_mixer_contact(
    sub: Subgraph,
    labeled_addresses: set[str] | None = None,
    coinjoin_txids: set[str] | None = None,
) -> bool:
    labeled = labeled_addresses or set()
    cj_txids = coinjoin_txids or set()
    for n in sub.nodes:
        if n["label"] in labeled:
            return True
    for e in sub.edges:
        if e["is_remixer"] or e.get("txid") in cj_txids:
            return True
    return False


def passes_filter(sub: Subgraph, **contact_kwargs) -> bool:
    if len(sub.nodes) < MIN_PATTERN_NODES:
        return False
    return has_mixer_contact(sub, **contact_kwargs)


# ---------------------------------------------------------------------------
# canonical 序列化 + content_hash（IG-10/11 幂等与 upsert key）
# ---------------------------------------------------------------------------

def _normalize(obj):
    """递归规范化：float 统一精度、dict 键排序——保证同内容必得同 hash。"""
    if isinstance(obj, float):
        r = round(obj, FLOAT_PRECISION)
        return 0.0 if r == 0 else r  # 归一 -0.0
    if isinstance(obj, dict):
        return {k: _normalize(obj[k]) for k in sorted(obj)}
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    return obj


def to_canonical(sub: Subgraph) -> dict:
    # 从边派生 tx 节点（基线把 tx 当边属性；D3 下 tx 是一等节点）
    nodes = list(sub.nodes)
    known = {n["id"] for n in nodes}
    for e in sub.edges:
        if e["target"].startswith("tx:") and e["target"] not in known:
            nodes.append({
                "id": e["target"], "kind": "transaction",
                "label": e["target"].split(":", 1)[1][:16],
                "first_layer": 0,
            })
            known.add(e["target"])

    canon = {
        "seed_address": sub.seed_address,
        # 节点/边按 id 排序：序列化结果与构建顺序无关
        "nodes": sorted((_normalize(n) for n in nodes), key=lambda n: n["id"]),
        "edges": sorted((_normalize(e) for e in sub.edges), key=lambda e: e["id"]),
    }
    canon["stats"] = {
        "node_count": len(canon["nodes"]),
        "edge_count": len(canon["edges"]),
        "max_first_layer": max((n["first_layer"] for n in canon["nodes"]), default=0),
    }
    return canon


def content_hash(canon: dict) -> str:
    payload = json.dumps(_normalize(canon), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# WL 子树指纹（IG-12）：各轮子树标签多重集 → 排序 hash 列表，结构可比较
# ---------------------------------------------------------------------------

def wl_fingerprint(nodes: list[dict], edges: list[dict], iterations: int = 3) -> dict:
    labels: dict[str, str] = {n["id"]: n["kind"] for n in nodes}
    neighbors: dict[str, list[str]] = {}
    for e in edges:
        neighbors.setdefault(e["source"], []).append(e["target"])
        neighbors.setdefault(e["target"], []).append(e["source"])

    rounds: list[list[str]] = []
    for _ in range(iterations):
        multiset = [labels[n["id"]] + "|" + ",".join(sorted(labels.get(t, "?") for t in neighbors.get(n["id"], [])))
                    for n in nodes]
        hashed = sorted(hashlib.sha1(m.encode()).hexdigest() for m in multiset)
        rounds.append(hashed)
        # 迭代聚合：新标签 = 旧标签 + 邻居标签多重集
        labels = {
            n["id"]: hashlib.sha1(
                (labels[n["id"]] + "|" + ",".join(sorted(labels.get(t, "?") for t in neighbors.get(n["id"], [])))).encode()
            ).hexdigest()[:16]
            for n in nodes
        }
    return {"rounds": rounds}


# ---------------------------------------------------------------------------
# 语义描述文本：实现上移至 backend.retrieval.features（模块顶部导入），
# 入库与检索共用同一文本空间——两侧各写一份迟早漂移
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 结构特征向量（IG-09）：复用 retrieval features 的 FEATURE_DIM=20 定义
# ---------------------------------------------------------------------------

def structural_features(canon: dict) -> list[float]:
    return extract_features(canon["nodes"], canon["edges"])


# ---------------------------------------------------------------------------
# DB 工具
# ---------------------------------------------------------------------------

ADVISORY_LOCK_KEY = 918273645  # run_all 全流程互斥（IG-17）


def upsert_rows(session, model, rows: list[dict], key_cols: list[str],
                no_touch: tuple[str, ...] = ("id", "added_at", "created_at"),
                chunk_size: int = 500) -> int:
    """通用 upsert：key 冲突时覆盖其余列、保留原 id 与创建时间。

    id 稳定是 embedding 按 pattern_id 缓存（IG-14）的前提；
    分块执行避免数千行 × JSONB 的单条巨型语句。
    """
    from sqlalchemy.dialects.postgresql import insert

    if not rows:
        return 0
    total = 0
    for i in range(0, len(rows), chunk_size):
        stmt = insert(model).values(rows[i:i + chunk_size])
        update_cols = {
            c.name: stmt.excluded[c.name]
            for c in model.__table__.columns
            if c.name not in key_cols and c.name not in no_touch
        }
        session.execute(stmt.on_conflict_do_update(
            index_elements=key_cols, set_=update_cols))
        total += len(rows[i:i + chunk_size])
    return total


def get_engine():
    from sqlalchemy import create_engine

    from backend.core.config import get_settings

    return create_engine(get_settings().database_url.replace(
        "postgresql://", "postgresql+psycopg://"), pool_pre_ping=True)


@contextmanager
def ingest_lock(engine):
    """会话级 advisory lock：双实例并发时第二个阻塞等待，不产生竞态写入。"""
    from sqlalchemy import text

    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})


def new_pattern_row(*, name: str, source: str, grade: str, sub: Subgraph,
                    canon: dict | None = None,
                    provenance: str | None = None) -> dict:
    """组装一行 pattern 记录（向量列由 compute_embeddings 二段填充）。

    issue #10：provenance 由 source 派生（confirmed/synthetic/negative），
    单独显式传入时以其为准。
    """
    canon = canon or to_canonical(sub)
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "source": source,
        "provenance": provenance or provenance_of(source),
        "evidence_grade": grade,
        "seed_address": sub.seed_address,
        "description": describe_subgraph(canon),
        "canonical_subgraph": canon,
        "wl_fingerprint": wl_fingerprint(canon["nodes"], canon["edges"]),
        "content_hash": content_hash(canon),
    }
