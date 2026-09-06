// 前端展示用「地址为节点、交易为边」的流式子图变换（address-flow display）。
//
// 背景：后端返回的 canonical 子图把交易也建模成节点（D3：节点 addr:<addr> / tx:<txid>，
// 边 edge:<src>-><dst>；addr→tx 为消费边，tx→addr 为输出边）。前端直接画这个子图时，
// address 和 tx 都成为画布节点，每条资金流被拉成 address→tx→address 两跳。
//
// 目标（本轮需求）：前端**不直接使用**生成的子图，而是把它变换成
//   - 仅 address 作为节点；
//   - 交易作为「边」把参与该交易的地址连起来（同笔交易的每个输入地址 → 每个输出地址）；
//   - 每条边上标注该笔交易的**入边交易金额**（交易输入侧总额）与**出边交易金额**
//     （该目标地址从本交易收到的金额）。
//
// 规则与取舍：
//   1. 节点只保留 address；tx 节点（含 tx:<txid>:overflow 摘要节点）全部剔除。
//   2. 一笔交易 T：inputs = {A: addr→T 的源}，outputs = {B: T→addr 的目标}；
//      为每个 (A, B) 组合生成一条 A→B 的有向边（比特币交易本身不绑定 input→output，
//      因此跨界用「满二分」表达；数据源已按 fanout_truncate_threshold 截断单边扇出）。
//   3. 金额：in_btc = 交易输入侧总额（资金进入该交易，即源地址 A 的流出）；out_btc =
//      该目标地址从本交易收到的金额（资金离开交易进入 B，即目标地址 B 的流入）。
//      in_btc 由 (dst_value_btc / value_ratio) 反推（value_ratio = dst_value / total_input），
//      不可推导时回退为输出额合计（忽略手续费）。展示时沿 A→B 流向标为「出{in_btc}→入{out_btc}」。
//   4. is_stopped_expansion 交易（coinjoin/crosschain/时间窗外）无输出地址，不产生 A→B 边；
//      在流式视图中渲染为**独立终止交易节点**（kind=terminalTransaction，id=tx:<txid>），
//      每个输入地址产生一条 A→终止交易边，视图中流到此终止（该交易不再继续流动）。
//      Unknown OP_RETURN 不会被后端标 is_stopped_expansion（它继续扩展），故不会成为终止节点。
//   5. 高亮映射：canonical 证据 ID（addr:/tx:/edge:）→ 本视图的节点/边 ID 集合，见 resolveHighlightIds。
import type { GraphEdge, GraphNode } from "@/store/analysis";

const ADDR_PREFIX = "addr:";
const TX_PREFIX = "tx:";
const OVERFLOW_SUFFIX = ":overflow";

// 某地址在 canonical 子图中出现的节点（按 id 去重；引用于边但缺节点时合成占位）
export function isAddressId(id: string): boolean {
  return id.startsWith(ADDR_PREFIX);
}

function txidOfNodeId(id: string): string | null {
  if (!id.startsWith(TX_PREFIX)) return null;
  // tx:<txid> 或 tx:<txid>:overflow → <txid>
  const rest = id.slice(TX_PREFIX.length);
  if (rest.endsWith(OVERFLOW_SUFFIX)) {
    return rest.slice(0, -OVERFLOW_SUFFIX.length);
  }
  return rest.split(":")[0] ?? null;
}

export interface FlowEdge extends GraphEdge {
  /** 本交易所属 txid（必填，区别于 canonical 边可能为空） */
  txid: string;
  /** 入边交易金额：该交易输入侧总额（BTC）——展示为「出」（源地址流出额）。 */
  in_btc: number;
  /** 源侧金额：该源地址在本交易中消费的 UTXO 金额（src_value_btc；缺省回退 in_btc）。 */
  src_btc: number;
  /** 出边交易金额：该目标地址从本交易收到的金额（BTC）——展示为「入」（目标地址流入额）。 */
  out_btc: number;
  /** 参与本交易的输入/输出来源数量（来自 canonical 边统计，便于悬浮展示）。 */
  total_num_inputs?: number;
  total_num_outputs?: number;
}

export interface AddressFlow {
  nodes: GraphNode[];
  edges: FlowEdge[];
}

