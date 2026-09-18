#!/usr/bin/env bash
# issue #78 — 完整 E2E 门禁：全新 checkout 一条命令跑通
#   构建 → 隔离 DB/Redis → 迁移+fixture KB seed（init，失败即退）→
#   backend/worker/frontend → 完整 E2E（分析→判断→子图→PDF/HTML）→
#   停 worker 队列形态证明 → 清理（仅本次 project 资源）。
#
# 隔离保障（compose.e2e.yml）：
#   -p pt-e2e 独立 project（网络/卷）；端口错开（13000/18000/16380/15433）；
#   DB 名 pt_e2e（seed 脚本拒绝非 e2e 库）；Redis DB index /1（判决缓存隔离）。
#   测试运行期不访问公网（mock LLM + fixture 数据源 + stub embedding）。
#
# 用法：
#   bash infra/verify_e2e_local.sh          # 本地/CI 同一路径
#   bash infra/verify_e2e_local.sh --skip-build   # 镜像已存在时复用
#   bash infra/verify_e2e_local.sh --keep   # 失败后保留环境便于排查
set -uo pipefail
cd "$(dirname "$0")/.."

PROJ=pt-e2e
ART="${E2E_ARTIFACT_DIR:-/tmp/pt-e2e}"
SKIP_BUILD=0
KEEP_ON_FAIL=0
for arg in "$@"; do
  case "$arg" in
    --skip-build) SKIP_BUILD=1 ;;
    --keep) KEEP_ON_FAIL=1 ;;
  esac
done

COMPOSE=(docker compose -p "$PROJ" -f docker-compose.yml -f infra/compose.e2e.yml)
export DATABASE_URL="postgresql://pt:pt@localhost:15433/pt_e2e"
export REDIS_URL="redis://localhost:16380/1"
export BOOTSTRAP_ADMIN_EMAIL=e2e-admin@patterntrace.test
export BOOTSTRAP_ADMIN_PASSWORD=E2eAdminPass123!

cleanup() {
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    mkdir -p "$ART"
    "${COMPOSE[@]}" logs --no-color >"$ART/compose.log" 2>&1 || true
    echo "  (失败产物已保存: $ART)"
  fi
  if [ "$KEEP_ON_FAIL" = "0" ] || [ "$rc" -eq 0 ]; then
    echo "== 清理测试资源（仅 project ${PROJ}）=="
    "${COMPOSE[@]}" down -v --remove-orphans
  else
    echo "  (--keep：保留 $PROJ 环境便于排查；清理: docker compose -p $PROJ down -v)"
  fi
  exit "$rc"
}
trap cleanup EXIT

# interpreter：优先项目 .venv，其次 uv run（CI 无 .venv 时）
if [ -x .venv/bin/python ]; then PY=.venv/bin/python
elif command -v uv >/dev/null; then PY="uv run python"
else echo "no .venv and no uv; run 'uv sync --extra dev' first"; exit 1; fi

build_service() {
  # BuildKit 需要联网 resolve 基础镜像元数据；本地缓存场景下若 resolver
  # 超时（如断网/代理受限），回退 legacy builder（纯本地层）
  if ! "${COMPOSE[@]}" build "$1" 2>&1; then
    if [ "$SKIP_BUILD" = "1" ]; then return 1; fi
    echo "  (buildkit build 失败，回退 DOCKER_BUILDKIT=0 重试)"
    DOCKER_BUILDKIT=0 "${COMPOSE[@]}" build "$1"
  fi
}

echo "== 1. 启动 db/redis（隔离 project ${PROJ}）=="
if [ "$SKIP_BUILD" = "1" ]; then
  "${COMPOSE[@]}" up -d db redis || exit 1
else
  "${COMPOSE[@]}" up -d --build db redis || exit 1
fi

echo "== 2. 初始化：迁移 + fixture KB seed（失败立即退出，不先等 readyz）=="
build_service init || exit 1
"${COMPOSE[@]}" run --rm init || {
  echo "init failed"; exit 1; }

echo "== 3. 启动 backend/worker/frontend =="
"${COMPOSE[@]}" up -d db redis || exit 1
for svc in backend worker frontend; do build_service "$svc" || exit 1; done
"${COMPOSE[@]}" up -d backend worker frontend || exit 1

echo "== 3b. 就绪检查（backend readyz + 前端同源代理）=="
code=000
for i in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:18000/readyz 2>/dev/null || echo 000)
  [ "$code" = "200" ] && break
  sleep 2
done
[ "$code" = "200" ] || { echo "backend readyz never healthy (last=$code)"; exit 1; }
code=000
for i in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w '%{http_code}' \
    http://localhost:13000/api/v1/demo/addresses 2>/dev/null || echo 000)
  [ "$code" = "200" ] && break
  sleep 2
done
[ "$code" = "200" ] || { echo "frontend proxy smoke expected 200 got $code"; exit 1; }

echo "== 3. 完整 E2E（主流程经前端同源代理 :13000）=="
"$PY" tests/evaluation/run_e2e.py --base-url http://localhost:13000/api/v1 \
  --artifact-dir "$ART" || exit 1

echo "== 4. 独立 worker 消费证明（停 worker → queued → 恢复 → 完成）=="
"$PY" tests/evaluation/run_e2e.py \
  --base-url http://localhost:18000/api/v1 \
  --worker-gap "docker compose -p $PROJ -f docker-compose.yml -f infra/compose.e2e.yml" \
  --artifact-dir "$ART" || exit 1

echo ""
echo "✅ E2E 门禁通过（分析→判断→子图→PDF/HTML + 独立 worker 队列形态）"
