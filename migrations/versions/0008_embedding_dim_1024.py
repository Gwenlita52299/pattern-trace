"""semantic_embedding 维度 1536 → 1024（LFM2.5-Embedding-350M 接入）

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-11

embedding provider 从 stub 切换到 openai_compat（OpenRouter，
liquid/lfm-2.5-embedding-350m）。该模型输出 1024 维 CLS 向量，
pgvector 列维度是物理约束，必须与 provider 输出严格一致：

- patterns / pattern_negatives 的 semantic_embedding：vector(1536) → vector(1024)
- 旧向量先置 NULL——换模型即向量空间整体失效，数据不可迁移，只能全量重算
  （这也满足 pgvector 的 ALTER TYPE 前置条件：列内不得存在旧维度数据）
- ix_patterns_semantic_hnsw：改列类型前必须先删 HNSW 索引，改完按原参数重建
- structural_features（vector(20)）不受影响，索引保留

向量数据本身不迁移——换模型意味着向量空间整体失效，全量重算
（compute_embeddings 按 embedding_model 校验 + 本地缓存失效）。
"""

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_patterns_semantic_hnsw", table_name="patterns")
    # 旧 stub 向量随模型切换全部失效：置 NULL 而非迁移（compute_embeddings 全量重算）
    op.execute("UPDATE patterns SET semantic_embedding = NULL, "
               "embedding_model = '', embedding_dim = NULL")
    op.execute("UPDATE pattern_negatives SET semantic_embedding = NULL, "
               "embedding_model = '', embedding_dim = NULL")
    op.execute("ALTER TABLE patterns "
               "ALTER COLUMN semantic_embedding TYPE vector(1024)")
    op.execute("ALTER TABLE pattern_negatives "
               "ALTER COLUMN semantic_embedding TYPE vector(1024)")
    op.create_index(
        "ix_patterns_semantic_hnsw", "patterns", ["semantic_embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"semantic_embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_patterns_semantic_hnsw", table_name="patterns")
    op.execute("ALTER TABLE patterns "
               "ALTER COLUMN semantic_embedding TYPE vector(1536)")
    op.execute("ALTER TABLE pattern_negatives "
               "ALTER COLUMN semantic_embedding TYPE vector(1536)")
    op.create_index(
        "ix_patterns_semantic_hnsw", "patterns", ["semantic_embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"semantic_embedding": "vector_cosine_ops"},
    )
