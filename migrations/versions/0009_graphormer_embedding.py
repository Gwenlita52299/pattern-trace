"""Graphormer 检索向量接入 — unseen-similarity 基准的 cos 通道

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-16

知识库数据源切换到 graphormer_v2（last_version/graphormer_test 基准数据），
检索 stage1 cosine 使用实验验证的 Graphormer pooled 向量：

- pooled = [768d 节点嵌入深度加权均值 | 16 维标量 z 分数]，L2 归一化
- 基准 dev 网格 + 5 轮验证的组合 0.1·cos + 0.1·wljac + 0.8·ov 依赖此通道
- 列维度 784 是物理约束；旧向量空间不迁移（新数据源全量重算）

patterns / pattern_negatives 各加 graphormer_embedding vector(784)，
patterns 上建 HNSW cosine 索引（规模 9,346 行，精确扫描也可行，
索引为规模化预留）。
"""

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE patterns "
               "ADD COLUMN graphormer_embedding vector(784)")
    op.execute("ALTER TABLE pattern_negatives "
               "ADD COLUMN graphormer_embedding vector(784)")
    # 检索指纹（wljac/fp/ov 三通道预计算输入）：rerank 无需回传大体积
    # canonical JSONB（大闭包单行 MB 级，500 行池 = GB 级传输不可行）
    op.execute("ALTER TABLE patterns "
               "ADD COLUMN retrieval_fingerprint JSONB")
    op.execute("ALTER TABLE pattern_negatives "
               "ADD COLUMN retrieval_fingerprint JSONB")
    op.create_index(
        "ix_patterns_graphormer_hnsw", "patterns", ["graphormer_embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"graphormer_embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_patterns_graphormer_hnsw", table_name="patterns")
    op.execute("ALTER TABLE patterns DROP COLUMN IF EXISTS retrieval_fingerprint")
    op.execute("ALTER TABLE pattern_negatives "
               "DROP COLUMN IF EXISTS retrieval_fingerprint")
    op.execute("ALTER TABLE patterns DROP COLUMN IF EXISTS graphormer_embedding")
    op.execute("ALTER TABLE pattern_negatives DROP COLUMN IF EXISTS graphormer_embedding")
