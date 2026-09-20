"""THORChain 协议 Decoder — backend/detection.protocols（issue #4）。

独立实现，只负责把**一条 OP_RETURN 证据**解码为 THORChain 协议匹配；不做任何跨链结论。
通过 registry 注册到 OP_RETURN 通道。

THORChain 在 Bitcoin 侧把「swap 意图 memo」放进 OP_RETURN。memo 是 UTF-8 明文，采用
``<ACTION>:<asset>:<dest_addr>:<limit>`` 的冒号分隔格式（经典大写动作如 ``SWAP:`` / ``ADD:``），
以及「memo 长度缩短」的紧凑编码（单字母动作前缀 ``s:`` / ``a:`` / ``w:`` / ``l:``，
或 ``:<action>:`` 形式）。本 Decoder 只做前缀识别，不解析资产/地址字段——那是协议级
后续步骤，且当前仅需要决定「是否跨链」。

已知边界：紧凑 ``s:`` 前缀可能误匹配其他自描述文本；这是 Decoder 的职责边界，
交由 Detector 的 conflict/ambiguous 语义兜底（本 issue 只有 THORChain/pegout 两个 decoder）。
"""
from __future__ import annotations

from ..models import OpReturnPayload, ProtocolMatch

DECODER_VERSION = "thorchain-decoder-1"
PROTOCOL = "thorchain"

# 经典大写动作（隐含后跟冒号）：
#   SWAP:  THOR.RUNE/ETH:0xdead:12
_ACTIONS = ("SWAP", "ADD", "WITHDRAW", "LOOP", "PERMISSION",
            "REFUND", "DONATE", "UNBOND", "BOND")
# 紧凑/缩短 memo 的单字母动作（大小写不敏感）：
#   s:  → SWAP,  a: → ADD,  w: → WITHDRAW,  l: → LOOP
_COMPACT = ("S", "A", "W", "L", "P", "R", "D")
# 官方符号简写（THORChain memo 规范）："= " 等价 SWAP，"+ " 等价 ADD，
# "- " 等价 WITHDRAW。链上实测漏检案例：``=:r:thor1lpe8z5upw7ss779hyx…``
# （= 交换为 RUNE 并指定 thor 目标地址；vout OP_RETURN 59B pushdata）
_SYMBOL_ACTIONS = ("=", "+", "-")


class ThorchainDecoder:
    """THORChain OP_RETURN memo 解码器。无状态。"""

    protocol = PROTOCOL
    version = DECODER_VERSION

    def try_decode(self, evidence: OpReturnPayload) -> ProtocolMatch | None:
        if evidence.channel != "op_return" or not evidence.valid:
            return None
        if not evidence.payload:
            return None

        text = evidence.payload.decode("utf-8", errors="ignore").strip()
        if not text:
            return None
        upper = text.upper()

        head = upper.split(":", 1)[0].strip()
        # 1) 经典 `<ACTION>:` 前缀
        if head in _ACTIONS:
            return self._match(text)
        # 2) 冒号前缀 `:SWAP:` / `:s:`
        if upper.startswith(":"):
            inner_head = upper.lstrip(":").split(":", 1)[0].strip()
            if inner_head in _ACTIONS or inner_head in _COMPACT:
                return self._match(text)
        # 3) 紧凑前缀 `s:`（无前置冒号）
        if head in _COMPACT and ":" in text:
            return self._match(text)
        # 4) 符号简写 `=:<asset>[:<dest>][:<limit>]`（"=" 官方等价 SWAP；
        #    链上实测 `=:r:thor1lpe8…` 曾漏检）与 `+:` / `-:`
        if text[0] in _SYMBOL_ACTIONS:
            parts = text.split(":", 2)
            if len(parts) >= 2 and parts[1].strip():
                return self._match(text)
        return None

    @staticmethod
    def _match(text: str) -> ProtocolMatch:
        return ProtocolMatch(
            protocol=PROTOCOL, decoder=PROTOCOL,
            decoder_version=DECODER_VERSION,
            channel="op_return", evidence=text[:72],
        )
