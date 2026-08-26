// 路由保护（frontend-spec §2）：middleware 仅检查登录存在性信号，
// 验签由后端 401 兜底。/analyze/* 明确豁免（免登录演示，FE-09）。
import { NextResponse, type NextRequest } from 'next/server';

export function middleware(req: NextRequest) {
  if (req.nextUrl.pathname.startsWith('/cases')) {
    // 内存 access token 对服务端不可见；用 pt_auth 标记 cookie 作代理信号
    if (!req.cookies.has('pt_auth')) {
      const url = req.nextUrl.clone();
      url.pathname = '/login';
      url.search = '';
      url.searchParams.set('next', req.nextUrl.pathname);
      return NextResponse.redirect(url); // FE-08：/login?next=/cases
    }
  }
  return NextResponse.next();
}

export const config = {
  matcher: ['/cases/:path*'],
};
