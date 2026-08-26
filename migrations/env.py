from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text
from sqlalchemy.dialects import postgresql

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("JWT_SECRET", "migration-placeholder")

from backend.models.base import Base
import backend.models.knowledge  # noqa: F401  — 注册知识库表到 metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql://pt:pt@localhost:5432/patterntrace"
    )


def run_migrations_offline() -> None:
    url = get_url().replace("postgresql://", "postgresql+psycopg://")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()
    configuration["sqlalchemy.url"] = get_url().replace("postgresql://", "postgresql+psycopg://")
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # 多实例同时启动时串行化迁移（spec infra §4）；session 级 advisory lock
        # 绑定本连接，异常路径由 finally 保证释放
        is_pg = connection.dialect.name == "postgresql"
        if is_pg:
            connection.execute(text("SELECT pg_advisory_lock(7453112401)"))
            # WHY: 取锁的 execute 会 autobegin 一个事务。若不清账，alembic 的
            # begin_transaction() 会误认为事务已存在而不接管提交，导致迁移 DDL 在
            # 连接关闭时被回滚、空库不建表（offline 不受影响）。commit 把它清掉，
            # 让 alembic 自己开事务、迁移后提交。会话级锁不受 COMMIT 影响，仍在。
            connection.commit()
        try:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
        finally:
            if is_pg:
                connection.execute(text("SELECT pg_advisory_unlock(7453112401)"))
                connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
