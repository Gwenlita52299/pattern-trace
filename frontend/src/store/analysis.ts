// 分析页核心状态（frontend-spec §5）：Zustand store + 轮询协议。
//
// 轮询协议：2s 固定起步，连续失败退避至 5s 上限（成功后计数器重置）；
// 终止条件 status ∈ {completed, failed} 或总时长超 NEXT_PUBLIC_POLL_TIMEOUT_MS
// （默认 90s，测试可缩短）；组件卸载经 cancelPolling() abort。
import { create } from "zustand";

import { api } from "@/lib/api";

export type AnalysisStatus =
  | "idle"
  | "queued"
  | "processing"
  | "completed"
  | "failed";

export interface GraphNode {
  id: string;
  kind: string;
  label?: string;
  first_layer?: number;
  total_received_btc?: number;
  total_sent_btc?: number;
  utxo_count?: number;
  direct_related_to_lazarus?: boolean;
  // 终止交易节点（issue #9）：前端在地址流式变换中合成 terminalTransaction 节点，
  // 携带 txs 聚合而来的事实字段——txid、停止原因、协议、混币/跨链标志。
  txid?: string;
  stop_reason?: "coinjoin" | "crosschain" | "out_of_range" | string;
  protocol?: string | null;
  is_stopped_expansion?: boolean;
  is_remixer?: boolean;
  is_crosschain?: boolean;
  op_return_protocol?: string | null;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  txid?: string;
  tx_layer?: string;
  value_ratio?: number;
  // 源侧金额（addr→tx 边）：源地址在该交易中消费的 UTXO 金额（出金额）
  src_value_btc?: number;
  dst_value_btc?: number;
  is_stopped_expansion?: boolean;
  is_remixer?: boolean;
  is_crosschain?: boolean;
  op_return_protocol?: string | null;
}

export interface JudgmentPayload {
  id: string;
  address: string;
  status: string;
  // issue #40：当前分析阶段（worker 写 Redis，轮询下发）；
  // queued/processing 时非空，终态为 null
  stage?: 'building_subgraph' | 'retrieval_topk' | 'wl_rerank' | 'llm_judging' | null;
  hops?: number;
  time_window_days?: number;
  risk_level?: string | null;
  matched_pattern_name?: string | null;
  confidence?: number | null;
  evidence?: string[];
  reasoning?: string | null;
  recommended_action?: string | null;
  error_code?: string | null;
  error_message?: string | null;
  retry_count?: number;
  subgraph?: { nodes: GraphNode[]; edges: GraphEdge[] };
  latency_ms?: number | null;
  concluded_at?: string | null;
  data_as_of?: string | null;
  data_quality?: "complete" | "degraded" | string;
  requires_manual_review?: boolean;
  missing_branches?: number;
  source_errors?: { stage: string; address?: string; error_code: string; message?: string }[];
}

const START_INTERVAL_MS = 2_000;
const MAX_BACKOFF_MS = 5_000;

// issue #73：持久化分析 trace（后端 analysis_spans）——与 Redis 实时阶段
// 互补：终态后仍可查看每阶段耗时、上游调用计数与稳定错误码
export interface TraceSpan {
  id: number;
  parent_id: number | null;
  name: string;
  status: string;
  attempt: number;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  error_code: string | null;
  metadata: Record<string, unknown>;
}

export interface TracePayload {
  judgment_id: string;
  trace_id: string | null;
  status: string;
  error_code: string | null;
  total_duration_ms: number | null;
  stage_duration_ms: Record<string, number | null>;
  spans: TraceSpan[];
}

// issue #73：span 名 → 展示文案（后端 services/tracing.py 的写入点对齐）
export const TRACE_SPAN_LABELS: Record<string, string> = {
  analysis: "整体分析",
  building_subgraph: "构建子图 BFS",
  esplora_fetch: "上游数据源请求",
  retrieval_topk: "混合检索 Top-K",
  embedding_call: "Embedding 调用",
  wl_rerank: "WL kernel 精排",
  llm_judging: "LLM 结构化判断",
  llm_call: "LLM 调用",
};

// issue #40：阶段 → 文案；与后端 orchestration._report_stage 的 key 对齐。
// 顺序即管线阶段，UI（进度条/结果呈现门控）共用
export const ANALYSIS_STAGES = [
  { key: "building_subgraph", label: "构建子图 BFS" },
  { key: "retrieval_topk", label: "混合检索 Top-K" },
  { key: "wl_rerank", label: "WL kernel 精排" },
  { key: "llm_judging", label: "LLM 结构化判断" },
] as const;
const POLL_TIMEOUT_MS = Number(
  process.env.NEXT_PUBLIC_POLL_TIMEOUT_MS ?? 90_000,
);