function normalizeAmount(n: number | undefined | null): number {
  if (typeof n !== "number" || Number.isNaN(n)) return 0;
  return n;
}

interface TxGroup {
  txid: string;
  txLayer: string;
  inputs: Set<string>; // addr 节点 id
  /** 每个输入地址自己的 addr→tx 边（携带其 src_value_btc，按源区分出金额） */
  inputEdgeBySrc: Map<string, GraphEdge>;
  outputs: Map<string, GraphEdge>; // addr 节点 id → tx→addr 输出边
  inputEdge: GraphEdge | null; // 任一 addr→tx 边（其 dst_value_btc = 交易输出总额）
  isRemixer: boolean;
  isCrosschain: boolean;
  isStopped: boolean;
  opReturnProtocol: string | null;
}

/** 由交易聚合事实推导出终止原因（issue #9）。 */
export function stopReasonOf(g: Pick<TxGroup, "isRemixer" | "isCrosschain">):
  "coinjoin" | "crosschain" | "out_of_range" {
  if (g.isRemixer) return "coinjoin";
  if (g.isCrosschain) return "crosschain";
  return "out_of_range";
}

/** 终止交易在流式视图中渲染为一个 terminalTransaction 节点（issue #9）。 */
export interface TerminalNode extends GraphNode {
  kind: "terminalTransaction";
  txid: string;
  stop_reason: "coinjoin" | "crosschain" | "out_of_range";
  protocol: string | null;
}

function classifyAndGroup(
  edges: GraphEdge[],
): { txs: Map<string, TxGroup>; orphans: GraphEdge[] } {
  const txs = new Map<string, TxGroup>();
  const orphans: GraphEdge[] = [];

  function group(txid: string): TxGroup {
    let g = txs.get(txid);
    if (!g) {
      g = {
        txid,
        txLayer: "",
        inputs: new Set(),
        inputEdgeBySrc: new Map(),
        outputs: new Map(),
        inputEdge: null,
        isRemixer: false,
        isCrosschain: false,
        isStopped: false,
        opReturnProtocol: null,
      };
      txs.set(txid, g);
    }
    return g;
  }

  for (const e of edges) {
    const srcIsAddr = isAddressId(e.source);
    const dstIsAddr = isAddressId(e.target);
    const srcTx = txidOfNodeId(e.source);
    const dstTx = txidOfNodeId(e.target);

    if (srcIsAddr && dstTx) {
      // addr→tx 消费边：A 在 tx 中作为输入
      const g = group(dstTx);
      g.inputs.add(e.source);
      g.inputEdgeBySrc.set(e.source, e);
      g.inputEdge = g.inputEdge ?? e;
      g.txLayer = g.txLayer || e.tx_layer || "";
      g.isRemixer = g.isRemixer || !!e.is_remixer;
      g.isCrosschain = g.isCrosschain || !!e.is_crosschain;
      g.isStopped = g.isStopped || !!e.is_stopped_expansion;
      g.opReturnProtocol = g.opReturnProtocol ?? e.op_return_protocol ?? null;
    } else if (srcTx && dstIsAddr) {
      // tx→addr 输出边：B 从 tx 中收到输出
      const g = group(srcTx);
      const existing = g.outputs.get(e.target);
      if (existing) {
        // issue #9 「不得覆盖」：同一交易多个输出到同一地址时聚合 dst_value_btc，
        // 其余事实字段沿用首个输出边（金额展示以聚合后的 dst 为准）。
        g.outputs.set(e.target, {
          ...existing,
          dst_value_btc: (existing.dst_value_btc ?? 0) + (e.dst_value_btc ?? 0),
        });
      } else {
        g.outputs.set(e.target, e);
      }
      g.txLayer = g.txLayer || e.tx_layer || "";
      g.isRemixer = g.isRemixer || !!e.is_remixer;
      g.isCrosschain = g.isCrosschain || !!e.is_crosschain;
      g.isStopped = g.isStopped || !!e.is_stopped_expansion;
      g.opReturnProtocol = g.opReturnProtocol ?? e.op_return_protocol ?? null;
    } else {
      // 其它（地址→地址、摘要节点边、未知形态）：不参与地址流式变换
      orphans.push(e);
    }
  }
  return { txs, orphans };
}

