"""0010: judgment_events.detail + judgments.mock_scenario — issue #78。

- judgment_events.detail：阶段/检索摘要观测点。E2E 需要「本次检索实际
  返回了什么」与阶段顺序的持久化记录——HTTP 轮询会跳过快阶段，
  Redis 只存当前值，顺序只能靠持久化行。
- judgments.mock_scenario：mock provider 的 per-judgment 场景（E2E
  HTTP 模式注入 worker 侧 LLM 故障的通道；live provider 下 API 拒绝）。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0010"
down_revision: str | None = "0009"


def upgrade() -> None:
    op.add_column("judgment_events", sa.Column("detail", JSONB()))
    op.add_column("judgments", sa.Column("mock_scenario", sa.String(100)))


def downgrade() -> None:
    op.drop_column("judgments", "mock_scenario")
    op.drop_column("judgment_events", "detail")
