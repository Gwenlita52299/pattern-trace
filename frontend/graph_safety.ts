// CT-03 渲染防线：GraphCanvas 数据进入 ReactFlow 前过滤悬空边，
// 并对无效引用输出结构化日志（两层防御的后端侧见 retriever.validate_canonical_subgraph）。
import type { GraphEdge, GraphNode } from "@/store/analysis";

export interface SafeSubgraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

function warn(message: string, detail?: unknown): void {
  // 浏览器无统一 logger 约定；console 结构化前缀便于日志采集器匹配
  console.warn(`[frontend.graph_safety] ${message}`, detail ?? "");
}

export function build_flow_safe(subgraph: {
  nodes?: GraphNode[];
  edges?: GraphEdge[];
}): { rfNodes: GraphNode[]; rfEdges: GraphEdge[] } {
  const nodes = (subgraph.nodes ?? []).filter((n) => n && typeof n.id === "string");
  const ids = new Set(nodes.map((n) => n.id));
  const edges: GraphEdge[] = [];
  for (const e of subgraph.edges ?? []) {
    if (!e || typeof e.id !== "string") continue;
    if (!ids.has(e.source) || !ids.has(e.target)) {
      warn("invalid edge reference dropped", { edge_id: e.id });
      continue;
    }
    if (e.source === e.target) {
      warn("self-loop edge dropped", { edge_id: e.id });
      continue;
    }
    edges.push(e);
  }
  return { rfNodes: nodes, rfEdges: edges };
}
