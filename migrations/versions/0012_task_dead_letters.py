"""0012: 任务死信表 + Report 排队态（issue #74）。

- task_dead_letters：重试耗尽/永久错误的任务档案（任务类型、业务 ID、
  尝试次数、最后错误、失败时间、重跑记录），供管理员治理
- reports.status 默认值从 processing 改为 queued：此前投递即落 processing，
  无法区分「已入队待执行」与「worker 正在生成」；issue #74 需要排队态来
  支持取消（排队可直接取消）与队列深度观测。历史行不回填（它们确实
  已经进入过执行阶段）。
"""

from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: str | None = "0011"


def upgrade() -> None:
    op.create_table(
        "task_dead_letters",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("task_type", sa.String(20), nullable=False),
        sa.Column("business_id", sa.String(64), nullable=False),
        sa.Column("queue", sa.String(64)),
        sa.Column("attempts", sa.Integer(), nullable=False,
                  server_default="1"),
        sa.Column("error_code", sa.String(50)),
        sa.Column("last_error", sa.Text()),
        sa.Column("failed_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("requeued_at", sa.DateTime(timezone=True)),
        sa.Column("requeued_by", sa.String(36)),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_dead_letters_task_type", "task_dead_letters",
                    ["task_type"])
    op.create_index("ix_task_dead_letters_business_id", "task_dead_letters",
                    ["business_id"])
    op.create_index("ix_task_dead_letters_failed_at", "task_dead_letters",
                    ["failed_at"])
    op.alter_column("reports", "status", server_default="queued")
    # 已经落 processing 的历史行保持原值（它们确实进入过执行）


def downgrade() -> None:
    op.alter_column("reports", "status", server_default="processing")
    op.drop_index("ix_task_dead_letters_failed_at",
                  table_name="task_dead_letters")
    op.drop_index("ix_task_dead_letters_business_id",
                  table_name="task_dead_letters")
    op.drop_index("ix_task_dead_letters_task_type",
                  table_name="task_dead_letters")
    op.drop_table("task_dead_letters")
