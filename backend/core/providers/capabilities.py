"""Provider 能力声明 — issue #76。

此前能力是隐式的：OpenAI 端支持原生 json_schema、DeepSeek 不支持、
Anthropic 完全不支持——靠分散的 `strict_schema` 类属性与代码注释表达，
接入方只能读源码猜。统一声明后，编排层/健康检查/未来的 reranker 可以
按能力分支，而不必 isinstance 判断具体类。

structured_output 语义（与 llm-judge-spec §2 一致）：
各 provider 原生约束解码能力不同，统一约定为「尽力约束 + 本地 Pydantic
硬校验兜底」——无论 native_json_schema 真假，返回值都必须通过本地校验
才算有效。native_json_schema=False 时包装层会把 schema 文本内嵌进 prompt。
"""
from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["ProviderCapabilities"]


@dataclass(frozen=True)
class ProviderCapabilities:
    kind: str                        # "llm" | "embedding"
    structured_output: bool = False  # 能产出符合给定 schema 的结构化结果
    native_json_schema: bool = False  # 原生约束解码（否则 prompt 内嵌 schema）
    requires_api_key: bool = False    # 本地 Ollama/stub 为 False
    max_batch: int = 1               # 单次请求最大条目数（embedding）
    context_limit: int | None = None  # 上下文窗口（token，None=未知）
    dim: int | None = None           # embedding 维度（None=不固定/可变）
    extra: dict = field(default_factory=dict)

    def supports_structured_output(self) -> bool:
        return self.structured_output

    def as_dict(self) -> dict:
        """供健康检查/管理端点输出（不含任何密钥）。"""
        out = {
            "kind": self.kind,
            "structured_output": self.structured_output,
            "native_json_schema": self.native_json_schema,
            "requires_api_key": self.requires_api_key,
            "max_batch": self.max_batch,
        }
        if self.context_limit is not None:
            out["context_limit"] = self.context_limit
        if self.dim is not None:
            out["dim"] = self.dim
        if self.extra:
            out["extra"] = dict(self.extra)
        return out
