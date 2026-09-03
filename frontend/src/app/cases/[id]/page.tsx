"use client";
// 案件详情（frontend-spec §2 /cases/[id]）：地址关联 + 逐地址分析入口 +
// 报告异步导出与轮询下载（FE-20 / 排期完成标志「分析 → 导出报告」）。
import { useParams } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';

import { API_BASE, api, ApiError } from '@/lib/api';

interface CaseDetail {
  id: string;
  title: string;
  status: string;
  description: string;
  addresses: {
    address: string;
    label: string | null;
    added_at: string | null;
    latest_judgment: {
      id: string;
      risk_level: string | null;
      confidence: number | null;
      concluded_at: string | null;
    } | null;
  }[];
}

const RISK_COLOR: Record<string, string> = {
  high: 'bg-red-100 text-red-700',
  medium: 'bg-orange-100 text-orange-700',
  low: 'bg-green-100 text-green-700',
  no_match: 'bg-slate-100 text-slate-600',
};

export default function CaseDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  // 报告导出状态机：idle → submitting → processing → ready/failed
  const [reportState, setReportState] = useState<
    { phase: string; downloadUrl?: string; error?: string }
  >({ phase: 'idle' });
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(() => {
    api<CaseDetail>(`/cases/${id}`)
      .then(setDetail)
      .catch((err) => setError((err as Error).message));
  }, [id]);

  useEffect(() => {
    load();
    return () => {
      if (pollTimer.current) clearTimeout(pollTimer.current);
    };
  }, [load]);

  async function addAddress(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const address = String(form.get('address') ?? '').trim();
    if (!address) return;
    try {
      await api(`/cases/${id}/addresses`, {
        method: 'POST',
        body: { addresses: [address] },
      });
      (e.target as HTMLFormElement).reset();
      load();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function exportReport(format: 'pdf' | 'html') {
    setReportState({ phase: 'submitting' });
    try {
      const r = await api<{ report_id: string; status: string }>(
        `/cases/${id}/reports?format=${format}`, { method: 'POST' });
      setReportState({ phase: r.status === 'completed' ? 'ready' : 'processing' });
      void pollReport(r.report_id, format);
    } catch (err) {
      setReportState({
        phase: 'failed',
        error: err instanceof ApiError ? err.message : String(err),
      });
    }
  }

  function pollReport(reportId: string, format: string) {
    pollTimer.current = setTimeout(async () => {
      try {
        const r = await api<{ status: string; download_url?: string; error_code?: string }>(
          `/reports/${reportId}`);
        if (r.status === 'completed' && r.download_url) {
          // 后端返回的是相对路径（/api/v1/...），拼接为指向后端的绝对地址
          setReportState({ phase: 'ready', downloadUrl: `${API_BASE}${r.download_url}` });
        } else if (r.status === 'failed') {
          setReportState({ phase: 'failed', error: r.error_code ?? 'render failed' });
        } else {
          pollReport(reportId, format);
        }
      } catch (err) {
        setReportState({ phase: 'failed', error: (err as Error).message });
      }
    }, 1200);
  }

  if (error && !detail) {
    return <main className="mx-auto max-w-4xl px-4 py-10 text-sm text-red-600">{error}</main>;
  }
  if (!detail) {
    return <main className="mx-auto max-w-4xl px-4 py-10 text-sm text-slate-400">加载中…</main>;
  }

  return (
    <main className="mx-auto max-w-4xl px-4 py-10">
      <h1 className="text-xl font-bold">{detail.title}</h1>
      <p className="mt-1 text-xs text-slate-400">案件 {detail.id} · 状态 {detail.status}</p>

      {/* 地址关联 */}
      <section className="mt-6 rounded-xl border border-slate-200 bg-white p-5">
        <h2 className="text-sm font-semibold">关联地址</h2>
        <form onSubmit={addAddress} className="mt-3 flex gap-2">
          <input
            name="address"
            placeholder="bc1q…"
            className="w-full max-w-md rounded-lg border border-slate-300 px-3 py-1.5 font-mono text-xs"
          />
          <button className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs text-white hover:bg-slate-900">
            关联
          </button>
        </form>

        <ul className="mt-4 space-y-3">
          {(detail.addresses ?? []).map((a) => (
            <li key={a.address} className="rounded-lg border border-slate-100 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="break-all font-mono text-[11px]">{a.address}</span>
                {a.latest_judgment?.risk_level && (
                  <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                    RISK_COLOR[a.latest_judgment.risk_level] ?? 'bg-slate-100'
                  }`}>
                    {a.latest_judgment.risk_level.toUpperCase()}
                  </span>
                )}
              </div>
              <div className="mt-2 flex items-center gap-3">
                <a
                  href={`/analyze/${a.latest_judgment?.id ?? ''}`}
                  onClick={(e) => {
                    if (!a.latest_judgment) e.preventDefault();
                  }}
                  className={`text-xs ${a.latest_judgment ? 'text-cyan-700 hover:underline' : 'text-slate-400'}`}
                >
                  查看分析 →
                </a>
                {a.latest_judgment?.concluded_at && (
                  <span className="text-[10px] text-slate-400">
                    结论于 {new Date(a.latest_judgment.concluded_at).toLocaleString('zh-CN')}
                  </span>
                )}
                {!a.latest_judgment && (
                  <button
                    onClick={() => {
                      sessionStorage.setItem('pt_case_address', a.address);
                      window.location.href = `/?address=${encodeURIComponent(a.address)}`;
                    }}
                    className="text-xs text-cyan-700 hover:underline"
                  >
                    发起分析
                  </button>
                )}
              </div>
            </li>
          ))}
          {(detail.addresses ?? []).length === 0 && (
            <li className="text-xs text-slate-400">尚未关联地址。</li>
          )}
        </ul>
      </section>

      {/* 报告导出 */}
      <section className="mt-6 rounded-xl border border-slate-200 bg-white p-5">
        <h2 className="text-sm font-semibold">报告导出</h2>
        <div className="mt-3 flex items-center gap-3">
          <button
            onClick={() => void exportReport('pdf')}
            disabled={reportState.phase === 'submitting' || reportState.phase === 'processing'}
            className="rounded-lg bg-cyan-600 px-4 py-1.5 text-sm text-white hover:bg-cyan-700 disabled:opacity-50"
          >
            导出 PDF
          </button>
          <button
            onClick={() => void exportReport('html')}
            disabled={reportState.phase === 'submitting' || reportState.phase === 'processing'}
            className="rounded-lg border border-slate-300 px-4 py-1.5 text-sm hover:bg-slate-50 disabled:opacity-50"
          >
            导出 HTML
          </button>

          {reportState.phase === 'processing' && (
            <span className="text-xs text-slate-500">报告生成中…</span>
          )}
          {reportState.phase === 'ready' && reportState.downloadUrl && (
            <a href={reportState.downloadUrl}
               className="text-sm font-medium text-green-700 hover:underline">
              ⬇ 下载报告（15 分钟内有效）
            </a>
          )}
          {reportState.phase === 'failed' && (
            <span className="text-xs text-red-600">生成失败：{reportState.error}</span>
          )}
        </div>
        <p className="mt-2 text-[10px] text-slate-400">
          报告内嵌 judgment hash / model / prompt_version 证据链。
        </p>
      </section>
    </main>
  );
}
