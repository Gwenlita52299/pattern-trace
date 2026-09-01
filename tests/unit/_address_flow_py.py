"""Python 镜像 frontend/src/lib/address-flow.ts —— 地址流式展示变换的契约验证。

两份实现必须保持同一规则集：
  1. 仅保留 address 节点（id 以 "addr:" 前缀）；tx 及 tx:<txid>:overflow 摘要节点剔除；
  2. 一笔交易 T 的输入地址集 inputs 与输出地址集 outputs 做满二分，生成每个
     (src, dst) 一条 A→B 有向边，边 id 形如 flow:<src>-><dst>:<txid>；
  3. 每条边携带 in_btc（交易输入侧总额）与 out_btc（该目标地址接收额）；
     in_btc 由 (dst_value_btc / value_ratio) 反推，不可推导时回退为输出额合计；
  4. is_stopped_expansion 交易（coinjoin/crosschain/时间窗外）无输出地址，不产生 A→B 边；
  5. resolve_highlight_ids 把 canonical 证据 id（addr:/tx:/edge:）映射为本视图节点/边 id。
"""
from __future__ import annotations

ADDR_PREFIX = "addr:"
TX_PREFIX = "tx:"
OVERFLOW_SUFFIX = ":overflow"
EDGE_PREFIX = "edge:"


def is_address_id(nid: str) -> bool:
    return nid.startswith(ADDR_PREFIX)


def txid_of_node_id(nid: str) -> str | None:
    if not nid.startswith(TX_PREFIX):
        return None
    rest = nid[len(TX_PREFIX):]
    if rest.endswith(OVERFLOW_SUFFIX):
        rest = rest[: -len(OVERFLOW_SUFFIX)]
    return rest.split(":", 1)[0] or None


def _amt(v) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return 0.0


def _agg_amounts(tx_group: dict) -> tuple[float, float]:
    """(total_input, total_output)。"""
    # 输出总额：优先 addr→tx 边的 dst_value_btc（= 交易输出总额），否则合计输出边
    total_output = _amt(tx_group["input_edge"].get("dst_value_btc")) \
        if tx_group["input_edge"] else 0.0
    if total_output <= 0:
        total_output = sum(_amt(oe.get("dst_value_btc"))
                           for oe in tx_group["outputs"].values())
    # 输入总额：value_ratio = dst_value / total_input → total_input = dst / ratio
    total_input = 0.0
    for oe in tx_group["outputs"].values():
        dst = _amt(oe.get("dst_value_btc"))
        ratio = _amt(oe.get("value_ratio"))
        if dst > 0 and ratio > 0:
            total_input = dst / ratio
            break
    if total_input <= 0:
        total_input = total_output
    return total_input, total_output


