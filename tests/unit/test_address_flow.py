"""地址流式展示变换契约测试 —— 镜像 frontend/src/lib/address-flow.ts。

验证核心语义（与 TS 实现保持同一规则集）：
  1. 变换后仅保留 address 节点，tx（含 tx:<txid>:overflow 摘要节点）剔除；
  2. 交易成为有向边，连接其（输入地址 → 输出地址），边 id flow:<src>-><dst>:<txid>；
  3. 每条边带 in_btc（交易输入侧总额）与 out_btc（目标地址接收额）；
     in_btc 由 (dst_value_btc / value_ratio) 反推；
  4. 多输入多输出交易做满二分；
  5. is_stopped_expansion 交易无输出地址 → 渲染为独立 terminalTransaction 节点
     （id=tx:<txid>），每个输入地址产生一条 A→终止交易边；
  6. resolve_highlight_ids 把 canonical 证据 id 映射到本视图节点/边 id（含终止节点）。
"""
from __future__ import annotations

import pytest

from tests.unit._address_flow_py import (
    resolve_highlight_ids,
    stop_reason_of,
    to_address_flow,
)


def _node(nid, kind="address", first_layer=0, **kw):
    d = {"id": nid, "kind": kind, "first_layer": first_layer}
    d.update(kw)
    return d


def _edge(eid, src, dst, txid=None, value_ratio=None, dst_value_btc=None,
          is_stopped_expansion=False, is_remixer=False, is_crosschain=False,
          op_return_protocol=None):
    d = {"id": eid, "source": src, "target": dst}
    if txid is not None:
        d["txid"] = txid
    if value_ratio is not None:
        d["value_ratio"] = value_ratio
    if dst_value_btc is not None:
        d["dst_value_btc"] = dst_value_btc
    if is_stopped_expansion:
        d["is_stopped_expansion"] = True
    if is_remixer:
        d["is_remixer"] = True
    if is_crosschain:
        d["is_crosschain"] = True
    if op_return_protocol is not None:
        d["op_return_protocol"] = op_return_protocol
    return d


def test_address_only_nodes_and_summary_dropped():
    subgraph = {
        "nodes": [
            _node("addr:seed", first_layer=0),
            _node("tx:A", kind="transaction"),
            _node("addr:b", first_layer=1),
            _node("tx:A:overflow", kind="transaction", label="N more outputs..."),
        ],
        "edges": [
            _edge("e1", "addr:seed", "tx:A", txid="A", dst_value_btc=1.0),
            _edge("e2", "tx:A", "addr:b", txid="A", value_ratio=0.4,
                  dst_value_btc=0.4),
            # 摘要边：tx→tx，非地址 → 应剔除
            _edge("e3", "tx:A", "tx:A:overflow"),
        ],
    }
    flow = to_address_flow(subgraph)
    ids = {n["id"] for n in flow["nodes"]}
    assert ids == {"addr:seed", "addr:b"}
    assert len(flow["edges"]) == 1
    e = flow["edges"][0]
    assert e["id"] == "flow:addr:seed->addr:b:A"
    assert e["in_btc"] == pytest.approx(1.0)   # 0.4 / 0.4
    assert e["out_btc"] == pytest.approx(0.4)


def test_amount_derivation_via_value_ratio():
    # value_ratio = dst/total_input → total_input = dst/ratio
    subgraph = {
        "nodes": [_node("addr:seed"), _node("tx:T", kind="transaction"),
                  _node("addr:b")],
        "edges": [
            _edge("e1", "addr:seed", "tx:T", txid="T", dst_value_btc=3.0),
            _edge("e2", "tx:T", "addr:b", txid="T", value_ratio=0.25,
                  dst_value_btc=0.75),
        ],
    }
    flow = to_address_flow(subgraph)
    e = flow["edges"][0]
    assert e["in_btc"] == pytest.approx(3.0)   # 0.75 / 0.25
    assert e["out_btc"] == pytest.approx(0.75)


