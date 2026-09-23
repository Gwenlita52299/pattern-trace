"""0014: 模式知识库版本/审核/索引生命周期（issue #75）。

patterns 增加生命周期列（当前生效版本）：
- revision：单调递增的版本号（编辑/回滚各产生一个新版本）
- status：draft | active | deprecated——**只有 active 参与业务召回**
- index_status：pending | indexed | failed（内容变化后异步重算索引）
- last_editor_id / reviewed_by / reviewed_at：取证可追溯的审核留痕

pattern_revisions：不可变版本历史。每行是一个完整版本快照（内容 + 索引
元数据，含向量列与检索指纹）：
- origin：ingest（建库初版）| edit | rollback
- 编辑只写 draft revision，审核通过才把它 COW 进 patterns 当前行——
  因此「索引完成前继续使用上一 active 版本」是结构性保证，而不是靠状态
  判断的竞态窗口

judgments.matched_pattern_revision：判定当时实际使用的模式版本，历史判定
在模式被编辑后仍能精确定位到当时的依据。

回填策略：存量 9,346 条模式全部视为已发布的初版（revision=1, active,
indexed），并为每条写一条 revision=1 的历史行——保证升级前后检索行为与
结果集合完全一致（不把现网 KB 变成 draft）。
"""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0014"
down_revision: str | None = "0013"


def upgrade() -> None:
    op.add_column("patterns", sa.Column("revision", sa.Integer(),
                                        nullable=False, server_default="1"))
    op.add_column("patterns", sa.Column("status", sa.String(20),
                                        nullable=False,
                                        server_default="active"))
    op.add_column("patterns", sa.Column("index_status", sa.String(20),
                                        nullable=False,
                                        server_default="indexed"))
    op.add_column("patterns", sa.Column("last_editor_id", sa.String(36)))
    op.add_column("patterns", sa.Column("reviewed_by", sa.String(36)))
    op.add_column("patterns", sa.Column("reviewed_at",
                                        sa.DateTime(timezone=True)))
    # 召回过滤走 status —— 单列索引足够（index_status 与之几乎同分布）
    op.create_index("ix_patterns_status", "patterns", ["status"])

    op.add_column("judgments",
                  sa.Column("matched_pattern_revision", sa.Integer()))

    op.create_table(
        "pattern_revisions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("pattern_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("provenance", sa.String(20), nullable=False),
        sa.Column("evidence_grade", sa.String(1), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("canonical_subgraph", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("structural_features", Vector(20)),
        sa.Column("semantic_embedding", Vector(1024)),
        sa.Column("graphormer_embedding", Vector(784)),
        sa.Column("graphormer_model_id", sa.String(120)),
        sa.Column("retrieval_fingerprint", JSONB),
        sa.Column("wl_fingerprint", JSONB),
        sa.Column("embedding_model", sa.String(100), nullable=False,
                  server_default=""),
        sa.Column("embedding_dim", sa.Integer()),
        sa.Column("index_status", sa.String(20), nullable=False,
                  server_default="pending"),
        sa.Column("index_error", sa.Text()),
        sa.Column("index_metadata", JSONB),
        sa.Column("change_note", sa.Text()),
        sa.Column("reviewed_by", sa.String(36)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["pattern_id"], ["patterns.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pattern_id", "revision",
                            name="uq_pattern_revisions_pattern_rev"),
    )
    op.create_index("ix_pattern_revisions_pattern_id", "pattern_revisions",
                    ["pattern_id"])

    # 存量模式：视为已发布初版（检索行为零变化），并补一条历史快照
    op.execute("""
        INSERT INTO pattern_revisions (
            pattern_id, revision, status, origin, name, source, provenance,
            evidence_grade, description, canonical_subgraph, content_hash,
            structural_features, semantic_embedding, graphormer_embedding,
            graphormer_model_id, retrieval_fingerprint, wl_fingerprint,
            embedding_model, embedding_dim, index_status, index_metadata,
            created_at
        )
        SELECT id, 1, 'active', 'ingest', name, source, provenance,
               evidence_grade, description, canonical_subgraph, content_hash,
               structural_features, semantic_embedding, graphormer_embedding,
               graphormer_model_id, retrieval_fingerprint, wl_fingerprint,
               embedding_model, embedding_dim, 'indexed',
               '{"backfilled": true}'::jsonb, created_at
        FROM patterns
    """)


def downgrade() -> None:
    op.drop_index("ix_pattern_revisions_pattern_id",
                  table_name="pattern_revisions")
    op.drop_table("pattern_revisions")
    op.drop_column("judgments", "matched_pattern_revision")
    op.drop_index("ix_patterns_status", table_name="patterns")
    op.drop_column("patterns", "reviewed_at")
    op.drop_column("patterns", "reviewed_by")
    op.drop_column("patterns", "last_editor_id")
    op.drop_column("patterns", "index_status")
    op.drop_column("patterns", "status")
    op.drop_column("patterns", "revision")
