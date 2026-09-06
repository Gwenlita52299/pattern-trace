"use client";
// 分析结果页（frontend-spec §2 /analyze/[id]）。
// 挂载即以 id 恢复轮询——刷新不重新发起 analyze（FE-02）；
// failed 态重试按钮以相同地址重新 POST 创建新 judgment（FE-03）；
// 组件卸载取消轮询（FE-30）。
import { useParams, useRouter } from 'next/navigation';
import { useEffect, useMemo, useState } from 'react';

import ErrorBoundary from '@/components/ErrorBoundary';
import AnalysisStages from '@/components/AnalysisStages';
import GraphCanvas from '@/components/GraphCanvas';
import VerdictCard from '@/components/VerdictCard';
import { ANALYSIS_STAGES, useAnalysisStore, type GraphNode } from '@/store/analysis';

export default function AnalyzePage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const judgmentId = params.id;

  const status = useAnalysisStore((s) => s.status);
  const subgraph = useAnalysisStore((s) => s.subgraph);
  const highlightIds = useAnalysisStore((s) => s.highlightIds);
  const judgment = useAnalysisStore((s) => s.judgment);
  const selectedNodeId = useAnalysisStore((s) => s.selectedNodeId);
  const stageProgress = useAnalysisStore((s) => s.stageProgress);
  const pollJudgment = useAnalysisStore((s) => s.pollJudgment);
  const cancelPolling = useAnalysisStore((s) => s.cancelPolling);
  const reset = useAnalysisStore((s) => s.reset);
  const startAnalysis = useAnalysisStore((s) => s.startAnalysis);

  const [maxLayer, setMaxLayer] = useState<number | null>(null);
  // issue #39：物证节点集合（canonical evidence id），供画布点击高亮关联路径
  const evidenceIds = useMemo(
    () => new Set(judgment?.evidence ?? []),
    [judgment],
  );
  const selectedNode: GraphNode | undefined = subgraph?.nodes.find(
    (n) => n.id === selectedNodeId,
  );
  // 完成（或失败）前不呈现子图/结论：等进度条流势走满再揭示
  const resultShown = status === 'completed' && stageProgress >= ANALYSIS_STAGES.length;
  // completed 但子图缺失/为空（如 live 模式下地址无近期活动）→ 显示无数据，
  // 避免停留在「正在构建子图…」造成假卡死
  const graphEmpty =
    !subgraph || subgraph.nodes.length === 0;

  // FE-02：挂载恢复轮询；卸载 abort（FE-30）。
  // 不能用 startedRef 守卫「重复轮询」：StrictMode 双挂载时第一次轮询会被
  // cleanup 的 cancelPolling 中止，而守卫会让第二次挂载跳过恢复 → 页面永远
  // 停在「正在构建子图…」。pollJudgment 入口自带 pollAbort 竞态防重，足够了。
  useEffect(() => {
    void pollJudgment(judgmentId);
    return () => cancelPolling();
  }, [judgmentId, pollJudgment, cancelPolling]);

  async function retry() {
    if (!judgment?.address) return;
    try {
      const newId = await startAnalysis({
        address: judgment.address,
        hops: judgment.hops ?? 3,
      });
      router.replace(`/analyze/${newId}`); // 新 id 由上面的 effect 统一恢复轮询
    } catch {
      /* startAnalysis 抛错时 store 已置 error 态 */
    }
  }

  return (
    <main className="mx-auto flex max-w-7xl flex-col gap-4 px-4 py-6">
      {/* 分阶段进度条（issue #40）：横向，横跨子图与右侧栏 */}
      <AnalysisStages />
      <div className="flex gap-4">
      <section className="relative h-[calc(100vh-190px)] flex-1 overflow-hidden rounded-xl border border-pt-line bg-[#0c0f13]">
        {subgraph && resultShown ? (
          <ErrorBoundary>
            <div className="absolute left-3 top-3 z-10 flex items-center gap-2 rounded-lg border border-pt-line bg-pt-panel/90 px-3 py-1.5 font-mono text-xs shadow-sm backdrop-blur">
              <label htmlFor="layer-filter" className="tracking-widest text-pt-muted">深度</label>
              <select
                id="layer-filter"
                value={maxLayer ?? ''}
                onChange={(e) =>
                  setMaxLayer(e.target.value === '' ? null : Number(e.target.value))}
                className="rounded border border-pt-line bg-pt-panel-2 px-1 py-0.5 text-pt-muted"
              >
                <option value="">全部</option>
                {[0, 1, 2].map((l) => (
                  <option key={l} value={l}>≤ L{l}</option>
                ))}
              </select>
            </div>
            <GraphCanvas
              subgraph={subgraph}
              highlightIds={highlightIds}
              evidenceIds={evidenceIds}
              maxLayer={maxLayer}
              onNodeClick={(n) => useAnalysisStore.getState().selectNode(n.id)}
            />
          </ErrorBoundary>
        ) : (
          <div className="flex h-full items-center justify-center font-mono text-sm tracking-widest text-pt-faint">
            {status === 'failed' || (status === 'completed' && graphEmpty)
              ? '无子图数据'
              : status === 'completed'
                ? '分析完成，正在呈现…'
                : '正在构建子图…'}
          </div>
        )}

        {/* FE-16 节点详情侧栏 */}
        {selectedNode && (
          <aside className="absolute right-3 top-14 z-10 w-64 rounded-lg border border-pt-line bg-pt-panel/95 p-4 shadow-md backdrop-blur">
            <button
              aria-label="关闭详情"
              onClick={() => useAnalysisStore.getState().selectNode(null)}
              className="float-right text-xs text-pt-faint hover:text-pt-ink"
            >
              ✕
            </button>
            <p className="mb-2 text-[10px] font-semibold uppercase tracking-[0.2em] text-pt-muted">
              {selectedNode.kind === 'address' ? '地址' : '交易'}
            </p>
            <p className="break-all font-mono text-[11px] text-pt-ink">{selectedNode.label ?? selectedNode.id}</p>
            <dl className="mt-3 space-y-1 font-mono text-[11px] text-pt-muted">
              {selectedNode.first_layer !== undefined && (
                <div>first_layer: L{selectedNode.first_layer}</div>
              )}
              {selectedNode.total_received_btc !== undefined && (
                <div>received: {selectedNode.total_received_btc} BTC</div>
              )}
              {selectedNode.total_sent_btc !== undefined && (
                <div>sent: {selectedNode.total_sent_btc} BTC</div>
              )}
              {selectedNode.utxo_count !== undefined && (
                <div>utxo: {selectedNode.utxo_count}</div>
              )}
            </dl>
          </aside>
        )}
      </section>

      <aside className="w-96 shrink-0 space-y-4">
        {/* 结论与子图同步揭示：失败立即呈现失败卡，成功等进度条走满 */}
        {status === 'failed' || resultShown ? (
          <VerdictCard onRetry={() => void retry()} />
        ) : (
          <VerdictCard forceLoading />
        )}
        {judgment?.address && (
          <div className="rounded-xl border border-pt-line bg-pt-panel p-4 font-mono text-xs text-pt-muted">
            <span className="text-pt-faint">分析地址：</span>
            <span className="break-all text-pt-ink">{judgment.address}</span>
          </div>
        )}
        <button
          onClick={() => reset()}
          className="text-xs text-pt-faint underline hover:text-pt-muted"
        >
          重置本页状态
        </button>
      </aside>
      </div>
    </main>
  );
}
