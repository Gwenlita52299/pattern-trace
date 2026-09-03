"""协议 Decoder 包 — backend/detection.protocols（issue #4）。

每个协议一个模块，独立 Decoder，通过 registry 注册。检测器按通道（op_return / pegout）
调度。包内公开 registry 与常用 decoder 类。
"""
from __future__ import annotations

from .registry import DecoderRegistry, build_default_registry
from .thorchain import ThorchainDecoder
from .pegout import PegoutDecoder

__all__ = [
    "DecoderRegistry",
    "build_default_registry",
    "ThorchainDecoder",
    "PegoutDecoder",
]
