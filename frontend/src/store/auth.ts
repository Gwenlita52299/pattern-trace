// 认证态（frontend-spec §3）：access token 仅存内存（Zustand），
// 禁止 localStorage；refresh 凭 HTTPOnly Cookie 由后端管理。
import { create } from "zustand";

interface AuthState {
  accessToken: string | null;
  email: string | null;
  setSession: (token: string, email: string) => void;
  // issue #43：整页刷新后凭 pt_email cookie 回填展示态（token 仍只走 refresh 恢复）
  setEmail: (email: string) => void;
  clear: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  accessToken: null,
  email: null,
  setSession: (token, email) =>
    set({ accessToken: token, email }),
  setEmail: (email) => set({ email }),
  // 刷新内存态时同步清除客户端标记 cookie（middleware 的登录信号）
  clear: () => {
    document.cookie = "pt_auth=; Max-Age=0; Path=/";
    // issue #43：展示态 cookie 同步清除，退出后不残留假登录信号
    document.cookie = "pt_email=; Max-Age=0; Path=/";
    set({ accessToken: null, email: null });
  },
}));
