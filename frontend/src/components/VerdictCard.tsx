"use client";
// Verdict 面板（frontend-spec §4 VerdictCard）：四态矩阵
// loading / failed / no_match / success；四档色卡颜色+图标双编码（a11y）；
// evidence 列表每条可点击 → 高亮画布对应节点/边。
import { useAnalysisStore } from "@/store/analysis";

const RISK_STYLES: Record<
  string,
  { bg: string; icon: string; label: string }
> = {
  high: { bg: "bg-red-100 border-red-400", icon: "⚠", label: "HIGH" },
  medium: { bg: "bg-orange-100 border-orange-400", icon: "●", label: "MEDIUM" },
  low: { bg: "bg-green-100 border-green-500", icon: "✓", label: "LOW" },
  no_match: { bg: "bg-slate-100 border-slate-300", icon: "○", label: "NO_MATCH" },
};

const ACTION_LABELS: Record<string, string> = {
  freeze: "freeze 冻结申报",
  monitor: "monitor 持续监控",
  review: "review 人工复核",
  none: "none 无需操作",
};

export interface VerdictCardProps {
  onRetry?: () => void; // failed 态重试（重新发起 analyze）
}

export default function VerdictCard({ onRetry }: VerdictCardProps) {
  const status = useAnalysisStore((s) => s.status);
  const judgment = useAnalysisStore((s) => s.judgment);
  const progressText = useAnalysisStore((s) => s.progressText);
  const error = useAnalysisStore((s) => s.error);
  const setHighlight = useAnalysisStore((s) => s.setHighlight);

  if (status === "idle" || status === "queued" || status === "processing") {
    return (
      <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm" data-testid="verdict-loading">
        <div className="mb-3 h-4 w-2/3 animate-pulse rounded bg-slate-200" />
        <div className="mb-2 h-3 w-full animate-pulse rounded bg-slate-100" />
        <div className="h-3 w-4/5 animate-pulse rounded bg-slate-100" />
        <p className="mt-4 text-sm text-slate-500">{progressText || "准备中…"}</p>
      </div>
    );
  }

  if (status === "failed") {
    return (
      <div className="rounded-xl border border-red-300 bg-red-50 p-5" data-testid="verdict-failed">
        <p className="font-semibold text-red-700">分析失败</p>
        <p className="mt-1 text-xs font-mono text-red-600">
          {judgment?.error_code ?? "ERROR"}
        </p>
        <p className="mt-2 text-sm text-red-800">{error}</p>
        {onRetry && (
          <button
            onClick={onRetry}
            className="mt-4 rounded-lg bg-red-600 px-4 py-1.5 text-sm text-white hover:bg-red-700"
          >
            重试
          </button>
        )}
      </div>
    );
  }

  const risk = judgment?.risk_level ?? "no_match";
  const style = RISK_STYLES[risk] ?? RISK_STYLES.no_match;
  const confidence = judgment?.confidence ?? 0;

  return (
    <div
      data-testid="verdict-card"
      role="status"
      aria-label={`风险等级：${style.label}`}
      className={`rounded-xl border p-5 shadow-sm ${style.bg}`}
    >
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-2 text-lg font-bold tracking-wide">
          <span aria-hidden>{style.icon}</span>
          {style.label}
        </span>
        {judgment?.recommended_action && (
          <span className="rounded-full bg-white/70 px-3 py-1 text-xs font-medium text-slate-700">
            {ACTION_LABELS[judgment.recommended_action] ??
              judgment.recommended_action}
          </span>
        )}
      </div>

      {judgment?.matched_pattern_name && (
        <p className="mt-2 text-xs text-slate-600">
          命中模式：
          <span className="font-mono">{judgment.matched_pattern_name}</span>
        </p>
      )}

      {/* 置信度进度条 */}
      <div className="mt-3">
        <div className="flex justify-between text-[10px] text-slate-500">
          <span>confidence</span>
          <span>{(confidence * 100).toFixed(0)}%</span>
        </div>
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-white/60">
          <div
            className="h-full rounded-full bg-slate-500 transition-all"
            style={{ width: `${Math.min(100, confidence * 100)}%` }}
          />
        </div>
      </div>

      {judgment?.reasoning && (
        <p className="mt-3 whitespace-pre-wrap text-xs leading-relaxed text-slate-700">
          {judgment.reasoning}
        </p>
      )}

      {(judgment?.evidence?.length ?? 0) > 0 && (
        <div className="mt-4">
          <p className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-500">
            Evidence（点击在图中高亮）
          </p>
          <ul className="space-y-1">
            {judgment!.evidence!.map((eid) => (
              <li key={eid}>
                <button
                  onClick={() => setHighlight([eid])}
                  aria-label={`高亮 ${eid.startsWith("addr:") ? "地址" : eid.startsWith("tx:") ? "交易" : "边"} ${eid}`}
                  className="w-full truncate rounded bg-white/80 px-2 py-1 text-left font-mono text-[10px] text-cyan-700 hover:bg-white"
                >
                  {eid}
                </button>
              </li>
            ))}
          </ul>
          <button
            onClick={() => setHighlight([])}
            className="mt-2 text-[10px] text-slate-400 underline hover:text-slate-600"
          >
            清除高亮
          </button>
        </div>
      )}

      {judgment?.latency_ms !== undefined && (
        <p className="mt-3 text-right text-[10px] text-slate-400">
          {judgment.latency_ms} ms · model {judgment.id.slice(0, 8)}
        </p>
      )}
    </div>
  );
}
