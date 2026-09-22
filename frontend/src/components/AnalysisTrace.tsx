"use client";
// issue #73：可展开的分析阶段详情（持久化 trace）。
// - 完成/失败后自动拉取；进行中展开时也能看到已结束阶段（span 结束时落库）
// - 每行展示阶段耗时、状态、尝试次数与稳定错误码；metadata 折叠在行内
// - 色 + 图标双编码（design-tone）：完成=琥珀，失败=红，跳过/运行中=弱化
import { useEffect, useState } from "react";

import {
  TRACE_SPAN_LABELS,
  useAnalysisStore,
  type TraceSpan,
} from "@/store/analysis";

export const STATUS_ICON: Record<string, string> = {
  completed: "✓",
  failed: "✕",
  skipped: "–",
  running: "●",
  retrying: "↻",
  cancelled: "⊘",
};

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

export function metaEntries(
  meta: Record<string, unknown>,
): [string, string][] {
  return Object.entries(meta ?? {})
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => {
      const text = typeof v === "object" ? JSON.stringify(v) : String(v);
      return [k, text.length > 80 ? `${text.slice(0, 80)}…` : text];
    });
}

function SpanRow({ span }: { span: TraceSpan }) {
  const failed = span.status === "failed" || span.status === "cancelled";
  const muted = span.status === "skipped";
  const meta = metaEntries(span.metadata);
  return (
    <div
      className="border-t border-pt-line px-3 py-2 first:border-t-0"
      data-testid="trace-span"
      data-span-name={span.name}
    >
      <div className="flex items-baseline gap-3 font-mono text-[11px]">
        <span
          aria-hidden
          className={`w-3 text-center ${
            failed ? "text-red-400" : muted ? "text-pt-faint" : "text-pt-medium"
          }`}
        >
          {STATUS_ICON[span.status] ?? "•"}
        </span>
        <span className={`w-40 shrink-0 ${muted ? "text-pt-faint" : "text-pt-ink"}`}>
          {TRACE_SPAN_LABELS[span.name] ?? span.name}
        </span>
        <span className={`w-20 text-right ${muted ? "text-pt-faint" : "text-pt-ink"}`}>
          {formatDuration(span.duration_ms)}
        </span>
        <span className="w-14 text-pt-faint">#{span.attempt}</span>
        {failed && span.error_code && (
          <span className="text-red-400">{span.error_code}</span>
        )}
      </div>
      {meta.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 pl-6 font-mono text-[10px] text-pt-faint">
          {meta.map(([k, v]) => (
            <span key={k}>
              <span className="text-pt-muted">{k}</span>={v}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export default function AnalysisTrace({ judgmentId }: { judgmentId: string }) {
  const status = useAnalysisStore((s) => s.status);
  const trace = useAnalysisStore((s) => s.trace);
  const traceError = useAnalysisStore((s) => s.traceError);
  const fetchTrace = useAnalysisStore((s) => s.fetchTrace);
  const [open, setOpen] = useState(false);

  const terminal = status === "completed" || status === "failed";
  // 终态后自动加载（验收：完成和失败后均可查看）；进行中展开即加载
  useEffect(() => {
    if (open || terminal) void fetchTrace(judgmentId);
  }, [judgmentId, open, terminal, fetchTrace]);

  if (status === "idle") return null;

  const stages = (trace?.spans ?? []).filter((s) => s.parent_id !== null);
  const failedSpan = (trace?.spans ?? []).find((s) => s.status === "failed");

  return (
    <section
      className="rounded-xl border border-pt-line bg-pt-panel"
      data-testid="analysis-trace"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-3 px-4 py-3 text-left"
      >
        <span aria-hidden className="font-mono text-xs text-pt-muted">
          {open ? "▾" : "▸"}
        </span>
        <span className="font-mono text-[12px] text-pt-ink">阶段详情</span>
        {trace?.total_duration_ms != null && (
          <span className="font-mono text-[11px] text-pt-muted">
            总耗时 {formatDuration(trace.total_duration_ms)}
          </span>
        )}
        {failedSpan?.error_code && (
          <span className="font-mono text-[11px] text-red-400">
            {failedSpan.error_code}
          </span>
        )}
        {!trace && (
          <span className="font-mono text-[10px] text-pt-faint">点击加载</span>
        )}
      </button>

      {open && (
        <div className="border-t border-pt-line" data-testid="trace-detail">
          {traceError && (
            <p className="px-4 py-3 font-mono text-[11px] text-red-400">
              {traceError}
            </p>
          )}
          {!traceError && stages.length === 0 && (
            <p className="px-4 py-3 font-mono text-[11px] text-pt-faint">
              暂无阶段记录
            </p>
          )}
          {stages.map((span) => (
            <SpanRow key={span.id} span={span} />
          ))}
          {trace?.trace_id && (
            <p className="px-4 py-2 font-mono text-[10px] text-pt-faint">
              trace_id: {trace.trace_id}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
