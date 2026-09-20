"use client";
// Verdict 面板（frontend-spec §4 VerdictCard）：四态矩阵
// loading / failed / no_match / success；四档色卡颜色+图标双编码（a11y）；
// evidence 列表每条可点击 → 高亮画布对应节点/边。
import { useAnalysisStore } from "@/store/analysis";
import RetrievalExplanationPanel from "@/components/RetrievalExplanation";
import { evidenceTag, shortId } from "@/lib/evidence-display";
import { splitConclusion, tokenizeReasoning } from "@/lib/reasoning-highlight";

// 调性规范（docs/design-tone.md）：四档微色彩编码——小圆点 + 文字标签，
// 大面板永远冷静；琥珀只用于证据/高危。色+图标双编码（a11y）保留。
const RISK_STYLES: Record<
  string,
  { dot: string; labelCls: string; icon: string; label: string; bar: string }
> = {
  high: {
    dot: "bg-pt-amber shadow-[0_0_6px_rgba(240,180,41,0.5)]",
    labelCls: "text-pt-amber-hi",
    icon: "⚠",
    label: "HIGH",
    bar: "bg-pt-amber",
  },
  medium: {
    dot: "bg-pt-medium",
    labelCls: "text-[#9cc0ff]",
    icon: "●",
    label: "MEDIUM",
    bar: "bg-pt-medium",
  },
  low: {
    dot: "bg-[#3a4250]",
    labelCls: "text-pt-muted",
    icon: "✓",
    label: "LOW",
    bar: "bg-[#3a4250]",
  },
  no_match: {
    dot: "border border-dashed border-[#3a4250]",
    labelCls: "text-pt-muted",
    icon: "○",
    label: "NO_MATCH",
    bar: "bg-[#3a4250]",
  },
};

const ACTION_LABELS: Record<string, string> = {
  freeze: "freeze 冻结申报",
  monitor: "monitor 持续监控",
  review: "review 人工复核",
  none: "none 无需操作",
};