def test_multi_input_multi_output_full_bipartite():
    subgraph = {
        "nodes": [_node("addr:x"), _node("addr:y"), _node("addr:p"),
                  _node("addr:q"), _node("tx:M", kind="transaction")],
        "edges": [
            _edge("in1", "addr:x", "tx:M", txid="M", dst_value_btc=2.0),
            _edge("in2", "addr:y", "tx:M", txid="M", dst_value_btc=2.0),
            _edge("out1", "tx:M", "addr:p", txid="M", value_ratio=0.5,
                  dst_value_btc=1.0),
            _edge("out2", "tx:M", "addr:q", txid="M", value_ratio=0.5,
                  dst_value_btc=1.0),
        ],
    }
    flow = to_address_flow(subgraph)
    edge_ids = {e["id"] for e in flow["edges"]}
    assert edge_ids == {
        "flow:addr:x->addr:p:M",
        "flow:addr:x->addr:q:M",
        "flow:addr:y->addr:p:M",
        "flow:addr:y->addr:q:M",
    }
    for e in flow["edges"]:
        assert e["in_btc"] == pytest.approx(2.0)  # 1.0 / 0.5
        assert e["out_btc"] == pytest.approx(1.0)


def test_stopped_tx_without_outputs_becomes_terminal_node():
    subgraph = {
        "nodes": [_node("addr:seed"), _node("tx:CJ", kind="transaction")],
        "edges": [
            _edge("e1", "addr:seed", "tx:CJ", txid="CJ", dst_value_btc=0.5,
                  is_stopped_expansion=True),
        ],
    }
    flow = to_address_flow(subgraph)
    node_ids = {n["id"] for n in flow["nodes"]}
    assert node_ids == {"addr:seed", "tx:CJ"}
    term = next(n for n in flow["nodes"] if n["kind"] == "terminalTransaction")
    assert term["id"] == "tx:CJ"
    assert term["txid"] == "CJ"
    assert term["stop_reason"] == "out_of_range"
    assert term["is_stopped_expansion"] is True
    assert len(flow["edges"]) == 1
    e = flow["edges"][0]
    assert e["id"] == "flow:addr:seed->tx:CJ:CJ"
    assert e["source"] == "addr:seed"
    assert e["target"] == "tx:CJ"
    assert e["is_stopped_expansion"] is True
    assert e["in_btc"] == pytest.approx(0.5)


def test_coinjoin_terminal_node_shows_mixer_stop_reason():
    subgraph = {
        "nodes": [_node("addr:A", first_layer=0), _node("addr:B", first_layer=0),
                  _node("tx:CJ", kind="transaction")],
        "edges": [
            _edge("e1", "addr:A", "tx:CJ", txid="CJ", dst_value_btc=2.0,
                  is_stopped_expansion=True, is_remixer=True),
            _edge("e2", "addr:B", "tx:CJ", txid="CJ", dst_value_btc=3.0,
                  is_stopped_expansion=True, is_remixer=True),
        ],
    }
    flow = to_address_flow(subgraph)
    term = next(n for n in flow["nodes"] if n["kind"] == "terminalTransaction")
    assert term["stop_reason"] == "coinjoin"
    assert term["txid"] == "CJ"
    assert term["is_remixer"] is True
    edge_ids = {e["id"] for e in flow["edges"]}
    assert edge_ids == {
        "flow:addr:A->tx:CJ:CJ",
        "flow:addr:B->tx:CJ:CJ",
    }


def test_crosschain_terminal_node_shows_protocol():
    subgraph = {
        "nodes": [_node("addr:A", first_layer=0), _node("tx:CC", kind="transaction")],
        "edges": [
            _edge("e1", "addr:A", "tx:CC", txid="CC", dst_value_btc=1.0,
                  is_stopped_expansion=True, is_crosschain=True,
                  op_return_protocol="thorchain"),
        ],
    }
    flow = to_address_flow(subgraph)
    term = next(n for n in flow["nodes"] if n["kind"] == "terminalTransaction")
    assert term["stop_reason"] == "crosschain"
    assert term["protocol"] == "thorchain"
    assert term["is_crosschain"] is True


