"""语义 embedding provider 抽象 — ingest-spec §5 / retrieval-spec §4。

stub（默认）：确定性 token-bag 哈希向量——每个 token 经 SHA-256 派生伪随机
分量后加权求和、归一化。相比整句哈希，共享词汇的文本向量真实相关，
使检索评估在无外部 API 时有意义；相同文本必得相同向量（幂等）。

openai_compat：OpenAI 兼容 /embeddings 端点（OpenRouter / SiliconFlow 等），
base_url + api_key 走配置。模型版本锁定由调用方写入 embedding_model 列
并在检索入口校验（RT-04）。
"""
from __future__ import annotations

import hashlib
import math
import re

_TOKEN_RE = re.compile(r"[a-z_]+|\d+(?:\.\d+)?")


class EmbeddingAPIError(RuntimeError):
    """上游故障（超时/429/响应畸形），供续跑路径与调用方处理。"""


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


class OpenAICompatEmbedding:
    """OpenAI 兼容 /embeddings 端点客户端（OpenRouter / SiliconFlow / …）。

    响应按 item["index"] 归位——容忍上游乱序；维度与配置不符立即报错，
    严禁把维度漂移的向量写入库（RT-04 模型锁的数据一致性前提）。
    """

    def __init__(self, model: str, dim: int, base_url: str, api_key: str,
                 timeout: float = 60.0):
        if not base_url or not api_key:
            raise ValueError(
                "embedding_base_url / embedding_api_key 未配置——"
                "provider=openai_compat 需要在 .env 中提供")
        self.model = model
        self.dim = dim
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.calls = 0  # provider 调用计数（IG-15 断言用）

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        import httpx

        self.calls += 1
        try:
            resp = httpx.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": texts},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise EmbeddingAPIError(f"embedding 请求失败：{exc}") from exc
        if resp.status_code != 200:
            raise EmbeddingAPIError(
                f"embedding endpoint {resp.status_code}: {resp.text[:200]}")

        data = resp.json().get("data") or []
        vectors: list[list[float] | None] = [None] * len(texts)
        for item in data:
            vectors[item["index"]] = item["embedding"]
        if any(v is None for v in vectors):
            raise EmbeddingAPIError("embedding 响应缺少向量项")
        for v in vectors:  # type: ignore[union-attr]
            if len(v) != self.dim:
                raise EmbeddingAPIError(
                    f"向量维度漂移：期望 {self.dim}，实际 {len(v)}")
        return vectors  # type: ignore[return-value]


def build_provider(settings):
    """按配置构建 provider；未接入的 provider 名一律显式报错。"""
    name = settings.embedding_provider
    if name == "stub":
        return StubEmbedding(settings.embedding_model, settings.embedding_dim)
    if name == "openai_compat":
        return OpenAICompatEmbedding(
            settings.embedding_model, settings.embedding_dim,
            settings.embedding_base_url, settings.embedding_api_key)
    raise NotImplementedError(f"embedding_provider={name} 未接入；当前支持 stub / openai_compat")
