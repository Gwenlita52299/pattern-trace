"use client";
// 公共登录表单（issue #42）：/login 页与导航栏弹窗复用。
// 成功后 access token 入内存 store，另设 pt_auth 标记 cookie 供
// middleware 做登录信号（验签由后端 401 兜底）。
import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api } from '@/lib/api';
import { useAuthStore } from '@/store/auth';

interface LoginFormProps {
  // 登录成功后的跳转目标；不传则留在原地（弹窗场景）
  redirectTo?: string;
  // 登录成功回调（弹窗用它关闭自己）
  onSuccess?: () => void;
}

export default function LoginForm({ redirectTo, onSuccess }: LoginFormProps) {
  const router = useRouter();
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
      // middleware 的登录存在性信号（生产由后端 Set-Cookie refresh 承担）。
      // TTL 对齐后端 refresh token 的 7 天：若短于 refresh 生命周期，
      // 第 2 天起导航栏就会显示未登录，而会话实际仍可 refresh 续命
      document.cookie =
        'pt_auth=1; Path=/; SameSite=Lax; max-age=604800';
      // 非敏感展示信息（issue #43）：供整页刷新后回填用户中心，
      // access token 本体绝不落 cookie（frontend-spec §3）
      document.cookie =
        `pt_email=${encodeURIComponent(resp.user.email)}; Path=/; SameSite=Lax; max-age=604800`;
      if (redirectTo) {
        // 必须软导航：硬导航整页刷新会清空内存 token，此后每个请求都要走
        // refresh——而 refresh cookie 是 SameSite=lax，前端经 127.0.0.1 访问时
        // 对 localhost:8000 是 cross-site，cookie 不被携带，登录后立即被弹回。
        // 软导航保留内存 token，GET /cases 直接带 Bearer 成功。
        // （Nav 对 /cases 的 prefetch 已关，见 layout.tsx，Router Cache 不会被
        // middleware 307 污染，push 能真实到达 /cases。）
        router.push(redirectTo);
      }
      onSuccess?.();
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-3">
      <input
        type="email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        placeholder="email"
        autoComplete="username"
        required
        className="w-full rounded-lg border border-pt-line bg-pt-panel px-3 py-2 font-mono text-sm text-pt-ink outline-none focus:border-pt-amber"
      />
      <input
        type="password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        placeholder="password"
        autoComplete="current-password"
        required
        className="w-full rounded-lg border border-pt-line bg-pt-panel px-3 py-2 font-mono text-sm text-pt-ink outline-none focus:border-pt-amber"
      />
      {error && <p className="text-xs text-red-400">{error}</p>}
      <button
        type="submit"
        disabled={busy}
        className="w-full rounded-lg bg-pt-amber py-2 text-sm font-semibold text-[#201601] hover:bg-pt-amber-hi disabled:opacity-50"
      >
        {busy ? '登录中…' : '登录'}
      </button>
    </form>
  );
}