def test_unknown_op_return_not_terminal_and_expands():
    # Unknown OP_RETURN 不是终止类型：后端不标 is_stopped_expansion → 正常扩展为 A→B 流边。
    subgraph = {
        "nodes": [_node("addr:A", first_layer=0), _node("tx:T", kind="transaction"),
                  _node("addr:B", first_layer=1)],
        "edges": [
            _edge("e1", "addr:A", "tx:T", txid="T", dst_value_btc=1.0),
            _edge("e2", "tx:T", "addr:B", txid="T", value_ratio=0.4,
                  dst_value_btc=0.4),
        ],
    }
    flow = to_address_flow(subgraph)
    assert all(n["kind"] != "terminalTransaction" for n in flow["nodes"])
    assert len(flow["edges"]) == 1
    assert flow["edges"][0]["target"] == "addr:B"


def test_multi_output_same_address_amount_aggregated_not_overwritten():
    # 同一交易两个输出到同一地址：金额聚合，不得覆盖（issue #9）。
    subgraph = {
        "nodes": [_node("addr:A"), _node("tx:T", kind="transaction"),
                  _node("addr:B")],
        "edges": [
            _edge("in", "addr:A", "tx:T", txid="T", dst_value_btc=2.0),
            _edge("out1", "tx:T", "addr:B", txid="T", value_ratio=0.25,
                  dst_value_btc=0.5),
            _edge("out2", "tx:T", "addr:B", txid="T", value_ratio=0.25,
                  dst_value_btc=0.5),
        ],
    }
    flow = to_address_flow(subgraph)
    assert len(flow["edges"]) == 1
    e = flow["edges"][0]
    assert e["id"] == "flow:addr:A->addr:B:T"
    # dst_value_btc = 0.5 + 0.5 = 1.0，不得只保留最后一个输出（0.5）
    assert e["out_btc"] == pytest.approx(1.0)
    # total_input = dst / ratio = 1.0 / 0.25 = 4.0
    assert e["in_btc"] == pytest.approx(4.0)


def test_resolve_highlight_terminal_node_highlighted_via_txid():
    subgraph = {
        "nodes": [_node("addr:A", first_layer=0), _node("tx:CJ", kind="transaction")],
        "edges": [
            _edge("e1", "addr:A", "tx:CJ", txid="CJ", dst_value_btc=0.5,
                  is_stopped_expansion=True, is_remixer=True),
        ],
    }
    flow = to_address_flow(subgraph)
    out = resolve_highlight_ids({"tx:CJ"}, flow)
    assert out == {"flow:addr:A->tx:CJ:CJ", "addr:A", "tx:CJ"}


def test_stop_reason_of_derivation():
    assert stop_reason_of({"is_remixer": True, "is_crosschain": False}) == "coinjoin"
    assert stop_reason_of({"is_remixer": False, "is_crosschain": True}) == "crosschain"
    assert stop_reason_of({"is_remixer": False, "is_crosschain": False}) == "out_of_range"


def test_resolve_highlight_tx_expands_to_edges_and_endpoints():
    subgraph = {
        "nodes": [_node("addr:seed"), _node("addr:b"), _node("tx:A",
                  kind="transaction")],
        "edges": [
            _edge("e1", "addr:seed", "tx:A", txid="A", dst_value_btc=1.0),
            _edge("e2", "tx:A", "addr:b", txid="A", value_ratio=0.4,
                  dst_value_btc=0.4),
        ],
    }
    flow = to_address_flow(subgraph)
    out = resolve_highlight_ids({"tx:A"}, flow)
    assert out == {"flow:addr:seed->addr:b:A", "addr:seed", "addr:b"}


def test_resolve_highlight_addr_and_unknown_ignored():
    flow = {
        "nodes": [{"id": "addr:seed", "kind": "address"}],
        "edges": [{"id": "flow:addr:seed->addr:b:A", "source": "addr:seed",
                   "target": "addr:b", "txid": "A"}],
    }
    out = resolve_highlight_ids({"addr:seed", "tx:unknown", "addr:ghost"}, flow)
    assert out == {"addr:seed"}