function txAggregateAmounts(g: TxGroup): { totalInput: number; totalOutput: number } {
  // 输出总额：优先 addr→tx 边的 dst_value_btc（= 交易输出总额），否则合计输出边
  let totalOutput = 0;
  if (g.inputEdge) {
    totalOutput = normalizeAmount(g.inputEdge.dst_value_btc);
  }
  if (totalOutput <= 0) {
    for (const oe of g.outputs.values()) {
      totalOutput += normalizeAmount(oe.dst_value_btc);
    }
  }

  // 输入总额：value_ratio = dst_value / total_input → total_input = dst_value / value_ratio
  let totalInput = 0;
  for (const oe of g.outputs.values()) {
    const dst = normalizeAmount(oe.dst_value_btc);
    const ratio = normalizeAmount(oe.value_ratio);
    if (dst > 0 && ratio > 0) {
      totalInput = dst / ratio;
      break;
    }
  }
  if (totalInput <= 0) {
    // 回退：用输出额合计近似（忽略手续费）
    totalInput = totalOutput;
  }
  return { totalInput, totalOutput };
}

export function toAddressFlow(subgraph: {
  nodes?: GraphNode[];
  edges?: GraphEdge[];
}): AddressFlow {
  const nodes = subgraph.nodes ?? [];
  const edges = subgraph.edges ?? [];

  // address 节点 id → 节点（保留原属性供 AddressNode/详情栏复用）
  const addrNodeById = new Map<string, GraphNode>();
  for (const n of nodes) {
    if (isAddressId(n.id)) {
      addrNodeById.set(n.id, { ...n, kind: "address", first_layer: n.first_layer ?? 0 });
    }
  }

  const { txs } = classifyAndGroup(edges);

  const flowEdges: FlowEdge[] = [];
  const flowNodes: GraphNode[] = [];
  const seenEdgeId = new Set<string>();
  const seenTerminalId = new Set<string>();

  for (const g of txs.values()) {
    // --- 终止交易（issue #9）：无输出地址 + is_stopped → terminalTransaction 节点 ---
    if (g.outputs.size === 0) {
      if (g.isStopped) {
        const terminalInputs = Array.from(g.inputs).filter((a) => addrNodeById.has(a));
        if (terminalInputs.length === 0) continue;
        const txNodeId = `tx:${g.txid}`;
        const stopReason = stopReasonOf(g);
        const { totalInput } = txAggregateAmounts(g);
        const inputLayer = Math.max(
          0,
          ...terminalInputs.map((a) => addrNodeById.get(a)?.first_layer ?? 0),
        );
        if (!seenTerminalId.has(txNodeId)) {
          seenTerminalId.add(txNodeId);
          flowNodes.push({
            id: txNodeId,
            kind: "terminalTransaction",
            label: g.txid.slice(0, 16),
            txid: g.txid,
            stop_reason: stopReason,
            protocol: g.opReturnProtocol,
            op_return_protocol: g.opReturnProtocol,
            first_layer: inputLayer + 1, // 终止节点位于其输入地址之后（流向下游）
            is_stopped_expansion: true,
            is_remixer: g.isRemixer,
            is_crosschain: g.isCrosschain,
          } satisfies TerminalNode);
        }
        for (const srcId of terminalInputs) {
          const edgeId = `flow:${srcId}->${txNodeId}:${g.txid}`;
          if (seenEdgeId.has(edgeId)) continue;
          seenEdgeId.add(edgeId);
          const inEdge = g.inputEdgeBySrc.get(srcId);
          flowEdges.push({
            id: edgeId,
            source: srcId,
            target: txNodeId,
            txid: g.txid,
            tx_layer: g.txLayer || undefined,
            in_btc: totalInput,
            src_btc: inEdge?.src_value_btc ?? totalInput,
            out_btc: 0,
            is_stopped_expansion: true,
            is_remixer: g.isRemixer,
            is_crosschain: g.isCrosschain,
            op_return_protocol: g.opReturnProtocol ?? null,
            total_num_inputs: g.inputs.size,
            total_num_outputs: 0,
          });
        }
      }
      continue;
    }

    // --- 普通交易（有输出地址）→ A→B 流边，金额语义保持 FE-14/15 不变 ---
    const { totalInput } = txAggregateAmounts(g);
    for (const srcId of g.inputs) {
      if (!addrNodeById.has(srcId)) continue;
      const inEdge = g.inputEdgeBySrc.get(srcId);
      for (const [dstId, outEdge] of g.outputs) {
        if (!addrNodeById.has(dstId)) continue;
        const edgeId = `flow:${srcId}->${dstId}:${g.txid}`;
        if (seenEdgeId.has(edgeId)) continue;
        seenEdgeId.add(edgeId);
        flowEdges.push({
          id: edgeId,
          source: srcId,
          target: dstId,
          txid: g.txid,
          tx_layer: g.txLayer || undefined,
          value_ratio: normalizeAmount(outEdge.value_ratio) || undefined,
          dst_value_btc: normalizeAmount(outEdge.dst_value_btc) || undefined,
          in_btc: totalInput,
          src_btc: inEdge?.src_value_btc ?? totalInput,
          out_btc: normalizeAmount(outEdge.dst_value_btc),
          is_stopped_expansion: g.isStopped,
          is_remixer: g.isRemixer,
          is_crosschain: g.isCrosschain,
          op_return_protocol: g.opReturnProtocol ?? null,
          // 交易边的端点逻辑仍然是有向的，携带输入/输出数量便于悬浮展示
          total_num_inputs: g.inputs.size,
          total_num_outputs: g.outputs.size,
        });
      }
    }
  }

  // 排序保证输出稳定：节点（ address + terminalTransaction）按 id，边按 id
  const flowAllNodes = [...addrNodeById.values(), ...flowNodes].sort((a, b) =>
    a.id.localeCompare(b.id),
  );
  flowEdges.sort((a, b) => a.id.localeCompare(b.id));
  return { nodes: flowAllNodes, edges: flowEdges };
}

