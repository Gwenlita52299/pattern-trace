"""结构特征向量提取与图相似度 — retrieval-spec §3/§6."""
from __future__ import annotations

import hashlib
import math
from collections import Counter

FEATURE_DIM = 20


def _get(obj, key, default=None):
    """兼容属性对象（SubgraphResult dataclass）与 dict 两种节点/边形态。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def extract_features(nodes: list, edges: list) -> list[float]:
    """从子图 nodes/edges 提取特征向量（retrieval-spec §3 维度序）。

    MVP 实现核心维度；其余维度以 0 填充至 FEATURE_DIM。
    """
    node_count = len(nodes)
    edge_count = len(edges)
    density = edge_count / (node_count * (node_count - 1)) if node_count > 1 else 0.0
    degrees: dict[str, int] = {}
    mixer_contact = 0
    stopped_expansion = sum(1 for e in edges if _get(e, "is_stopped_expansion", False))
    crosschain = sum(1 for e in edges if _get(e, "is_crosschain", False))

    for e in edges:
        src = _get(e, "source", "") or ""
        degrees[src] = degrees.get(src, 0) + 1
        if _get(e, "is_remixer", False):
            mixer_contact += 1

    avg_out_degree = sum(degrees.values()) / len(degrees) if degrees else 0.0
    max_out_degree = max(degrees.values()) if degrees else 0

    features = [
        float(node_count),
        float(edge_count),
        density,
        avg_out_degree,
        float(max_out_degree),
        float(mixer_contact),
        float(stopped_expansion),
        float(crosschain),
    ]
    features += [0.0] * (FEATURE_DIM - len(features))
    return [round(min(f, 1e6), 8) for f in features]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1e-10
    norm_b = math.sqrt(sum(y * y for y in b)) or 1e-10
    return max(0.0, min(dot / (norm_a * norm_b), 1.0))


# ---------------------------------------------------------------------------
# 带属性 WL 子树核（retrieval-spec §6 修正版）
#
# 初始标签 = 类型 | 层深度档 | 金额数量级档；逐轮聚合邻居标签多重集；
# 相似度 = 各轮次子树标签多重集 Jaccard 的加权平均。
# 轮次权重递增（结构差异在后续轮放大）：链 vs 星在 round1 仅 0.25、
# round2 归零——等权平均会高于 RT-10 的 0.3 阈值，[1,2,3] 权重下 ≈0.17。
# ---------------------------------------------------------------------------

_WL_ROUND_WEIGHTS = (1.0, 2.0, 3.0)


def _wl_initial_label(node) -> str:
    kind = _get(node, "kind", "?") or "?"
    layer = min(int(_get(node, "first_layer", 0) or 0), 9)
    amount = (_get(node, "total_received_btc")
              or _get(node, "value_btc")
              or _get(node, "dst_value_btc")
              or 0)
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        amount = 0.0
    # 金额数量级分桶：0 / 个位 sat 级 / ... / 大额，log10 档位封顶
    magnitude = 0 if amount <= 0 else min(int(math.log10(amount)) + 2, 7)
    return f"{kind}|L{layer}|M{magnitude}"


def wl_subtree_similarity(graph_a: dict, graph_b: dict,
                          iterations: int = 3) -> float:
    """带属性 WL 子树核相似度 ∈ [0,1]。"""
    a_nodes = graph_a.get("nodes", []) or []
    a_edges = graph_a.get("edges", []) or []
    b_nodes = graph_b.get("nodes", []) or []
    b_edges = graph_b.get("edges", []) or []

    if not a_nodes and not b_nodes:
        return 1.0  # RT-12：双空图视为完全一致
    if not a_nodes or not b_nodes:
        return 0.0

    def adjacency(edges):
        nbrs: dict[str, list[str]] = {}
        for e in edges:
            s, t = _get(e, "source"), _get(e, "target")
            nbrs.setdefault(s, []).append(t)
            nbrs.setdefault(t, []).append(s)
        return nbrs

    def round_jaccard(nodes_a, nbrs_a, labels_a, nodes_b, nbrs_b, labels_b) -> float:
        def subtree_multiset(nodes, nbrs, labels):
            counter: Counter[str] = Counter()
            for n in nodes:
                nid = _get(n, "id")
                own = labels.get(nid, "?")
                neighbor_part = ",".join(sorted(labels.get(t, "?") for t in nbrs.get(nid, [])))
                counter[f"{own}|{neighbor_part}"] += 1
            return counter

        ma = subtree_multiset(nodes_a, nbrs_a, labels_a)
        mb = subtree_multiset(nodes_b, nbrs_b, labels_b)
        inter = sum((ma & mb).values())
        union = sum((ma | mb).values())
        return inter / union if union else 0.0

    def advance(nodes, nbrs, labels):
        return {
            _get(n, "id"): hashlib.sha1(
                (labels[_get(n, "id")] + "|"
                 + ",".join(sorted(labels.get(t, "?") for t in nbrs.get(_get(n, "id"), [])))
                 ).encode()).hexdigest()[:16]
            for n in nodes
        }

    nbrs_a, nbrs_b = adjacency(a_edges), adjacency(b_edges)
    labels_a = {_get(n, "id"): _wl_initial_label(n) for n in a_nodes}
    labels_b = {_get(n, "id"): _wl_initial_label(n) for n in b_nodes}

    total, weight_sum = 0.0, 0.0
    for i in range(iterations):
        weight = _WL_ROUND_WEIGHTS[i] if i < len(_WL_ROUND_WEIGHTS) else _WL_ROUND_WEIGHTS[-1]
        j = round_jaccard(a_nodes, nbrs_a, labels_a, b_nodes, nbrs_b, labels_b)
        total += weight * j
        weight_sum += weight
        if i < iterations - 1:  # 最后一轮的聚合不再使用
            labels_a = advance(a_nodes, nbrs_a, labels_a)
            labels_b = advance(b_nodes, nbrs_b, labels_b)
    return max(0.0, min(total / weight_sum if weight_sum else 0.0, 1.0))


FLOAT_PRECISION = 8


def structural_features(canon: dict) -> list[float]:
    """canonical 子图 → 特征向量（入库 IG-09 与检索共用同一实现）。"""
    return extract_features(canon["nodes"], canon["edges"])


def describe_subgraph(canon: dict) -> str:
    """canonical 子图 → 自然语言描述（retrieval-spec §4 模板）。

    入库（ingest）与检索（Retriever）共用此实现——两侧文本空间必须
    一致，embedding 才有可比性。
    """
    nodes, edges = canon["nodes"], canon["edges"]
    n_addr = sum(1 for n in nodes if n.get("kind") == "address")
    n_tx = len(nodes) - n_addr
    stopped = sum(1 for e in edges if e.get("is_stopped_expansion"))
    crosschain = sum(1 for e in edges if e.get("is_crosschain"))
    remixer = sum(1 for e in edges if e.get("is_remixer"))
    layers = canon.get("stats", {}).get("max_first_layer", 0)
    # 只累计 addr→tx 边的金额（tx→dst 是同一笔钱的拆分，重复计会翻倍）
    volume = round(sum(e.get("dst_value_btc") or 0 for e in edges
                       if str(e.get("source", "")).startswith("addr:")),
                   FLOAT_PRECISION)

    s = (f"A bitcoin transaction subgraph rooted at address "
         f"{canon.get('seed_address', 'unknown')} "
         f"with {len(nodes)} nodes ({n_addr} addresses, {n_tx} transactions) "
         f"and {len(edges)} edges, expanding across {layers} hop layers.")
    if remixer:
        s += (f" The root funds passed through a CoinJoin mixer transaction "
              f"({remixer} mixing edges observed).")
    if crosschain:
        s += f" {crosschain} edges bridge to cross-chain protocols via OP_RETURN."
    if stopped:
        s += f" Expansion stopped at {stopped} leaves."
    s += f" Total traced volume ~{volume} BTC."
    return s


def generate_difference_note(input_graph: dict, candidate_graph: dict) -> str | None:
    """差异说明提示（§6）——附加到 LLM prompt，说明输入子图与候选的结构差。

    差异小于阈值时返回 None（不值得占用 prompt 预算）。

    issue #71：节点构成同构但差找零/分支边（Δnode=0、Δedge≠0）是同簇
    变体的典型形态——旧口径 |Δnode|≤2 即静默（无任何校准），judge 只能
    从 description 的精确计数硬锚定。改为 Δedge≠0 也生成 note。
    """
    in_nodes = input_graph.get("nodes", []) or []
    cand_nodes = candidate_graph.get("nodes", []) or []
    in_edges = input_graph.get("edges", []) or []
    cand_edges = candidate_graph.get("edges", []) or []

    def addr_count(nodes):
        return sum(1 for n in nodes if (_get(n, "kind") or "") == "address")

    delta = len(in_nodes) - len(cand_nodes)
    addr_delta = addr_count(in_nodes) - addr_count(cand_nodes)
    edge_delta = len(in_edges) - len(cand_edges)
    if abs(delta) <= 2 and abs(addr_delta) <= 2 and abs(edge_delta) == 0:
        return None
    if abs(delta) <= 2 and abs(addr_delta) <= 2:
        # #71 变体形态：节点构成相同、仅差找零/分支边——明确告诉 judge
        # 差异是边级的，比较主干结构而非逐边计数
        return (f"Node composition is identical, but the input has "
                f"{'more' if edge_delta > 0 else 'fewer'} edges "
                f"({abs(edge_delta)} diff); the extra edges are likely "
                "change/self-spend or branch edges — compare the value "
                "backbone, not exact edge counts.")
    if delta > 0:
        return (f"The input subgraph has {delta} more nodes than this pattern "
                f"({addr_delta} more intermediate addresses); "
                "extra fan-out layers may not be part of the matched tactic.")
    return (f"The input subgraph has {-delta} fewer nodes than this pattern "
            f"({-addr_delta} intermediate addresses missing); "
            "the pattern's deeper layers were not observed on-chain.")
