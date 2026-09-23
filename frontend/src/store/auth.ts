// 认证态（frontend-spec §3）：access token 仅存内存（Zustand），
// 禁止 localStorage；refresh 凭 HTTPOnly Cookie 由后端管理。
import { create } from "zustand";

interface AuthState {
  accessToken: string | null;
  email: string | null;
  // issue #75：角色仅用于前端入口显隐（知识库编辑/审核只对 admin 展示）；
  // 真正的权限判定始终在后端 require_role
  role: string | null;
  setSession: (token: string, email: string, role?: string) => void;
  // issue #43：整页刷新后凭 pt_email cookie 回填展示态（token 仍只走 refresh 恢复）
  setEmail: (email: string) => void;
  clear: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  accessToken: null,
  email: null,
  role: null,
  setSession: (token, email, role) =>
    set({ accessToken: token, email, role: role ?? null }),
  setEmail: (email) => set({ email }),
  // 刷新内存态时同步清除客户端标记 cookie（restoreSession 的登录信号）
  clear: () => {
    document.cookie = "pt_auth=; Max-Age=0; Path=/";
    // issue #43：展示态 cookie 同步清除，退出后不残留假登录信号
    document.cookie = "pt_email=; Max-Age=0; Path=/";
    set({ accessToken: null, email: null, role: null });
  },
}));
