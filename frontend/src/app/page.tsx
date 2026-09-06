"use client";
// 首页（frontend-spec §2 /）：地址查询输入 + 演示地址 chips。
import { useRouter } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';

import { api } from '@/lib/api';
import { useAnalysisStore } from '@/store/analysis';

export default function Home() {
  const router = useRouter();
  const startAnalysis = useAnalysisStore((s) => s.startAnalysis);
  const [address, setAddress] = useState('');
  const addressRef = useRef<HTMLInputElement>(null);
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
      <h1 className="font-mono text-2xl font-semibold tracking-widest">
        PatternTrace
      </h1>
      <p className="mt-3 text-sm tracking-wide text-pt-muted">
        BTC 链上洗钱模式追踪 · 子图构建 → 前科比对 → AI 裁决
      </p>

      <form
        className="mt-10 w-full"
        onSubmit={(e) => {
          e.preventDefault();
          void submit(address);
        }}
      >
        <label htmlFor="address" className="mb-1 block text-[10px] uppercase tracking-[0.2em] text-pt-muted">
          标的地址
        </label>
        <div className="flex gap-2">
          <input
            id="address"
            ref={addressRef}
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            placeholder="bc1q…"
            className="w-full rounded-lg border border-pt-line bg-pt-panel px-3 py-2 font-mono text-sm text-pt-ink outline-none focus:border-pt-amber"
          />
          <select
            value={hops}
            onChange={(e) => setHops(Number(e.target.value))}
            aria-label="分析跳数"
            className="rounded-lg border border-pt-line bg-pt-panel-2 px-2 font-mono text-sm text-pt-muted"
          >
            {[1, 2, 3].map((h) => (
              <option key={h} value={h}>{h} hop</option>
            ))}
          </select>
          <button
            type="submit"
            disabled={submitting || !address.trim()}
            className="rounded-lg bg-pt-amber px-5 py-2 text-sm font-semibold text-[#201601] hover:bg-pt-amber-hi disabled:opacity-50"
          >
            {submitting ? '提交中…' : '展开子图并比对'}
          </button>
        </div>

        {error && <p className="mt-2 text-xs text-red-400">{error}</p>}

        {demoAddresses.length > 0 && (
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <span className="text-xs text-pt-faint">演示地址：</span>
            {demoAddresses.map((a) => (
              // 仅填充输入框（issue #41）：用户查看/修改后手动触发分析
              <button
                key={a}
                type="button"
                onClick={() => {
                  setAddress(a);
                  addressRef.current?.focus();
                }}
                className="max-w-[280px] truncate rounded-full border border-pt-line bg-pt-panel px-3 py-1 font-mono text-[10px] text-pt-muted hover:border-pt-amber hover:text-pt-amber-hi"
              >
                {a}
              </button>
            ))}
          </div>
        )}
        <p className="mt-3 text-[11px] text-pt-faint">
          免登录仅支持演示地址；登录后可分析任意地址。
        </p>
      </form>
    </main>
  );
}
