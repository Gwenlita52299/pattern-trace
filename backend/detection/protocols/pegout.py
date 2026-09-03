"""Liquid/Elements peg-out Decoder — backend/detection.protocols（issue #4）。

独立实现，只负责把**一条 ``vout.pegout`` 证据**解码为 peg-out 协议匹配；通过 registry
注册到 **pegout 通道**，与 OP_RETURN 通道语义隔离（不混淆两种跨链语义）。

Esplora 的 Liquid/Elements ``/tx/:txid`` 在 vout 上携带 ``pegout`` 子对象：
``{"scriptpubkey", "scriptpubkey_asm", "scriptpubkey_type", "value"}``，标识该输出把资金
peg-out 到侧链。本 Decoder 只确认「存在非空 pegout 子对象且含可识别脚本字段」即判定为
Liquid peg-out —— 这是 Esplora 数据源对 peg-out 的权威标记。不做 peg-out 目标资产解析。
"""
from __future__ import annotations

from ..models import PegoutEvidence, ProtocolMatch

DECODER_VERSION = "liquid-pegout-decoder-1"
PROTOCOL = "liquid_pegout"


class PegoutDecoder:
    """Liquid/Elements ``vout.pegout`` 解码器。无状态。"""

    protocol = PROTOCOL
    version = DECODER_VERSION

    def try_decode(self, evidence: PegoutEvidence) -> ProtocolMatch | None:
        if evidence.channel != "pegout":
            return None
        # 存在非空 vout.pegout 子对象即足够判定（Esplora 权威标记）。
        # 可选断言：拥有脚本字段，避免空 pegout {} 误判。
        if not evidence.pegout_scriptpubkey and not evidence.pegout_scriptpubkey_asm:
            return None
        return ProtocolMatch(
            protocol=PROTOCOL, decoder=self.protocol,
            decoder_version=self.version,
            channel="pegout",
            evidence=str(evidence.pegout_scriptpubkey_asm or evidence.pegout_scriptpubkey)[:72],
        )
