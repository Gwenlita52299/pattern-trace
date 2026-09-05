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

echo "== 1. 测试收口 =="
# 不加 CLI -q：pyproject addopts 已有 -q，叠加成 -qq 会隐藏 "N passed" 汇总行
if .venv/bin/python -m pytest tests/unit >/tmp/pt_p6_unit.log 2>&1; then
  ok "单元测试全量通过 ($(grep -oE '[0-9]+ passed' /tmp/pt_p6_unit.log | tail -1))"
else
  bad "单元测试失败"; tail -20 /tmp/pt_p6_unit.log
fi

if .venv/bin/python tests/evaluation/run_e2e_phase5.py \
     LLM_PROVIDER=mock GRAPH_DATA_MODE=fixture \
     >/tmp/pt_p6_e2e.log 2>&1; then
  ok "E2E 业务闭环通过"
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
if .venv/bin/python tests/performance/perf_phase6.py PERF-01 \
     >/tmp/pt_p6_perf.log 2>&1; then
  grep "\[ok\]" /tmp/pt_p6_perf.log | sed 's/^/    /'
  ok "PERF-01 达标"
else
  bad "PERF-01 未达标"; tail -12 /tmp/pt_p6_perf.log
fi
grep -q "perf_phase6" .github/workflows/ci.yml \
  && ok "nightly perf job 已挂接" || bad "CI 缺 nightly perf job"

echo "== 4. CI/CD 与部署配置 =="
for f in .github/workflows/ci.yml .github/workflows/deploy.yml \
         fly.toml infra/Dockerfile.backend frontend/Dockerfile \
         vercel.json README.md docs/demo-script.md; do
  [ -f "$f" ] && ok "配置存在: $f" || bad "缺失: $f"
done
grep -q "flyctl" .github/workflows/deploy.yml 2>/dev/null \
  && ok "main → Fly.io 自动部署已挂接" || bad "deploy.yml 未挂 flyctl"
grep -q "vercel" .github/workflows/deploy.yml 2>/dev/null \
  && ok "main → Vercel 自动部署已挂接" || bad "deploy.yml 未挂 vercel"
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
