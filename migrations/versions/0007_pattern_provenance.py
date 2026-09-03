"""patterns / pattern_negatives 增加 provenance 来源性质字段（issue #10）

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-09

issue #10：合成样本使用 source=lazarus_synth 但仍沿用真实样本的 evidence_grade=A，
在检索、LLM 判断、前端与报告中容易被误解为真实确认证据。本次引入 provenance
区分证据来源性质，并把合成样本的 evidence_grade 从 A 调整为 S（synthetic template，
不再伪装成真实 Grade A 证据）：

- patterns.provenance：confirmed(真实链上) | synthetic(合成结构模板)；
- pattern_negatives.provenance：negative（合成普通交易负样本，仅阈值校准/误报评估）。

纯增量列 + 数据回填：先给列默认值，再按 source 一次性回填 provenance，
并把 lazarus_synth 行的 evidence_grade 从 A 刷为 S。
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "patterns",
        sa.Column("provenance", sa.String(20), nullable=False,
                  server_default="confirmed"),
    )
    op.add_column(
        "pattern_negatives",
        sa.Column("provenance", sa.String(20), nullable=False,
                  server_default="negative"),
    )

    # 数据回填：按 source 校正 provenance；合成样本不再保留真实 Grade A 语义
    op.execute(
        "UPDATE patterns SET provenance='confirmed' "
        "WHERE source='lazarus_confirmed'")
    op.execute(
        "UPDATE patterns SET provenance='synthetic' "
        "WHERE source='lazarus_synth'")
    op.execute(
        "UPDATE patterns SET evidence_grade='S' "
        "WHERE source='lazarus_synth' AND evidence_grade='A'")
    op.execute(
        "UPDATE pattern_negatives SET provenance='negative' "
        "WHERE source='constructed_normal'")


def downgrade() -> None:
    op.drop_column("pattern_negatives", "provenance")
    op.drop_column("patterns", "provenance")
