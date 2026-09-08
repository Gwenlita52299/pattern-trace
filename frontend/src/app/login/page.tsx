"use client";
// 登录页（frontend-spec §2 /login）：API 401 兜底与直接访问的落点；
// 表单抽到了 LoginForm（issue #42），与导航栏弹窗复用。
import { useSearchParams } from 'next/navigation';
import { Suspense } from 'react';

import LoginForm from '@/components/LoginForm';
import { sanitizeNextPath } from '@/lib/next-path';

export default function LoginPage() {
  // 静态预渲染要求 useSearchParams 位于 Suspense 边界内
  return (
    <Suspense fallback={
      <main className="mx-auto flex max-w-sm flex-col px-4 py-24">
        <p className="font-mono text-sm tracking-widest text-pt-faint">加载中…</p>
      </main>
    }>
      <LoginContent />
    </Suspense>
  );
}

function LoginContent() {
  const search = useSearchParams();
  // issue #70：next 不可信，只放行站内绝对路径，非法值回退 /cases
  const nextPath = sanitizeNextPath(search.get('next'));

  return (
    <main className="mx-auto flex max-w-sm flex-col px-4 py-24">
      <h1 className="font-mono text-lg font-semibold tracking-widest">登录 PatternTrace</h1>
      <div className="mt-6">
        <LoginForm redirectTo={nextPath} />
      </div>
    </main>
  );
}
