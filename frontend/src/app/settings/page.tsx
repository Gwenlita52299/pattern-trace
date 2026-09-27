"use client";
// 管理端 provider 配置（前端「配置」页）：自选 provider + API 密钥 + 自定义地址/模型。
//
// 纯逻辑（类型/文案/提交体/校验）在 lib/provider-config.ts，这里只管渲染与请求。
// 权限只做入口显隐——真正的判定在后端 require_role("admin")。
import { useCallback, useEffect, useState } from "react";

import { api, ApiError, restoreSession } from "@/lib/api";
import {
  buildPayload,
  formFromConfig,
  keySourceLabel,
  sourceLabel,
  validateForm,
} from "@/lib/provider-config";
import type {
  ProviderConfigResponse,
  ProviderForm,
} from "@/lib/provider-config";
import { useAuthStore } from "@/store/auth";

export default function SettingsPage() {
  const role = useAuthStore((s) => s.role);
  const [cfg, setCfg] = useState<ProviderConfigResponse | null>(null);
  const [form, setForm] = useState<ProviderForm | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [customOpen, setCustomOpen] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const data = await api<ProviderConfigResponse>(
        "/admin/provider-config", { on401: "throw" });
      setCfg(data);
      setForm(formFromConfig(data));
    } catch (err) {
      setError(err instanceof ApiError && err.status === 403
        ? "仅管理员可配置 provider"
        : err instanceof Error ? err.message : "加载失败");
    }
  }, []);

  // 整页刷新后 role 由 refresh 响应恢复（后端已带 user.role）；未恢复前
  // 先触发一次恢复，避免管理员刷新后被误判成非管理员
  useEffect(() => {
    restoreSession().then(() => {
      if (useAuthStore.getState().role === "admin") load();
    });
  }, [load]);

  useEffect(() => {
    if (role === "admin" && !cfg) load();
  }, [role, cfg, load]);

  async function save() {
    if (!form || !cfg) return;
    const provider = cfg.providers.find((p) => p.name === form.provider);
    const invalid = validateForm(
      form, provider, cfg.active, cfg.secrets_key_configured);
    if (invalid) {
      setError(invalid);
      setNotice(null);
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const next = await api<ProviderConfigResponse>(
        "/admin/provider-config",
        { method: "PUT", body: buildPayload(form), on401: "throw" });
      setCfg(next);
      setForm(formFromConfig(next));
      setNotice("已保存，后续分析立即使用新配置");
    } catch (err) {
      setError(err instanceof ApiError
        ? `${err.message}${err.errorCode ? `（${err.errorCode}）` : ""}`
        : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function resetToEnv() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const next = await api<ProviderConfigResponse>(
        "/admin/provider-config", { method: "DELETE", on401: "throw" });
      setCfg(next);
      setForm(formFromConfig(next));
      setNotice("已清除面板配置，回落到环境变量");
    } catch (err) {
      setError(err instanceof Error ? err.message : "重置失败");
    } finally {
      setBusy(false);
    }
  }

  if (!role || role !== "admin") {
    return (
      <main className="mx-auto max-w-3xl px-4 py-10">
        <h1 className="font-mono text-lg font-semibold tracking-widest">配置</h1>
        <p className="mt-4 text-sm text-pt-muted">
          {error ?? "仅管理员可配置 provider。"}
        </p>
      </main>
    );
  }

  const inputCls =
    "w-full rounded-lg border border-pt-line bg-pt-panel px-3 py-2 font-mono " +
    "text-sm text-pt-ink outline-none placeholder:text-pt-faint " +
    "focus:border-pt-amber";

  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <h1 className="font-mono text-lg font-semibold tracking-widest">配置</h1>
      <p className="mt-1 text-xs text-pt-muted">
        provider 由管理员统一配置；保存后立即生效（worker 进程 ≤5s）。
      </p>

      {cfg && (
        <p className="mt-3 text-xs text-pt-faint">
          当前生效：
          <span className="font-mono text-pt-muted">
            {cfg.active.provider} · {cfg.active.model}
          </span>
          <span className="mx-2">|</span>
          来源：{sourceLabel(cfg.active.source)}
          <span className="mx-2">|</span>
          密钥：{keySourceLabel(cfg.active.key_source)}
          {cfg.active.updated_by && (
            <>
              <span className="mx-2">|</span>
              最后修改：{cfg.active.updated_by}
            </>
          )}
        </p>
      )}

      <section className="mt-6 rounded-xl border border-pt-line bg-pt-panel p-5">
        <label className="block text-sm text-pt-ink" htmlFor="provider">
          提供方
        </label>
        <select
          id="provider"
          value={form?.provider ?? ""}
          disabled={!form}
          onChange={(e) => form
            && setForm({ ...form, provider: e.target.value, clearKey: false })}
          className={`${inputCls} mt-2`}
        >
          {(cfg?.providers ?? []).map((p) => (
            <option key={p.name} value={p.name} disabled={p.switchable === false}>
              {p.name}
              {p.switchable === false ? "（不可切换）" : ""}
            </option>
          ))}
        </select>

        <label className="mt-4 block text-sm text-pt-ink" htmlFor="api_key">
          API 密钥
        </label>
        <input
          id="api_key"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={form?.api_key ?? ""}
          onChange={(e) => form
            && setForm({ ...form, api_key: e.target.value, clearKey: false })}
          placeholder="输入 API 密钥，或留空沿用环境认证"
          className={`${inputCls} mt-2`}
        />
        {form?.api_key && (
          <p className="mt-1 text-[11px] text-pt-muted">
            保存后不再回显；如需改用环境变量，勾选下方「清除已保存密钥」。
          </p>
        )}

        <button
          type="button"
          onClick={() => setCustomOpen((v) => !v)}
          aria-expanded={customOpen}
          className="mt-5 text-sm text-pt-muted hover:text-pt-ink"
        >
          <span aria-hidden className="mr-1">{customOpen ? "▾" : "▸"}</span>
          自定义设置
        </button>

        {customOpen && (
          <div className="mt-3 rounded-lg border border-pt-line bg-pt-panel-2 p-4">
            <label className="block text-sm text-pt-ink" htmlFor="base_url">
              API 地址
            </label>
            <input
              id="base_url"
              value={form?.base_url ?? ""}
              onChange={(e) => form && setForm({ ...form, base_url: e.target.value })}
              placeholder="提供方默认"
              className={`${inputCls} mt-2`}
            />
            <p className="mt-1 text-[11px] text-pt-faint">
              留空使用该提供方内置默认地址；接入 OpenRouter 等兼容网关时填其
              API 地址（如 https://openrouter.ai/api/v1）。
            </p>

            <label className="mt-4 block text-sm text-pt-ink" htmlFor="model">
              模型
            </label>
            <input
              id="model"
              value={form?.model ?? ""}
              onChange={(e) => form && setForm({ ...form, model: e.target.value })}
              placeholder="如 gpt-4o-mini / deepseek-chat"
              className={`${inputCls} mt-2`}
            />
          </div>
        )}

        {cfg?.active.key_source === "database" && (
          <label className="mt-4 flex items-center gap-2 text-xs text-pt-muted">
            <input
              type="checkbox"
              checked={form?.clearKey ?? false}
              onChange={(e) => form
                && setForm({ ...form, clearKey: e.target.checked, api_key: "" })}
            />
            清除已保存密钥（回落环境变量 LLM_API_KEY）
          </label>
        )}

        {error && <p className="mt-4 text-xs text-pt-amber-hi">{error}</p>}
        {notice && <p className="mt-4 text-xs text-pt-muted">{notice}</p>}

        <div className="mt-5 flex items-center justify-end gap-2">
          {cfg?.active.source === "database" && (
            <button
              type="button"
              onClick={resetToEnv}
              disabled={busy}
              className="mr-auto rounded border border-pt-line px-3 py-1 text-sm text-pt-muted hover:border-pt-amber hover:text-pt-amber-hi disabled:opacity-40"
            >
              重置为环境配置
            </button>
          )}
          <button
            type="button"
            onClick={() => cfg && setForm(formFromConfig(cfg))}
            disabled={busy}
            className="rounded border border-pt-line px-4 py-1.5 text-sm text-pt-muted hover:border-pt-amber hover:text-pt-amber-hi disabled:opacity-40"
          >
            取消
          </button>
          <button
            type="button"
            onClick={save}
            disabled={busy || !form}
            className="rounded border border-pt-amber/40 px-4 py-1.5 text-sm text-pt-amber-hi hover:bg-pt-amber/10 disabled:opacity-40"
          >
            {busy ? "保存中…" : "保存"}
          </button>
        </div>
      </section>
    </main>
  );
}
