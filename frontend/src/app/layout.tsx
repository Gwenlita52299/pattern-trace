import type { Metadata } from 'next';
import Link from 'next/link';
import './globals.css';

export const metadata: Metadata = {
  title: 'PatternTrace',
  description: 'BTC 链上模式追踪分析平台',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen bg-slate-50 text-slate-900">
        <header className="border-b border-slate-200 bg-white">
          <nav className="mx-auto flex max-w-6xl items-center gap-6 px-4 py-3">
            <Link href="/" className="font-bold tracking-tight">
              Pattern<span className="text-cyan-600">Trace</span>
            </Link>
            <Link href="/" className="text-sm text-slate-600 hover:text-slate-900">分析</Link>
            <Link href="/patterns" className="text-sm text-slate-600 hover:text-slate-900">Patterns</Link>
            <Link href="/cases" className="text-sm text-slate-600 hover:text-slate-900">Cases</Link>
            <div className="ml-auto">
              <Link href="/login" className="text-sm text-slate-600 hover:text-slate-900">登录</Link>
            </div>
          </nav>
        </header>
        {children}
      </body>
    </html>
  );
}
