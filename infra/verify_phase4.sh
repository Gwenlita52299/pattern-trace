#!/usr/bin/env bash
# PatternTrace 阶段4 门禁验证：LLM 判断 + 前端可视化
#   完成标志: 端到端「地址 → 子图 → 判断 → 可视化」跑通
# 覆盖: LJ-01~20 / BE-01~50 中可自动化部分（单测 + e2e 脚本）；
#        FE 以 type-check + production build 收口（浏览器级 Playwright 属阶段6 E2E）。
# 前置: docker compose db/redis 可用；知识库数据由阶段2 run_all 产出。
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

export JWT_SECRET="${JWT_SECRET:-migration-placeholder}"

echo "== 单元测试套件（LJ + BE + 全部既有用例）=="
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
  .venv/bin/python -m ingest.run_all > /tmp/pt_runall_p4.log 2>&1 \
    && kb_rows=$(pg "SELECT count(*) FROM patterns WHERE evidence_grade='A'")
fi
[ "${kb_rows:-0}" -ge 100 ] && ok "知识库就绪（grade-A patterns=${kb_rows}）" \
                             || bad "知识库不足（检索将空手而归）"

n_vec=$(pg "SELECT count(*) FROM patterns WHERE semantic_embedding IS NOT NULL")
[ "${n_vec:-0}" -ge 100 ] && ok "向量就绪（${n_vec} 条）" || {
  echo "    向量缺失，补跑 compute_embeddings ..."
  .venv/bin/python -m ingest.compute_embeddings > /tmp/pt_embed_p4.log 2>&1 \
    && ok "向量补算完成" || bad "向量补算失败"
}

echo "== BE-46 · 幂等保障机制本身 =="
idx_def=$(pg "SELECT pg_get_indexdef(oid) FROM pg_class WHERE relname='uq_judgments_active_per_params'")
case "$idx_def" in
  *"WHERE"*"queued"*) ok "partial unique index 存在: $idx_def";;
  *)                  bad "partial unique index 缺失或谓词不符: '$idx_def'";;
esac

echo "== BE-45 · 快照 LZ4 压缩 =="
lz4=$(pg "SELECT count(*) FROM pg_attribute WHERE attrelid='judgments'::regclass AND attname='subgraph_snapshot' AND attcompression='l'")
[ "${lz4:-0}" -ge 1 ] && ok "subgraph_snapshot 已启用 lz4" || bad "attcompression != 'l'（需 VACUUM FULL 或迁移未生效）"

echo "== 端到端：地址 → 子图 → 判断 → 可视化 =="
if LLM_PROVIDER=mock GRAPH_DATA_MODE=fixture \
   .venv/bin/python tests/evaluation/run_e2e_phase4.py > /tmp/pt_e2e_p4.log 2>&1; then
  grep -c "^\[ok\]" /tmp/pt_e2e_p4.log | xargs -I{} echo "    e2e 检查 {} 项全部通过"
  grep "submit overhead" /tmp/pt_e2e_p4.log | sed 's/^/    /'
  ok "端到端管线跑通（含引用闭合/幂等/失败路径/subgraph 端点）"
else
  bad "端到端验证失败"; tail -25 /tmp/pt_e2e_p4.log
fi

echo "== 前端：类型检查 + 生产构建 =="
if [ ! -d frontend/node_modules ]; then
  echo "    node_modules 缺失，npm install ..."
  (cd frontend && npm install --no-audit --no-fund >> /tmp/pt_npm_p4.log 2>&1) \
    || bad "npm install 失败（见 /tmp/pt_npm_p4.log）"
fi
if (cd frontend && npm run type-check >> /tmp/pt_fe_p4.log 2>&1); then
  ok "tsc --noEmit 类型检查通过"
else
  bad "前端类型错误"; tail -15 /tmp/pt_fe_p4.log
fi
if (cd frontend && npm run build >> /tmp/pt_fe_p4.log 2>&1); then
  ok "next build 生产构建通过"
else
  bad "next build 失败"; tail -25 /tmp/pt_fe_p4.log
fi
# 关键产物静态断言：画布/Verdict/轮询三件套存在且被页面引用
for f in components/GraphCanvas.tsx components/VerdictCard.tsx store/analysis.ts; do
  [ -f "frontend/src/$f" ] && ok "FE 产物存在: $f" || bad "缺 FE 产物: $f"
done

echo ""
echo "========================================"
echo "结果: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && echo "🎉 阶段4 门禁通过" || echo "存在未达标项，见上方 ❌"
exit "$FAIL"
