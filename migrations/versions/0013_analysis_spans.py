"""0013: 分析阶段 span（持久化 trace）+ judgments.trace_id（issue #73）。

- analysis_spans：以 judgment_id 为 trace 根节点的阶段观测事实源。
  每行一个阶段（building_subgraph / esplora_fetch / retrieval_topk /
  embedding_call / wl_rerank / llm_judging / llm_call），记录
  status/attempt/started_at/finished_at/duration_ms/error_code 与
  非敏感 metadata（计数、模型与 prompt/builder 版本）。Redis 继续承担
  实时进度通知，本表是历史追溯依据（Redis TTL 过期后仍可查）。
- judgments.trace_id：HTTP 请求入口生成的 trace 标识，随任务投递传递到
  worker（验收标准「HTTP 请求创建的 trace ID 能传递至异步 worker」）。

metadata 列名沿用 issue 措辞（ORM 属性名 span_metadata，避免与
DeclarativeBase.metadata 冲突）。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0013"
down_revision: str | None = "0012"


def upgrade() -> None:
    op.add_column("judgments", sa.Column("trace_id", sa.String(64)))
    op.create_index("ix_judgments_trace_id", "judgments", ["trace_id"])

    op.create_table(
        "analysis_spans",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("judgment_id", sa.String(36), nullable=False),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("parent_id", sa.BigInteger()),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("error_code", sa.String(50)),
        sa.Column("metadata", JSONB),
        sa.ForeignKeyConstraint(["judgment_id"], ["judgments.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_analysis_spans_judgment_id", "analysis_spans",
                    ["judgment_id"])
    op.create_index("ix_analysis_spans_trace_id", "analysis_spans",
                    ["trace_id"])
    op.create_index("ix_analysis_spans_name", "analysis_spans", ["name"])


def downgrade() -> None:
    op.drop_index("ix_analysis_spans_name", table_name="analysis_spans")
    op.drop_index("ix_analysis_spans_trace_id", table_name="analysis_spans")
    op.drop_index("ix_analysis_spans_judgment_id", table_name="analysis_spans")
    op.drop_table("analysis_spans")
    op.drop_index("ix_judgments_trace_id", table_name="judgments")
    op.drop_column("judgments", "trace_id")
