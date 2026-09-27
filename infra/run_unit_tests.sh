#!/usr/bin/env bash
# 单测隔离跑法：把 tests/unit 指向专用库 patterntrace_test，绝不碰开发库。
#
# 为什么需要它：单测里存在全表清理（如 DELETE FROM judgments），而
# DATABASE_URL 默认就是开发库（和本地栈共用同一个 Postgres）。直接
# `uv run pytest tests/unit` 会清掉开发库里正在查看的分析记录，页面随即 404。
# 库名守卫（scripts/db_guard.py）会拒绝在非专用库上执行这类清理。
#
# 与 CI 一致：CI 也是在只跑过 `alembic upgrade head` 的空库上跑单测，
# 所以这里同样**只建 schema 不拷数据**（秒级），不需要同步开发库的 2.7GB KB。
#
# 用法：
#   bash infra/run_unit_tests.sh                # 重建 patterntrace_test 后全量跑
#   bash infra/run_unit_tests.sh tests/unit/test_provider_config.py -q
#   bash infra/run_unit_tests.sh --keep         # 复用现有测试库（更快，会累积数据）
set -uo pipefail
cd "$(dirname "$0")/.."

KEEP=0
if [[ "${1:-}" == "--keep" ]]; then KEEP=1; shift; fi

TEST_DB="${TEST_DB_NAME:-patterntrace_test}"
ADMIN_URL="${ADMIN_DB_URL:-postgresql://pt:pt@localhost:5432/postgres}"
TEST_DB_URL="${TEST_DB_URL:-postgresql://pt:pt@localhost:5432/$TEST_DB}"
PY=.venv/bin/python

export JWT_SECRET="${JWT_SECRET:-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef}"
# 进程内测试工具不消费 Redis 队列 → 指向不可达端口走显式降级路径，
# 判决缓存同步降级 InMemoryCache，避免污染开发环境的 Redis
export REDIS_URL="redis://127.0.0.1:6399/0"
export EMBEDDING_PROVIDER=stub
export EMBEDDING_MODEL=test-stub-1024
export EMBEDDING_DIM=1024
# 关键：单测一律指向专用库
export DATABASE_URL="$TEST_DB_URL"

if ! "$PY" - <<PYEOF
import psycopg, sys
try:
    psycopg.connect("$ADMIN_URL").close()
except Exception as exc:
    print(f"Postgres 不可达（{exc.__class__.__name__}）——先启动 db："
          "docker compose up -d db")
    sys.exit(1)
PYEOF
then exit 1; fi

if [[ "$KEEP" == "0" ]]; then
  "$PY" - "$ADMIN_URL" "$TEST_DB" <<'PYEOF'
import sys
import psycopg

admin_url, db_name = sys.argv[1], sys.argv[2]
conn = psycopg.connect(admin_url, autocommit=True)
conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
conn.execute(f'CREATE DATABASE "{db_name}"')
print(f"[unit] 测试库已重建：{db_name}")
PYEOF
  [[ $? -eq 0 ]] || { echo "建库失败"; exit 1; }
fi

"$PY" -m alembic upgrade head >/dev/null 2>&1 || { echo "迁移失败（$TEST_DB_URL）"; exit 1; }
echo "[unit] DATABASE_URL=$TEST_DB_URL"
exec "$PY" -m pytest "${@:-tests/unit}"
