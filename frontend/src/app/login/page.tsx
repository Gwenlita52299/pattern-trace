"use client";
// 登录页（frontend-spec §2 /login）：成功后 access token 入内存 store，
// 另设 pt_auth 标记 cookie 供 middleware 做登录信号（验签由后端 401 兜底）。
import { useRouter, useSearchParams } from 'next/navigation';
import { Suspense, useState } from 'react';

import { api } from '@/lib/api';
import { useAuthStore } from '@/store/auth';

export default function LoginPage() {
  // 静态预渲染要求 useSearchParams 位于 Suspense 边界内
  return (
    <Suspense fallback={
      <main className="mx-auto flex max-w-sm flex-col px-4 py-24">
        <p className="text-sm text-slate-400">加载中…</p>
      </main>
    }>
      <LoginContent />
    </Suspense>
  );
}

function LoginContent() {
  const router = useRouter();
  const search = useSearchParams();
  const nextPath = search.get('next') ?? '/cases';

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const resp = await api<{
        access_token: string;
        user: { email: string; role: string };
      }>('/auth/login', { method: 'POST', body: { email, password } });
      useAuthStore.getState().setSession(resp.access_token, resp.user.email);
      // middleware 的登录存在性信号（生产由后端 Set-Cookie refresh 承担）
      document.cookie = 'pt_auth=1; Path=/; SameSite=Lax; max-age=86400';
      router.push(nextPath);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex max-w-sm flex-col px-4 py-24">
      <h1 className="text-xl font-bold">登录 PatternTrace</h1>
      <form onSubmit={submit} className="mt-6 space-y-3">
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="email"
          autoComplete="username"
          required
          className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
        />
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="password"
          autoComplete="current-password"
          required
          className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
        />
        {error && <p className="text-xs text-red-600">{error}</p>}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-lg bg-cyan-600 py-2 text-sm font-medium text-white hover:bg-cyan-700 disabled:opacity-50"
        >
          {busy ? '登录中…' : '登录'}
        </button>
      </form>
    </main>
  );
}
