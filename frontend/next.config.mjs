// API 同源代理（修复「手动刷新后掉登录」）：浏览器只与前端同源通信，
// refresh cookie 永远 same-site（127.0.0.1/localhost 混用不再触发
// SameSite=Lax 拦截），下载链接也天然同源。
// 注意：rewrites 在 next build 时烘焙进 routes-manifest，目的地必须在
// 构建期就位——本地 dev 默认 http://localhost:8000；docker 经 build arg
// 注入 http://backend:8000（容器网络内 localhost 不是后端）。
const config = {
  async rewrites() {
    const dest = process.env.API_PROXY_URL ?? "http://localhost:8000";
    return [
      {
        source: "/api/v1/:path*",
        destination: `${dest}/api/v1/:path*`,
      },
    ];
  },
};

export default config;
