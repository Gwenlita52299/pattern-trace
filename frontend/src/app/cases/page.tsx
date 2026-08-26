"use client";
// 案件列表 + 新建（frontend-spec §4 CaseList / FE-07）。
// 状态徽章颜色+文字双编码（FE-19）；未登录由 middleware 重定向。
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';

import { api } from '@/lib/api';
import { useAuthStore } from '@/store/auth';

interface CaseRow {
  id: string;
  title: string;
  status: string;
  created_at?: string | null;
}

const STATUS_BADGE: Record<string, { cls: string; label: string }> = {
  open: { cls: 'bg-blue-100 text-blue-700', label: 'Open' },
  investigating: { cls: 'bg-orange-100 text-orange-700', label: 'Investigating' },
  closed: { cls: 'bg-slate-200 text-slate-600', label: 'Closed' },
};

export default function CasesPage() {
  const router = useRouter();
  const email = useAuthStore((s) => s.email);
  const [cases, setCases] = useState<CaseRow[] | null>(null);
  const [title, setTitle] = useState('');
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api<{ items: CaseRow[] }>('/cases')
      .then((d) => setCases(d.items))
      .catch((err) => setError((err as Error).message));
  }, []);

  useEffect(() => load(), [load]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim() || creating) return;
    setCreating(true);
    setError(null);
    try {
      await api('/cases', { method: 'POST', body: { title: title.trim() } });
      setTitle('');
      load(); // FE-07：新案件立即出现在列表中
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setCreating(false);
    }
  }

  return (
    <main className="mx-auto max-w-5xl px-4 py-10">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">案件</h1>
        {email && <span className="text-xs text-slate-400">{email}</span>}
      </div>

      <form onSubmit={create} className="mt-4 flex gap-2">
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="新建案件标题，如 Test Case #001"
          className="w-full max-w-md rounded-lg border border-slate-300 px-3 py-2 text-sm"
        />
        <button
          type="submit"
          disabled={creating || !title.trim()}
          className="rounded-lg bg-cyan-600 px-4 py-2 text-sm font-medium text-white hover:bg-cyan-700 disabled:opacity-50"
        >
          新建案件
        </button>
      </form>

      {error && <p className="mt-3 text-sm text-red-600">{error}</p>}

      {!error && cases !== null && cases.length === 0 && (
        <p className="mt-6 text-sm text-slate-400">暂无案件——用上方表单创建第一个。</p>
      )}

      {cases && cases.length > 0 && (
        <table className="mt-6 w-full text-left text-sm">
          <thead>
            <tr className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-400">
              <th className="py-2">标题</th>
              <th className="py-2">状态</th>
              <th className="py-2">创建时间</th>
              <th className="py-2"></th>
            </tr>
          </thead>
          <tbody>
            {cases.map((c) => {
              const badge = STATUS_BADGE[c.status] ?? STATUS_BADGE.closed;
              return (
                <tr key={c.id} className="border-b border-slate-100 hover:bg-slate-50">
                  <td className="py-2">{c.title}</td>
                  <td className="py-2">
                    <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${badge.cls}`}>
                      {badge.label}
                    </span>
                  </td>
                  <td className="py-2 text-xs text-slate-400">{c.created_at?.slice(0, 19) ?? '-'}</td>
                  <td className="py-2 text-right">
                    <Link href={`/cases/${c.id}`} className="text-xs text-cyan-700 hover:underline">
                      详情 →
                    </Link>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </main>
  );
}
