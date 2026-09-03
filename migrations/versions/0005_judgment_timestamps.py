"""judgments 增加结论/数据时间戳（issue #7）

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-09

issue #7：历史 Judgment 时间版本化——
- concluded_at：Judgment 进入 completed/failed 终态的时间；
- data_as_of：本次分析使用的链上数据时间点（统一链上时间不可得时，
  记录 provider 查询时间或最近可用确认时间）。

纯增量列（可空，存量行无需回填），旧行仅在重建于报告/子图查询时按需补写。
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "judgments",
        sa.Column("concluded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "judgments",
        sa.Column("data_as_of", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("judgments", "data_as_of")
    op.drop_column("judgments", "concluded_at")
