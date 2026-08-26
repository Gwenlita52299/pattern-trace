"use client";
// 首页（frontend-spec §2 /）：地址查询输入 + 演示地址 chips。
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

import { api } from '@/lib/api';
import { useAnalysisStore } from '@/store/analysis';

export default function Home() {
  const router = useRouter();
  const startAnalysis = useAnalysisStore((s) => s.startAnalysis);
  const [address, setAddress] = useState('');
  const [hops, setHops] = useState(3);
  const [demoAddresses, setDemoAddresses] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<{ addresses: string[] }>('/demo/addresses')
      .then((d) => setDemoAddresses(d.addresses))
      .catch(() => setDemoAddresses([])); // 后端未启动时首页仍可用
  }, []);

  // 承接案件详情「发起分析」的跳转（cases/[id] 写入 ?address= / sessionStorage）
  useEffect(() => {
    const fromQuery = new URLSearchParams(window.location.search).get('address');
    const fromStorage = sessionStorage.getItem('pt_case_address');
    const addr = fromQuery ?? fromStorage;
    if (addr) {
      setAddress(addr);
      sessionStorage.removeItem('pt_case_address');
      window.history.replaceState(null, '', window.location.pathname);
    }
  }, []);

  async function submit(addr: string) {
    if (!addr.trim() || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const jid = await startAnalysis({ address: addr.trim(), hops });
      router.push(`/analyze/${jid}`); // FE-01：立即跳转，loading 在分析页呈现
    } catch (err) {
      setError((err as Error).message);
      setSubmitting(false);
    }
  }

  return (
    <main className="mx-auto flex max-w-3xl flex-col items-center px-4 py-24">
      <h1 className="text-3xl font-bold tracking-tight">
        Pattern<span className="text-cyan-600">Trace</span>
      </h1>
      <p className="mt-2 text-sm text-slate-500">
        BTC 链上洗钱模式追踪 · 子图构建 → 模式检索 → AI 判断
      </p>

      <form
        className="mt-10 w-full"
        onSubmit={(e) => {
          e.preventDefault();
          void submit(address);
        }}
      >
        <label htmlFor="address" className="mb-1 block text-xs font-medium text-slate-600">
          BTC 地址
        </label>
        <div className="flex gap-2">
          <input
            id="address"
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            placeholder="bc1q…"
            className="w-full rounded-lg border border-slate-300 px-3 py-2 font-mono text-sm outline-none focus:border-cyan-500"
          />
          <select
            value={hops}
            onChange={(e) => setHops(Number(e.target.value))}
            aria-label="分析跳数"
            className="rounded-lg border border-slate-300 bg-white px-2 text-sm"
          >
            {[1, 2, 3].map((h) => (
              <option key={h} value={h}>{h} hop</option>
            ))}
          </select>
          <button
            type="submit"
            disabled={submitting || !address.trim()}
            className="rounded-lg bg-cyan-600 px-5 py-2 text-sm font-medium text-white hover:bg-cyan-700 disabled:opacity-50"
          >
            {submitting ? '提交中…' : '分析'}
          </button>
        </div>

        {error && <p className="mt-2 text-xs text-red-600">{error}</p>}

        {demoAddresses.length > 0 && (
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <span className="text-xs text-slate-400">演示地址：</span>
            {demoAddresses.map((a) => (
              <button
                key={a}
                type="button"
                onClick={() => void submit(a)}
                className="max-w-[280px] truncate rounded-full border border-slate-200 bg-white px-3 py-1 font-mono text-[10px] text-slate-500 hover:border-cyan-400 hover:text-cyan-700"
              >
                {a}
              </button>
            ))}
          </div>
        )}
        <p className="mt-3 text-[11px] text-slate-400">
          免登录仅支持演示地址；登录后可分析任意地址。
        </p>
      </form>
    </main>
  );
}
