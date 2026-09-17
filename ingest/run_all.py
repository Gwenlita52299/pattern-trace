"""一键入库全流程 — ingest-spec §7 / IG-01 / IG-10 / IG-17。

顺序（标签先行：切图的混币器接触判定依赖 addresses_meta/coinjoin_txids）：
    load_labels → load_lazarus_subgraphs → compute_embeddings

2026-09 项目决策：知识库只用真实数据（lazarus_confirmed），synth 语料不再
入库参与匹配（corpus_gen 保留存档，--synth 显式开启）。

无负样本（2026-09 项目决策）：真实数据阶段负样本来源未接入，合成负样本
（generate_negatives）不再执行，pattern_negatives 保持为空（IG-04 停用，
隔离原则不变）。模块保留存档，待真实负样本来源接入后恢复。

并发安全（IG-17）：pg advisory lock 串行化整个流程，双实例同时启动时
第二个阻塞等待而非竞态写入；配合 (seed_address, content_hash) 唯一约束
双重保证幂等。

用法：
    python -m ingest.run_all                 # 全流程（仅真实数据）
    python -m ingest.run_all --synth         # 追加 playbook 合成正样本
"""
from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PatternTrace 知识库一键入库")
    parser.add_argument("--synth", action="store_true",
                        help="追加 playbook 合成正样本（默认关闭）")
    args = parser.parse_args(argv)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.environ.setdefault("JWT_SECRET", "migration-placeholder")

    try:  # 包内运行（python -m ingest.run_all）
        from . import (
            compute_embeddings,
            corpus_gen,
            load_labels,
            load_lazarus_subgraphs,
            load_graphormer_candidates,
        )
        from .common import get_engine, ingest_lock
    except ImportError:  # 直接运行（python ingest/run_all.py）
        import compute_embeddings
        import corpus_gen
        import load_graphormer_candidates
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

            kb_stats: dict = {}
            if settings.lazarus_subgraph_source == "graphormer_v2":
                # benchmark 数据源：candidates 直写 Graphormer 向量，
                # 文本 semantic_embedding 不参与召回（unseen-sim 基准口径）。
                # --synth 仅对 legacy 数据源生效（graphormer 路径无文本 embedding）
                kb_stats = load_graphormer_candidates.run(session)
                session.commit()
                embeddings = {"provider": "graphormer_pooled(784d)",
                              "note": "semantic embedding skipped"}
            else:
                load_lazarus_subgraphs.run(session)
                session.commit()

                synth_n = settings.ingest_synth_positives if args.synth else 0
                if synth_n:
                    corpus_gen.run(session, synth_n, settings.ingest_synth_seed)
                    session.commit()

                embeddings = compute_embeddings.run(session)

            # 汇总校验（IG-01/05）：positive 口径 = patterns 表全部行
            # （confirmed + synthetic，issue #10 不再以 evidence_grade='A' 为口径）。
            # 无负样本阶段（IG-04/05 停用）：pattern_negatives 必须为空，
            # 负样本源（constructed_normal）不得出现在 patterns 业务召回库。
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

    print("\n===== run_all 汇总 =====")
    print(f"labels: {labels}")
    if kb_stats:
        print(f"graphormer_v2: {kb_stats}")
    print(f"positives: total={pos_total} "
          f"(confirmed={pos_confirmed}, synthetic={pos_synth})")
    print(f"negatives: {neg_total}（无负样本阶段，预期 0）")
    print(f"embeddings: {embeddings}")

    problems = []
    if pos_total == 0:
        problems.append("正样本为空（IG-01）")
    if neg_total != 0:
        problems.append("无负样本阶段 pattern_negatives 应为空（IG-04 停用）")
    if neg_in_patterns != 0:
        problems.append("负样本泄漏进 patterns 业务召回库（IG-04）")
    for p in problems:
        print(f"❌ {p}")
    if problems:
        return 1
    print("✅ 入库完成：知识库可查询")
    return 0


if __name__ == "__main__":
    sys.exit(main())
