"""阶段5 业务闭环：reports 表、case_addresses 复合唯一、audit_logs 规范化、judgment_events

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-24

- reports（backend-api-spec §4）：异步导出任务，storage_key 指向本地文件；
- case_addresses 补 PK(case_id,address) 语义的唯一约束（BE-24 幂等跳过）；
- audit_logs 由阶段0骨架重建为 spec §4 完整形状（BE-07/32 字段全集）；
- judgment_events 记录 D5 状态机迁移序列（CM-03 断言数据源）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36),
                  sa.ForeignKey("cases.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("format", sa.String(10), nullable=False),  # pdf | html
        sa.Column("status", sa.String(20), server_default="processing",
                  nullable=False),  # processing | completed | failed
        sa.Column("storage_key", sa.String(200)),
        sa.Column("error_code", sa.String(50)),
        sa.Column("error_message", sa.Text),
        sa.Column("created_by", sa.String(36)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )

    # 幂等关联（BE-24）：先清历史重复行再加约束
    op.execute(
        "DELETE FROM case_addresses a USING case_addresses b "
        "WHERE a.id > b.id AND a.case_id = b.case_id AND a.address = b.address")
    op.add_column("case_addresses",
                  sa.Column("judgment_id", sa.String(36)))
    op.create_unique_constraint(
        "uq_case_addresses_case_addr", "case_addresses", ["case_id", "address"])

    op.drop_table("audit_logs")
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("user_id", sa.String(36)),  # 可空：匿名请求也留痕
        sa.Column("request_id", sa.String(36), nullable=False, index=True),
        sa.Column("http_method", sa.String(10), nullable=False),
        sa.Column("http_path", sa.String(500), nullable=False),
        sa.Column("response_status", sa.Integer),
        sa.Column("action", sa.String(50), nullable=False, index=True),
        sa.Column("resource_type", sa.String(50)),
        sa.Column("resource_id", sa.String(100)),
        sa.Column("action_result", sa.String(20)),  # success | failure
        sa.Column("detail", JSONB),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("ip", sa.String(64)),
        sa.Column("user_agent", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )
    # CM-10：同一请求上下文可按 request_id 串联
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])

    op.create_table(
        "judgment_events",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("judgment_id", sa.String(36),
                  sa.ForeignKey("judgments.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("from_status", sa.String(20)),
        sa.Column("to_status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("judgment_events")
    op.drop_table("audit_logs")
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(36)),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("resource_type", sa.String(50)),
        sa.Column("resource_id", sa.String(36)),
        sa.Column("detail", sa.Text),
        sa.Column("ip_address", sa.String(45)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )
    op.drop_constraint("uq_case_addresses_case_addr", "case_addresses",
                       type_="unique")
    op.drop_column("case_addresses", "judgment_id")
    op.drop_table("reports")
