"""一键入库全流程 — ingest-spec §7 / IG-01 / IG-10 / IG-17。

顺序（标签先行：切图的混币器接触判定依赖 addresses_meta/coinjoin_txids）：
    load_labels → load_lazarus_subgraphs → corpus_gen(synth 正样本)
    → generate_negatives(3:1) → compute_embeddings(两表)

并发安全（IG-17）：pg advisory lock 串行化整个流程，双实例同时启动时
第二个阻塞等待而非竞态写入；配合 (seed_address, content_hash) 唯一约束
双重保证幂等。

用法：
    python -m ingest.run_all                 # 全流程
    python -m ingest.run_all --no-synth      # 仅真实 golden 数据
"""
from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PatternTrace 知识库一键入库")
    parser.add_argument("--no-synth", action="store_true",
                        help="跳过 playbook 合成正样本")
    args = parser.parse_args(argv)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.environ.setdefault("JWT_SECRET", "migration-placeholder")

    try:  # 包内运行（python -m ingest.run_all）
        from . import (
            compute_embeddings,
            corpus_gen,
            generate_negatives,
            load_labels,
            load_lazarus_subgraphs,
        )
        from .common import get_engine, ingest_lock
    except ImportError:  # 直接运行（python ingest/run_all.py）
        import compute_embeddings
        import corpus_gen
        import generate_negatives
        import load_labels
        import load_lazarus_subgraphs
        from common import get_engine, ingest_lock

    from sqlalchemy.orm import Session

    from backend.core.config import get_settings

    settings = get_settings()
    engine = get_engine()

    with ingest_lock(engine):
        with Session(engine) as session:
            labels = load_labels.run(session)
            session.commit()
            load_lazarus_subgraphs.run(session)
            session.commit()

            synth_n = 0 if args.no_synth else settings.ingest_synth_positives
            if synth_n:
                corpus_gen.run(session, synth_n, settings.ingest_synth_seed)
                session.commit()

            generate_negatives.run(session, ratio=settings.negative_ratio,
                                   seed_key=settings.ingest_synth_seed)
            session.commit()
            embeddings = compute_embeddings.run(session)

            # 汇总校验（IG-01/05）：positive 口径 = patterns 表全部行
            # （confirmed + synthetic，issue #10 不再以 evidence_grade='A' 为口径）；
            # 负样本仅存于 pattern_negatives，不入 patterns（IG-04）。
            from sqlalchemy import func

            from backend.models.knowledge import Pattern, PatternNegative

            pos_total = session.query(func.count(Pattern.id)).scalar() or 0
            pos_confirmed = (session.query(func.count(Pattern.id))
                             .filter(Pattern.provenance == "confirmed").scalar() or 0)
            pos_synth = (session.query(func.count(Pattern.id))
                         .filter(Pattern.provenance == "synthetic").scalar() or 0)
            neg_total = session.query(func.count(PatternNegative.id)).scalar() or 0
            neg_in_patterns = (session.query(func.count(Pattern.id))
                               .filter(Pattern.source == "constructed_normal").scalar() or 0)

    ratio = neg_total / pos_total if pos_total else 0.0
    print("\n===== run_all 汇总 =====")
    print(f"labels: {labels}")
    print(f"positives: total={pos_total} "
          f"(confirmed={pos_confirmed}, synthetic={pos_synth})")
    print(f"negatives: {neg_total}  ratio(neg:pos)={ratio:.2f}:1 "
          f"(目标 {settings.negative_ratio}:1)")
    print(f"embeddings: {embeddings}")

    problems = []
    if pos_total == 0 or neg_total == 0:
        problems.append("正或负样本为空（IG-01）")
    if neg_in_patterns != 0:
        problems.append("负样本泄漏进 patterns 业务召回库（IG-04）")
    if not 2.5 <= ratio <= 3.5:  # IG-05 允许区间
        problems.append(f"负正比例 {ratio:.2f} 越界（IG-05）")
    for p in problems:
        print(f"❌ {p}")
    if problems:
        return 1
    print("✅ 入库完成：知识库可查询，比例达标")
    return 0


if __name__ == "__main__":
    sys.exit(main())
