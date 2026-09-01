"use client";
// 图谱画布（frontend-spec §4 · 地址流式展示）：React Flow 自定义节点 + 分层布局。
//
// 展示模型变更（address-as-node / tx-as-edge）：画布**不直接渲染后端返回的
// canonical 子图**（其中 tx 也是节点），而是经 toAddressFlow() 变换为
// 「仅 address 节点 + 交易作为有向边」的流式图，每条边标注入边/出边交易金额。
// canonical 证据 ID（addr:/tx:/edge:）经 resolveHighlightIds() 映射到本视图
// 的节点/边 ID，供 FE-26 证据高亮沿用。
//
// 高亮实现（FE-26）：highlightIds 通过 Context 下发，自定义节点组件
// 内部订阅——高亮变化不重建 nodes 数组（引用稳定），只有受影响节点重渲染。
// 布局：按 first_layer 左→右分层（BFS 深度即横轴），无额外依赖；
// dagre 属 P2 打磨项。只读画布：nodesConnectable/nodesDraggable 均关闭。
import { createContext, memo, useContext, useMemo } from "react";
import ReactFlow, {
  Background,
  Controls,
  MarkerType,
  type Edge as RFEdge,
  type Node as RFNode,
} from "reactflow";
import "reactflow/dist/style.css";

import type { GraphEdge, GraphNode } from "@/store/analysis";
import {
  resolveHighlightIds,
  toAddressFlow,
  type AddressFlow,
  type FlowEdge,
} from "@/lib/address-flow";

const LAYER_SPACING_X = 280;
const NODE_SPACING_Y = 120;

const HighlightContext = createContext<Set<string>>(new Set());

interface NodeData {
  raw: GraphNode;
  onSelect: (id: string) => void;
}

function isHighlighted(id: string): string {
  return useContext(HighlightContext).has(id) ? " pt-highlight" : "";
}

const AddressNode = memo(function AddressNode({
  data,
}: {
  data: NodeData;
}) {
  const hlClass = isHighlighted(data.raw.id);
  const mixer = data.raw.direct_related_to_lazarus;
  return (
    <div
      role="button"
      aria-label={`address ${data.raw.label ?? data.raw.id}`}
      onClick={() => data.onSelect(data.raw.id)}
      className={`rounded-lg border px-3 py-2 text-xs shadow-sm cursor-pointer transition-shadow ${
        mixer
          ? "border-red-500 border-2 bg-white" // FE-13：混币器红框 + ⚠ 双编码
          : "border-slate-300 bg-white"
      }${hlClass}`}
      style={{ minWidth: 150 }}
    >
      <div className="flex items-center gap-1 font-mono text-[10px] text-slate-700">
        {mixer && <span aria-label="mixer warning">⚠</span>}
        {(data.raw.label ?? data.raw.id).slice(0, 18)}
        {((data.raw.label ?? "").length > 18 || data.raw.id.length > 18) &&
          (data.raw.label ?? data.raw.id).length > 18 &&
          "…"}
      </div>
      <div className="text-[10px] text-slate-400">
        {data.raw.first_layer !== undefined ? `L${data.raw.first_layer}` : ""}
        {data.raw.total_received_btc !== undefined &&
          ` · Σ${data.raw.total_received_btc.toFixed(3)} BTC`}
      </div>
    </div>
  );
});

const nodeTypes = { address: AddressNode };

function flowAmountLabel(e: FlowEdge): string {
  const outAmt = e.in_btc ?? 0; // 出边：资金离开源地址进入交易（源侧流出额）
  const inAmt = e.out_btc ?? 0; // 入边：资金进入目标地址（目标侧接收额）
  if (inAmt > 0 || outAmt > 0) {
    // 沿 A→B 流向：出(离开A) → 入(进入B)
    return `出${outAmt.toFixed(4)}→入${inAmt.toFixed(4)}`;
  }
  if (e.value_ratio !== undefined) return `${Math.round(e.value_ratio * 100)}%`;
  return "";
}

