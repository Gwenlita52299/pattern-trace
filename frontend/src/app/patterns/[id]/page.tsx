"use client";
// issue #84：模式详情 —— 渲染 KB 模式的 canonical 子图，并可与来源判定的
// 分析子图对比（?compare=<judgment_id>，从「匹配依据」抽屉跳转时携带）。
//
// 结构数据与 analyze 子图同 schema，直接复用 GraphCanvas（层级/跨链/混币
// 视觉编码一致）。大闭包由后端按层级截断（graph_truncated），前端明确提示。
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";

import GraphCanvas from "@/components/GraphCanvas";
import {
  getJudgment,
  getPattern,
  type PatternDetail,
} from "@/lib/api";
import type { GraphEdge, GraphNode } from "@/store/analysis";

const PROVENANCE_LABELS: Record<string, string> = {
  confirmed: "真实确认",
  synthetic: "合成模板",
  negative: "负样本",
};

function provenanceLabel(p: string): string {
  return PROVENANCE_LABELS[p] ?? p;
}

function isSynthetic(provenance: string): boolean {
  return provenance !== "confirmed";
}

/** 模式/判定子图 → GraphCanvas 入参（canonical schema 同构）。 */
function asCanvasGraph(g: {
  nodes: Array<Record<string, unknown>>;
  edges: Array<Record<string, unknown>>;
}): { nodes: GraphNode[]; edges: GraphEdge[] } {
  return {
    nodes: g.nodes as unknown as GraphNode[],
    edges: g.edges as unknown as GraphEdge[],
  };
}

export default function PatternDetailPage() {
  // 静态预渲染要求 useSearchParams 位于 Suspense 边界内（同列表页）
  return (
    <Suspense
      fallback={
        <main className="mx-auto max-w-5xl px-4 py-10">
          <p className="font-mono text-sm tracking-widest text-pt-faint">加载中…</p>
        </main>
      }
    >
      <PatternDetailContent />
    </Suspense>
  );
}

