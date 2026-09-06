// API client（frontend-spec §6）：Bearer + CSRF header 自动附加、15s 超时、
// 幂等 GET 网络类失败自动重试 ×2、同类 pending 请求取消（防竞态）、
// 401 → refresh → 重放一次；refresh 失败清内存态跳 /login。
import { useAuthStore } from "@/store/auth";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

const TIMEOUT_MS = 15_000;

export class ApiError extends Error {
  status: number;
  errorCode?: string;

  constructor(status: number, message: string, errorCode?: string) {
    super(message);
    this.status = status;
    this.errorCode = errorCode;
  }
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  params?: Record<string, string | number | undefined | null>;
  signal?: AbortSignal; // 调用方取消（轮询/组件卸载）
  /** 默认 GET 视为幂等：502/503/504 指数退避重试最多 2 次 */
  idempotent?: boolean;
  /** 401 后已尝试过 refresh 的防循环标记（内部使用） */
  _authRetried?: boolean;
}

const pendingByPath = new Map<string, AbortController>();

async function doRefresh(): Promise<boolean> {
  try {
    const resp = await fetch(`${API_BASE}/auth/refresh`, {
      method: "POST",
      credentials: "include",
      // CSRF 中间件对一切写请求强制校验，裸 fetch 也不例外
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });
    if (!resp.ok) return false;
    const data = (await resp.json()) as { access_token?: string; email?: string };
    if (!data.access_token) return false;
    const current = useAuthStore.getState();
    // email：issue #43，后端 refresh 响应携带，刷新后据此恢复用户中心展示
    useAuthStore.getState().setSession(
      data.access_token, data.email ?? current.email ?? "");
    return true;
  } catch {
    return false;
  }
}

// 单飞（issue #43）：挂载恢复与 401 被动刷新可能并发；refresh 轮换一次性，
// 并发的第二个请求会撞 reuse detection 撤销整个 token family，强制全员重新登录
let refreshInFlight: Promise<boolean> | null = null;

function refreshAccessToken(): Promise<boolean> {
  refreshInFlight ??= doRefresh().finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
}

// issue #43：整页刷新后内存 access token 清空。检测 pt_auth 标记存在时，
// 先用非敏感 pt_email cookie 即时回填用户中心展示态，再主动 refresh 拿回
// access token——避免刷新后首个 API 请求被动走 401→refresh→重放。
export async function restoreSession(): Promise<void> {
  if (useAuthStore.getState().accessToken) return;
  const cookies = document.cookie.split("; ").reduce<Record<string, string>>(
    (acc, kv) => {
      const i = kv.indexOf("=");
      if (i > 0) acc[kv.slice(0, i)] = decodeURIComponent(kv.slice(i + 1));
      return acc;
    }, {});
  if (!cookies.pt_auth) return;
  if (cookies.pt_email) useAuthStore.getState().setEmail(cookies.pt_email);
  const ok = await refreshAccessToken();
  if (!ok) {
    // refresh cookie 已失效：清掉全部标记 cookie，导航栏不留假登录态
    useAuthStore.getState().clear();
  }
}

function buildUrl(path: string, params?: RequestOptions["params"]): string {
  const qs = new URLSearchParams();
  Object.entries(params ?? {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  });
  const query = qs.toString();
  return `${API_BASE}${path}${query ? `?${query}` : ""}`;
}

export async function api<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const url = buildUrl(path, opts.params);
  const method = opts.method ?? "GET";
  const pathKey = `${method} ${url.split("?")[0]}${url.includes("?") ? url.slice(url.indexOf("?")) : ""}`;

  // FE-24：同 URL+params 的旧请求直接取消，防止快速切换地址的竞态覆盖
  let externalCtrl: AbortController | undefined;
  if (!opts.signal) {
    pendingByPath.get(pathKey)?.abort();
    externalCtrl = new AbortController();
    pendingByPath.set(pathKey, externalCtrl);
  }

  const retriable = () => opts.idempotent ?? method === "GET";

  for (let attempt = 0; ; attempt++) {
    const ctrl = opts.signal ? undefined : externalCtrl;
    const timer = ctrl ? setTimeout(() => ctrl.abort(), TIMEOUT_MS) : undefined;
    try {
      const token = useAuthStore.getState().accessToken;
      const resp = await fetch(url, {
        method,
        headers: {
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          // CSRF 双重防护：不可被 HTML 表单伪造的自定义头（FE-21）
          "X-Requested-With": "XMLHttpRequest",
          ...(opts.body !== undefined ? { "Content-Type": "application/json" } : {}),
        },
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        credentials: "include",
        signal: opts.signal ?? ctrl?.signal,
      });

      if (resp.status === 401 && !opts._authRetried) {
        if (await refreshAccessToken()) {
          return api<T>(path, { ...opts, _authRetried: true });
        }
        useAuthStore.getState().clear(); // FE-11：清空内存态并回登录页
        window.location.href = "/login";
        throw new ApiError(401, "session expired");
      }

      if (!resp.ok) {
        const networkish = resp.status === 502 || resp.status === 503 || resp.status === 504;
        if (networkish && attempt < 2 && retriable()) {
          await new Promise((r) => setTimeout(r, 300 * 2 ** attempt));
          continue; // FE-23：幂等 GET 自动重试
        }
        let detail = `${resp.status} ${resp.statusText}`;
        let errorCode: string | undefined;
        try {
          const problem = (await resp.json()) as { detail?: string; error_code?: string };
          detail = problem.detail ?? detail;
          errorCode = problem.error_code;
        } catch {
          /* 非 JSON 错误体保留默认文案 */
        }
        throw new ApiError(resp.status, detail, errorCode);
      }

      return (await resp.json()) as T;
    } catch (err) {
      if (err instanceof ApiError) throw err;
      if ((err as Error).name === "AbortError") throw err;
      // 网络异常：幂等请求退避重试，否则上抛
      if (attempt < 2 && retriable()) {
        await new Promise((r) => setTimeout(r, 300 * 2 ** attempt));
        continue;
      }
      throw new ApiError(0, `network error: ${(err as Error).message}`);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }
}
