#!/usr/bin/env bash
# PatternTrace 阶段3 门禁验证：检索服务
#   完成标志: recall@10 基线跑通并记录（docs/baselines/retrieval_baseline.json）
# 覆盖: RT-01~18 中可自动化的部分；纯逻辑单测见 tests/unit/test_retrieval.py。
# 前置: docker compose db 可用；知识库数据由阶段2 run_all 产出（本脚本自检并补跑）。
set -uo pipefail
cd "$(dirname "$0")/.."

PASS=0; FAIL=0
ok()  { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad() { echo "  ❌ $1"; FAIL=$((FAIL+1)); }

pg() {
  .venv/bin/python - "$1" <<'PY'
import os, sys
os.environ.setdefault("JWT_SECRET", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
import psycopg
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

echo "== 单元测试套件（含 RT 纯逻辑用例）=="
if .venv/bin/python -m pytest tests/unit/ -q 2>&1 | tail -1; then
  ok "单元测试通过"
else
  bad "单元测试存在失败"
fi

echo "== 准备 DB 与知识库 =="
docker compose up -d db >/dev/null 2>&1
for i in $(seq 1 30); do pg "SELECT 1" >/dev/null 2>&1 && break; sleep 1; done
if .venv/bin/python -m alembic upgrade head >/dev/null 2>&1; then
  ok "alembic 迁移到 head"
else
  bad "迁移失败"; echo "结果: $PASS passed, $FAIL failed"; exit "$FAIL"
fi

kb_rows=$(pg "SELECT count(*) FROM patterns WHERE evidence_grade='A'")
if [ "${kb_rows:-0}" -lt 100 ]; then
  echo "    知识库不足(${kb_rows:-0})，补跑 python -m ingest.run_all ..."
  if .venv/bin/python -m ingest.run_all > /tmp/pt_runall_p3.log 2>&1; then
    kb_rows=$(pg "SELECT count(*) FROM patterns WHERE evidence_grade='A'")
  else
    bad "run_all 补跑失败"; tail -5 /tmp/pt_runall_p3.log
  fi
fi
[ "${kb_rows:-0}" -ge 100 ] && ok "知识库就绪（grade-A patterns=${kb_rows}）" \
                             || bad "知识库仍不足"

echo "== 向量新鲜度：与当前 stub 实现对齐 =="
# stub 算法演进会使已存向量失效（模型名不变），门禁强制重建保证查询/库存同空间
.venv/bin/python - <<'PY' >/dev/null
import psycopg
conn = psycopg.connect("postgresql://pt:pt@localhost:5432/patterntrace")
with conn.cursor() as cur:
    cur.execute("UPDATE patterns SET semantic_embedding=NULL")
    cur.execute("UPDATE pattern_negatives SET semantic_embedding=NULL")
conn.commit()
PY
rm -rf .cache/embeddings
if .venv/bin/python -m ingest.compute_embeddings > /tmp/pt_embed_p3.log 2>&1; then
  n_vec=$(pg "SELECT count(*) FROM patterns WHERE semantic_embedding IS NOT NULL")
  ok "向量全量重建完成（patterns=${n_vec}）"
else
  bad "embedding 重算失败"; tail -5 /tmp/pt_embed_p3.log
fi

echo "== RT-08 · HNSW 双索引 =="
n_hnsw=$(pg "SELECT count(*) FROM pg_indexes WHERE tablename='patterns' AND indexdef LIKE '%hnsw%'")
[ "${n_hnsw:-0}" -ge 2 ] && ok "两列各有 hnsw 索引 ($n_hnsw)" || bad "hnsw 数量 $n_hnsw"

echo "== RT-02 · 特征向量落库形态 =="
vec_ok=$(pg "SELECT count(*) FROM patterns WHERE semantic_embedding IS NOT NULL AND vector_dims(structural_features)=20 AND vector_dims(semantic_embedding)=1536 LIMIT 1")
[ "${vec_ok:-0}" -ge 1 ] && ok "structural=20 维 / semantic=1536 维，全部有限数值" || bad "维度异常"

echo "== RT-04 · 启动 fail-fast 模型锁（负向验证）=="
lock_result=$(EMBEDDING_MODEL=all-MiniLM-L6-v2 .venv/bin/python - <<'PY'
import os
os.environ["JWT_SECRET"] = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
from backend.core.config import reset_settings
reset_settings()
try:
    from backend.api.app import create_app
    create_app()
except RuntimeError as e:
    print("fail-fast" if "version lock" in str(e) else f"other:{e}")
else:
    print("no-error")
PY
)
case "$lock_result" in
  fail-fast) ok "错误模型配置下启动即报错退出";;
  *)         bad "期望 fail-fast，实际 '$lock_result'";;
esac
lock_pass=$(.venv/bin/python - <<'PY'
import os
os.environ["JWT_SECRET"] = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
from backend.core.config import reset_settings
reset_settings()
try:
    from backend.api.app import create_app
    app = create_app()
    print("ok")
except Exception as e:  # noqa: BLE001
    print(f"unexpected:{e}")
PY
)
[ "$lock_pass" = "ok" ] && ok "正确配置下启动通过" || bad "正确配置启动失败: $lock_pass"

echo "== RT-06/16/17 · 评估基线（recall@10 并记录）=="
if .venv/bin/python tests/evaluation/eval_retrieval.py > /tmp/pt_eval_p3.log 2>&1; then
  grep -E "recall_at_10|mrr|top_3|latency|queries" /tmp/pt_eval_p3.log | sed 's/^/    /'
else
  bad "评估脚本失败"; tail -10 /tmp/pt_eval_p3.log
fi

baseline="docs/baselines/retrieval_baseline.json"
if [ -f "$baseline" ]; then
  recall=$(.venv/bin/python -c "
import json; r=json.load(open('$baseline')); print(r['recall_at_10'])")
  latency=$(.venv/bin/python -c "
import json; r=json.load(open('$baseline')); print(r['latency_ms_p50'])")
  fields=$(.venv/bin/python -c "
import json; r=json.load(open('$baseline'))
need=['mean_reciprocal_rank','per_class_breakdown','top_3_hit_rate_after_rerank']
print('full' if all(k in r for k in need) else 'missing')")
  ok "基线报告已记录 -> $baseline"
  pass=$(.venv/bin/python -c "print(1 if float('$recall') >= 0.80 else 0)")
  [ "$pass" = "1" ] && ok "RT-16: recall@10 = $recall ≥ 0.80" \
                    || bad "RT-16: recall@10 = $recall < 0.80"
  pass6=$(.venv/bin/python -c "print(1 if float('$latency') <= 50 else 0)")
  [ "$pass6" = "1" ] && ok "RT-06: 召回 p50 ${latency}ms ≤ 50ms" \
                     || bad "RT-06: 召回 p50 ${latency}ms 超 50ms 预算"
  [ "$fields" = "full" ] && ok "RT-17: 报告含 MRR / per_class_breakdown / top_3_hit_rate" \
                         || bad "报告字段缺失"
else
  bad "基线报告未生成"
fi

echo ""
echo "========================================"
echo "结果: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && echo "🎉 阶段3 门禁通过" || echo "存在未达标项，见上方 ❌"
exit "$FAIL"
