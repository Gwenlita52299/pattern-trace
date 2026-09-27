// 管理端 provider 配置的纯逻辑（页面在 app/settings/page.tsx）。
//
// 抽到 lib/ 有两个原因：Next.js 的 page 文件只允许导出页面本身（导出普通
// 函数会让 next build 直接失败），且纯函数不需要 DOM 就能单测。
//
// 两条不变量：
// 1. 密钥只写不读——后端永不回显明文，表单里永远是空的，留空即"不改动"。
// 2. 前端的校验只覆盖能确定的事；需要后端才知道的（如清除后 env 有无密钥）
//    交给后端判定，不在这里猜。
import type {} from "react";

export interface ProviderInfo {
  name: string;
  display: string;
  requires_api_key: boolean;
  structured_output: boolean;
  native_json_schema: boolean;
  context_limit?: number;
  switchable?: boolean;
  note?: string;
}

export interface ActiveProvider {
  provider: string;
  model: string;
  base_url: string;
  source: "database" | "environment";
  key_source: "database" | "environment" | "not_required" | "none";
  has_key: boolean;
  updated_by: string | null;
  updated_at: string | null;
}

export interface ProviderConfigResponse {
  kind: string;
  active: ActiveProvider;
  secrets_key_configured: boolean;
  providers: ProviderInfo[];
}

export interface ProviderForm {
  provider: string;
  model: string;
  base_url: string;
  api_key: string;
  clearKey: boolean;
}

export const SOURCE_LABELS: Record<string, string> = {
  database: "面板配置",
  environment: "环境变量",
};

export const KEY_SOURCE_LABELS: Record<string, string> = {
  database: "已保存到面板",
  environment: "沿用环境变量",
  not_required: "该提供方无需密钥",
  none: "未配置",
};

export function sourceLabel(v: string): string {
  return SOURCE_LABELS[v] ?? v;
}

export function keySourceLabel(v: string): string {
  return KEY_SOURCE_LABELS[v] ?? v;
}

export function formFromConfig(cfg: ProviderConfigResponse): ProviderForm {
  return {
    provider: cfg.active.provider,
    model: cfg.active.model,
    base_url: cfg.active.base_url,
    // 密钥不回显：切 provider 时表单里永远是空的，留空即"不改动"
    api_key: "",
    clearKey: false,
  };
}

/** 提交体：密钥留空且未勾选清除时不带该字段——后端据此保留既有密文。 */
export function buildPayload(form: ProviderForm): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    provider: form.provider,
    model: form.model.trim(),
    base_url: form.base_url.trim(),
  };
  const key = form.api_key.trim();
  if (key) payload.api_key = key;
  else if (form.clearKey) payload.clear_api_key = true;
  return payload;
}

/** 保存前的前置校验；返回 null 表示可提交。 */
export function validateForm(
  form: ProviderForm,
  provider: ProviderInfo | undefined,
  active: ActiveProvider,
  secretsKeyConfigured: boolean,
): string | null {
  if (!form.provider) return "请选择提供方";
  if (provider && provider.switchable === false) {
    return provider.note || "该提供方在当前形态下不可切换";
  }
  if (!form.model.trim()) return "模型 ID 不能为空";
  if (form.api_key.trim() && !secretsKeyConfigured) {
    return "服务端未配置 SECRETS_KEY，无法保存密钥（可改用环境变量 LLM_API_KEY）";
  }
  if (provider?.requires_api_key && !form.api_key.trim()
      && active.key_source === "none") {
    // key_source==="none" 是唯一能确定「面板与环境都没有密钥」的信号。
    // 清除已存密钥后是否还有 env 密钥，前端无从得知——那种情况放行，
    // 由后端返回 PROVIDER_KEY_REQUIRED，而不是在这里猜一个可能错的答案。
    return "该提供方需要 API Key：面板与环境变量都没有可用密钥";
  }
  return null;
}
