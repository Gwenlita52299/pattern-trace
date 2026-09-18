"""issue #78 — E2E fixture KB seed：infra/fixtures/kb/patterns.json → patterns 表。

幂等 upsert（key: seed_address + content_hash），可重复执行；附带
--verify 校验（条数/embedding 模型锁），供初始化步骤 fail-fast。

测试环境保护：写入只允许指向 e2e 库（数据库名 pt_e2e），
防止把 E2E fixture 灌进开发/生产 KB。

用法（容器内 repo 路径一致）：
    python infra/seed_kb_fixture.py            # seed + verify
    python infra/seed_kb_fixture.py --verify   # 仅校验
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FIXTURE_PATH = ROOT / "infra/fixtures/kb/patterns.json"
EXPECTED_E2E_DB_MARKER = "pt_e2e"


def load_fixture() -> tuple[str, list[dict]]:
    d = json.loads(FIXTURE_PATH.read_text())
    return d["kb_version"], d["patterns"]


def build_rows(patterns: list[dict]) -> list[dict]:
    """fixture JSON → patterns 行；向量在本脚本内直接填充（二段合一）。"""
    from backend.retrieval.channels import build_fingerprint
    from backend.retrieval.embedding import build_provider
    from backend.retrieval.features import describe_subgraph, extract_features
    from backend.core.config import get_settings
    from ingest.common import content_hash

    settings = get_settings()
    provider = build_provider(settings)
    rows = []
    for p in patterns:
        canon = p["canonical_subgraph"]
        nodes, edges = canon["nodes"], canon["edges"]
        desc = describe_subgraph(canon)
        rows.append({
            "id": str(uuid.uuid4()),
            "name": p["name"],
            "source": p["source"],
            "provenance": ("synthetic" if p["evidence_grade"] == "S"
                           else "confirmed"),
            "evidence_grade": p["evidence_grade"],
            "seed_address": p["seed_address"],
            "description": desc,
            "canonical_subgraph": canon,
            "structural_features": extract_features(nodes, edges),
            "semantic_embedding": provider.embed_batch([desc])[0],
            "retrieval_fingerprint": build_fingerprint(nodes, edges),
            "embedding_model": settings.embedding_model,
            "embedding_dim": settings.embedding_dim,
            "content_hash": content_hash(canon),
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true",
                    help="只校验（条数/模型锁），不写入")
    args = ap.parse_args()

    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from backend.core.config import get_settings
    from backend.models.knowledge import Pattern
    from ingest.common import get_engine, upsert_rows

    kb_version, patterns = load_fixture()
    settings = get_settings()

    # 测试环境标识（方案 4.5）：E2E fixture 只允许进 e2e 库
    db_name = settings.database_url.rstrip("/").rsplit("/", 1)[-1]
    if db_name != EXPECTED_E2E_DB_MARKER and not args.verify:
        print(f"refusing to seed non-E2E database {db_name!r}; target DB "
              "must be named 'pt_e2e' (use --verify to check without writing)")
        return 2

    engine = get_engine()
    with Session(engine) as session:
        if args.verify:
            e2e = session.execute(
                select(func.count(Pattern.id)).where(
                    Pattern.source == "e2e_fixture")).scalar()
            models = session.execute(
                select(Pattern.embedding_model).distinct()).scalars().all()
        else:
            rows = build_rows(patterns)
            n = upsert_rows(session, Pattern, rows,
                            key_cols=["seed_address", "content_hash"])
            session.commit()
            e2e = session.execute(
                select(func.count(Pattern.id)).where(
                    Pattern.source == "e2e_fixture")).scalar()
            models = [settings.embedding_model]
            print(f"[ok] fixture KB seeded: upserted {n}/{len(rows)} rows "
                  f"(kb_version={kb_version})")

    expected = len(patterns)
    models = sorted(m for m in models if m)
    mismatch = [m for m in models if m != settings.embedding_model]
    if mismatch:
        print(f"[FAIL] embedding model lock violated: DB={mismatch} "
              f"configured={settings.embedding_model!r}")
        return 1
    # 幂等 seed 的形态下 e2e_fixture 行数必须精确等于 fixture 条数；
    # 不满足说明 KB 被外部改过或 seed 未完成——不凭「非空」复用旧数据
    if e2e != expected:
        print(f"[FAIL] fixture KB rows mismatch: e2e_fixture={e2e} "
              f"expected={expected} (kb_version={kb_version})")
        return 1
    print(f"[ok] fixture KB verified: e2e_fixture={e2e} models={models} "
          f"kb_version={kb_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
