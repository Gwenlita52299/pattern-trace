import type { Metadata } from 'next';
import Link from 'next/link';
import './globals.css';

import UserMenu from '@/components/UserMenu';

export const metadata: Metadata = {
  title: 'PatternTrace',
  description: 'BTC 链上模式追踪分析平台',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen bg-pt-bg font-sans text-pt-ink antialiased">
        <header className="border-b border-pt-line bg-pt-panel">
          <nav className="mx-auto flex max-w-6xl items-center gap-6 px-4 py-3">
            <Link href="/" className="flex items-center gap-2 font-mono text-sm font-semibold tracking-widest">
              <span aria-hidden className="inline-block h-3.5 w-3.5 border border-pt-amber">
                <span className="m-[3px] block h-2 w-2 bg-pt-amber" />
              </span>
              PatternTrace
            </Link>
            <Link href="/" className="text-sm text-pt-muted hover:text-pt-ink">分析</Link>
            <Link href="/patterns" className="text-sm text-pt-muted hover:text-pt-ink">Patterns</Link>
            {/* prefetch={false}：未登录时 prefetch /cases 会把 middleware 的 307
                重定向产物缓存进 Router Cache，登录后 push 命中缓存被弹回 /login */}
            <Link href="/cases" prefetch={false} className="text-sm text-pt-muted hover:text-pt-ink">Cases</Link>
            <div className="ml-auto">
              <UserMenu />
            </div>
          </nav>
        </header>
        {children}
      </body>
    </html>
  );
}
