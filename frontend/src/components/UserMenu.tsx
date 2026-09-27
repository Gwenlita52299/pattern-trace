"use client";
// 导航栏右侧认证入口（issue #42）：未登录显示「登录」按钮，点击弹居中
// 模态框（窄屏降级跳 /login 页）；已登录显示头像下拉（用户中心）。
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';

import LoginForm from '@/components/LoginForm';
import { logout as serverLogout, restoreSession } from '@/lib/api';
import { useAuthStore } from '@/store/auth';

export default function UserMenu() {
  const email = useAuthStore((s) => s.email);
  // 入口显隐用；真正的权限判定在后端 require_role("admin")
  const role = useAuthStore((s) => s.role);
  const router = useRouter();
  const [loginOpen, setLoginOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // issue #43：整页刷新后内存态清空，挂载时凭标记 cookie 恢复
  // 展示态与 access token；仅客户端执行，SSR 渲染未登录态不匹配无关
  useEffect(() => {
    restoreSession();
  }, []);

  function openLogin() {
    // 窄屏降级：小模态框在手机上难以操作，直接走独立登录页
    if (typeof window !== 'undefined' && !window.matchMedia('(min-width: 640px)').matches) {
      router.push('/login');
      return;
    }
    setLoginOpen(true);
  }

  // issue #68：退出先请求服务端 /auth/logout 撤销 refresh token family
  // （HttpOnly cookie 前端删不掉），完成后再清内存态并导航
  async function logout() {
    setMenuOpen(false);
    await serverLogout();
    router.push('/');
  }

  // Esc 关闭弹窗
  useEffect(() => {
    if (!loginOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setLoginOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [loginOpen]);

  // 点击下拉外部关闭
  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    window.addEventListener('mousedown', onDown);
    return () => window.removeEventListener('mousedown', onDown);
  }, [menuOpen]);

  if (email) {
    const initial = email[0]?.toUpperCase() ?? '?';
    return (
      <div ref={menuRef} className="relative">
        <button
          onClick={() => setMenuOpen((v) => !v)}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          className="flex items-center gap-2 text-sm text-pt-muted hover:text-pt-ink"
        >
          <span aria-hidden className="flex h-7 w-7 items-center justify-center rounded-full border border-pt-amber bg-pt-panel font-mono text-xs font-semibold text-pt-amber">
            {initial}
          </span>
          <span aria-hidden className="text-xs">▾</span>
        </button>
        {menuOpen && (
          <div role="menu" className="absolute right-0 top-10 z-50 w-56 rounded-lg border border-pt-line bg-pt-panel p-1 shadow-xl">
            <p className="truncate px-3 py-2 font-mono text-xs text-pt-muted">{email}</p>
            <Link
              href="/cases"
              onClick={() => setMenuOpen(false)}
              className="block rounded-md px-3 py-2 text-sm text-pt-ink hover:bg-pt-panel-2"
            >
              我的案例
            </Link>
            {role === 'admin' && (
              <Link
                href="/settings"
                onClick={() => setMenuOpen(false)}
                className="block rounded-md px-3 py-2 text-sm text-pt-ink hover:bg-pt-panel-2"
              >
                配置
              </Link>
            )}
            <button
              onClick={logout}
              className="block w-full rounded-md px-3 py-2 text-left text-sm text-pt-muted hover:bg-pt-panel-2 hover:text-pt-ink"
            >
              退出登录
            </button>
          </div>
        )}
      </div>
    );
  }

  return (
    <>
      <button onClick={openLogin} className="text-sm text-pt-muted hover:text-pt-ink">
        登录
      </button>
      {loginOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div aria-hidden className="absolute inset-0 bg-black/60" onClick={() => setLoginOpen(false)} />
          <div
            role="dialog"
            aria-modal="true"
            aria-label="登录 PatternTrace"
            className="relative w-full max-w-sm rounded-lg border border-pt-line bg-pt-bg p-5 shadow-xl"
          >
            <button
              onClick={() => setLoginOpen(false)}
              aria-label="关闭"
              className="absolute right-3 top-2 text-lg text-pt-faint hover:text-pt-ink"
            >
              ×
            </button>
            <h2 className="font-mono text-base font-semibold tracking-widest">登录 PatternTrace</h2>
            <div className="mt-4">
              <LoginForm onSuccess={() => setLoginOpen(false)} />
            </div>
          </div>
        </div>
      )}
    </>
  );
}
