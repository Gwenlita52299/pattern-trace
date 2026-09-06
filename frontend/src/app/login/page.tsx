"use client";
// 登录页（frontend-spec §2 /login）：middleware 307 重定向与 API 401 兜底
// 仍落在这里；表单抽到了 LoginForm（issue #42），与导航栏弹窗复用。
import { useSearchParams } from 'next/navigation';
import { Suspense } from 'react';

import LoginForm from '@/components/LoginForm';

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
  const nextPath = search.get('next') ?? '/cases';

  return (
    <main className="mx-auto flex max-w-sm flex-col px-4 py-24">
      <h1 className="font-mono text-lg font-semibold tracking-widest">登录 PatternTrace</h1>
      <div className="mt-6">
        <LoginForm redirectTo={nextPath} />
      </div>
    </main>
  );
}
