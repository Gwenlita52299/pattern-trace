"use client";
// 图谱画布（frontend-spec §4 · 地址流式展示）：React Flow 自定义节点 + 分层布局。
//
// 展示模型变更（address-as-node / tx-as-edge）：画布**不直接渲染后端返回的
// canonical 子图**（其中 tx 也是节点），而是经 toAddressFlow() 变换为
// 「仅 address 节点 + 交易作为有向边」的流式图，每条边标注入边/出边交易金额。
// canonical 证据 ID（addr:/tx:/edge:）经 resolveHighlightIds() 映射到本视图
// 的节点/边 ID，供 FE-26 证据高亮沿用。
//
// 交互（本轮新增）：
//   - 节点级下游「展开 / 收回」：有下游（出边）的节点右上角出现 +/− 圆钮，
//     点击折叠/展开该节点下游（可达）的所有节点与边。
//   - 节点自由拖动：nodesDraggable 开启；拖动位置写入 posMap（受控节点+onNodesChange），
//     折叠/展开或深度过滤时已见节点沿用自己的位置（不跳动），仅新节点取布局位置。
//
// 高亮实现（FE-26）：highlightIds 通过 Context 下发，自定义节点组件
// 内部订阅——高亮变化不重建 nodes 数组（引用稳定），只有受影响节点重渲染。
// 布局：按 first_layer 左→右分层（BFS 深度即横轴），无额外依赖；
// dagre 属 P2 打磨项。
import { createContext, memo, useCallback, useContext, useEffect, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  MarkerType,
  type Edge as RFEdge,
  type Node as RFNode,
  type OnNodesChange,
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
  /** 该节点当前是否处于「已收回」状态（下游隐藏）。 */
  collapsed: boolean;
  /** 该节点是否有下游节点（用于决定是否显示展开/收回圆钮）。 */
  hasChildren: boolean;
  onToggle: (id: string) => void;
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
      className={`relative rounded-lg border px-3 py-2 text-xs shadow-sm cursor-pointer transition-shadow ${
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
      {data.hasChildren && (
        <button
          type="button"
          // nodrag：React Flow 在此按下时不要启动节点拖动；stopPropagation 防止冒泡到
          // 节点选中（React Flow onNodeClick）。
          className="nodrag absolute -right-2 -top-2 flex h-5 w-5 items-center justify-center rounded-full border border-slate-300 bg-white text-[11px] font-bold leading-none text-slate-600 shadow-sm hover:bg-slate-100"
          onClick={(e) => {
            e.stopPropagation();
            e.preventDefault();
            data.onToggle(data.raw.id);
          }}
          aria-label={data.collapsed ? "展开下游" : "收起下游"}
          title={data.collapsed ? "展开下游" : "收起下游"}
        >
          {data.collapsed ? "+" : "−"}
        </button>
      )}
    </div>
  );
});

/**
 * 终止交易节点（issue #9）：CoinJoin / Crosschain / Out of Range 等停止扩展的交易
 * 在地址流式视图中渲染为独立 terminal 节点，展示停止原因与协议。
 */
const TerminalNode = memo(function TerminalNode({ data }: { data: NodeData }) {
  const raw = data.raw;
  const hlClass = isHighlighted(raw.id);
  const stopReason = (raw.stop_reason ?? "out_of_range") as string;
  const protocolLabel = raw.is_crosschain && raw.protocol ? ` · ${raw.protocol}` : "";
  const isMixer = raw.is_remixer;
  return (
    <div
      role="button"
      aria-label={`terminal transaction ${raw.txid ?? raw.id}`}
      className={`relative rounded-lg border px-3 py-2 text-xs shadow-sm cursor-pointer transition-shadow ${
        isMixer
          ? "border-purple-500 border-2 bg-purple-50"
          : raw.is_crosschain
            ? "border-blue-500 border-2 bg-blue-50"
            : "border-slate-400 border-2 bg-slate-50"
      }${hlClass}`}
      style={{ minWidth: 150 }}
    >
      <div className="flex items-center gap-1 font-mono text-[10px] text-slate-700">
        {isMixer && <span aria-label="mixer">♻</span>}
        terminal tx:{stopReason}
      </div>
      <div className="break-all font-mono text-[10px] text-slate-600">
        {(raw.txid ?? raw.id).slice(0, 18)}
        {protocolLabel}
      </div>
    </div>
  );
});

const nodeTypes = { address: AddressNode, terminalTransaction: TerminalNode };

function flowAmountLabel(e: FlowEdge): string {
  const outAmt = e.in_btc ?? 0; // 出边：资金离开源地址进入交易（源侧流出额）
  const inAmt = e.out_btc ?? 0; // 入边：资金进入目标地址（目标侧接收额）
  // 终止交易边（target=tx:<txid>，issue #9）：资金进入停止扩展的交易，无输出地址，
  // 只标注源侧流出额，避免出现误导性的「→入0」。
  if (e.target.startsWith("tx:")) {
    return outAmt > 0 ? `出${outAmt.toFixed(4)}` : "";
  }
  if (inAmt > 0 || outAmt > 0) {
    // 沿 A→B 流向：出(离开A) → 入(进入B)
    return `出${outAmt.toFixed(4)}→入${inAmt.toFixed(4)}`;
  }
  if (e.value_ratio !== undefined) return `${Math.round(e.value_ratio * 100)}%`;
  return "";
}

/**
 * 从 id 出发，沿有向边可达的所有**严格下游**节点（不含 id 本身）。
 *
 * 地址流虽近似有向无环，但自转账/回流地址可能形成自环或环路（A→A、A→B→A）。
 * 为让折叠操作只隐藏严格下游、不隐藏触发折叠的节点本身，同时避免环导致死循环：
 *   - 用独立于结果的 `visited` 集合记录已访问节点，并**预先加入起始 id**；
 *     这样环路（或自环）回到起点时不会再把 id 计入结果（descendants）。
 */
function computeDescendants(flow: AddressFlow, id: string): Set<string> {
  const children = new Map<string, string[]>();
  for (const e of flow.edges) {
    const arr = children.get(e.source);
    if (arr) arr.push(e.target);
    else children.set(e.source, [e.target]);
  }
  const out = new Set<string>();
  const visited = new Set<string>([id]); // 起始节点视为已访问：自环/环路不重新计入
  const stack = [id];
  while (stack.length) {
    const cur = stack.pop()!;
    for (const c of children.get(cur) ?? []) {
      if (!visited.has(c)) {
        visited.add(c);
        out.add(c);
        stack.push(c);
      }
    }
  }
  return out;
}

/**
 * 所有「已收回」节点的下游节点的并集——这些节点当前应隐藏。
 *
 * 折叠节点自身必须保持可见（以便用户再次展开并恢复下游），即使它们互相位于
 * 对方的下游中（自环/环路 A→B→A、或多个节点嵌套折叠）。因此从隐藏集合中剔除
 * 所有已折叠节点：隐藏的是「下游」而非「折叠的锚点节点」。
 */
function hiddenSet(flow: AddressFlow, collapsed: Set<string>): Set<string> {
  const hidden = new Set<string>();
  for (const id of collapsed) {
    for (const d of computeDescendants(flow, id)) hidden.add(d);
  }
  for (const c of collapsed) hidden.delete(c);
  return hidden;
}

function buildFlow(
  flow: AddressFlow,
  maxLayer: number | null,
  collapsed: Set<string>,
  posMap: Record<string, { x: number; y: number }>,
  onToggle: (id: string) => void,
): {
  rfNodes: RFNode[];
  rfEdges: RFEdge[];
  layoutPos: Record<string, { x: number; y: number }>;
} {
  const hidden = hiddenSet(flow, collapsed);
  const visible = flow.nodes.filter(
    (n) =>
      (maxLayer === null || (n.first_layer ?? 0) <= maxLayer) &&
      !hidden.has(n.id),
  );
  const visibleIds = new Set(visible.map((n) => n.id));

  // 节点是否有下游（在层过滤范围之内）：决定是否显示展开/收回圆钮
  const childByLayer = new Set<string>();
  const targetLayer = new Map<string, number>();
  for (const n of flow.nodes) targetLayer.set(n.id, n.first_layer ?? 0);
  for (const e of flow.edges) {
    const tLayer = targetLayer.get(e.target) ?? 0;
    if (maxLayer === null || tLayer <= maxLayer) childByLayer.add(e.source);
  }

  // 分层布局：同层节点纵向均排，层序即横轴
  const byLayer = new Map<number, GraphNode[]>();
  visible.forEach((n) => {
    const layer = n.first_layer ?? 0;
    if (!byLayer.has(layer)) byLayer.set(layer, []);
    byLayer.get(layer)!.push(n);
  });

  const rfNodes: RFNode[] = [];
  const layoutPos: Record<string, { x: number; y: number }> = {};
  byLayer.forEach((layerNodes, layer) => {
    const height = layerNodes.length * NODE_SPACING_Y;
    layerNodes.forEach((n, i) => {
      const lp = {
        x: layer * LAYER_SPACING_X,
        y: i * NODE_SPACING_Y - height / 2,
      };
      layoutPos[n.id] = lp;
      rfNodes.push({
        id: n.id,
        type: n.kind === "terminalTransaction" ? "terminalTransaction" : "address",
        position: posMap[n.id] ?? lp, // 已见节点沿用已有/拖拽位置，新节点取布局位置
        data: {
          raw: n,
          collapsed: collapsed.has(n.id),
          hasChildren: childByLayer.has(n.id),
          onToggle,
        } satisfies NodeData,
        draggable: true, // 自由拖动节点
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

  return { rfNodes, rfEdges, layoutPos };
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
  // 已收回（折叠）的下游节点集合，及其稳定位置缓存（供拖动 & 折叠不跳动）
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [posMap, setPosMap] = useState<Record<string, { x: number; y: number }>>({});

  const handleSelect = useCallback(
    (id: string) => {
      const node = subgraph.nodes.find((n) => n.id === id);
      if (node && onNodeClick) onNodeClick(node);
    },
    [subgraph, onNodeClick],
  );

  const handleToggle = useCallback((id: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // 受控节点拖动：仅保存 position 类变更到 posMap，供受控渲染跟随。
  const handleNodesChange: OnNodesChange = useCallback((changes) => {
    setPosMap((prev) => {
      let next: Record<string, { x: number; y: number }> | null = null;
      for (const ch of changes) {
        if (ch.type === "position" && ch.position) {
          if (!next) next = { ...prev };
          next[ch.id] = { x: ch.position.x, y: ch.position.y };
        }
      }
      return next ?? prev;
    });
  }, []);

  // 展示变换：canonical 子图 → 地址节点 + 交易边（含入/出边金额）。
  const flow = useMemo(() => toAddressFlow(subgraph), [subgraph]);
  const { rfNodes, rfEdges, layoutPos } = useMemo(
    () => buildFlow(flow, maxLayer ?? null, collapsed, posMap, handleToggle),
    [flow, maxLayer, collapsed, posMap, handleToggle],
  );

  // 播种稳定位置：把每次首次出现的节点布局位置记入 posMap，折叠/过滤不跳动；
  // 后续仅用户拖动（onNodesChange）会改写对应位。
  useEffect(() => {
    setPosMap((prev) => {
      let next: Record<string, { x: number; y: number }> | null = null;
      for (const id in layoutPos) {
        if (!(id in prev)) {
          if (!next) next = { ...prev };
          next[id] = layoutPos[id];
        }
      }
      return next ?? prev;
    });
  }, [layoutPos]);

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
          onNodesChange={handleNodesChange}
          nodeTypes={nodeTypes}
          onNodeClick={(_evt, node) => handleSelect(node.id)}
          onlyRenderVisibleElements // 大图虚拟化（200 节点性能预算）
          nodesConnectable={false}
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
