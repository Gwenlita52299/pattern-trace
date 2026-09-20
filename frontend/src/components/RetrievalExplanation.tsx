"use client";
// 检索解释快照抽屉（issue #77）：展示 Top-K 候选的结构/语义/WL/融合分数
// 与排名、淘汰说明、来源性质（合成模板必须持续标识「非真实链上证据」）。
// 懒加载：展开时才请求 explanation，避免拖慢 verdict 首屏。
import Link from "next/link";
import { useCallback, useState } from "react";

import {
  ApiError,
  getRetrievalExplanation,
  type ExplanationCandidate,
  type RetrievalExplanation,
} from "@/lib/api";

/** empty_reason → 用户可读文案（无候选时明确说明，而不是空白）。 */
export function emptyReasonText(reason: string | null | undefined): string {
  switch (reason) {
    case "empty_subgraph":
      return "子图为空（无地址节点），检索未执行。";
    case "no_recall_match":
      return "召回阶段无任何候选达到阈值（知识库无结构相近模式）。";
    case "analysis_in_progress":
      return "分析进行中，检索快照尚未生成。";
    case "retrieval_not_recorded":
      return "该判定未记录检索快照（历史数据或检索阶段失败）。";
    default:
      return reason ? `无候选：${reason}` : "无候选";
  }
}

/** provenance → 徽标文案/配色（合成模板与真实确认双重区分）。 */
export function provenanceBadge(provenance: string): {
  text: string;
  cls: string;
} {
  if (provenance === "confirmed") {
    return { text: "confirmed 真实链上证据", cls: "text-pt-amber-hi border-pt-amber/40" };
  }
  return { text: "合成模板 · 非真实链上证据", cls: "text-pt-muted border-pt-line" };
}

const RECALL_MODE_LABELS: Record<string, string> = {
  graphormer_online: "Graphormer 在线前向",
  graphormer_ego: "Graphormer ego 池化",
  hybrid: "hybrid（结构+文本）",
};

function fmtScore(v: number | undefined): string {
  return typeof v === "number" ? v.toFixed(3) : "-";
}

function CandidateRow({
  c,
  compareJudgmentId,
}: {
  c: ExplanationCandidate;
  compareJudgmentId: string;
}) {
  const badge = provenanceBadge(c.provenance);
  return (
    <li
      className="rounded border border-pt-line bg-pt-panel-2 px-2.5 py-2 transition-colors hover:border-pt-amber/60"
      data-testid={`match-evidence-${c.rank}`}
    >
      <div className="flex items-baseline justify-between gap-2">
        <Link
          href={`/patterns/${encodeURIComponent(c.pattern_id)}?compare=${encodeURIComponent(compareJudgmentId)}`}
          title="查看该模式子图（并与本次分析子图对比）"
          data-testid={`pattern-link-${c.rank}`}
          className="min-w-0 truncate font-mono text-[11px] text-pt-ink underline decoration-pt-line underline-offset-2 hover:text-pt-amber-hi"
        >
          <span className="mr-1.5 text-[10px] text-pt-faint">#{c.rank}</span>
          {c.name}
        </Link>
        <span className="shrink-0 font-mono text-[10px] text-pt-amber-hi">
          final {fmtScore(c.similarity_score)}
        </span>
      </div>
      <div className="mt-1 flex flex-wrap gap-1.5 text-[10px]">
        <span className={`rounded border px-1.5 py-0.5 ${badge.cls}`}>
          {badge.text}
        </span>
        <span className="rounded border border-pt-line px-1.5 py-0.5 text-pt-muted">
          grade {c.evidence_grade}
        </span>
      </div>
      <dl className="mt-1.5 grid grid-cols-5 gap-1 font-mono text-[10px] text-pt-muted">
        <div>
          <dt className="text-pt-faint">struct</dt>
          <dd>{fmtScore(c.structural_similarity)}</dd>
        </div>
        <div>
          <dt className="text-pt-faint">sem</dt>
          <dd>{fmtScore(c.semantic_similarity)}</dd>
        </div>
        <div>
          <dt className="text-pt-faint">wl</dt>
          <dd>{fmtScore(c.wl_kernel_score)}</dd>
        </div>
        <div>
          <dt className="text-pt-faint">ov</dt>
          <dd>{fmtScore(c.ov_score)}</dd>
        </div>
        <div>
          <dt className="text-pt-faint">fp</dt>
          <dd>{fmtScore(c.fp_score)}</dd>
        </div>
      </dl>
      {c.difference_note && (
        <p className="mt-1.5 text-[10px] leading-relaxed text-pt-muted">
          {c.difference_note}
        </p>
      )}
    </li>
  );
}

