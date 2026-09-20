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
import { createContext, memo, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  type Edge as RFEdge,
  type Node as RFNode,
  type OnNodesChange,
  type ReactFlowInstance,
} from "reactflow";
import "reactflow/dist/style.css";

import type { GraphEdge, GraphNode } from "@/store/analysis";
import {
  computeDescendants,
  hiddenSet,
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

// hook 命名（rules-of-hooks）：内部 useContext，调用点须在组件体内无条件调用
function useHighlightMark(id: string): string {
  return useContext(HighlightContext).has(id) ? " pt-highlight" : "";
}

// 拖动时 buildFlow 每帧重建所有节点的 data 对象；默认浅比较会令全部节点
// 重渲染 → Handle 重测量 → handleBounds 每帧失效 → 相连的边被 React Flow
// 卸载重挂载（视觉频闪）。按字段比较：位置变化不经过此组件（由 React Flow
// wrapper 的 transform 承担），内容不变就不重渲染。
function nodeDataEqual(
  prev: { data: NodeData },
  next: { data: NodeData },
): boolean {
  return (
    prev.data.raw === next.data.raw &&
    prev.data.collapsed === next.data.collapsed &&
    prev.data.hasChildren === next.data.hasChildren
  );
}

const AddressNode = memo(function AddressNode({
  data,
}: {
  data: NodeData;
}) {
  const hlClass = useHighlightMark(data.raw.id);
  const mixer = data.raw.direct_related_to_lazarus;
  return (
    <div
      role="button"
      aria-label={`address ${data.raw.label ?? data.raw.id}`}
      className={`relative rounded-lg border px-3 py-2 text-xs cursor-pointer transition-shadow ${
        mixer
          ? "border-2 border-pt-amber bg-[#171a14]" // 调性规范：混币器=琥珀证据框 + ⚠ 双编码（不做红色警报）
          : "border-[#2a3340] bg-pt-panel"
      }${hlClass}`}
      style={{ minWidth: 150 }}
    >
      <div className="flex items-center gap-1 font-mono text-[10px] text-pt-ink">
        {mixer && <span aria-label="mixer warning" className="text-pt-amber">⚠</span>}
        {(data.raw.label ?? data.raw.id).slice(0, 18)}
        {((data.raw.label ?? "").length > 18 || data.raw.id.length > 18) &&
          (data.raw.label ?? data.raw.id).length > 18 &&
          "…"}
      </div>
      <div className="font-mono text-[10px] text-pt-muted">
        {data.raw.first_layer !== undefined ? `L${data.raw.first_layer}` : ""}
        {data.raw.total_received_btc !== undefined &&
          ` · Σ${data.raw.total_received_btc.toFixed(3)} BTC`}
      </div>
      {data.hasChildren && (
        <button
          type="button"
          // nodrag：React Flow 在此按下时不要启动节点拖动；stopPropagation 防止冒泡到
          // 节点选中（React Flow onNodeClick）。
          className="nodrag absolute -right-2 -top-2 flex h-5 w-5 items-center justify-center rounded-full border border-[#2a3340] bg-pt-panel-2 text-[11px] font-bold leading-none text-pt-muted hover:border-pt-amber hover:text-pt-amber-hi"
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
      {/* 边连接锚点：不可见但必须存在，否则 React Flow 丢弃所有边 */}
      <Handle type="target" position={Position.Left} isConnectable={false}
        className="!border-none !bg-transparent" />
      <Handle type="source" position={Position.Right} isConnectable={false}
        className="!border-none !bg-transparent" />
    </div>
  );
}, nodeDataEqual);

/**
 * 证据边取色（调性规范：琥珀 = 证据，唯一强调色；色 + 图标双编码）。
 *
 * 优先级：物证高亮 > 混币(remixer) > 跨链(crosschain) > 其他停止 > 默认。
 * 跨链（如 thorchain）是最终可疑链路的高危证据点——必须与普通
 * out_of_range 停止边（灰虚线）区分（issue: 前端未标记 thorchain 跨链 tx）。
 */
export function edgeStroke(
  e: { is_remixer?: boolean; is_crosschain?: boolean;
       is_stopped_expansion?: boolean },
  highlighted: boolean,
): string {
  if (highlighted) return "#f0b429";
  if (e.is_remixer) return "#f0b429";
  if (e.is_crosschain) return "#f0b429";
  if (e.is_stopped_expansion) return "#3a4250";
  return "#2a3340";
}

/** 虚线仅用于「非证据的停止扩展」边；证据边（混币/跨链）保持实线。 */
export function edgeDashed(e: {
  is_remixer?: boolean; is_crosschain?: boolean;
  is_stopped_expansion?: boolean;
}): boolean {
  return !!e.is_stopped_expansion && !e.is_remixer && !e.is_crosschain;
}

/** 终止交易节点语气：mixer / crosschain 均为证据点（琥珀），其余普通停止。 */
export function terminalTone(
  isRemixer: boolean | undefined,
  isCrosschain: boolean | undefined,
): "mixer" | "crosschain" | "stopped" {
  if (isRemixer) return "mixer";
  if (isCrosschain) return "crosschain";
  return "stopped";
}

/**
 * 终止交易节点（issue #9）：CoinJoin / Crosschain / Out of Range 等停止扩展的交易
 * 在地址流式视图中渲染为独立 terminal 节点，展示停止原因与协议。
 */
const TerminalNode = memo(function TerminalNode({ data }: { data: NodeData }) {
  const raw = data.raw;
  const hlClass = useHighlightMark(raw.id);
  const stopReason = (raw.stop_reason ?? "out_of_range") as string;
  const protocol = raw.is_crosschain ? (raw.protocol ?? "crosschain") : null;
  const tone = terminalTone(raw.is_remixer, raw.is_crosschain);
  const isEvidence = tone !== "stopped";
  const icon = tone === "mixer" ? "♻" : tone === "crosschain" ? "⛓" : null;
  const ariaKind = tone === "mixer" ? "mixer coinjoin"
    : tone === "crosschain" ? `crosschain ${protocol}` : "stop";
  return (
    <div
      role="button"
      aria-label={`terminal transaction ${raw.txid ?? raw.id} (${ariaKind})`}
      className={`relative rounded-lg border px-3 py-2 text-xs cursor-pointer transition-shadow ${
        isEvidence
          ? "border-2 border-pt-amber bg-[#171a14]" // 证据点（混币/跨链）：琥珀证据框
          : "border-2 border-dashed border-[#3a4250] bg-pt-panel-2"
      }${hlClass}`}
      style={{ minWidth: 150 }}
    >
      <div className="flex items-center gap-1 font-mono text-[10px] text-pt-muted">
        {icon && (
          <span aria-hidden className="text-pt-amber">{icon}</span>
        )}
        {tone === "crosschain" ? (
          <span
            className="rounded border border-pt-amber/50 bg-pt-amber/10 px-1 py-0.5 font-semibold text-pt-amber-hi"
            data-testid="crosschain-badge"
          >
            跨链 {protocol}
          </span>
        ) : (
          <span>terminal tx:{stopReason}</span>
        )}
      </div>
      <div className="break-all font-mono text-[10px] text-pt-muted">
        {(raw.txid ?? raw.id).slice(0, 18)}
        {tone === "mixer" && <span className="text-pt-amber"> · coinjoin</span>}
      </div>
      {/* 终止交易是流向终点，只需 target 锚点 */}
      <Handle type="target" position={Position.Left} isConnectable={false}
        className="!border-none !bg-transparent" />
    </div>
  );
}, nodeDataEqual);

const nodeTypes = { address: AddressNode, terminalTransaction: TerminalNode };

function flowAmountLabel(e: FlowEdge): string {
  // 「出」侧用 src_btc（该源地址本人消费的 UTXO 金额，与兄弟节点排序同口径）；
  // 旧快照无此字段时回退 in_btc（整笔交易输入总额）
  const outAmt = e.src_btc ?? e.in_btc ?? 0;
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

function buildFlow(
  flow: AddressFlow,
  maxLayer: number | null,
  collapsed: Set<string>,
  posMap: Record<string, { x: number; y: number }>,
  dimMap: Record<string, { width: number; height: number }>,
  onToggle: (id: string) => void,
): {
  rfNodes: RFNode[];
  layoutPos: Record<string, { x: number; y: number }>;
} {
  const hidden = hiddenSet(flow, collapsed);
  const visible = flow.nodes.filter(
    (n) =>
      (maxLayer === null || (n.first_layer ?? 0) <= maxLayer) &&
      !hidden.has(n.id),
  );

  // 节点是否有下游（在层过滤范围之内）：决定是否显示展开/收回圆钮
  const childByLayer = new Set<string>();
  const targetLayer = new Map<string, number>();
  for (const n of flow.nodes) targetLayer.set(n.id, n.first_layer ?? 0);
  for (const e of flow.edges) {
    const tLayer = targetLayer.get(e.target) ?? 0;
    if (maxLayer === null || tLayer <= maxLayer) childByLayer.add(e.source);
  }

  // 分层布局：列基准 = 节点的 first_layer（终端交易 = 输入地址层 + 1，即交易
  // 发生的跳数）。列内排序（两级）：
  //   1. 父节点行序——同一父节点扇出的子节点保持相邻，子树不交叉；
  //   2. 同父的兄弟节点按「出金额」降序——即该父节点流向各子节点的金额
  //      （src_value_btc，源地址在该交易中的消费额），大额在上，终端交易不例外。
  const inAmount = new Map<string, number>(); // 节点收到的最大流入金额
  const primaryParent = new Map<string, string>(); // 金额最大的入边的源节点
  for (const e of flow.edges) {
    // 出金额 = 源侧消费金额（src_value_btc）；旧快照无此字段时回退 dst_value
    const amt = e.src_btc > 0 ? e.src_btc : (e.dst_value_btc ?? e.out_btc ?? 0);
    if ((inAmount.get(e.target) ?? -1) < amt) {
      inAmount.set(e.target, amt);
      primaryParent.set(e.target, e.source);
    }
  }

  const byLayer = new Map<number, GraphNode[]>();
  visible.forEach((n) => {
    const layer = n.first_layer ?? 0;
    if (!byLayer.has(layer)) byLayer.set(layer, []);
    byLayer.get(layer)!.push(n);
  });

  const nodeRank = new Map<string, number>(); // 全局行序（跨层比较父节点用）
  let rankCursor = 0;
  const sortedLayers = [...byLayer.keys()].sort((a, b) => a - b);
  for (const layer of sortedLayers) {
    const layerNodes = byLayer.get(layer)!;
    layerNodes.sort((a, b) => {
      // 父节点行序：无入边（种子）或父节点被过滤隐藏 → 视为 -1，排在该列最前
      const ra = primaryParent.has(a.id) ? nodeRank.get(primaryParent.get(a.id)!) ?? -1 : -1;
      const rb = primaryParent.has(b.id) ? nodeRank.get(primaryParent.get(b.id)!) ?? -1 : -1;
      if (ra !== rb) return ra - rb;
      const am = inAmount.get(a.id) ?? 0;
      const bm = inAmount.get(b.id) ?? 0;
      if (am !== bm) return bm - am; // 出金额降序：大额在上（终端交易不例外）
      return a.id.localeCompare(b.id);
    });
    layerNodes.forEach((n) => nodeRank.set(n.id, rankCursor++));
  }

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
        // 回填测量尺寸：React Flow 测得尺寸写入 store internals 的同时会派发
        // dimensions change；本组件重建 nodes 时若不回填，节点丢失尺寸 →
        // getNodeData 判定无效 → 相连的边被卸载重挂载（逐帧交替 = 拖动频闪）。
        // dims 存 ref，不触发额外渲染。
        width: dimMap[n.id]?.width,
        height: dimMap[n.id]?.height,
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

  return { rfNodes, layoutPos };
}

/**
 * 边的构建与节点位置（posMap）完全解耦：拖动时每帧重建 edges 数组会让
 * React Flow 重挂载所有边组件（实测 SVG 组逐帧销毁重建 → 视觉频闪）。
 * 边只依赖拓扑与可见性（flow / maxLayer / collapsed）。
 */
function buildEdges(
  flow: AddressFlow,
  maxLayer: number | null,
  collapsed: Set<string>,
  highlightIds: Set<string>,
): RFEdge[] {
  const hidden = hiddenSet(flow, collapsed);
  const visibleIds = new Set(
    flow.nodes
      .filter(
        (n) =>
          (maxLayer === null || (n.first_layer ?? 0) <= maxLayer) &&
          !hidden.has(n.id),
      )
      .map((n) => n.id),
  );

  const rfEdges: RFEdge[] = flow.edges
    .filter((e) => visibleIds.has(e.source) && visibleIds.has(e.target))
    .map((e) => {
      const flags: string[] = [];
      if (e.is_crosschain) flags.push(e.op_return_protocol ?? "crosschain"); // FE-15
      if (e.is_stopped_expansion) flags.push("stopped"); // FE-14
      const amountLabel = flowAmountLabel(e);
      const label = [amountLabel, flags.join("·")].filter(Boolean).join(" · ");
      // 高亮边（FE-26 / issue #39）：琥珀证据色 + 加粗；
      // 混币/跨链证据边琥珀实线，普通停止边灰虚线（edgeDashed）
      const hl = highlightIds.has(e.id);
      const stroke = edgeStroke(e, hl);
      return {
        id: e.id,
        source: e.source,
        target: e.target,
        animated: false,
        label: label || undefined,
        labelStyle: { fontSize: 9, fill: hl ? "#ffd166" : "#8b93a1" },
        labelBgStyle: { fill: "#141920" },
        style: {
          ...(edgeDashed(e) ? { strokeDasharray: "6 4" } : {}),
          stroke,
          ...(hl ? { strokeWidth: 2 } : {}),
        },
        markerEnd: { type: MarkerType.ArrowClosed, width: 12, height: 12, color: stroke },
      } satisfies RFEdge;
    });

  return rfEdges;
}

export interface GraphCanvasProps {
  subgraph: { nodes: GraphNode[]; edges: GraphEdge[] };
  highlightIds: Set<string>;
  /** 物证节点集合（canonical id，issue #39）：点击后高亮关联边与邻接节点 */
  evidenceIds?: Set<string>;
  maxLayer?: number | null; // FE-17 跳数过滤；null=全部
  onNodeClick?: (node: GraphNode) => void;
}

export default function GraphCanvas({
  subgraph,
  highlightIds,
  evidenceIds,
  maxLayer = null,
  onNodeClick,
}: GraphCanvasProps) {
  // 已收回（折叠）的下游节点集合，及其稳定位置缓存（供拖动 & 折叠不跳动）
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [posMap, setPosMap] = useState<Record<string, { x: number; y: number }>>({});
  // issue #39：点击物证节点产生的高亮（flow 空间 id：节点 + 关联边 + 邻接节点）
  const [evidenceFocus, setEvidenceFocus] = useState<{ id: string; ids: Set<string> } | null>(null);
  // 折叠/过滤后重新 fitView：fitView prop 只在首次渲染生效
  const rfRef = useRef<ReactFlowInstance | null>(null);
  useEffect(() => {
    // 等一拍让 React Flow 提交新的节点尺寸，否则按旧 bounds 拟合
    const t = setTimeout(
      () => rfRef.current?.fitView({ duration: 300, padding: 0.15 }),
      60,
    );
    return () => clearTimeout(t);
  }, [collapsed, maxLayer]);

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

  // 受控节点拖动：position 存 posMap（受控渲染跟随）；dimensions 存 dimMap
  // （回填到节点对象，防止重建时丢失尺寸导致边逐帧卸载重挂载，见 buildFlow）。
  const dimMapRef = useRef<Record<string, { width: number; height: number }>>({});
  const handleNodesChange: OnNodesChange = useCallback((changes) => {
    const dims = { ...dimMapRef.current };
    let dimsChanged = false;
    for (const ch of changes) {
      if (ch.type === "dimensions" && ch.dimensions) {
        dims[ch.id] = { width: ch.dimensions.width ?? 0, height: ch.dimensions.height ?? 0 };
        dimsChanged = true;
      }
    }
    if (dimsChanged) dimMapRef.current = dims;
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

  // issue #39：物证节点点击 → 高亮关联路径；再次点击取消
  const handleNodeClick = useCallback(
    (_evt: React.MouseEvent, node: RFNode) => {
      handleSelect(node.id);
      if (!evidenceIds?.has(node.id)) return;
      setEvidenceFocus((prev) => {
        if (prev?.id === node.id) return null;
        // 关联路径 = 该节点 + 所有相邻边 + 边的另一端节点
        const ids = new Set<string>([node.id]);
        for (const e of flow.edges) {
          if (e.source === node.id || e.target === node.id) {
            ids.add(e.id);
            ids.add(e.source);
            ids.add(e.target);
          }
        }
        return { id: node.id, ids };
      });
    },
    [handleSelect, evidenceIds, flow],
  );

  // 节点跟随 posMap（拖动每帧更新）；边与 posMap 解耦（见 buildEdges 注释），
  // 两者依赖不同，拖动不再触发边重建/重挂载。
  const { rfNodes, layoutPos } = useMemo(
    () => buildFlow(flow, maxLayer ?? null, collapsed, posMap, dimMapRef.current, handleToggle),
    [flow, maxLayer, collapsed, posMap, handleToggle],
  );
  // 证据高亮：canonical id（addr:/tx:/edge:）→ 本视图节点/边 id
  const resolvedHighlight = useMemo(
    () => resolveHighlightIds(highlightIds, flow),
    [highlightIds, flow],
  );
  // issue #39：物证节点点击高亮与 VerdictCard 证据高亮取并集
  const displayHighlightIds = useMemo(() => {
    if (!evidenceFocus) return resolvedHighlight;
    return new Set([...resolvedHighlight, ...evidenceFocus.ids]);
  }, [resolvedHighlight, evidenceFocus]);

  const rfEdges = useMemo(
    () => buildEdges(flow, maxLayer ?? null, collapsed, displayHighlightIds),
    [flow, maxLayer, collapsed, displayHighlightIds],
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

  return (
    <HighlightContext.Provider value={displayHighlightIds}>
      <div className="relative" style={{ width: "100%", height: "100%" }} data-testid="graph-canvas">
        <ReactFlow
          nodes={rfNodes}
          edges={rfEdges}
          onNodesChange={handleNodesChange}
          nodeTypes={nodeTypes}
          onNodeClick={handleNodeClick}
          onPaneClick={() => setEvidenceFocus(null)} // 点击空白取消物证高亮（issue #39）
          // 注意：不要开 onlyRenderVisibleElements —— fitView 完成测量前视口判定
          // 会把所有边裁剪掉（实测 17 节点 0 边），≤200 节点规模无性能压力
          nodesConnectable={false}
          elementsSelectable
          fitView
          onInit={(inst) => (rfRef.current = inst)}
          proOptions={{ hideAttribution: true }}
        >
          <Background gap={24} color="#1a2029" />
          <Controls showInteractive={false} />
        </ReactFlow>
        {/* 图例（仅展示当前子图实际存在的元素）：跨链/混币为琥珀证据点，
            普通停止扩展为灰虚线——避免 thorchain 跨链 tx 被淹没在停止边里 */}
        <GraphLegend subgraph={subgraph} />
      </div>
    </HighlightContext.Provider>
  );
}

function GraphLegend({
  subgraph,
}: {
  subgraph: { edges: GraphEdge[] };
}) {
  const hasCross = subgraph.edges.some((e) => e.is_crosschain);
  const hasMixer = subgraph.edges.some((e) => e.is_remixer);
  const hasStop = subgraph.edges.some(
    (e) => e.is_stopped_expansion && !e.is_remixer && !e.is_crosschain);
  if (!hasCross && !hasMixer && !hasStop) return null;
  return (
    <div
      className="pointer-events-none absolute bottom-3 left-3 z-10 flex flex-wrap gap-x-3 gap-y-1 rounded-md border border-pt-line bg-pt-panel/90 px-2.5 py-1.5 font-mono text-[10px] text-pt-muted"
      data-testid="graph-legend"
    >
      {hasCross && (
        <span data-testid="legend-crosschain">
          <span aria-hidden className="text-pt-amber">⛓</span> 跨链证据
        </span>
      )}
      {hasMixer && (
        <span data-testid="legend-mixer">
          <span aria-hidden className="text-pt-amber">♻</span> 混币 CoinJoin
        </span>
      )}
      {hasStop && <span data-testid="legend-stopped">┅ 停止扩展</span>}
    </div>
  );
}
