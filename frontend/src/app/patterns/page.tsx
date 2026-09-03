"use client";
// Pattern 知识库列表：分页/筛选同步 URL searchParams（FE-18），
// 复制链接或刷新后状态保留。
import { useRouter, useSearchParams } from 'next/navigation';
import { Suspense, useEffect, useState } from 'react';

import { api } from '@/lib/api';

interface PatternRow {
  id: string;
  name: string;
  source: string;
  provenance: string;
  evidence_grade: string;
  seed_address: string;
  node_count?: number | null;
}

interface PageEnvelope {
  items: PatternRow[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

const PROVENANCE_LABELS: Record<string, string> = {
  confirmed: "真实确认",
  synthetic: "合成模板",
  negative: "负样本",
};

function provenanceLabel(p: string): string {
  return PROVENANCE_LABELS[p] ?? p;
}

export default function PatternsPage() {
  // 静态预渲染要求 useSearchParams 位于 Suspense 边界内
  return (
    <Suspense fallback={
      <main className="mx-auto max-w-5xl px-4 py-10">
        <p className="text-sm text-slate-400">加载中…</p>
      </main>
    }>
      <PatternsContent />
    </Suspense>
  );
}

function PatternsContent() {
  const router = useRouter();
  const search = useSearchParams();
  const page = Number(search.get('page') ?? '1');
  const grade = search.get('evidence_grade') ?? '';
  const prov = search.get('provenance') ?? '';
  const q = search.get('search') ?? '';

  const [data, setData] = useState<PageEnvelope | null>(null);
  const [error, setError] = useState<string | null>(null);

  function updateParams(patch: Record<string, string>) {
    const next = new URLSearchParams(search.toString());
    Object.entries(patch).forEach(([k, v]) => {
      if (v) next.set(k, v);
      else next.delete(k);
    });
    router.replace(`/patterns?${next.toString()}`);
  }

  useEffect(() => {
    setData(null);
    api<PageEnvelope>('/patterns', {
      params: {
        page,
        page_size: 20,
        evidence_grade: grade || undefined,
        provenance: prov || undefined,
        search: q || undefined,
      },
    })
      .then(setData)
      .catch((err) => setError((err as Error).message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, grade, prov, q]);

  return (
    <main className="mx-auto max-w-5xl px-4 py-10">
      <h1 className="text-xl font-bold">Pattern 知识库</h1>

      <div className="mt-4 flex flex-wrap items-center gap-3 text-sm">
        <select
          aria-label="证据等级筛选"
          value={grade}
          onChange={(e) => {
            const next = new URLSearchParams(search.toString());
            if (e.target.value) next.set('evidence_grade', e.target.value);
            else next.delete('evidence_grade');
            next.delete('page');
            router.replace(`/patterns?${next.toString()}`);
          }}
          className="rounded border border-slate-300 px-2 py-1"
        >
          <option value="">全部等级</option>
          <option value="A">Grade A</option>
          <option value="B">Grade B</option>
          <option value="S">Grade S(合成模板)</option>
        </select>
        <select
          aria-label="来源性质筛选"
          value={prov}
          onChange={(e) => {
            const next = new URLSearchParams(search.toString());
            if (e.target.value) next.set('provenance', e.target.value);
            else next.delete('provenance');
            next.delete('page');
            router.replace(`/patterns?${next.toString()}`);
          }}
          className="rounded border border-slate-300 px-2 py-1"
        >
          <option value="">全部来源</option>
          <option value="confirmed">真实确认</option>
          <option value="synthetic">合成模板</option>
        </select>
        {q && <span className="text-xs text-slate-400">search: {q}</span>}
        {data && (
          <span className="text-xs text-slate-400">
            共 {data.total} 条 · 第 {data.page}/{Math.max(data.pages, 1)} 页
          </span>
        )}
      </div>

      {error && <p className="mt-4 text-sm text-red-600">{error}</p>}

      {!error && data && (
        <>
          <table className="mt-4 w-full text-left text-sm">
            <thead>
              <tr className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-400">
                <th className="py-2">名称</th>
                <th className="py-2">来源</th>
                <th className="py-2">性质</th>
                <th className="py-2">等级</th>
                <th className="py-2">节点数</th>
                <th className="py-2">seed</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((p) => (
                <tr key={p.id} className="border-b border-slate-100 hover:bg-slate-50">
                  <td className="py-2 font-mono text-xs">{p.name}</td>
                  <td className="py-2 text-xs">{p.source}</td>
                  <td className="py-2 text-xs">
                    <span
                      className={
                        p.provenance === 'synthetic'
                          ? 'rounded bg-amber-50 px-1.5 py-0.5 font-medium text-amber-700'
                          : 'rounded bg-emerald-50 px-1.5 py-0.5 font-medium text-emerald-700'
                      }
                      title={
                        p.provenance === 'synthetic'
                          ? '合成结构模板，非真实链上交易，仅作检索参考'
                          : '真实链上确认样本'
                      }
                    >
                      {provenanceLabel(p.provenance)}
                    </span>
                  </td>
                  <td className="py-2 text-xs">{p.evidence_grade}</td>
                  <td className="py-2 text-xs">{p.node_count ?? '-'}</td>
                  <td className="max-w-[220px] truncate py-2 font-mono text-[10px] text-slate-400">
                    {p.seed_address}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="mt-4 flex gap-2 text-sm">
            <button
              disabled={page <= 1}
              onClick={() => updateParams({ page: String(page - 1) })}
              className="rounded border border-slate-300 px-3 py-1 disabled:opacity-40"
            >
              上一页
            </button>
            <button
              disabled={!!data && page >= data.pages}
              onClick={() => updateParams({ page: String(page + 1) })}
              className="rounded border border-slate-300 px-3 py-1 disabled:opacity-40"
            >
              下一页
            </button>
          </div>
        </>
      )}
    </main>
  );
}
