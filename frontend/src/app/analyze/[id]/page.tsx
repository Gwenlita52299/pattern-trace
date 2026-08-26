"use client";
// 分析结果页（frontend-spec §2 /analyze/[id]）。
// 挂载即以 id 恢复轮询——刷新不重新发起 analyze（FE-02）；
// failed 态重试按钮以相同地址重新 POST 创建新 judgment（FE-03）；
// 组件卸载取消轮询（FE-30）。
import { useParams, useRouter } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';

import ErrorBoundary from '@/components/ErrorBoundary';
import GraphCanvas from '@/components/GraphCanvas';
import VerdictCard from '@/components/VerdictCard';
import { useAnalysisStore, type GraphNode } from '@/store/analysis';

export default function AnalyzePage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const judgmentId = params.id;

  const status = useAnalysisStore((s) => s.status);
  const subgraph = useAnalysisStore((s) => s.subgraph);
  const highlightIds = useAnalysisStore((s) => s.highlightIds);
  const judgment = useAnalysisStore((s) => s.judgment);
  const selectedNodeId = useAnalysisStore((s) => s.selectedNodeId);
  const pollJudgment = useAnalysisStore((s) => s.pollJudgment);
  const cancelPolling = useAnalysisStore((s) => s.cancelPolling);
  const reset = useAnalysisStore((s) => s.reset);
  const startAnalysis = useAnalysisStore((s) => s.startAnalysis);

  const [maxLayer, setMaxLayer] = useState<number | null>(null);
  const selectedNode: GraphNode | undefined = subgraph?.nodes.find(
    (n) => n.id === selectedNodeId,
  );

  // FE-02：挂载恢复轮询；卸载 abort（FE-30）。用 ref 防 StrictMode 双触发重复轮询
  const startedRef = useRef(false);
  useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;
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
      // 仅重置守卫：router.replace 改变 id 后由上面的 effect 统一恢复轮询，
      // 避免手动调用与 effect 并发两条轮询循环
      startedRef.current = false;
      router.replace(`/analyze/${newId}`);
    } catch {
      /* startAnalysis 抛错时 store 已置 error 态 */
    }
  }

  return (
    <main className="mx-auto flex max-w-7xl gap-4 px-4 py-6">
      <section className="relative h-[calc(100vh-140px)] flex-1 overflow-hidden rounded-xl border border-slate-200 bg-white">
        {subgraph ? (
          <ErrorBoundary>
            <div className="absolute left-3 top-3 z-10 flex items-center gap-2 rounded-lg bg-white/90 px-3 py-1.5 text-xs shadow-sm ring-1 ring-slate-200">
              <label htmlFor="layer-filter" className="text-slate-500">深度</label>
              <select
                id="layer-filter"
                value={maxLayer ?? ''}
                onChange={(e) =>
                  setMaxLayer(e.target.value === '' ? null : Number(e.target.value))}
                className="rounded border border-slate-200 px-1 py-0.5"
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
              maxLayer={maxLayer}
              onNodeClick={(n) => useAnalysisStore.getState().selectNode(n.id)}
            />
          </ErrorBoundary>
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-slate-400">
            {status === 'failed' ? '无子图数据' : '正在构建子图…'}
          </div>
        )}

        {/* FE-16 节点详情侧栏 */}
        {selectedNode && (
          <aside className="absolute right-3 top-14 z-10 w-64 rounded-lg border border-slate-200 bg-white/95 p-4 shadow-md">
            <button
              aria-label="关闭详情"
              onClick={() => useAnalysisStore.getState().selectNode(null)}
              className="float-right text-xs text-slate-400 hover:text-slate-700"
            >
              ✕
            </button>
            <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
              {selectedNode.kind === 'address' ? '地址' : '交易'}
            </p>
            <p className="break-all font-mono text-[11px]">{selectedNode.label ?? selectedNode.id}</p>
            <dl className="mt-3 space-y-1 text-[11px] text-slate-600">
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
        <VerdictCard onRetry={() => void retry()} />
        {judgment?.address && (
          <div className="rounded-xl border border-slate-200 bg-white p-4 text-xs text-slate-500">
            <span className="text-slate-400">分析地址：</span>
            <span className="break-all font-mono">{judgment.address}</span>
          </div>
        )}
        <button
          onClick={() => reset()}
          className="text-xs text-slate-400 underline hover:text-slate-600"
        >
          重置本页状态
        </button>
      </aside>
    </main>
  );
}
