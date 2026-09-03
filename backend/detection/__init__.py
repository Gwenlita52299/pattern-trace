"""交易结构级检测器包 — backend/detection。

- ``coinjoin.py``：CoinJoin 启发式判定（结构规则，无 CSV/ML/聚类依赖）。
- ``op_return.py``：OP_RETURN 脚本解析器（只解析，不判协议）。
- ``crosschain.py``：CrosschainDetector 交易级跨链判定（Parser → Decoder Registry → Detector）。
- ``models.py``：跨链检测的数据模型（CrosschainType / Detection / Evidence）。
- ``protocols/``：协议 Decoder 注册表（thorchain / liquid pegout）。
"""
from __future__ import annotations

from .coinjoin import CoinJoinDetector
from .crosschain import CrosschainDetector
from .models import (
    CrosschainType,
    Detection,
    OpReturnPayload,
    PegoutEvidence,
    ProtocolMatch,
)
from .op_return import OpReturnParser

__all__ = [
    "CoinJoinDetector",
    "CrosschainDetector",
    "OpReturnParser",
    "CrosschainType",
    "Detection",
    "OpReturnPayload",
    "PegoutEvidence",
    "ProtocolMatch",
]
