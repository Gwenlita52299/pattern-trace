"""跨链检测的数据模型 — backend/detection（issue #4 模块化运行时 OP_RETURN 检测）。

检测结果的三个层次，与 issue 的「Parser → Decoder Registry → Detector」语义对应：

- ``OpReturnPayload``：OP_RETURN 解析器对单个 pushdata 输出出的原始载荷（审计证据）。
- ``ProtocolMatch``：某个已注册协议 Decoder 对单条证据的成功匹配（协议级判定）。
- ``Detection``：CrosschainDetector 对一笔交易的**稳定交易级**判定结果，包含
  ``type / protocol / is_crosschain / reason / evidence`` 与 parser/decoder/detector version。

关键不变量（issue #4）：
1. 发现 OP_RETURN 不等于发现跨链；未知 OP_RETURN 不得设置 ``is_crosschain=True``。
2. Decoder 冲突返回 ``ambiguous``：**默认不停止扩展**（is_crosschain=False）。
3. 只有已注册且成功匹配的协议 Decoder 才能确认跨链。
4. OP_RETURN 通道与 Liquid/Elements ``vout.pegout`` 通道语义隔离，不混淆。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# 各组件版本号：缓存 key / 审计证据需要区分不同版本的解析与判定结果
PARSER_VERSION = "op_return-parser-1"
DETECTOR_VERSION = "crosschain-detector-1"


class CrosschainType(str, Enum):
    """跨链通道类型。``NONE`` 表示无跨链；``AMBIGUOUS`` 表示检测到候选但无法唯一判定。"""

    OP_RETURN = "op_return"
    PEGOUT = "pegout"
    AMBIGUOUS = "ambiguous"
    NONE = "none"


@dataclass(frozen=True)
class OpReturnPayload:
    """单个 OP_RETURN pushdata 载荷（parser 的原始审计证据）。

    - ``valid`` 为 False 表示解析失败（空 payload / 非法 hex / 截断脚本），
      此时 ``error`` 说明原因，``payload`` 为空字节。
    - ``channel`` 固定为 ``"op_return"``，与 pegout 通道区分。
    """

    vout: int
    channel: str = "op_return"
    valid: bool = True
    error: str | None = None
    payload: bytes = b""
    payload_hex: str = ""
    scriptpubkey: str = ""
    scriptpubkey_asm: str = ""
    scriptpubkey_type: str = "op_return"
    pushdata_count: int = 0


@dataclass(frozen=True)
class PegoutEvidence:
    """Esplora ``vout.pegout`` 子对象证据（Liquid/Elements peg-out 通道）。"""

    vout: int
    channel: str = "pegout"
    pegout_scriptpubkey: str = ""
    pegout_scriptpubkey_asm: str = ""
    pegout_type: str = ""
    value: float = 0.0


@dataclass(frozen=True)
class ProtocolMatch:
    """协议 Decoder 对单条证据的成功匹配。"""

    protocol: str
    decoder: str  # 注册名（如 "thorchain" / "pegout"）
    decoder_version: str
    channel: str  # "op_return" | "pegout"
    evidence: str  # 审计摘要（payload 前缀 / pegout 摘要）
    confidence: float = 1.0


@dataclass(frozen=True)
class Detection:
    """CrosschainDetector 交易级检测结果（稳定接口，供 GraphBuilder 依赖）。

    - ``is_crosschain=True`` 仅当且仅当至少一个已注册协议 Decoder 成功匹配，
      且不存在导致 ambiguous 的跨 decoder 冲突。
    - ``ambigous`` 场景：检测到候选证据但不同 decoder 判定为不同协议，
      或同一推播被多个协议同时命中——此时 ``is_crosschain=False``、``type=AMBIGUOUS``，
      默认**不停止展开**（issue #4「Decoder 冲突时默认不停止」）。
    - ``reason`` 机器可读：``"op_return_protocol"`` / ``"pegout_protocol"`` /
      ``"unknown_op_return"`` / ``"malformed_op_return"`` / ``"ambiguous"`` / ``"none"``。
    """

    type: CrosschainType = CrosschainType.NONE
    is_crosschain: bool = False
    protocol: str | None = None
    reason: str = "none"
    evidence: list = field(default_factory=list)  # OpReturnPayload | PegoutEvidence 审计痕迹
    decoder: str | None = None
    parser_version: str = PARSER_VERSION
    detector_version: str = DETECTOR_VERSION
    decoder_version: str | None = None

    @property
    def stopped(self) -> bool:
        """是否应触发 early_stop_crosschain（仅确认跨链时）。"""
        return self.is_crosschain