interface AnalysisState {
  judgment: JudgmentPayload | null;
  subgraph: { nodes: GraphNode[]; edges: GraphEdge[] } | null;
  status: AnalysisStatus;
  progressText: string;
  stage: string | null;
  // 流势已显示完成的阶段数（AnalysisStages 逐格推进）；
  // 页面据此门控结果呈现：完成后等进度条走满再显示子图/结论
  stageProgress: number;
  error: string | null;
  highlightIds: Set<string>;
  selectedNodeId: string | null;
  // issue #73：持久化阶段 trace（可展开详情）
  trace: TracePayload | null;
  traceError: string | null;
  fetchTrace: (id: string) => Promise<void>;
  startAnalysis: (params: {
    address: string;
    hops: number;
    time_window_days?: number;
  }) => Promise<string>; // 返回 judgment_id（FE-01 跳转 /analyze/<id>）
  pollJudgment: (id: string) => Promise<void>; // 独立可重试（FE-03 重试按钮 / FE-02 恢复轮询）
  cancelPolling: () => void; // 组件卸载取消（FE-30）
  reset: () => void;
  setHighlight: (ids: string[]) => void; // FE-05/33 evidence 点击高亮
  selectNode: (id: string | null) => void; // FE-16 详情侧栏
}

let pollAbort: AbortController | null = null;

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}

const initial = {
  judgment: null as JudgmentPayload | null,
  subgraph: null,
  status: "idle" as AnalysisStatus,
  progressText: "",
  stage: null as string | null,
  stageProgress: 0,
  error: null as string | null,
  highlightIds: new Set<string>(),
  selectedNodeId: null as string | null,
  trace: null as TracePayload | null,
  traceError: null as string | null,
};

export const useAnalysisStore = create<AnalysisState>((set, get) => ({
  ...initial,

  async startAnalysis(params) {
    get().cancelPolling();
    set({ ...initial });
    set({ status: "queued", progressText: "正在构建子图…" });
    const resp = await api<{ judgment_id: string }>("/addresses/analyze", {
      method: "POST",
      body: params,
    });
    return resp.judgment_id;
  },

  cancelPolling() {
    pollAbort?.abort();
    pollAbort = null;
  },

  async pollJudgment(id) {
    // 新轮询开始前终止旧的（重试按钮场景）
    pollAbort?.abort();
    const ctrl = new AbortController();
    pollAbort = ctrl;

    const startedAt = Date.now();
    let interval = START_INTERVAL_MS;
    let sawRunning = false; // 是否观察到过排队/进行中（刷新恢复已完成结果时为 false）
    set({ status: "processing", progressText: "AI 分析中…", error: null });

    while (Date.now() - startedAt < POLL_TIMEOUT_MS) {
      if (ctrl.signal.aborted) return;
      try {
        const j = await api<JudgmentPayload>(`/judgments/${id}`, {
          signal: ctrl.signal,
        });
        if (j.status === "completed") {
          set({
            status: "completed",
            judgment: j,
            subgraph: j.subgraph ?? null,
            stage: null,
            // 首轮轮询即 completed（页面刷新恢复）：跳过流势动画直接呈现
            stageProgress: sawRunning
              ? get().stageProgress
              : ANALYSIS_STAGES.length,
          });
          return;
        }
        if (j.status === "failed") {
          set({
            status: "failed",
            judgment: j,
            stage: j.stage ?? null,
            error: j.error_message ?? j.error_code ?? "analysis failed",
          });
          return;
        }
        sawRunning = true;
        set({
          status: "processing",
          judgment: j,
          stage: j.stage ?? null,
          progressText:
            j.status === "queued" ? "排队中，等待分析资源…" : "AI 分析中…",
        });
        interval = START_INTERVAL_MS; // FE-29：成功后退避计数器重置
      } catch (err) {
        if ((err as Error).name === "AbortError") return;
        interval = Math.min(interval + 1_000, MAX_BACKOFF_MS); // 失败退避
      }
      await sleep(interval, ctrl.signal);
    }

    // FE-28：超时转 failed UI（保留重试入口）
    set({ status: "failed", error: "分析超时，请重试" });
  },

  reset() {
    get().cancelPolling();
    set({ ...initial });
  },

  async fetchTrace(id) {
    // issue #73：trace 拉取失败不影响主流程（进度条/结论照常展示）
    try {
      const trace = await api<TracePayload>(`/judgments/${id}/trace`);
      set({ trace, traceError: null });
    } catch (err) {
      set({ traceError: (err as Error).message || "阶段详情加载失败" });
    }
  },

  setHighlight(ids) {
    set({ highlightIds: new Set(ids) });
  },

  selectNode(id) {
    set({ selectedNodeId: id });
  },
}));
