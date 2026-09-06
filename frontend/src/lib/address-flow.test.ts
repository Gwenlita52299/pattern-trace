// address-flow 纯函数单测（issue #57）：交易聚合/终止节点/金额语义/高亮解析。
// 这些是 #39/#51 两轮 bug 的发生地：变换逻辑此前零测试覆盖。
import { describe, expect, it } from "vitest";

import {
  resolveHighlightIds,
  stopReasonOf,
  toAddressFlow,
  type AddressFlow,
} from "@/lib/address-flow";
import type { GraphEdge, GraphNode } from "@/store/analysis";

function addrNode(id: string, layer = 0): GraphNode {
  return { id: `addr:${id}`, kind: "address", label: id, first_layer: layer };
}

function inputEdge(from: string, txid: string, srcBtc: number, extra: Partial<GraphEdge> = {}): GraphEdge {
  return {
    id: `edge:addr:${from}->tx:${txid}`,
    source: `addr:${from}`,
    target: `tx:${txid}`,
    txid,
    src_value_btc: srcBtc,
    dst_value_btc: srcBtc, // 输入侧占位：dst = 交易输出总额
    value_ratio: 1,
    ...extra,
  };
}

function outputEdge(txid: string, to: string, dstBtc: number, ratio: number): GraphEdge {
  return {
    id: `edge:tx:${txid}->addr:${to}`,
    source: `tx:${txid}`,
    target: `addr:${to}`,
    txid,
    dst_value_btc: dstBtc,
    value_ratio: ratio,
  };
}

describe("toAddressFlow", () => {
  it("普通交易聚合为 A→B 流边，金额沿流向标注", () => {
    const flow = toAddressFlow({
      nodes: [addrNode("A"), addrNode("B"), addrNode("C")],
      edges: [
        inputEdge("A", "T1", 1.0),
        outputEdge("T1", "B", 0.4, 0.4),
        outputEdge("T1", "C", 0.55, 0.55),
      ],
    });

    // 地址节点保留，无 terminal 节点
    expect(flow.nodes.map((n) => n.id)).toEqual(["addr:A", "addr:B", "addr:C"]);
    // 1 输入 × 2 输出 = 2 条流边（满二分）
    expect(flow.edges).toHaveLength(2);
    const [eB, eC] = flow.edges;

    // in_btc = dst/ratio 反推的交易输入总额；src_btc = 源侧消费额
    expect(eB.in_btc).toBeCloseTo(1.0);
    expect(eB.src_btc).toBeCloseTo(1.0);
    expect(eB.out_btc).toBeCloseTo(0.4);
    expect(eB.target).toBe("addr:B");
    expect(eC.out_btc).toBeCloseTo(0.55);
  });

  it("同交易多输出到同一地址：dst_value_btc 聚合（issue #9 不得覆盖）", () => {
    const flow = toAddressFlow({
      nodes: [addrNode("A"), addrNode("B")],
      edges: [
        inputEdge("A", "T1", 2.0),
        outputEdge("T1", "B", 0.3, 0.15),
        outputEdge("T1", "B", 0.5, 0.25),
      ],
    });

    expect(flow.edges).toHaveLength(1);
    expect(flow.edges[0].out_btc).toBeCloseTo(0.8); // 0.3 + 0.5
  });

  it("stopped 交易渲染为 terminalTransaction 节点，停止原因按 remixer > crosschain > out_of_range", () => {
    const mk = (extra: Partial<GraphEdge>) => toAddressFlow({
      nodes: [addrNode("D")],
      edges: [inputEdge("D", "TX", 1.0, extra)],
    });

    const coinjoin = mk({ is_stopped_expansion: true, is_remixer: true });
    const node = coinjoin.nodes.find((n) => n.kind === "terminalTransaction");
    expect(node).toBeDefined();
    expect(node!.first_layer).toBe(1); // 输入地址层 + 1
    expect(coinjoin.edges[0].target).toBe("tx:TX");

    expect(stopReasonOf({ isRemixer: true, isCrosschain: true })).toBe("coinjoin");
    expect(stopReasonOf({ isRemixer: false, isCrosschain: true })).toBe("crosschain");
    expect(stopReasonOf({ isRemixer: false, isCrosschain: false })).toBe("out_of_range");
  });

  it("stopped 且有输出地址的交易不产生 terminal 节点（继续流式）", () => {
    const flow = toAddressFlow({
      nodes: [addrNode("A"), addrNode("B")],
      edges: [
        inputEdge("A", "T1", 1.0, { is_stopped_expansion: true }),
        outputEdge("T1", "B", 0.9, 0.9),
      ],
    });
    expect(flow.nodes.every((n) => n.kind !== "terminalTransaction")).toBe(true);
  });

  it("addr→addr 孤儿边不参与地址流式变换", () => {
    const flow = toAddressFlow({
      nodes: [addrNode("A"), addrNode("B")],
      edges: [{
        id: "edge:addr:A->addr:B", source: "addr:A", target: "addr:B", txid: "T9",
      }],
    });
    expect(flow.edges).toHaveLength(0);
  });
});

describe("resolveHighlightIds", () => {
  const flow: AddressFlow = toAddressFlow({
    nodes: [addrNode("A"), addrNode("B"), addrNode("C")],
    edges: [
      inputEdge("A", "T1", 1.0),
      outputEdge("T1", "B", 0.4, 0.4),
      outputEdge("T1", "C", 0.55, 0.55),
    ],
  });

  it("addr 物证 → 该节点 + 相邻边 + 邻接节点（完整链路）", () => {
    const ids = resolveHighlightIds(["addr:A"], flow);
    expect(ids.has("addr:A")).toBe(true);
    expect(ids.has("addr:B")).toBe(true);
    expect(ids.has("addr:C")).toBe(true);
    expect(ids.has(flow.edges[0].id)).toBe(true);
    expect(ids.has(flow.edges[1].id)).toBe(true);
  });

  it("tx:/edge: 历史引用不再解析（物证统一为 addr），不产生高亮也不误报", () => {
    expect(resolveHighlightIds(["tx:T1"], flow)).toEqual(new Set());
    expect(resolveHighlightIds(["edge:addr:A->tx:T1"], flow)).toEqual(new Set());
  });

  it("不在子图中的 addr 引用被忽略", () => {
    expect(resolveHighlightIds(["addr:ghost"], flow)).toEqual(new Set());
  });
});
