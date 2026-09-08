// issue #70：登录后跳转目标只放行站内绝对路径。
// `next` 来自 URL 查询参数，是不可信输入，直传 router.push() 存在
// javascript: URL 执行与开放重定向（钓鱼）风险。
// 拒绝：非 / 开头、//（协议相对）、反斜杠（浏览器可将其规范化为 /，
// 使 /\\evil 退化为 //evil）、冒号（站内路由不含冒号，一并拦掉
// 编码/变体协议）与控制字符（%0a 等经 useSearchParams 已解码）。
export function sanitizeNextPath(
  raw: string | null | undefined,
  fallback = '/cases',
): string {
  if (!raw) return fallback;
  if (!raw.startsWith('/') || raw.startsWith('//')) return fallback;
  if (/[\u0000-\u001f\u007f\\:]/.test(raw)) return fallback;
  return raw;
}
