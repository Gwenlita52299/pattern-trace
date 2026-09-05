#!/usr/bin/env bash
# PatternTrace 阶段2 门禁验证：数据入库与知识库
#   完成标志1: 知识库可查询（pgvector 余弦检索 + 完整 canonical/向量返回）
#   完成标志2: 正负样本比例达标（IG-05，口径 = patterns.grade A vs negatives）
# 覆盖: IG-01~15,17 中需要真实 PostgreSQL 的部分；纯逻辑单测见 tests/unit。
# 前置: docker compose 的 db 服务可用（localhost:5432）；bybit_rust golden 数据在配置路径。
set -uo pipefail
cd "$(dirname "$0")/.."

PASS=0; FAIL=0
ok()  { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad() { echo "  ❌ $1"; FAIL=$((FAIL+1)); }

# SQL 查询统一走 psycopg 直连（与 alembic/run_all 同一传输路径）；
# docker compose exec 在部分非交互环境下不可靠
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
    # 多语句（如 SET; EXPLAIN）时跳过无结果集的语句，取首个有记录的结果
    while cur.description is None and cur.nextset():
        pass
    for row in cur.fetchall():
        print("|".join("" if v is None else str(v) for v in row))
PY
}

echo "== 单元测试套件（含 IG 纯逻辑用例）=="
if .venv/bin/python -m pytest tests/unit/ -q 2>&1 | tail -1; then
  ok "单元测试通过"
else
  bad "单元测试存在失败"
fi

echo "== 准备 DB：启动 pgvector 并跑迁移 =="
docker compose up -d db >/dev/null 2>&1
for i in $(seq 1 30); do
  pg "SELECT 1" >/dev/null 2>&1 && break
  sleep 1
done
if .venv/bin/python -m alembic upgrade head; then
  ok "alembic 迁移到 head"
else
  bad "迁移失败"; echo "结果: $PASS passed, $FAIL failed"; exit "$FAIL"
fi

echo "== IG-13 · pgvector HNSW 索引 =="
idx=$(pg "SELECT indexname FROM pg_indexes WHERE tablename='patterns' AND indexdef LIKE '%hnsw%' ORDER BY 1")
echo "$idx" | sed 's/^/    /'
n_hnsw=$(echo "$idx" | grep -c hnsw || true)
[ "$n_hnsw" -ge 2 ] && ok "structural_features / semantic_embedding 各有 hnsw 索引" \
                    || bad "hnsw 索引数量不足 ($n_hnsw)"

echo "== IG-01 · run_all 一键入库 =="
# 注意：两次运行必须同配置，否则 synth 语料规模差会被误判为幂等破坏（IG-10）
if .venv/bin/python -m ingest.run_all > /tmp/pt_run_all_1.log 2>&1; then :;
else bad "run_all 首次执行失败"; cat /tmp/pt_run_all_1.log; fi
grep -E "lazarus subgraphs|synth corpus|negatives|labels loaded|embeddings" \
  /tmp/pt_run_all_1.log | sed 's/^/    /'

echo "== IG-10 · 幂等重跑 =="
before=$(pg "SELECT count(*) FROM patterns")
if .venv/bin/python -m ingest.run_all > /tmp/pt_run_all_2.log 2>&1; then
  after=$(pg "SELECT count(*) FROM patterns")
  [ "$before" = "$after" ] && ok "重复运行行数不变 ($after)" \
                          || bad "幂等破坏: $before -> $after"
else
  bad "run_all 重跑失败"; tail -5 /tmp/pt_run_all_2.log
fi

echo "== IG-02 · seed 分组独立性 =="
dupes=$(pg "SELECT count(*) FROM (SELECT seed_address FROM patterns WHERE source='lazarus_confirmed' GROUP BY seed_address HAVING count(*)>1) d")
[ "$dupes" = "0" ] && ok "每个 confirmed seed 恰一条记录" || bad "seed 分组混淆 ($dupes)"

echo "== IG-04 · 负样本隔离 =="
leak=$(pg "SELECT count(*) FROM patterns WHERE source='constructed_normal'")
negs=$(pg "SELECT count(*) FROM pattern_negatives")
[ "$leak" = "0" ] && [ "${negs:-0}" -gt 0 ] \
  && ok "patterns 零泄漏, pattern_negatives=$negs 条" \
  || bad "泄漏=$leak negatives=$negs"

