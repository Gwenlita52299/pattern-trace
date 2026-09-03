"""CrosschainDetector 总入口 — backend/detection（issue #4）。

稳定的交易级检测接口：输入一笔交易（dict / SimpleNamespace），输出
``Detection``（type / protocol / is_crosschain / reason / evidence / 各组件 version）。

数据流：``Esplora tx -> OpReturnParser -> Protocol Decoder Registry -> CrosschainDetector``

判定规则（issue #4）：
- 发现 OP_RETURN 不等于跨链；未知 OP_RETURN 不得设置 ``is_crosschain=True``。
- 只有已注册且成功匹配的协议 Decoder 才能确认跨链。
- Decoder 冲突（不同协议同时命中）→ ``ambiguous``，默认不停止展开（is_crosschain=False）。
- OP_RETURN 通道与 Liquid/Elements ``vout.pegout`` 通道相互独立，不混淆。
"""
from __future__ import annotations

from .models import (
    CrosschainType,
    Detection,
    OpReturnPayload,
    PegoutEvidence,
    ProtocolMatch,
)
from .op_return import OpReturnParser
from .protocols import DecoderRegistry, build_default_registry


class CrosschainDetector:
    """跨链检测器。无状态，可复用；默认使用 internal registry（thorchain + liquid pegout）。"""

    def __init__(self, registry: DecoderRegistry | None = None,
                 parser: OpReturnParser | None = None) -> None:
        self.registry = registry or build_default_registry()
        self.parser = parser or OpReturnParser()

    # ------------------------------------------------------------------
    def detect(self, tx) -> Detection:
        """对一笔交易做跨链判定，返回稳定 ``Detection``。"""
        op_payloads = self.parser.parse_tx(tx)
        pegouts = self._parse_pegouts(tx)

        evidence: list = list(op_payloads) + list(pegouts)
        matches: list[ProtocolMatch] = []

        for payload in op_payloads:
            for decoder in self.registry.op_return_decoders():
                m = decoder.try_decode(payload)
                if m is not None:
                    matches.append(m)

        for peg in pegouts:
            for decoder in self.registry.pegout_decoders():
                m = decoder.try_decode(peg)
                if m is not None:
                    matches.append(m)

        return self._aggregate(op_payloads, evidence, matches)

    def is_crosschain(self, tx) -> bool:
        """便捷布尔判定（仅确认跨链返回 True；unknown/malformed/ambiguous 均 False）。"""
        return self.detect(tx).is_crosschain

    def verdict(self, tx) -> Detection:
        """与 CoinJoinDetector.verdict 命名对齐的别名。"""
        return self.detect(tx)

    # ------------------------------------------------------------------
    def _parse_pegouts(self, tx) -> list[PegoutEvidence]:
        raw = self.parser.parse_pegouts(tx)
        if not raw:
            return []
        pegouts: list[PegoutEvidence] = []
        for idx, peg in raw:
            pegouts.append(PegoutEvidence(
                vout=idx,
                pegout_scriptpubkey=str(peg.get("scriptpubkey") or ""),
                pegout_scriptpubkey_asm=str(peg.get("scriptpubkey_asm") or ""),
                pegout_type=str(peg.get("scriptpubkey_type") or ""),
                value=float(peg.get("value", 0.0) or 0.0),
            ))
        return pegouts

    @staticmethod
    def _aggregate(op_payloads: list[OpReturnPayload], evidence: list,
                   matches: list[ProtocolMatch]) -> Detection:
        """聚合 channel 匹配结果，处理 conflict/ambiguous 与 unknown/malformed。"""
        if not matches:
            # 无任何协议命中：跨链判定为否。
            if op_payloads:
                malformed = [p for p in op_payloads if not p.valid]
                reason = "malformed_op_return" if malformed else "unknown_op_return"
                return Detection(
                    type=CrosschainType.NONE, is_crosschain=False,
                    protocol=None, reason=reason, evidence=evidence,
                )
            return Detection(
                type=CrosschainType.NONE, is_crosschain=False,
                protocol=None, reason="none", evidence=evidence,
            )

        protocols = {m.protocol for m in matches}
        if len(protocols) > 1:
            # 跨 decoder 冲突 → ambiguous，默认不停止扩展
            return Detection(
                type=CrosschainType.AMBIGUOUS, is_crosschain=False,
                protocol=None, reason="ambiguous", evidence=evidence,
                decoder=", ".join(sorted({m.decoder for m in matches})),
                decoder_version=", ".join(sorted({m.decoder_version for m in matches})),
            )

        m = matches[0]
        ctype = CrosschainType.PEGOUT if m.channel == "pegout" else CrosschainType.OP_RETURN
        reason = "pegout_protocol" if m.channel == "pegout" else "op_return_protocol"
        return Detection(
            type=ctype, is_crosschain=True, protocol=m.protocol,
            reason=reason, evidence=evidence,
            decoder=m.decoder, decoder_version=m.decoder_version,
        )


def is_crosschain(tx, registry: DecoderRegistry | None = None) -> bool:
    """便捷单笔判定。"""
    return CrosschainDetector(registry).is_crosschain(tx)
