"""Provider Registry — issue #76。

目标：新 provider 只需实现稳定接口 + 注册（类 + 工厂 + 能力），
无需改动编排主流程或工厂的 if-elif 链。

注册采用"各模块自注册"：llm_judge/providers.py 与 retrieval/embedding.py
在模块底部把自己的 provider 注册进 `REGISTRY`。这样既不引入循环导入，
也让 `create()` 在未预先 import 时能通过 `load_builtins()` 兜底加载。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable

from .capabilities import ProviderCapabilities
from .errors import ProviderError, ProviderErrorCode

__all__ = ["ProviderSpec", "ProviderRegistry", "REGISTRY", "load_builtins"]


@dataclass(frozen=True)
class ProviderSpec:
    kind: str                              # "llm" | "embedding" | "reranker"
    name: str                              # 配置里的 provider 名
    factory: Callable[..., Any]            # (settings, **kwargs) -> 实例
    capabilities: ProviderCapabilities
    wrap: bool = True                      # 是否套统一包装（测试替身不套）
    display: str = ""                      # 人类可读名（健康检查输出）


class ProviderRegistry:
    def __init__(self) -> None:
        self._specs: dict[tuple[str, str], ProviderSpec] = {}
        self._lock = threading.Lock()

    def register(self, spec: ProviderSpec, *, replace: bool = False) -> None:
        key = (spec.kind, spec.name)
        with self._lock:
            if key in self._specs and not replace:
                raise ValueError(f"provider already registered: {spec.kind}/{spec.name}")
            self._specs[key] = spec

    def get(self, kind: str, name: str) -> ProviderSpec:
        spec = self._specs.get((kind, name))
        if spec is None:
            # 兜底：入口未 import provider 模块时按需加载内置注册
            load_builtins()
            spec = self._specs.get((kind, name))
        if spec is None:
            known = ", ".join(sorted(n for k, n in self._specs if k == kind)) or "(none)"
            raise ProviderError(
                ProviderErrorCode.CONFIGURATION,
                f"unknown {kind} provider: {name!r}; registered: {known}")
        return spec

    def create(self, kind: str, name: str, **kwargs) -> Any:
        return self.get(kind, name).factory(**kwargs)

    def names(self, kind: str | None = None) -> list[str]:
        with self._lock:
            return sorted(n for k, n in self._specs if kind is None or k == kind)

    def specs(self, kind: str | None = None) -> list[ProviderSpec]:
        with self._lock:
            return [s for (k, _), s in sorted(self._specs.items())
                    if kind is None or k == kind]

    def clear(self) -> None:  # 测试用
        with self._lock:
            self._specs.clear()
            global _loaded
            _loaded = False


REGISTRY = ProviderRegistry()
_loaded = False
_load_lock = threading.Lock()


def load_builtins() -> None:
    """惰性导入内置 provider 模块（各自模块底部完成注册）。幂等。"""
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        import backend.llm_judge.providers  # noqa: F401  自注册 llm/*
        import backend.retrieval.embedding  # noqa: F401  自注册 embedding/*
        _loaded = True
