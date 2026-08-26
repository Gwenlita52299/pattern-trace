"""语义 embedding provider 抽象 — ingest-spec §5 / retrieval-spec §4。

stub（默认）：确定性 token-bag 哈希向量——每个 token 经 SHA-256 派生伪随机
分量后加权求和、归一化。相比整句哈希，共享词汇的文本向量真实相关，
使检索评估在无外部 API 时有意义；相同文本必得相同向量（幂等）。

openai 兼容 provider 待真实 key 接入后启用；模型版本锁定由调用方写入
embedding_model 列并在检索入口校验（RT-04）。
"""
from __future__ import annotations

import hashlib
import math
import re

_TOKEN_RE = re.compile(r"[a-z_]+|\d+(?:\.\d+)?")


class EmbeddingAPIError(RuntimeError):
    """模拟上游故障（超时/429），供续跑路径测试。"""


def tokenize(text: str) -> list[str]:
    """小写分词；数字归一化为数量级桶——"12 addresses" 与 "15 addresses"
    共享同一 token，避免逐值哈希稀释相似度。"""
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text.lower()):
        if raw[0].isdigit():
            value = float(raw)
            magnitude = int(math.log10(value)) + 1 if value > 0 else 0
            out.append(f"num{min(magnitude, 9)}")
        else:
            out.append(raw[:24])
    return out


class StubEmbedding:
    """确定性 token-bag embedding（dim 维单位向量）。"""

    def __init__(self, model: str, dim: int):
        self.model = model
        self.dim = dim
        self.calls = 0  # provider 调用计数（IG-15 断言用）

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        tokens = tokenize(text) or ["<empty>"]
        acc = [0.0] * self.dim
        weight = 1.0 / math.sqrt(len(tokens))
        for tok in tokens:
            digest = hashlib.sha256(f"{self.model}:{tok}".encode()).digest()
            for i in range(0, 32, 4):
                idx = (i // 4) % self.dim
                acc[idx] += weight * (
                    int.from_bytes(digest[i:i + 4], "big") / 2**32 - 0.5)
        norm = math.sqrt(sum(x * x for x in acc)) or 1.0
        return [round(x / norm, 8) for x in acc]


def build_provider(settings):
    """按配置构建 provider；未接入的 provider 名一律显式报错。"""
    if settings.embedding_provider != "stub":
        raise NotImplementedError(
            f"embedding_provider={settings.embedding_provider} 未接入；当前仅 stub")
    return StubEmbedding(settings.embedding_model, settings.embedding_dim)