function PatternDetailContent() {
  const params = useParams<{ id: string }>();
  const search = useSearchParams();
  const patternId = params.id;
  const compareJid = search.get("compare");

  const [pattern, setPattern] = useState<PatternDetail | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [compareGraph, setCompareGraph] = useState<{
    nodes: Array<Record<string, unknown>>;
    edges: Array<Record<string, unknown>>;
  } | null>(null);
  const [compareAddress, setCompareAddress] = useState<string>("");
  const [tab, setTab] = useState<"pattern" | "compare">("pattern");

  useEffect(() => {
    setPattern(null);
    setNotFound(false);
    setError(null);
    getPattern(patternId)
      .then(setPattern)
      .catch((e: { status?: number; message?: string }) => {
        if (e.status === 404) setNotFound(true);
        else setError(e.message ?? "加载失败");
      });
  }, [patternId]);

  useEffect(() => {
    if (!compareJid) return;
    getJudgment(compareJid)
      .then((j) => {
        setCompareGraph(j.subgraph ?? null);
        setCompareAddress(j.address ?? "");
      })
      .catch(() => setCompareGraph(null));
  }, [compareJid]);

  const patternGraph = useMemo(
    () => (pattern ? asCanvasGraph(pattern.canonical_subgraph) : null),
    [pattern],
  );
  const compareCanvas = useMemo(
    () => (compareGraph ? asCanvasGraph(compareGraph) : null),
    [compareGraph],
  );
  const showCompareTab = Boolean(compareJid && compareCanvas);

  if (notFound) {
    return (
      <main className="mx-auto max-w-5xl px-4 py-10">
        <div
          className="rounded-xl border border-dashed border-pt-line px-4 py-8 text-center"
          data-testid="pattern-not-found"
        >
          <p className="text-sm text-pt-muted">该模式已不在知识库</p>
          <p className="mt-1 font-mono text-[10px] text-pt-faint">
            {patternId}
          </p>
          <Link
            href="/patterns"
            className="mt-3 inline-block text-xs text-pt-amber-hi underline"
          >
            返回前科档案库
          </Link>
        </div>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-6xl px-4 py-8">
      <div className="flex items-center justify-between">
        <h1 className="min-w-0 truncate font-mono text-lg font-bold text-pt-ink">
          {pattern?.name ?? "加载中…"}
        </h1>
        <Link href="/patterns" className="text-xs text-pt-faint underline hover:text-pt-muted">
          返回列表
        </Link>
      </div>

      {error && (
        <p className="mt-4 text-sm text-red-400">加载失败：{error}</p>
      )}

      {pattern && (
        <>
          <div className="mt-3 flex flex-wrap items-center gap-2 text-[10px]">
            <span
              className={
                isSynthetic(pattern.provenance)
                  ? "rounded border border-pt-line bg-pt-panel-2 px-1.5 py-0.5 text-pt-muted"
                  : "rounded border border-pt-amber/40 bg-pt-amber/5 px-1.5 py-0.5 text-pt-amber-hi"
              }
            >
              {provenanceLabel(pattern.provenance)}
            </span>
            <span className="rounded border border-pt-line px-1.5 py-0.5 text-pt-muted">
              grade {pattern.evidence_grade}
            </span>
            <span className="font-mono text-pt-faint">
              {pattern.source} · {pattern.node_count} 节点 / {pattern.edge_count} 边
            </span>
            <span className="max-w-[280px] truncate font-mono text-pt-faint">
              seed {pattern.seed_address}
            </span>
          </div>

          {isSynthetic(pattern.provenance) && (
            <p
              className="mt-2 rounded border border-pt-line bg-pt-panel-2 px-2.5 py-1.5 text-[11px] text-pt-muted"
              data-testid="synthetic-notice"
            >
              合成结构模板 · 非真实链上证据：仅作检索结构参考，不可作为链上物证引用。
            </p>
          )}

          {pattern.description && (
            <p className="mt-3 whitespace-pre-wrap break-words text-xs leading-relaxed text-pt-muted">
              {pattern.description}
            </p>
          )}

          {pattern.graph_truncated && (
            <p
              className="mt-3 rounded border border-pt-line bg-pt-panel-2 px-2.5 py-1.5 font-mono text-[10px] text-pt-muted"
              data-testid="graph-truncated"
            >
              结构过大：已按层级截断显示 {pattern.displayed_node_count} /{" "}
              {pattern.node_count} 节点（种子与近层优先）
            </p>
          )}

          {showCompareTab && (
            <div className="mt-4 flex gap-2 text-xs" role="tablist">
              <button
                role="tab"
                aria-selected={tab === "pattern"}
                onClick={() => setTab("pattern")}
                data-testid="tab-pattern"
                className={`rounded border px-3 py-1 ${
                  tab === "pattern"
                    ? "border-pt-amber/50 bg-pt-amber/5 text-pt-amber-hi"
                    : "border-pt-line text-pt-muted hover:border-pt-amber"
                }`}
              >
                模式子图
              </button>
              <button
                role="tab"
                aria-selected={tab === "compare"}
                onClick={() => setTab("compare")}
                data-testid="tab-compare"
                className={`rounded border px-3 py-1 ${
                  tab === "compare"
                    ? "border-pt-amber/50 bg-pt-amber/5 text-pt-amber-hi"
                    : "border-pt-line text-pt-muted hover:border-pt-amber"
                }`}
              >
                分析子图 {compareAddress ? `· ${compareAddress.slice(0, 10)}…` : ""}
              </button>
            </div>
          )}

          <div
            className="relative mt-3 h-[560px] overflow-hidden rounded-xl border border-pt-line bg-[#0c0f13]"
            data-testid="pattern-graph"
          >
            {tab === "compare" && compareCanvas ? (
              <GraphCanvas subgraph={compareCanvas} highlightIds={new Set()} />
            ) : patternGraph ? (
              <GraphCanvas subgraph={patternGraph} highlightIds={new Set()} />
            ) : null}
          </div>
        </>
      )}
    </main>
  );
}
