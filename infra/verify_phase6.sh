#!/usr/bin/env bash
# PatternTrace 阶段6 门禁验证：测试与发布
#   覆盖：单元/集成/E2E 收口、契约测试 CT-01~03、性能 PERF-01（fixture 档）、
#         CI 流水线静态检查、部署配置存在性。
set -uo pipefail
cd "$(dirname "$0")/.."

PASS=0; FAIL=0
ok()  { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad() { echo "  ❌ $1"; FAIL=$((FAIL+1)); }
export JWT_SECRET="${JWT_SECRET:-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef}"
# 进程内测试工具（单元/E2E/perf）不消费 Redis 队列——指向不可达端口，
# task_queue 走显式进程内降级路径；判决缓存同步降级 InMemoryCache。
# embedding 同理钉 stub（本地 .env 的真实 provider 会限流/挂起）
export REDIS_URL="redis://127.0.0.1:6399/0"
export EMBEDDING_PROVIDER=stub
export EMBEDDING_MODEL=test-stub-1024
export EMBEDDING_DIM=1024
PY=.venv/bin/python

echo "== 1. 测试收口 =="
# E2E 跑在隔离 e2e 库上（fixture KB 只允许进 pt_e2e，见 seed_kb_fixture.py）
E2E_DB_URL="postgresql://pt:pt@localhost:5432/pt_e2e"
if ! "$PY" - <<PYEOF 2>/dev/null
import psycopg
conn = psycopg.connect("postgresql://pt:pt@localhost:5432/postgres", autocommit=True)
exists = conn.execute("SELECT 1 FROM pg_database WHERE datname='pt_e2e'").fetchone()
if not exists:
    conn.execute("CREATE DATABASE pt_e2e")
    print("created e2e database pt_e2e")
PYEOF
then
  echo "  (无 Postgres，跳过 E2E 初始化)" 
fi
if "$PY" - <<PYEOF >/dev/null 2>&1
import psycopg
conn = psycopg.connect("postgresql://pt:pt@localhost:5432/postgres")
conn.close()
PYEOF
  then
  export DATABASE_URL="$E2E_DB_URL"
  "$PY" -m alembic upgrade head >/dev/null 2>&1 \
    && "$PY" infra/seed_kb_fixture.py >/tmp/pt_p6_seed.log 2>&1 \
    && ok "e2e 库就绪（迁移 + fixture KB）" \
    || { bad "e2e 库初始化失败"; tail -5 /tmp/pt_p6_seed.log; }
fi
# 不加 CLI -q：pyproject addopts 已有 -q，叠加成 -qq 会隐藏 "N passed" 汇总行
if .venv/bin/python -m pytest tests/unit >/tmp/pt_p6_unit.log 2>&1; then
  ok "单元测试全量通过 ($(grep -oE '[0-9]+ passed' /tmp/pt_p6_unit.log | tail -1))"
else
  bad "单元测试失败"; tail -20 /tmp/pt_p6_unit.log
fi

if .venv/bin/python tests/evaluation/run_e2e.py \
     >/tmp/pt_p6_e2e.log 2>&1; then
  ok "E2E 业务闭环通过（run_e2e.py = phase4+phase5，隔离 e2e 库）"
else
  bad "E2E 失败"; tail -15 /tmp/pt_p6_e2e.log
fi

echo "== 2. 契约测试（CT-01~03）=="
n=$(grep -c "test_ct0" tests/unit/test_contract.py)
[ "$n" -ge 5 ] && ok "契约测试用例就位（${n} 个）" || bad "契约用例不足"
if .venv/bin/python -m pytest tests/unit/test_contract.py \
     >/tmp/pt_p6_ct.log 2>&1; then
  ok "契约测试执行通过（无 DB 环境自动跳过 DB 档）"
else
  bad "契约测试失败"; tail -12 /tmp/pt_p6_ct.log
fi
[ -f frontend/types/api.d.ts ] && ok "OpenAPI codegen 产物存在" \
                               || bad "缺 frontend/types/api.d.ts"
[ -f tests/unit/__snapshots__/openapi.json ] && ok "schema 快照守护存在" \
                                              || bad "缺 schema 快照"

echo "== 3. 性能档（PERF-01 nightly fixture 版）=="
if DATABASE_URL="$E2E_DB_URL" .venv/bin/python tests/performance/perf_phase6.py PERF-01 \
     >/tmp/pt_p6_perf.log 2>&1; then
  grep "\[ok\]" /tmp/pt_p6_perf.log | sed 's/^/    /'
  ok "PERF-01 达标"
else
  bad "PERF-01 未达标"; tail -12 /tmp/pt_p6_perf.log
fi
grep -q "perf_phase6" .github/workflows/ci.yml \
  && ok "nightly perf job 已挂接" || bad "CI 缺 nightly perf job"

echo "== 4. CI 与部署配置（compose 私有化形态，issue #78）=="
for f in .github/workflows/ci.yml docker-compose.yml infra/compose.e2e.yml \
         infra/Dockerfile.backend frontend/Dockerfile \
         infra/verify_e2e_local.sh infra/seed_kb_fixture.py \
         infra/fixtures/kb/patterns.json \
         README.md docs/demo-script.md; do
  [ -f "$f" ] && ok "配置存在: $f" || bad "缺失: $f"
done
grep -q "verify_e2e_local.sh" .github/workflows/ci.yml \
  && ok "CI e2e job 已挂接完整闭环（issue #78）" \
  || bad "CI 缺完整 E2E job"
command -v docker >/dev/null && {
  if JWT_SECRET=gate-secret docker compose config >/dev/null 2>&1; then
    ok "docker compose 配置合法"
  else
    bad "docker compose 配置非法"
  fi
} || echo "    (docker 不在 PATH，跳过 compose 校验)"

echo ""
echo "========================================"
echo "结果: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && echo "🎉 阶段6 门禁通过 — 可发布" || echo "存在未达标项，见上方 ❌"
exit "$FAIL"
