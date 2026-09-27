"""0015: 管理端可配置 provider（前端「配置」页自选 provider）。

provider_configs：按 kind 存一行（本期只有 llm），admin 全局一份。

- provider/model/base_url 存在即整体覆盖 env 同名配置——切换 provider 时
  不会把上一个 provider 的 base_url 带过去（env 的 base_url 同样 provider 专属）
- api_key_encrypted：只存 Fernet 密文（钥匙来自 SECRETS_KEY），
  NULL 表示不覆盖 env 密钥；接口永远只回 has_key 布尔
- 不加数据回填：无行 = 完全按 env 运行，行为与升级前一致
"""

from alembic import op
import sqlalchemy as sa

revision: str = "0015"
down_revision: str | None = "0014"


def upgrade() -> None:
    op.create_table(
        "provider_configs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("base_url", sa.String(500)),
        sa.Column("api_key_encrypted", sa.Text()),
        sa.Column("updated_by", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        # 每个 kind 一行：唯一约束即「全局一份」的落库表达
        sa.UniqueConstraint("kind", name="uq_provider_configs_kind"),
    )


def downgrade() -> None:
    op.drop_table("provider_configs")
