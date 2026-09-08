// api client 状态机单测（issue #57）：401→refresh→重放、refresh 单飞
// （防 reuse detection 竞态，#43）、on401 redirect/throw 分支、幂等重试。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAuthStore } from "@/store/auth";

const { api, ApiError, logout } = await import("@/lib/api");

function resp(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

const fetchMock = vi.fn();

function stubFetch() {
  vi.stubGlobal("fetch", fetchMock);
}

beforeEach(() => {
  fetchMock.mockReset();
  stubFetch();
  useAuthStore.setState({ accessToken: null, email: null });
  document.cookie = "pt_auth=; Max-Age=0; Path=/";
  document.cookie = "pt_email=; Max-Age=0; Path=/";
  // 用例间隔离：前一个用例的 redirect 会改写 location
  window.location.href = "http://localhost:3000/";
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("api 401 → refresh → 重放", () => {
  it("401 后凭 refresh cookie 换新 token 并带 Bearer 重放", async () => {
    let caseCalls = 0;
    let refreshCalls = 0;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.includes("/auth/refresh")) {
        refreshCalls++;
        return resp({ access_token: "t2", email: "a@b.c" });
      }
      caseCalls++;
      if (caseCalls === 1) return resp({}, 401);
      // 重放请求必须携带新 token
      expect((init!.headers as Record<string, string>).Authorization)
        .toBe("Bearer t2");
      return resp({ items: [] });
    });

    const result = await api<{ items: string[] }>("/cases");
    expect(result.items).toEqual([]);
    expect(refreshCalls).toBe(1);
    expect(useAuthStore.getState().accessToken).toBe("t2");
    expect(useAuthStore.getState().email).toBe("a@b.c");
  });

  it("并发 401 单飞：多个请求共享一次 refresh（防 reuse detection 撤销 token family）", async () => {
    let caseCalls = 0;
    let refreshCalls = 0;
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes("/auth/refresh")) {
        refreshCalls++;
        // 模拟网络延迟，让两个 401 调用真正并发撞车
        await new Promise((r) => setTimeout(r, 20));
        return resp({ access_token: "t2", email: "a@b.c" });
      }
      caseCalls++;
      return caseCalls <= 2 ? resp({}, 401) : resp({ items: [] });
    });

    const [a, b] = await Promise.all([
      api<{ items: string[] }>("/cases"),
      api<{ items: string[] }>("/cases"),
    ]);
    expect(a.items).toEqual([]);
    expect(b.items).toEqual([]);
    expect(refreshCalls).toBe(1);
  });

  it("refresh 失败 + 默认 redirect：清内存态、清标记 cookie、跳 /login", async () => {
    document.cookie = "pt_auth=1; Path=/";
    document.cookie = "pt_email=user%40x; Path=/";
    fetchMock.mockImplementation(async (url: string) =>
      url.includes("/auth/refresh") ? resp({}, 401) : resp({}, 401));

    await expect(api("/cases")).rejects.toMatchObject({ status: 401 });
    expect(window.location.pathname).toBe("/login");
    expect(document.cookie).not.toContain("pt_auth=1");
    expect(useAuthStore.getState().accessToken).toBeNull();
  });

  it("refresh 失败 + on401:'throw'：上抛 401，不跳转（cases 页内联请先登录）", async () => {
    fetchMock.mockImplementation(async (url: string) =>
      url.includes("/auth/refresh") ? resp({}, 401) : resp({}, 401));

    await expect(api("/cases", { on401: "throw" }))
      .rejects.toBeInstanceOf(ApiError);
    expect(window.location.pathname).toBe("/");
  });
});

describe("幂等 GET 网络类失败自动重试（FE-23）", () => {
  it("503 退避重试两次后成功", async () => {
    vi.useFakeTimers();
    let calls = 0;
    fetchMock.mockImplementation(async () => {
      calls++;
      return calls <= 2 ? resp({}, 503) : resp({ ok: 1 });
    });

    const pending = api<{ ok: number }>("/patterns");
    await vi.advanceTimersByTimeAsync(1200); // 300 + 600 退避
    expect(await pending).toEqual({ ok: 1 });
    expect(calls).toBe(3);
  });

  it("POST 不重试（仅幂等 GET）", async () => {
    fetchMock.mockResolvedValue(resp({}, 503));
    await expect(api("/cases", { method: "POST", body: {} }))
      .rejects.toMatchObject({ status: 503 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("204 No Content 与 logout（issue #68）", () => {
  it("204 响应不触发 JSON 解析错误，返回 null", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(api("/auth/logout", { method: "POST" }))
      .resolves.toBeNull();
  });

  it("logout 先 POST /auth/logout（带 CSRF 头），再清内存态与标记 cookie", async () => {
    useAuthStore.setState({ accessToken: "t1", email: "a@b.c" });
    document.cookie = "pt_auth=1; Path=/";
    document.cookie = "pt_email=a%40b.c; Path=/";
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));

    await logout();

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain("/auth/logout");
    expect(init.method).toBe("POST");
    expect(init.headers["X-Requested-With"]).toBe("XMLHttpRequest");
    expect(useAuthStore.getState().accessToken).toBeNull();
    expect(document.cookie).not.toContain("pt_auth=1");
    expect(document.cookie).not.toContain("a%40b.c");
  });

  it("logout 请求失败仍清本地态，但错误可观测", async () => {
    useAuthStore.setState({ accessToken: "t1", email: "a@b.c" });
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    fetchMock.mockRejectedValue(new TypeError("network down"));

    await logout();

    expect(useAuthStore.getState().accessToken).toBeNull();
    expect(errSpy).toHaveBeenCalled();
    errSpy.mockRestore();
  });
});