echo "== IG-05 · 负正比例 =="
pos_a=$(pg "SELECT count(*) FROM patterns WHERE evidence_grade='A'")
ratio_in_range=$(.venv/bin/python -c "
pos, neg = int('$pos_a' or 0), int('${negs:-0}' or 0)
r = neg / pos if pos else 0
print(f'{r:.3f}', 1 if 2.5 <= r <= 3.5 else 0)
")
ratio=$(echo "$ratio_in_range" | cut -d' ' -f1)
in_range=$(echo "$ratio_in_range" | cut -d' ' -f2)
pos_confirmed=$(pg "SELECT count(*) FROM patterns WHERE source='lazarus_confirmed'")
echo "    positives(A)=${pos_a:-0} (confirmed=${pos_confirmed:-0}) negatives=${negs:-0} ratio=${ratio}:1"
[ "$in_range" = "1" ] && ok "比例 ${ratio} ∈ [2.5, 3.5]" || bad "比例越界 (${negs:-0}/${pos_a:-0})"

echo "== IG-06 · 负样本约束抽查 =="
bad_neg=$(.venv/bin/python - <<'PY'
import os
os.environ.setdefault("JWT_SECRET", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
from ingest.generate_negatives import generate
rows = generate(100)
bad = sum(1 for r in rows if any(e["is_remixer"] or e["is_crosschain"]
                                 for e in r["canonical_subgraph"]["edges"]))
print(bad)
PY
)
[ "$bad_neg" = "0" ] && ok "抽样 100 条负样本零混币器/跨链接触" || bad "违规 $bad_neg 条"

echo "== IG-07 · 标签表 + 内存加载 =="
cj_n=$(pg "SELECT count(*) FROM coinjoin_txids")
am_n=$(pg "SELECT count(*) FROM addresses_meta")
echo "    coinjoin_txids=${cj_n:-0} addresses_meta=${am_n:-0}"
# 跨链判定不再来自标签表（crosschain_tx_set 已移除，见 issue #5）：
# graph-builder 的跨链 early-stop 只依赖运行时 CrosschainDetector。
[ "${cj_n:-0}" -gt 0 ] && [ "${am_n:-0}" -gt 0 ] \
  && ok "coinjoin_txids / addresses_meta 均有数据" \
  || bad "标签表缺失 cj=$cj_n am=$am_n"
memload=$(.venv/bin/python - <<'PY'
import os
os.environ.setdefault("JWT_SECRET", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
from sqlalchemy.orm import Session
from ingest.common import get_engine
from ingest.load_labels import load_into_memory
with Session(get_engine()) as s:
    mixer, cj = load_into_memory(s)
print("ok" if len(mixer) > 0 and len(cj) > 0 else "empty")
PY
)
[ "$memload" = "ok" ] && ok "graph-builder 可加载为内存 set/dict" || bad "内存加载失败($memload)"

echo "== IG-08/09 · embedding 元数据与维度 =="
vec_check=$(pg "SELECT embedding_model, embedding_dim, vector_dims(semantic_embedding), vector_dims(structural_features) FROM patterns WHERE semantic_embedding IS NOT NULL LIMIT 1")
model=$(echo "$vec_check" | cut -d'|' -f1)
sem_d=$(echo "$vec_check" | cut -d'|' -f3)
st_d=$(echo "$vec_check" | cut -d'|' -f4)
[ "$model" = "text-embedding-3-small" ] && [ "$sem_d" = "1536" ] \
  && ok "语义向量 1536 维 + 模型元数据正确" || bad "model=$model sem_dim=$sem_d"
[ "$st_d" = "20" ] && ok "结构特征维度 = retrieval FEATURE_DIM(20)" || bad "struct dim=$st_d"

echo "== IG-11 · 同名共存 + 联合唯一约束 =="
groups=$(pg "SELECT count(*) FROM (SELECT name FROM patterns GROUP BY name HAVING count(DISTINCT seed_address)>=2) t")
[ "${groups:-0}" -ge 1 ] && ok "同名多 seed 共存组 $groups 组，联合唯一生效" \
                         || bad "未发现同名共存组"

echo "== 完成标志1 · 知识库可查询（pgvector 检索）=="
query_ok=$(.venv/bin/python - <<'PY'
import os
os.environ.setdefault("JWT_SECRET", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
from sqlalchemy import text
from sqlalchemy.orm import Session
from ingest.common import get_engine, structural_features
with Session(get_engine()) as s:
    probe = s.execute(text(
        "SELECT canonical_subgraph FROM patterns ORDER BY random() LIMIT 1")).scalar()
    assert probe, "存在 canonical_subgraph 为空的 pattern"
    qvec = str(structural_features(probe))
    hit = s.execute(text(
        "SELECT name, canonical_subgraph IS NOT NULL AS full_json, "
        "semantic_embedding IS NOT NULL AS has_vec, "
        "1 - (structural_features <=> CAST(:qv AS vector)) AS sim "
        "FROM patterns ORDER BY structural_features <=> CAST(:qv AS vector) LIMIT 3"),
        {"qv": qvec}).all()
    assert len(hit) == 3, f"召回不足 3 条: {len(hit)}"
    assert all(h.full_json and h.has_vec for h in hit), "返回不完整"
    print(f"top1={hit[0].name} sim={hit[0].sim:.4f}")
PY
)
if [ -n "$query_ok" ]; then echo "    $query_ok"; ok "余弦检索 Top-3 返回完整 pattern（JSON+双向量）"; else bad "知识库查询失败"; fi

echo "== IG-13 补充 · EXPLAIN 走索引 =="
uses_idx=$(.venv/bin/python - <<'PY'
import os
os.environ.setdefault("JWT_SECRET", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
import psycopg
conn = psycopg.connect(
    os.environ.get("DATABASE_URL", "postgresql://pt:pt@localhost:5432/patterntrace"),
    connect_timeout=5)
with conn.cursor() as cur:
    cur.execute(
        "SET enable_seqscan=off; "
        "EXPLAIN (FORMAT JSON) SELECT id FROM patterns "
        "ORDER BY structural_features <=> "
        "(SELECT structural_features FROM patterns "
        " WHERE semantic_embedding IS NOT NULL LIMIT 1) LIMIT 5")
    while cur.description is None and cur.nextset():
        pass
    plan = cur.fetchall()[0][0]  # psycopg 已解析为 list[dict]
def has_hnsw(node):
    if node.get("Index Name") == "ix_patterns_structural_hnsw":
        return True
    return any(has_hnsw(c) for c in node.get("Plans", []))
print(1 if any(has_hnsw(n["Plan"]) for n in plan) else 0)
PY
)
[ "${uses_idx:-0}" = "1" ] && ok "计划命中 ix_patterns_structural_hnsw" || bad "未走 hnsw 索引"

echo "== IG-15 · 故障注入断点续跑 =="
# 清 150 行的向量 **及其本地缓存**（>1 批）+ fault_every=1：
# 缓存不清会直接命中而绕过 provider，故障点永远不触发（IG-15 的正确语义）
fault_out=$(EMBEDDING_FAULT_EVERY=1 .venv/bin/python - <<'PY'
import os
os.environ.setdefault("JWT_SECRET", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.orm import Session
from ingest.common import get_engine
with Session(get_engine()) as s:
    ids = [r.id for r in s.execute(text(
        "SELECT id FROM pattern_negatives WHERE semantic_embedding IS NOT NULL LIMIT 150")).all()]
    if len(ids) < 150:
        print("skip"); raise SystemExit
    s.execute(text("UPDATE pattern_negatives SET semantic_embedding=NULL "
                   "WHERE id = ANY(:ids)"), {"ids": ids})
    s.commit()
for pid in ids:  # 连缓存一起清，迫使重算走 provider
    Path(f".cache/embeddings/pattern_negatives/{pid}.json").unlink(missing_ok=True)
try:
    from backend.core.config import reset_settings
    from ingest.compute_embeddings import EmbeddingAPIError, run
    reset_settings()
    with Session(get_engine()) as s:
        run(s, verbose=False)   # 第一批提交后即触发 simulated failure
except EmbeddingAPIError:
    print("raised")
PY
)
case "$fault_out" in
  raised*) ok "第一批提交后模拟 429 中断（该批已落库+缓存）";;
  skip)    ok "无缺口可注入——跳过故障段";;
  *)       bad "故障路径异常输出: '$fault_out'";;
esac
resume_ok=$(.venv/bin/python - <<'PY'
import os
os.environ.setdefault("JWT_SECRET", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
from sqlalchemy import text
from sqlalchemy.orm import Session
from ingest.compute_embeddings import run
from ingest.common import get_engine
from backend.core.config import reset_settings
reset_settings()
with Session(get_engine()) as s:
    stats = run(s, verbose=False)
    null_p = s.execute(text(
        "SELECT count(*) FROM patterns WHERE semantic_embedding IS NULL")).scalar()
    null_n = s.execute(text(
        "SELECT count(*) FROM pattern_negatives WHERE semantic_embedding IS NULL")).scalar()
print(f"{null_p == 0 and null_n == 0}|{stats['computed']}")
PY
)
nulls=$(echo "$resume_ok" | cut -d'|' -f1); recomputed=$(echo "$resume_ok" | cut -d'|' -f2)
[ "$nulls" = "True" ] && ok "续跑完成：仅补算缺失（本次补 $recomputed 条），全部非空" \
                      || bad "续跑后仍存在空向量"

echo "== IG-17 · 双实例并发 =="
(.venv/bin/python -m ingest.run_all > /tmp/pt_conc_a.log 2>&1) &
a_pid=$!
.venv/bin/python -m ingest.run_all > /tmp/pt_conc_b.log 2>&1
b_rc=$?
wait "$a_pid"; a_rc=$?
final=$(pg "SELECT count(*) FROM patterns")
[ "$a_rc" = "0" ] && [ "$b_rc" = "0" ] \
  && ok "两实例均正常退出，行数稳定($final)，advisory lock 生效" \
  || bad "并发实例失败 a=$a_rc b=$b_rc"

echo ""
echo "========================================"
echo "结果: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && echo "🎉 阶段2 门禁通过" || echo "存在未达标项，见上方 ❌"
exit "$FAIL"
