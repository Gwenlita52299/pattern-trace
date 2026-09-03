"""judgments 增加数据质量字段（issue #8）

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-09

issue #8：部分 Esplora 分支失败时保留已获取子图，并明确标记数据不完整——
- data_quality：complete | degraded（存在上游请求失败）；
- requires_manual_review：degraded 时为 true，提示人工复核。

纯增量列（带默认值，存量行自动回填为 complete / false），无回填逻辑。
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "judgments",
        sa.Column("data_quality", sa.String(20), nullable=False,
                  server_default="complete"),
    )
    op.add_column(
        "judgments",
        sa.Column("requires_manual_review", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("judgments", "requires_manual_review")
    op.drop_column("judgments", "data_quality")