def to_address_flow(subgraph: dict) -> dict:
    """canonical {nodes, edges} → {nodes, edges(FlowEdge)}。"""
    nodes = subgraph.get("nodes") or []
    edges = subgraph.get("edges") or []

    addr_node_by_id: dict[str, dict] = {}
    for n in nodes:
        if isinstance(n, dict) and is_address_id(n.get("id", "")):
            clone = dict(n)
            clone["kind"] = "address"
            clone["first_layer"] = n.get("first_layer") or 0
            addr_node_by_id[n["id"]] = clone

    # 按 txid 分组
    txs: dict[str, dict] = {}

    def group(txid: str) -> dict:
        g = txs.get(txid)
        if g is None:
            g = {
                "txid": txid,
                "tx_layer": "",
                "inputs": set(),
                "outputs": {},  # addr → 输出边
                "input_edge": None,
                "is_remixer": False,
                "is_crosschain": False,
                "is_stopped": False,
                "op_return_protocol": None,
            }
            txs[txid] = g
        return g

    for e in edges:
        if not isinstance(e, dict):
            continue
        src, dst = e.get("source", ""), e.get("target", "")
        src_tx, dst_tx = txid_of_node_id(src), txid_of_node_id(dst)
        if is_address_id(src) and dst_tx:
            g = group(dst_tx)
            g["inputs"].add(src)
            g["input_edge"] = g["input_edge"] or e
            g["tx_layer"] = g["tx_layer"] or e.get("tx_layer") or ""
            g["is_remixer"] = g["is_remixer"] or bool(e.get("is_remixer"))
            g["is_crosschain"] = g["is_crosschain"] or bool(e.get("is_crosschain"))
            g["is_stopped"] = g["is_stopped"] or bool(e.get("is_stopped_expansion"))
            g["op_return_protocol"] = g["op_return_protocol"] or e.get("op_return_protocol")
        elif src_tx and is_address_id(dst):
            g = group(src_tx)
            g["outputs"][dst] = e
            g["tx_layer"] = g["tx_layer"] or e.get("tx_layer") or ""
            g["is_remixer"] = g["is_remixer"] or bool(e.get("is_remixer"))
            g["is_crosschain"] = g["is_crosschain"] or bool(e.get("is_crosschain"))
            g["is_stopped"] = g["is_stopped"] or bool(e.get("is_stopped_expansion"))
            g["op_return_protocol"] = g["op_return_protocol"] or e.get("op_return_protocol")
        # 其它形态（地址→地址、摘要节点边）忽略

    flow_edges: list[dict] = []
    seen: set[str] = set()
    for g in txs.values():
        if not g["outputs"]:
            continue
        total_input, _ = _agg_amounts(g)
        for src_id in sorted(g["inputs"]):
            if src_id not in addr_node_by_id:
                continue
            for dst_id in sorted(g["outputs"]):
                if dst_id not in addr_node_by_id:
                    continue
                oe = g["outputs"][dst_id]
                eid = f"flow:{src_id}->{dst_id}:{g['txid']}"
                if eid in seen:
                    continue
                seen.add(eid)
                flow_edges.append({
                    "id": eid,
                    "source": src_id,
                    "target": dst_id,
                    "txid": g["txid"],
                    "tx_layer": g["tx_layer"] or None,
                    "value_ratio": _amt(oe.get("value_ratio")) or None,
                    "dst_value_btc": _amt(oe.get("dst_value_btc")) or None,
                    "in_btc": total_input,
                    "out_btc": _amt(oe.get("dst_value_btc")),
                    "is_stopped_expansion": g["is_stopped"],
                    "is_remixer": g["is_remixer"],
                    "is_crosschain": g["is_crosschain"],
                    "op_return_protocol": g["op_return_protocol"],
                })

    flow_nodes = sorted(addr_node_by_id.values(), key=lambda n: n["id"])
    flow_edges.sort(key=lambda e: e["id"])
    return {"nodes": flow_nodes, "edges": flow_edges}


def resolve_highlight_ids(highlight_ids, flow: dict) -> set[str]:
    """canonical 证据 id → 本视图要高亮的节点/边 id 集合。"""
    out: set[str] = set()
    node_ids = {n["id"] for n in flow["nodes"]}
    for hid in highlight_ids:
        if hid.startswith(ADDR_PREFIX) and hid in node_ids:
            out.add(hid)
        elif hid.startswith(TX_PREFIX):
            txid = txid_of_node_id(hid)
            if txid:
                for e in flow["edges"]:
                    if e["txid"] == txid:
                        out.add(e["id"])
                        out.add(e["source"])
                        out.add(e["target"])
        elif hid.startswith(EDGE_PREFIX):
            body = hid[len(EDGE_PREFIX):]
            tx_match = _rx_txid(body)
            if tx_match is not None:
                for e in flow["edges"]:
                    if e["txid"] == tx_match:
                        out.add(e["id"])
    return out


def _rx_txid(body: str) -> str | None:
    # 形式 edge:addr:A->tx:T 或 edge:tx:T->addr:B
    for part in body.replace("->", ":").split(":"):
        if part.startswith(TX_PREFIX):
            return part[len(TX_PREFIX):]
    return None