export default function RetrievalExplanationPanel({
  judgmentId,
}: {
  judgmentId: string;
}) {
  const [open, setOpen] = useState(false);
  const [snap, setSnap] = useState<RetrievalExplanation | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setSnap(await getRetrievalExplanation(judgmentId));
    } catch (e) {
      // 带状态码便于定位（曾出现 404：双前缀路径 /api/v1/api/v1/...）
      const err = e as ApiError;
      setError(err.status ? `HTTP ${err.status} ${err.message}` : err.message);
    } finally {
      setLoading(false);
    }
  }, [judgmentId]);

  const toggle = useCallback(() => {
    const next = !open;
    setOpen(next);
    if (next && !snap && !loading) void load();
  }, [open, snap, loading, load]);

  const recall = snap?.recall;
  const candidates = snap?.candidates ?? [];

  return (
    <div className="mt-4 border-t border-pt-line pt-3" data-testid="retrieval-explanation">
      <button
        onClick={toggle}
        aria-expanded={open}
        className="flex w-full items-center justify-between text-left text-[10px] font-semibold uppercase tracking-[0.2em] text-pt-muted hover:text-pt-ink"
      >
        <span>匹配依据 Retrieval Explanation</span>
        <span aria-hidden className="font-mono">{open ? "−" : "+"}</span>
      </button>

      {open && (
        <div className="mt-2.5 space-y-2.5">
          {loading && (
            <p className="text-[11px] text-pt-muted">加载检索快照…</p>
          )}
          {error && (
            <div className="text-[11px] text-pt-amber-hi">
              <p>检索快照加载失败：{error}</p>
              <button
                onClick={() => void load()}
                className="mt-1 font-mono text-[10px] text-pt-muted underline hover:text-pt-ink"
              >
                重试
              </button>
            </div>
          )}
          {snap && !loading && !error && (
            <>
              <div className="font-mono text-[10px] leading-relaxed text-pt-muted">
                <p>
                  召回 {RECALL_MODE_LABELS[recall?.mode ?? ""] ?? (recall?.mode || "-")}{" "}
                  · 候选 {recall?.count ?? 0} → Top-K {candidates.length}
                  {snap.dropped_by_top_k > 0 &&
                    `（${snap.dropped_by_top_k} 个低于 Top-K 截断）`}
                </p>
                {snap.algorithm_version && (
                  <p className="text-pt-faint">
                    algorithm {snap.algorithm_version} · top_k{" "}
                    {String(snap.params?.top_k ?? "-")} · embedding{" "}
                    {String(snap.params?.embedding_model ?? "-")}
                  </p>
                )}
              </div>

              {candidates.length > 0 ? (
                <ul className="space-y-1.5">
                  {candidates.map((c) => (
                    <CandidateRow key={c.pattern_id} c={c}
                                  compareJudgmentId={judgmentId} />
                  ))}
                </ul>
              ) : (
                <p
                  className="rounded border border-dashed border-pt-line px-2.5 py-2 text-[11px] text-pt-muted"
                  data-testid="retrieval-empty"
                >
                  {emptyReasonText(snap.empty_reason ?? recall?.empty_reason)}
                </p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
