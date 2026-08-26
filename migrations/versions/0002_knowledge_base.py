"""knowledge base: patterns, pattern_negatives, label tables (pgvector)

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-23

ingest-spec §2/§3a/§4。HNSW 索引只建在 patterns 上（业务召回库）；
pattern_negatives 无向量索引——它只用于阈值校准与误报测量，不参与召回。
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels = None
depends_on = None


def _pattern_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("evidence_grade", sa.String(1), nullable=False),
        sa.Column("seed_address", sa.String(62), nullable=False, index=True),
        sa.Column("description", sa.Text, server_default=""),
        sa.Column("canonical_subgraph", JSONB, nullable=False),
        # 维度锁定：structural=FEATURE_DIM(retrieval)，semantic=text-embedding-3-small；
        # 换模型需全量重建并迁移（ingest-spec §5）
        sa.Column("structural_features", Vector(20)),
        sa.Column("semantic_embedding", Vector(1536)),
        sa.Column("embedding_model", sa.String(100), server_default="", nullable=False),
        sa.Column("embedding_dim", sa.Integer),
        sa.Column("wl_fingerprint", JSONB),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    ]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "patterns",
        *_pattern_columns(),
        sa.UniqueConstraint("seed_address", "content_hash", name="uq_patterns_seed_hash"),
    )
    op.create_table(
        "pattern_negatives",
        *_pattern_columns(),
        sa.UniqueConstraint("seed_address", "content_hash", name="uq_pattern_negatives_seed_hash"),
    )

    # 余弦距离度量（检索 SQL 用 <=>）；IG-13 要求两列各有一个 hnsw 索引
    op.create_index(
        "ix_patterns_structural_hnsw", "patterns", ["structural_features"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"structural_features": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_patterns_semantic_hnsw", "patterns", ["semantic_embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"semantic_embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "addresses_meta",
        sa.Column("address", sa.String(62), primary_key=True),
        sa.Column("labels", JSONB, nullable=False),
        sa.Column("source", sa.String(80), server_default="", nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "coinjoin_txids",
        sa.Column("txid", sa.String(64), primary_key=True),
        sa.Column("coordinator", sa.String(30), server_default="", nullable=False),
        sa.Column("source", sa.String(80), server_default="", nullable=False),
    )
    op.create_table(
        "crosschain_tx_set",
        # 基线口径：同一 txid 可携带多个协议（txid→sorted[protocols]），
        # 一行一对，复合主键即 upsert key（txid 不能单独做主键）
        sa.Column("txid", sa.String(64), nullable=False),
        sa.Column("protocol", sa.String(30), nullable=False),
        sa.Column("source", sa.String(80), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("txid", "protocol", name="pk_crosschain_tx_protocol"),
    )


def downgrade() -> None:
    op.drop_table("crosschain_tx_set")
    op.drop_table("coinjoin_txids")
    op.drop_table("addresses_meta")
    op.drop_index("ix_patterns_semantic_hnsw", table_name="patterns")
    op.drop_index("ix_patterns_structural_hnsw", table_name="patterns")
    op.drop_table("pattern_negatives")
    op.drop_table("patterns")