/** ISO 时间 → `YYYY-MM-DD HH:MM UTC`；空值显示 '-'。 */
function fmtTs(ts?: string | null): string {
  if (!ts) return "-";
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` +
    `${p(d.getUTCHours())}:${p(d.getUTCMinutes())} UTC`;
}

/** 描述文本的 token 渲染：地址=证据琥珀、交易=medium 蓝，均加粗缩短。 */
function ReasoningTokens({ text }: { text: string }) {
  return (
    <>
      {tokenizeReasoning(text).map((t, i) =>
        t.type === "text" ? (
          <span key={i}>{t.display}</span>
        ) : (
          <span
            key={i}
            title={t.value}
            className={`break-all font-mono font-semibold ${
              t.type === "addr" ? "text-pt-amber-hi" : "text-pt-medium"
            }`}
          >
            {t.display}
          </span>
        ),
      )}
    </>
  );
}

export interface VerdictCardProps {
  onRetry?: () => void; // failed 态重试（重新发起 analyze）
  /** 强制骨架态：成功结果由分析页在进度条走满后才揭示（reveal 门控） */
  forceLoading?: boolean;
}

export default function VerdictCard({ onRetry, forceLoading }: VerdictCardProps) {
  const status = useAnalysisStore((s) => s.status);
  const judgment = useAnalysisStore((s) => s.judgment);
  const progressText = useAnalysisStore((s) => s.progressText);
  const error = useAnalysisStore((s) => s.error);
  const setHighlight = useAnalysisStore((s) => s.setHighlight);

  if (forceLoading || status === "idle" || status === "queued" || status === "processing") {
    return (
      <div className="rounded-xl border border-pt-line bg-pt-panel p-5" data-testid="verdict-loading">
        <div className="mb-3 h-4 w-2/3 animate-pulse rounded bg-pt-panel-2" />
        <div className="mb-2 h-3 w-full animate-pulse rounded bg-pt-panel-2" />
        <div className="h-3 w-4/5 animate-pulse rounded bg-pt-panel-2" />
        <p className="mt-4 font-mono text-xs tracking-widest text-pt-muted">{progressText || "准备中…"}</p>
      </div>
    );
  }

  if (status === "failed") {
    return (
      <div className="rounded-xl border border-pt-line bg-pt-panel p-5" data-testid="verdict-failed">
        <p className="text-sm font-semibold text-red-400">分析失败</p>
        <p className="mt-1 font-mono text-xs text-pt-muted">
          {judgment?.error_code ?? "ERROR"}
        </p>
        <p className="mt-2 text-sm text-pt-ink">{error}</p>
        {onRetry && (
          <button
            onClick={onRetry}
            className="mt-4 rounded-lg border border-pt-line px-4 py-1.5 text-sm text-pt-muted hover:border-pt-amber hover:text-pt-amber-hi"
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
      className="min-w-0 rounded-xl border border-pt-line bg-pt-panel p-5"
    >
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-2.5 text-lg font-bold tracking-widest">
          <span aria-hidden className={`h-2.5 w-2.5 rounded-full ${style.dot}`} />
          <span className={style.labelCls}>{style.label}</span>
          <span aria-hidden className={`text-xs ${style.labelCls}`}>{style.icon}</span>
        </span>
        {judgment?.recommended_action && (
          <span className="rounded-md border border-pt-line bg-pt-panel-2 px-3 py-1 font-mono text-[10px] tracking-widest text-pt-muted">
            {ACTION_LABELS[judgment.recommended_action] ??
              judgment.recommended_action}
          </span>
        )}
      </div>

      {judgment?.matched_pattern_name && (
        <p className="mt-3 text-xs text-pt-muted">
          匹配前科：
          <span className="ml-1 font-mono text-pt-amber-hi">{judgment.matched_pattern_name}</span>
          {judgment.matched_pattern_name.startsWith('synth_') && (
            <span className="ml-1 rounded border border-pt-line px-1.5 py-0.5 text-[10px] text-pt-muted">
              合成模板 · 非真实链上证据
            </span>
          )}
        </p>
      )}

      {/* 置信度进度条 */}
      <div className="mt-3">
        <div className="flex justify-between font-mono text-[10px] tracking-widest text-pt-muted">
          <span>confidence</span>
          <span className="text-pt-ink">{(confidence * 100).toFixed(0)}%</span>
        </div>
        <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-white/10">
          <div
            className={`h-full rounded-full transition-all ${style.bar}`}
            style={{ width: `${Math.min(100, confidence * 100)}%` }}
          />
        </div>
      </div>

      {/* issue #8：数据不完整 → 人工复核提示 */}
      {judgment?.data_quality === "degraded" && (
        <div
          className="mt-3 rounded-md border border-pt-line bg-pt-panel-2 p-3 text-[11px] text-pt-muted"
          data-testid="verdict-degraded"
        >
          <p className="font-semibold text-pt-amber-hi">数据不完整 · 需人工复核</p>
          <p className="mt-1">
            部分上游数据请求失败，图可能不完整；缺失分支{" "}
            {judgment.missing_branches ?? "?"}。
          </p>
          {(judgment.source_errors?.length ?? 0) > 0 && (
            <ul className="mt-1 list-inside list-disc font-mono">
              {judgment.source_errors!.slice(0, 5).map((e, i) => (
                <li key={i}>
                  {e.error_code}
                  {e.address ? ` @ ${e.address}` : ""}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {judgment?.reasoning && (() => {
        const { body, conclusion } = splitConclusion(judgment.reasoning);
        return (
          <>
            {body && (
              <p className="mt-3 min-w-0 whitespace-pre-wrap break-words text-xs leading-relaxed text-pt-muted">
                <ReasoningTokens text={body} />
              </p>
            )}
            {conclusion && (
              // 末尾结论句高亮（用户要求）：琥珀细竖线 + 极浅底 + 亮色正文
              <p
                data-testid="reasoning-conclusion"
                className="mt-2 min-w-0 whitespace-pre-wrap break-words border-l-2 border-pt-amber/60 bg-pt-amber/[0.04] py-1.5 pl-2.5 text-xs font-medium leading-relaxed text-pt-ink"
              >
                <ReasoningTokens text={conclusion} />
              </p>
            )}
          </>
        );
      })()}

      {(judgment?.evidence?.length ?? 0) > 0 && (
        <div className="mt-4 min-w-0">
          <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-[0.2em] text-pt-muted">
            物证 Evidence（点击在图中高亮）
          </p>
          <ul className="min-w-0 space-y-1">
            {judgment!.evidence!.map((eid, i) => {
              // 重点层级：LLM 按 prompt 要求只列最强证据且顺序即强弱，
              // 首条标为主证据；关联跨链/混币边的地址加类型图标
              const tag = evidenceTag(eid, judgment?.subgraph?.edges);
              const primary = i === 0;
              return (
                <li key={eid} className="min-w-0">
                  <button
                    onClick={() => setHighlight([eid])}
                    title={`${eid}（点击在图中高亮）`}
                    aria-label={`高亮 ${eid.startsWith("addr:") ? "地址" : eid.startsWith("tx:") ? "交易" : "边"} ${eid}`}
                    className={`flex w-full min-w-0 items-center gap-1.5 rounded border px-2 py-1 text-left font-mono text-[10px] transition-colors hover:border-pt-amber ${
                      primary
                        ? "border-pt-amber/50 bg-pt-amber/5 text-pt-amber-hi"
                        : "border-pt-line bg-pt-panel-2 text-pt-muted"
                    }`}
                  >
                    <span
                      className={`shrink-0 text-[9px] ${
                        primary ? "font-semibold text-pt-amber-hi" : "text-pt-faint"
                      }`}
                    >
                      {primary ? "主证据" : `#${i + 1}`}
                    </span>
                    {tag && (
                      <span aria-hidden className="shrink-0 text-pt-amber">
                        {tag === "crosschain" ? "⛓" : "♻"}
                      </span>
                    )}
                    <span className="truncate">{shortId(eid)}</span>
                  </button>
                </li>
              );
            })}
          </ul>
          <button
            onClick={() => setHighlight([])}
            className="mt-2 text-[10px] text-pt-faint underline hover:text-pt-muted"
          >
            清除高亮
          </button>
        </div>
      )}

      {judgment?.latency_ms !== undefined && (
        <p className="mt-3 text-right font-mono text-[10px] text-pt-faint">
          {judgment.latency_ms} ms · model {judgment.id.slice(0, 8)}
        </p>
      )}

      {/* issue #77：检索解释快照抽屉（匹配依据） */}
      {judgment?.id && <RetrievalExplanationPanel judgmentId={judgment.id} />}

      {/* issue #7：结论/数据时间（历史 Judgment 时间版本化） */}
      <dl className="mt-3 grid grid-cols-2 gap-1 border-t border-pt-line pt-2 font-mono text-[10px] text-pt-muted">
        <div>
          <dt className="uppercase tracking-[0.15em] text-pt-faint">concluded</dt>
          <dd>{fmtTs(judgment?.concluded_at)}</dd>
        </div>
        <div>
          <dt className="uppercase tracking-[0.15em] text-pt-faint">data as of</dt>
          <dd>{fmtTs(judgment?.data_as_of)}</dd>
        </div>
      </dl>
    </div>
  );
}
