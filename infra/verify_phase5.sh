#!/usr/bin/env bash
# PatternTrace 阶段5 门禁验证：业务闭环
#   完成标志: 完整工作流可用——登录 → 建案 → 关联地址 → 分析 → 导出报告
# 覆盖: CM-01~12 / SEC-01~09 / REL-01~05 中可自动化部分；
#        另含 3 个种子案例（high/low/no_match）核对。
set -uo pipefail
cd "$(dirname "$0")/.."

PASS=0; FAIL=0
ok()  { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad() { echo "  ❌ $1"; FAIL=$((FAIL+1)); }

pg() {
  .venv/bin/python - "$1" <<'PY'
import os, sys, psycopg
conn = psycopg.connect(
    os.environ.get("DATABASE_URL", "postgresql://pt:pt@localhost:5432/patterntrace"),
    connect_timeout=5)
with conn.cursor() as cur:
    cur.execute(sys.argv[1])
    while cur.description is None and cur.nextset():
        pass
    for row in cur.fetchall():
        print("|".join("" if v is None else str(v) for v in row))
PY
}

export JWT_SECRET="${JWT_SECRET:-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef}"

echo "== 单元测试套件（含阶段5 新增用例）=="
if .venv/bin/python -m pytest tests/unit/ -q 2>&1 | tail -1; then
  ok "单元测试通过"
else
  bad "单元测试存在失败"
fi

echo "== 准备 DB / Redis 与知识库 =="
docker compose up -d db redis >/dev/null 2>&1
for i in $(seq 1 30); do pg "SELECT 1" >/dev/null 2>&1 && break; sleep 1; done
if .venv/bin/python -m alembic upgrade head >/dev/null 2>&1; then
  ok "alembic 迁移到 head"
else
  bad "迁移失败"; echo "结果: $PASS passed, $FAIL failed"; exit "$FAIL"
fi

kb_rows=$(pg "SELECT count(*) FROM patterns WHERE evidence_grade='A'")
if [ "${kb_rows:-0}" -lt 100 ]; then
  echo "    知识库不足(${kb_rows:-0})，补跑 python -m ingest.run_all ..."
  .venv/bin/python -m ingest.run_all > /tmp/pt_runall_p5.log 2>&1 \
    && kb_rows=$(pg "SELECT count(*) FROM patterns WHERE evidence_grade='A'")
fi
[ "${kb_rows:-0}" -ge 100 ] && ok "知识库就绪（grade-A patterns=${kb_rows}）" \
                             || bad "知识库不足"

n_vec=$(pg "SELECT count(*) FROM patterns WHERE semantic_embedding IS NOT NULL")
if [ "${n_vec:-0}" -lt 100 ]; then
  .venv/bin/python -m ingest.compute_embeddings > /tmp/pt_embed_p5.log 2>&1 \
    && ok "向量补算完成" || bad "向量补算失败"
else
  ok "向量就绪（${n_vec} 条）"
fi

echo "== 阶段5 schema 机制 =="
for tbl in reports judgment_events audit_logs; do
  n=$(pg "SELECT count(*) FROM information_schema.tables WHERE table_name='${tbl}'")
  [ "${n:-0}" -ge 1 ] && ok "表存在: ${tbl}" || bad "缺表: ${tbl}"
done
uq=$(pg "SELECT count(*) FROM pg_constraint WHERE conname='uq_case_addresses_case_addr'")
[ "${uq:-0}" -ge 1 ] && ok "case_addresses 复合唯一约束（BE-24）" || bad "缺复合唯一约束"

echo "== 种子案例：3 档风险各一 =="
if LLM_PROVIDER=mock GRAPH_DATA_MODE=fixture \
   .venv/bin/python -m backend.services.seed_cases > /tmp/pt_seed_p5.log 2>&1; then
  sed 's/^/    /' /tmp/pt_seed_p5.log | grep -E "seed case|skipped|risk=" || true
  ok "种子案例脚本执行成功（幂等可重入）"
else
  bad "种子案例脚本失败"; tail -10 /tmp/pt_seed_p5.log
fi
n_risks=$(pg "SELECT count(DISTINCT risk_level) FROM judgments WHERE status='completed' AND risk_level IN ('high','low','no_match')")
[ "${n_risks:-0}" -ge 3 ] && ok "三档 verdict 均已产出 (high/low/no_match)" \
                           || bad "三档 verdict 不齐（当前 ${n_risks:-0}/3）"

echo "== 端到端工作流：登录 → 建案 → 关联 → 分析 → 报告 =="
if LLM_PROVIDER=mock GRAPH_DATA_MODE=fixture \
   .venv/bin/python tests/evaluation/run_e2e_phase5.py > /tmp/pt_e2e_p5.log 2>&1; then
  n=$(grep -c "^\[ok\]" /tmp/pt_e2e_p5.log)
  echo "    e2e 检查 ${n} 项全部通过"
  ok "完整业务闭环跑通（含幂等/签名下载/审计串联/种子案例）"
else
  bad "端到端验证失败"; tail -25 /tmp/pt_e2e_p5.log
fi

echo "== 前端构建（案件页面 + 报告导出 UI）=="
if [ ! -d frontend/node_modules ]; then
  (cd frontend && npm install --no-audit --no-fund >> /tmp/pt_npm_p5.log 2>&1) \
    || bad "npm install 失败"
fi
if (cd frontend && npm run type-check >> /tmp/pt_fe_p5.log 2>&1); then
  ok "tsc --noEmit 通过"
else
  bad "前端类型错误"; tail -15 /tmp/pt_fe_p5.log
fi
if (cd frontend && npm run build >> /tmp/pt_fe_p5.log 2>&1); then
  ok "next build 生产构建通过"
else
  bad "next build 失败"; tail -25 /tmp/pt_fe_p5.log
fi
for f in "app/cases/page.tsx" "app/cases/[id]/page.tsx"; do
  [ -f "frontend/src/$f" ] && ok "FE 页面存在: $f" || bad "缺 FE 页面: $f"
done

echo ""
echo "========================================"
echo "结果: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && echo "🎉 阶段5 门禁通过" || echo "存在未达标项，见上方 ❌"
exit "$FAIL"