/**
 * 把 canonical 证据 ID（addr:/tx:/edge:）解析为本视图要高亮的「节点 id + 边 id」集合。
 *
 * - addr:<A> → 节点 addr:<A>（地址节点 id 在流式视图中保持不变）。
 * - tx:<T>  → 该交易的所有 A→B 边 + 其两端地址节点。
 * - edge:<S>-><D> → 尽力匹配（解析出 txid 或端点地址），命中则高亮对应边。
 */
export function resolveHighlightIds(
  highlightIds: Iterable<string>,
  flow: AddressFlow,
): Set<string> {
  const out = new Set<string>();
  const nodeIds = new Set(flow.nodes.map((n) => n.id));

  for (const id of highlightIds) {
    if (id.startsWith(ADDR_PREFIX) && nodeIds.has(id)) {
      out.add(id); // 地址节点
      // 与画布点击物证节点同语义：高亮相邻边与邻接节点（完整链路）
      for (const e of flow.edges) {
        if (e.source === id || e.target === id) {
          out.add(e.id);
          out.add(e.source);
          out.add(e.target);
        }
      }
    } else if (id.startsWith(TX_PREFIX)) {
      const txid = txidOfNodeId(id);
      if (txid) {
        // terminalTransaction 节点 id 即 tx:<txid>：直接高亮该终止节点（issue #9）
        const txNodeId = `tx:${txid}`;
        if (nodeIds.has(txNodeId)) out.add(txNodeId);
        for (const e of flow.edges) {
          if (e.txid === txid) {
            out.add(e.id);
            out.add(e.source);
            out.add(e.target);
          }
        }
      }
    } else if (id.startsWith("edge:")) {
      // 从 canonical 边 id 反查：形如 edge:addr:A->tx:T 或 edge:tx:T->addr:B
      const body = id.slice("edge:".length);
      const txMatch = body.match(/tx:([^:>]+)/);
      const addrMatch = body.match(/addr:([^>\]]+)/g);
      if (txMatch) {
        const txid = txMatch[1];
        for (const e of flow.edges) {
          if (e.txid === txid && nodeIds.has(e.source) && nodeIds.has(e.target)) {
            out.add(e.id);
          }
        }
      } else if (addrMatch) {
        // 无 txid 的边（理论上 canonical 边必带 tx 端点）：按端点地址匹配
        const addrs = new Set(addrMatch.map((a) => a));
        for (const e of flow.edges) {
          if (addrs.has(e.source) && addrs.has(e.target)) {
            out.add(e.id);
          }
        }
      }
    }
  }
  return out;
}
