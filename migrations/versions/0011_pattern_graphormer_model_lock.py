"""0011: patterns.graphormer_model_id — 检索向量模型版本锁（issue #82）。

在线 ego 前向（graphormer_online）与库内 graphormer_embedding 必须出自
同一模型权重，cos 才有意义。用法与文本 embedding 模型锁（RT-04）同型：
入库时写入模型名，查询侧校验 settings.graphormer_model_name 不一致即
fail-fast，防止换模型后新旧向量混比。历史行（迁移 0009 入库）回填
clefourrier/graphormer-base-pcqm4mv2（当时的唯一模型）。
"""

from alembic import op
import sqlalchemy as sa

revision: str = "0011"
down_revision: str | None = "0010"


def upgrade() -> None:
    op.add_column(
        "patterns",
        sa.Column("graphormer_model_id", sa.String(120)))
    op.execute(
        "UPDATE patterns SET graphormer_model_id = "
        "'clefourrier/graphormer-base-pcqm4mv2' "
        "WHERE graphormer_embedding IS NOT NULL")


def downgrade() -> None:
    op.drop_column("patterns", "graphormer_model_id")
