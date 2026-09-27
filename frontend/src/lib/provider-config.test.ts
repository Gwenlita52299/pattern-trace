// 配置页的纯函数单测：文案、表单初始化、提交体、前置校验。
// 密钥相关的不变量（留空=不改动、从不回显）都在这里钉住。
import { describe, expect, it } from "vitest";

import {
  buildPayload,
  formFromConfig,
  KEY_SOURCE_LABELS,
  keySourceLabel,
  SOURCE_LABELS,
  sourceLabel,
  validateForm,
} from "@/lib/provider-config";
import type {
  ActiveProvider,
  ProviderConfigResponse,
  ProviderForm,
  ProviderInfo,
} from "@/lib/provider-config";

const OPENAI: ProviderInfo = {
  name: "openai", display: "OpenAIClient", requires_api_key: true,
  structured_output: true, native_json_schema: true, switchable: true,
};

const OLLAMA: ProviderInfo = {
  name: "ollama", display: "OllamaClient", requires_api_key: false,
  structured_output: true, native_json_schema: false, switchable: true,
};

function active(over: Partial<ActiveProvider> = {}): ActiveProvider {
  return {
    provider: "deepseek", model: "deepseek-chat",
    base_url: "https://api.deepseek.com", source: "environment",
    key_source: "environment", has_key: true,
    updated_by: null, updated_at: null,
    ...over,
  };
}

function form(over: Partial<ProviderForm> = {}): ProviderForm {
  return {
    provider: "openai", model: "gpt-4o-mini", base_url: "", api_key: "",
    clearKey: false,
    ...over,
  };
}

function config(over: Partial<ProviderConfigResponse> = {}): ProviderConfigResponse {
  return {
    kind: "llm", active: active(), secrets_key_configured: true,
    providers: [OPENAI, OLLAMA], ...over,
  };
}

describe("配置页文案", () => {
  it("来源与密钥状态都有中文标签，未知值原样回退", () => {
    expect(sourceLabel("database")).toBe(SOURCE_LABELS.database);
    expect(sourceLabel("environment")).toBe(SOURCE_LABELS.environment);
    expect(keySourceLabel("not_required")).toBe(KEY_SOURCE_LABELS.not_required);
    expect(keySourceLabel("weird")).toBe("weird");
  });

  it("「数据库」显示为面板配置，避免把 DB 当成运维术语抛给用户", () => {
    expect(SOURCE_LABELS.database).toBe("面板配置");
  });
});

describe("表单初始化", () => {
  it("密钥框永远是空的（后端不回显明文）", () => {
    const f = formFromConfig(config());
    expect(f.api_key).toBe("");
    expect(f.provider).toBe("deepseek");
    expect(f.model).toBe("deepseek-chat");
    expect(f.base_url).toBe("https://api.deepseek.com");
  });
});

describe("提交体", () => {
  it("密钥留空且未清除时不含密钥字段（后端据此保留既有密文）", () => {
    expect(buildPayload(form())).toEqual({
      provider: "openai", model: "gpt-4o-mini", base_url: "",
    });
  });

  it("填入密钥时带上 api_key，且不与 clear_api_key 同时出现", () => {
    const p = buildPayload(form({ api_key: " sk-1 ", clearKey: true }));
    expect(p.api_key).toBe("sk-1");
    expect(p.clear_api_key).toBeUndefined();
  });

  it("显式清除时带 clear_api_key", () => {
    expect(buildPayload(form({ clearKey: true })).clear_api_key).toBe(true);
  });
});

describe("保存前校验", () => {
  it("模型为空即拒绝", () => {
    expect(validateForm(form({ model: "  " }), OPENAI, active(), true))
      .toContain("模型");
  });

  it("不可切换的提供方（live 形态的 mock）被拒绝", () => {
    const mock: ProviderInfo = { ...OPENAI, name: "mock", switchable: false,
                                 note: "live 形态下不可切换到 mock" };
    expect(validateForm(form({ provider: "mock" }), mock, active(), true))
      .toBe("live 形态下不可切换到 mock");
  });

  it("服务端没有 SECRETS_KEY 时不允许保存密钥", () => {
    expect(validateForm(form({ api_key: "sk-1" }), OPENAI, active(), false))
      .toContain("SECRETS_KEY");
  });

  it("需要密钥的提供方：面板与环境都没有密钥时拒绝", () => {
    const none = active({ key_source: "none", has_key: false });
    expect(validateForm(form(), OPENAI, none, true)).toContain("API Key");
  });

  it("沿用环境变量或保留已存密钥时允许提交", () => {
    expect(validateForm(form(), OPENAI, active(), true)).toBeNull();
    expect(validateForm(form(), OPENAI, active({ key_source: "database" }), true))
      .toBeNull();
    expect(validateForm(form({ api_key: "sk-1" }), OPENAI,
                        active({ key_source: "none" }), true)).toBeNull();
  });

  it("清除密钥后需要回落环境变量；环境也没有则拒绝", () => {
    const stored = active({ key_source: "database" });
    expect(validateForm(form({ clearKey: true }), OPENAI, stored, true))
      .toBeNull();

    const envOnly = active({ key_source: "environment" });
    expect(validateForm(form({ clearKey: true }), OPENAI, envOnly, true))
      .toBeNull();

    const none = active({ key_source: "none", has_key: false });
    expect(validateForm(form({ clearKey: true }), OPENAI, none, true))
      .toContain("API Key");
  });

  it("无需密钥的提供方（ollama）不因密钥为空被拒", () => {
    expect(validateForm(form({ provider: "ollama" }), OLLAMA,
                        active({ key_source: "none", has_key: false }), true))
      .toBeNull();
  });
});
