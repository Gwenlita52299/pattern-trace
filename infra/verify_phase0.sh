#!/usr/bin/env bash
# PatternTrace 阶段0 门禁验证：
#   完成标志1: docker compose 一键启动全服务 healthy（IF-01）
#   完成标志2: 登录 / 受保护接口跑通（JWT access + refresh rotation）
# 附带抽检: IF-02（worker 无 localhost:6379）
#
# 前置: 项目根目录有 .env（JWT_SECRET / BOOTSTRAP_ADMIN_EMAIL / BOOTSTRAP_ADMIN_PASSWORD）
# 用法: bash infra/verify_phase0.sh
set -uo pipefail
cd "$(dirname "$0")/.."

set -a; source .env; set +a
API=http://localhost:8000

PASS=0; FAIL=0
ok()  { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad() { echo "  ❌ $1"; FAIL=$((FAIL+1)); }

echo "== IF-01 · docker compose up --build =="
docker compose up --build -d || { echo "compose up 失败"; exit 1; }

all_healthy=""
for i in $(seq 1 36); do
  st=$(docker compose ps --format '{{.Service}} {{.Health}}' 2>/dev/null)
  total=$(echo "$st" | grep -c '.')
  healthy=$(echo "$st" | grep -c ' healthy')
  echo "  [poll $i] $healthy/$total healthy"
  if [ "$healthy" -eq 6 ] && [ "$total" -eq 6 ]; then all_healthy=yes; break; fi
  sleep 5
done
[ "$all_healthy" = yes ] && ok "6 个服务全部 Up(healthy)" \
  || { bad "120s 内未全部 healthy"; docker compose ps; exit 1; }

echo "== IF-02 · worker 环境变量 =="
wenv=$(docker compose exec -T worker env)
echo "$wenv" | grep -q '^REDIS_URL=redis://redis:6379/0$' \
  && ok "REDIS_URL 指向 redis 服务" || bad "REDIS_URL 异常"
echo "$wenv" | grep -q '^ARQ_QUEUE_GRAPH=q_graph$' \
  && ok "ARQ 队列配置可见" || bad "ARQ 队列配置缺失"

echo "== 完成标志2 · 登录 / 受保护接口 =="
code=$(curl -s -o /dev/null -w '%{http_code}' "$API/healthz")
[ "$code" = 200 ] && ok "GET /healthz → 200" || bad "/healthz → $code"

code=$(curl -s -o /dev/null -w '%{http_code}' "$API/api/v1/cases")
[ "$code" = 401 ] && ok "无 token 访问受保护接口 → 401" || bad "无 token → $code（期望 401）"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$BOOTSTRAP_ADMIN_EMAIL\",\"password\":\"wrong-password\"}")
[ "$code" = 401 ] && ok "错误密码登录 → 401" || bad "错误密码 → $code（期望 401）"

login=$(curl -s -X POST "$API/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$BOOTSTRAP_ADMIN_EMAIL\",\"password\":\"$BOOTSTRAP_ADMIN_PASSWORD\"}")
access=$(echo "$login" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
refresh=$(echo "$login" | python3 -c 'import sys,json;print(json.load(sys.stdin)["refresh_token"])')
[ -n "$access" ] && ok "登录成功，拿到 access/refresh token" || { bad "登录失败: $login"; exit 1; }

code=$(curl -s -o /dev/null -w '%{http_code}' "$API/api/v1/cases" -H "Authorization: Bearer $access")
[ "$code" = 200 ] && ok "Bearer token 访问 /cases → 200" || bad "/cases 带 token → $code（期望 200）"

code=$(curl -s -o /dev/null -w '%{http_code}' "$API/api/v1/audit-logs" -H "Authorization: Bearer $access")
[ "$code" = 200 ] && ok "admin 角色访问 /audit-logs → 200" || bad "/audit-logs → $code（期望 200）"

rot=$(curl -s -X POST "$API/api/v1/auth/refresh" -H 'Content-Type: application/json' \
  -d "{\"refresh_token\":\"$refresh\"}")
new_refresh=$(echo "$rot" | python3 -c 'import sys,json;print(json.load(sys.stdin)["refresh_token"])' 2>/dev/null)
if [ -n "$new_refresh" ] && [ "$new_refresh" != "$refresh" ]; then
  ok "refresh rotation 成功，新 refresh ≠ 旧 refresh"
else
  bad "refresh rotation 异常: $rot"
fi

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/api/v1/auth/refresh" \
  -H 'Content-Type: application/json' -d "{\"refresh_token\":\"$refresh\"}")
[ "$code" = 401 ] && ok "重放旧 refresh → 401（reuse detection 生效）" || bad "旧 refresh 重放 → $code（期望 401）"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/api/v1/auth/refresh" \
  -H 'Content-Type: application/json' -d "{\"refresh_token\":\"$access\"}")
[ "$code" = 401 ] && ok "access token 冒充 refresh → 401" || bad "access 冒充 refresh → $code（期望 401）"

echo "== DB schema（alembic upgrade head 生效）=="
tables=$(docker compose exec -T db psql -U pt -d patterntrace -tAc \
  "SELECT string_agg(tablename,',') FROM pg_tables WHERE schemaname='public'")
echo "  tables: $tables"
for t in users refresh_tokens cases case_addresses judgments audit_logs alembic_version; do
  echo "$tables" | grep -qw "$t" && ok "表 $t 存在" || bad "缺表 $t"
done

echo ""
echo "========================================"
echo "结果: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && echo "🎉 阶段0 门禁通过" || echo "存在未达标项，见上方 ❌"
exit "$FAIL"