function buildFlow(
  flow: AddressFlow,
  maxLayer: number | null,
  onSelect: (id: string) => void,
): { rfNodes: RFNode[]; rfEdges: RFEdge[] } {
  const visible = flow.nodes.filter(
    (n) => maxLayer === null || (n.first_layer ?? 0) <= maxLayer,
  );
  const visibleIds = new Set(visible.map((n) => n.id));

  // 分层布局：同层节点纵向均排，层序即横轴
  const byLayer = new Map<number, GraphNode[]>();
  visible.forEach((n) => {
    const layer = n.first_layer ?? 0;
    if (!byLayer.has(layer)) byLayer.set(layer, []);
    byLayer.get(layer)!.push(n);
  });

  const rfNodes: RFNode[] = [];
  byLayer.forEach((layerNodes, layer) => {
    const height = layerNodes.length * NODE_SPACING_Y;
    layerNodes.forEach((n, i) => {
      rfNodes.push({
        id: n.id,
        type: "address", // 地址流式视图只有 address 节点
        position: {
          x: layer * LAYER_SPACING_X,
          y: i * NODE_SPACING_Y - height / 2,
        },
        data: { raw: n, onSelect } satisfies NodeData,
        draggable: false,
        connectable: false,
      });
    });
  });

  const rfEdges: RFEdge[] = flow.edges
    .filter((e) => visibleIds.has(e.source) && visibleIds.has(e.target))
    .map((e) => {
      const flags: string[] = [];
      if (e.is_crosschain) flags.push(e.op_return_protocol ?? "crosschain"); // FE-15
      if (e.is_stopped_expansion) flags.push("stopped"); // FE-14
      const amountLabel = flowAmountLabel(e);
      const label = [amountLabel, flags.join("·")].filter(Boolean).join(" · ");
      return {
        id: e.id,
        source: e.source,
        target: e.target,
        animated: false,
        label: label || undefined,
        labelStyle: { fontSize: 9 },
        style: e.is_stopped_expansion
          ? { strokeDasharray: "6 4", stroke: "#94a3b8" }
          : e.is_remixer
            ? { stroke: "#ef4444" }
            : undefined,
        markerEnd: { type: MarkerType.ArrowClosed, width: 12, height: 12 },
      } satisfies RFEdge;
    });

  return { rfNodes, rfEdges };
}

export interface GraphCanvasProps {
  subgraph: { nodes: GraphNode[]; edges: GraphEdge[] };
  highlightIds: Set<string>;
  maxLayer?: number | null; // FE-17 跳数过滤；null=全部
  onNodeClick?: (node: GraphNode) => void;
}

export default function GraphCanvas({
  subgraph,
  highlightIds,
  maxLayer = null,
  onNodeClick,
}: GraphCanvasProps) {
  const handleSelect = useMemo(
    () => (id: string) => {
      const node = subgraph.nodes.find((n) => n.id === id);
      if (node && onNodeClick) onNodeClick(node);
    },
    [subgraph, onNodeClick],
  );

  // 展示变换：canonical 子图 → 地址节点 + 交易边（含入/出边金额）。
  // 不直接渲染生成的子图；仅子图或过滤变化时重建（FE-26 高亮不参与）。
  const flow = useMemo(() => toAddressFlow(subgraph), [subgraph]);
  const { rfNodes, rfEdges } = useMemo(
    () => buildFlow(flow, maxLayer ?? null, handleSelect),
    [flow, maxLayer, handleSelect],
  );

  // 证据高亮：canonical id（addr:/tx:/edge:）→ 本视图节点/边 id
  const displayHighlightIds = useMemo(
    () => resolveHighlightIds(highlightIds, flow),
    [highlightIds, flow],
  );

  return (
    <HighlightContext.Provider value={displayHighlightIds}>
      <div style={{ width: "100%", height: "100%" }} data-testid="graph-canvas">
        <ReactFlow
          nodes={rfNodes}
          edges={rfEdges}
          nodeTypes={nodeTypes}
          onlyRenderVisibleElements // 大图虚拟化（200 节点性能预算）
          nodesConnectable={false}
          nodesDraggable={false}
          elementsSelectable
          fitView
          proOptions={{ hideAttribution: true }}
        >
          <Background gap={24} />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
    </HighlightContext.Provider>
  );
}
