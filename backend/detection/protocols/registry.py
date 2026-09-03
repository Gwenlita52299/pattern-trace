"""Decoder 注册表 — backend/detection.protocols（issue #4）。

按「通道」隔离两类独立语义：
- ``op_return`` 通道：解码 OP_RETURN pushdata（THORChain 等 memo 协议）。
- ``pegout`` 通道：解码 Liquid/Elements ``vout.pegout`` 子对象。

每个协议独立实现 Decoder 并通过 ``register_op_return_decoder`` / ``register_pegout_decoder``
注册。探测时按通道取对应 decoder，检测器聚合所有匹配并处理 conflict/ambiguous。
"""
from __future__ import annotations

from .thorchain import ThorchainDecoder
from .pegout import PegoutDecoder


class DecoderRegistry:
    """按通道登记协议 Decoder，并提供按通道/全量枚举。"""

    def __init__(self) -> None:
        self._op_return: dict[str, object] = {}
        self._pegout: dict[str, object] = {}

    def register_op_return(self, decoder) -> None:
        self._op_return[decoder.protocol] = decoder

    def register_pegout(self, decoder) -> None:
        self._pegout[decoder.protocol] = decoder

    def op_return_decoders(self) -> list:
        return list(self._op_return.values())

    def pegout_decoders(self) -> list:
        return list(self._pegout.values())

    def all_decoders(self) -> list:
        return self.op_return_decoders() + self.pegout_decoders()


def build_default_registry() -> DecoderRegistry:
    """构造内置协议注册表（thorchain + liquid pegout），供 CrosschainDetector 默认使用。"""
    registry = DecoderRegistry()
    registry.register_op_return(ThorchainDecoder())
    registry.register_pegout(PegoutDecoder())
    return registry
