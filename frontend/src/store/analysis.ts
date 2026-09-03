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
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  txid?: string;
  tx_layer?: string;
  value_ratio?: number;
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
const POLL_TIMEOUT_MS = Number(
  process.env.NEXT_PUBLIC_POLL_TIMEOUT_MS ?? 90_000,
);

interface AnalysisState {
  judgment: JudgmentPayload | null;
  subgraph: { nodes: GraphNode[]; edges: GraphEdge[] } | null;
  status: AnalysisStatus;
  progressText: string;
  error: string | null;
  highlightIds: Set<string>;
  selectedNodeId: string | null;
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
  error: null as string | null,
  highlightIds: new Set<string>(),
  selectedNodeId: null as string | null,
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
          });
          return;
        }
        if (j.status === "failed") {
          set({
            status: "failed",
            judgment: j,
            error: j.error_message ?? j.error_code ?? "analysis failed",
          });
          return;
        }
        set({
          status: "processing",
          judgment: j,
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

  setHighlight(ids) {
    set({ highlightIds: new Set(ids) });
  },

  selectNode(id) {
    set({ selectedNodeId: id });
  },
}));
