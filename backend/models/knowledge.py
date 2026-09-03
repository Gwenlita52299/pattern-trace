"""知识库表 — ingest-spec §2/§3a/§4，阶段2 DB schema。

patterns 是业务召回库（只放正样本）；pattern_negatives 结构一致但无向量索引，
仅用于阈值校准与误报率测量（§3a 隔离要求）。upsert key 均为
(seed_address, content_hash)——(name, source) 在数千条子图中同名碰撞，已弃用。
"""
from __future__ import annotations

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from .base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Pattern(Base):
    __tablename__ = "patterns"
    __table_args__ = (
        UniqueConstraint("seed_address", "content_hash", name="uq_patterns_seed_hash"),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(120), nullable=False)
    source = Column(String(40), nullable=False)  # lazarus_confirmed | lazarus_synth
    # issue #10：证据来源性质——confirmed(真实链上样本) | synthetic(合成结构模板)。
    # provenance 与 evidence_grade 分离：合成样本可参与检索但不得伪装成真实 Grade A 证据。
    provenance = Column(String(20), nullable=False, server_default="confirmed")
    evidence_grade = Column(String(1), nullable=False)  # A(confirmed) | B | S(synthetic)
    seed_address = Column(String(62), nullable=False, index=True)
    description = Column(Text, default="")
    canonical_subgraph = Column(JSONB, nullable=False)
    # 向量由 compute_embeddings.py 二段填充；入库脚本先写行、后补向量
    structural_features = Column(Vector(20))
    semantic_embedding = Column(Vector(1536))
    embedding_model = Column(String(100), default="", nullable=False)
    embedding_dim = Column(Integer)
    wl_fingerprint = Column(JSONB)
    content_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PatternNegative(Base):
    """负样本隔离表（§3a）：结构与 patterns 一致，无 HNSW 索引，不入业务召回。"""

    __tablename__ = "pattern_negatives"
    __table_args__ = (
        UniqueConstraint("seed_address", "content_hash", name="uq_pattern_negatives_seed_hash"),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(120), nullable=False)
    source = Column(String(40), nullable=False)  # constructed_normal
    provenance = Column(String(20), nullable=False, server_default="negative")  # negative
    evidence_grade = Column(String(1), nullable=False)  # B
    seed_address = Column(String(62), nullable=False, index=True)
    description = Column(Text, default="")
    canonical_subgraph = Column(JSONB, nullable=False)
    structural_features = Column(Vector(20))
    semantic_embedding = Column(Vector(1536))
    embedding_model = Column(String(100), default="", nullable=False)
    embedding_dim = Column(Integer)
    wl_fingerprint = Column(JSONB)
    content_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AddressMeta(Base):
    """地址标签（混币器/交易所/黑名单）— graph-builder 启动时加载 mixer 集合。"""

    __tablename__ = "addresses_meta"

    address = Column(String(62), primary_key=True)
    labels = Column(JSONB, nullable=False, default=list)  # ["mixer", "exchange", ...]
    source = Column(String(80), default="", nullable=False)
    added_at = Column(DateTime(timezone=True), server_default=func.now())


class CoinjoinTxid(Base):
    """CoinJoin 交易标记（Wasabi / JoinMarket）— builder 用于 early_stop_wasabi。"""

    __tablename__ = "coinjoin_txids"

    txid = Column(String(64), primary_key=True)
    coordinator = Column(String(30), default="", nullable=False)
    source = Column(String(80), default="", nullable=False)
