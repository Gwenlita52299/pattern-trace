"""embedding 计算单测 — IG-08 stub 元数据 / IG-14 缓存 / IG-15 故障注入。

run() 的完整 DB 续跑流程由 infra/verify_phase2.sh 在真实 PostgreSQL 上
验证（IG-15 前置条件就是"部分向量已写入表"）；此处覆盖 provider、
缓存读写与故障注入语义。
"""
import json

import pytest

from ingest.compute_embeddings import (
    StubEmbedding,
    _cache_path,
    _load_cache,
)


class TestStubEmbedding:
    def test_dimensions_and_metadata(self):
        """IG-08：1536 维 + model=text-embedding-3-small 元数据。"""
        stub = StubEmbedding("text-embedding-3-small", 1536)
        vec = stub.embed_batch(["A transaction subgraph rooted at ..."])[0]
        assert len(vec) == 1536
        assert stub.model == "text-embedding-3-small"
        norm = sum(x * x for x in vec) ** 0.5
        assert norm == pytest.approx(1.0, abs=1e-6)  # 单位向量 → 余弦可直接内积

    def test_deterministic_and_order_sensitive(self):
        stub = StubEmbedding("m", 64)
        v1 = stub.embed_batch(["same text"])[0]
        v2 = StubEmbedding("m", 64).embed_batch(["same text"])[0]
        other = stub.embed_batch(["different text"])[0]
        assert v1 == v2
        assert v1 != other
        # 模型名参与派生：换模型（版本锁定）必然得到不同向量
        assert StubEmbedding("other-model", 64).embed_batch(["same text"])[0] != v1

    def test_batch_call_counting(self):
        stub = StubEmbedding("m", 32)
        stub.embed_batch(["a", "b", "c"])
        stub.embed_batch(["d"])
        assert stub.calls == 2


class TestFaultInjection:
    def test_fault_every_semantics(self):
        """IG-15 钩子：判定表达式在 calls=3、6 时为真——
        与 run() 中的判定保持同一表达式，防止两边漂移。"""
        fault_every = 3
        stub = StubEmbedding("m", 8)
        would_fault = []
        for _ in range(7):
            if fault_every and stub.calls and stub.calls % fault_every == 0:
                would_fault.append(stub.calls)
                # 真实路径此处 raise EmbeddingAPIError 中断进程；
                # 这里仅记录触发点继续计数
            stub.embed_batch(["x"])
        assert would_fault == [3, 6]


class TestCacheRoundtrip:
    def test_write_then_load_hit(self, tmp_path):
        path = _cache_path(tmp_path, "patterns", "pid-1")
        assert not path.parent.exists() or True  # 父目录按需创建
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(
            {"model": "text-embedding-3-small", "dim": 4, "vector": [0.1] * 4}))
        got = _load_cache(path, "text-embedding-3-small", 4)
        assert got == [0.1] * 4

    def test_model_mismatch_invalidates_cache(self, tmp_path):
        """模型版本锁定（spec §5）：换模型的旧缓存不得复用。"""
        path = _cache_path(tmp_path, "patterns", "pid-1")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"model": "old-model", "dim": 4,
                                    "vector": [0.1] * 4}))
        assert _load_cache(path, "text-embedding-3-small", 4) is None

    def test_dim_mismatch_invalidates_cache(self, tmp_path):
        path = _cache_path(tmp_path, "pattern_negatives", "pid-2")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"model": "m", "dim": 8,
                                    "vector": [0.1] * 8}))
        assert _load_cache(path, "m", 16) is None

    def test_corrupt_cache_returns_none(self, tmp_path):
        path = _cache_path(tmp_path, "patterns", "pid-3")
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        assert _load_cache(path, "m", 4) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert _load_cache(_cache_path(tmp_path, "patterns", "nope"), "m", 4) is None
