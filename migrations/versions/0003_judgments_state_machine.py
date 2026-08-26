"""judgments 表升级为 backend-api-spec §4 完整形状

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-24

阶段0 骨架表只有 verdict/evidence_ids(Text) 等 MVP 字段；本迁移重建为
spec §4 定义（D5 状态机字段、JSONB 快照、模型/版本三元组、错误链路）。
开发期无存量数据（judgments 由分析管线实时产生），直接重建最简。

关键机制：
- partial unique index (address, hops, time_window_days) WHERE status IN
  ('queued','processing') —— BE-12/46 同参数幂等的数据库层保障；
- subgraph_snapshot 列启用 LZ4 TOAST 压缩（快照估算 300–800KB/行，BE-45）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None


def _old_judgments() -> list[sa.Column]:
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("seed_address", sa.String(62), nullable=False, index=True),
        sa.Column("hops", sa.Integer, server_default="3", nullable=False),
        sa.Column("time_window_days", sa.Integer, server_default="90", nullable=False),
        sa.Column("status", sa.String(20), server_default="queued", nullable=False),
        sa.Column("verdict", sa.String(20)),
        sa.Column("confidence", sa.Numeric(5, 4)),
        sa.Column("evidence_ids", sa.Text),
        sa.Column("subgraph_snapshot", sa.Text),
        sa.Column("error_detail", sa.Text),
        sa.Column("created_by", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    ]


def upgrade() -> None:
    op.drop_table("judgments")
    op.create_table(
        "judgments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("address", sa.String(62), nullable=False, index=True),
        sa.Column("hops", sa.Integer, server_default="3", nullable=False),
        sa.Column("time_window_days", sa.Integer, server_default="90", nullable=False),
        sa.Column("status", sa.String(20), server_default="queued", nullable=False,
                  index=True),  # D5: queued → processing → completed | failed
        sa.Column("subgraph_snapshot", JSONB),
        sa.Column("subgraph_hash", sa.String(64)),
        sa.Column("risk_level", sa.String(20)),
        sa.Column("matched_pattern_id", sa.String(36)),
        sa.Column("matched_pattern_name", sa.String(120)),
        sa.Column("confidence", sa.Float),
        sa.Column("evidence", JSONB),
        sa.Column("reasoning", sa.Text),
        sa.Column("recommended_action", sa.String(20)),
        sa.Column("model", sa.String(200)),
        sa.Column("prompt_version", sa.String(20)),
        sa.Column("builder_version", sa.String(20)),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("error_code", sa.String(50)),
        sa.Column("error_message", sa.Text),
        sa.Column("retry_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("thinking", sa.Text),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id")),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # 同参数幂等：同一 (address, hops, window) 至多一条进行中记录；
    # 终态行不占锁——completed 后重新提交产生新 judgment（BE-12 反例）
    op.create_index(
        "uq_judgments_active_per_params", "judgments",
        ["address", "hops", "time_window_days"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','processing')"),
    )
    op.execute(
        "ALTER TABLE judgments ALTER COLUMN subgraph_snapshot SET COMPRESSION lz4")


def downgrade() -> None:
    op.drop_index("uq_judgments_active_per_params", table_name="judgments")
    op.drop_table("judgments")
    op.create_table("judgments", *_old_judgments())
