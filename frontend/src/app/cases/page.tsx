"use client";
// 案件列表 + 新建（frontend-spec §4 CaseList / FE-07）。
// 状态徽章颜色+文字双编码（FE-19）；未登录不再跳 /login，本页内联
// 提示「请先登录」，登录成功（store 的 accessToken 出现）后自动刷新列表。
import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';

import { api, ApiError } from '@/lib/api';
import { useAuthStore } from '@/store/auth';

interface CaseRow {
  id: string;
  title: string;
  status: string;
  created_at?: string | null;
}

const STATUS_BADGE: Record<string, { cls: string; label: string }> = {
  open: { cls: 'border border-pt-medium/40 text-[#9cc0ff]', label: 'Open' },
  investigating: { cls: 'border border-pt-amber/40 text-pt-amber-hi', label: 'Investigating' },
  closed: { cls: 'border border-pt-line text-pt-muted', label: 'Closed' },
};

export default function CasesPage() {
  const email = useAuthStore((s) => s.email);
  const accessToken = useAuthStore((s) => s.accessToken);
  const [cases, setCases] = useState<CaseRow[] | null>(null);
  const [title, setTitle] = useState('');
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [needLogin, setNeedLogin] = useState(false);

  const load = useCallback(() => {
    api<{ items: CaseRow[] }>('/cases', { on401: 'throw' })
      .then((d) => {
        setCases(d.items);
        setNeedLogin(false);
        setError(null);
      })
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) {
          setNeedLogin(true); // 未登录：内联提示，等登录后 accessToken 变化自动重拉
        } else {
          setError((err as Error).message);
        }
      });
  }, []);

  useEffect(() => load(), [load, accessToken]);

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
        <h1 className="text-xl font-bold">卷宗</h1>
        {email && <span className="font-mono text-xs text-pt-faint">{email}</span>}
      </div>

      {needLogin ? (
        <p className="mt-6 text-sm text-pt-muted">
          请先登录——点击右上角「登录」后本页会自动刷新。
        </p>
      ) : (
        <>
          <form onSubmit={create} className="mt-4 flex gap-2">
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="新建卷宗标题，如 Case #001"
              className="w-full max-w-md rounded-lg border border-pt-line bg-pt-panel px-3 py-2 font-mono text-sm text-pt-ink outline-none focus:border-pt-amber"
            />
            <button
              type="submit"
              disabled={creating || !title.trim()}
              className="rounded-lg bg-pt-amber px-4 py-2 text-sm font-semibold text-[#201601] hover:bg-pt-amber-hi disabled:opacity-50"
            >
              新建卷宗
            </button>
          </form>

          {error && <p className="mt-3 text-sm text-red-400">{error}</p>}

          {!error && cases !== null && cases.length === 0 && (
            <p className="mt-6 text-sm text-pt-faint">暂无卷宗——用上方表单创建第一个。</p>
          )}

          {cases && cases.length > 0 && (
            <table className="mt-6 w-full text-left text-sm">
              <thead>
                <tr className="border-b border-pt-line text-[10px] uppercase tracking-[0.18em] text-pt-muted">
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
                    <tr key={c.id} className="border-b border-white/5 hover:bg-pt-panel">
                      <td className="py-2 font-mono text-xs">{c.title}</td>
                      <td className="py-2">
                        <span className={`rounded-md px-2.5 py-0.5 text-[10px] tracking-widest ${badge.cls}`}>
                          {badge.label}
                        </span>
                      </td>
                      <td className="py-2 font-mono text-xs text-pt-faint">{c.created_at?.slice(0, 19) ?? '-'}</td>
                      <td className="py-2 text-right">
                        <Link href={`/cases/${c.id}`} className="text-xs text-pt-amber-hi hover:underline">
                          详情 →
                        </Link>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </>
      )}
    </main>
  );
}
