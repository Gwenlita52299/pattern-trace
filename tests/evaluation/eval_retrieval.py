"""retrieval 评估基线 — retrieval-spec §9 / RT-16 / RT-17。

协议：
- 知识库中全部 grade-A pattern 按 id 哈希确定性切 20% 留出集；
- 留出样本以**自身已存向量**构造查询（模拟"理想输入子图"），
  召回时排除自身；
- 相关性定义：候选与查询同模板族（name 去掉层数后缀，如
  synth_peel_chain_l3 → synth_peel_chain 族）；confirmed 单条跳过。
- 指标：recall@10（混合召回）、MRR、精排后 top_3_hit_rate（WL 重排），
  外加 RT-06 的召回延迟分布。

输出 JSON 报告至 docs/baselines/retrieval_baseline.json（基线记录）。

用法：python tests/evaluation/eval_retrieval.py [--limit N] [--out PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JWT_SECRET", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.core.config import get_settings, reset_settings


def template_family(name: str) -> str:
    """synth_peel_chain_l3 → synth_peel_chain；其余按全名（单例族）。"""
    parts = name.rsplit("_l", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return name


def load_patterns(session) -> list[dict]:
    # 不按 evidence_grade 过滤（issue #10 起 synth=S/confirmed=A）：
    # 留出查询只用 lazarus_synth 族（循环内过滤），confirmed 作为库底存在
    rows = session.execute(text(
        "SELECT id, name, source, canonical_subgraph, "
        "       structural_features::text AS svec, "
        "       semantic_embedding::text AS evec "
        "FROM patterns WHERE semantic_embedding IS NOT NULL")).mappings().all()
    return [dict(r) for r in rows]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="retrieval recall@10 基线评估")
    parser.add_argument("--limit", type=int, default=0,
                        help="限制留出查询数（0=全部）")
    parser.add_argument("--out", default="docs/baselines/retrieval_baseline.json")
    args = parser.parse_args(argv)

    reset_settings()
    settings = get_settings()

    from ingest.common import get_engine

    with Session(get_engine()) as session:
        patterns = load_patterns(session)
        if not patterns:
            print("知识库为空——先运行 python -m ingest.run_all", file=sys.stderr)
            return 2

        # 确定性 20% 留出（id 哈希切分，任何机器同一结果）
        holdout = [p for p in patterns
                   if int(hashlib.sha256(p["id"].encode()).hexdigest(), 16) % 5 == 0]
        if args.limit:
            holdout = holdout[:args.limit]

        from backend.retrieval.retriever import Retriever

        retriever = Retriever(session, settings)
        latencies: list[float] = []
        recalls: list[float] = []
        rr_list: list[float] = []
        top3_hits: list[float] = []
        breakdown: dict[str, dict[str, int]] = defaultdict(lambda: {"q": 0, "hit10": 0})

        for p in holdout:
            family = template_family(p["name"])
            if p["source"] != "lazarus_synth":
                continue  # confirmed 单例无同族判据
            breakdown[family]["q"] += 1
            t0 = time.perf_counter()
            rows = retriever._hybrid_recall(
                svec=json.loads(p["svec"]), evec=json.loads(p["evec"]),
                exclude_ids=[p["id"]], limit=settings.retrieval_recall_limit)
            latencies.append((time.perf_counter() - t0) * 1000)

            top10_families = [template_family(r["name"]) for r in rows[:10]]
            hit_rank = next(
                (i + 1 for i, f in enumerate(top10_families) if f == family), 0)
            recalls.append(1.0 if hit_rank else 0.0)
            if hit_rank:
                rr_list.append(1.0 / hit_rank)
                breakdown[family]["hit10"] += 1
            else:
                rr_list.append(0.0)

            # RT-17：WL 精排后首个相关是否进入 Top-3
            result = retriever._rerank(p["canonical_subgraph"], rows,
                                       k=max(settings.retrieval_top_k, 3))
            reranked_families = [template_family(c.name)
                                 for c in result.candidates[:3]]
            top3_hits.append(1.0 if family in reranked_families else 0.0)

    n = len(recalls) or 1
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "config": {
            "w_struct": settings.w_struct, "w_semantic": settings.w_semantic,
            "recall_limit": settings.retrieval_recall_limit,
            "top_k": settings.retrieval_top_k,
            "wl_iterations": settings.wl_iterations,
            "embedding_model": settings.embedding_model,
        },
        "knowledge_base_size": len(patterns),
        "n_holdout_queries": len(recalls),
        "recall_at_10": round(sum(recalls) / n, 4),
        "mean_reciprocal_rank": round(sum(rr_list) / n, 4),
        "top_3_hit_rate_after_rerank": round(sum(top3_hits) / n, 4),
        "latency_ms_p50": round(statistics.median(latencies), 2) if latencies else None,
        "latency_ms_p95": (round(sorted(latencies)[int(0.95 * (len(latencies) - 1))], 2)
                           if latencies else None),
        "per_class_breakdown": {
            fam: {"queries": v["q"], "hit_at_10": v["hit10"],
                  "recall_at_10": round(v["hit10"] / v["q"], 4) if v["q"] else 0.0}
            for fam, v in sorted(breakdown.items())},
        "notes": "相关性=同模板族；查询向量取自留出样本已存向量（理想输入假设）；"
                 "stub embedding 为 token-bag 哈希，接入真实模型后需重跑基线",
    }

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k: report[k] for k in (
        "recall_at_10", "mean_reciprocal_rank",
        "top_3_hit_rate_after_rerank", "latency_ms_p50",
        "n_holdout_queries")}, ensure_ascii=False, indent=2))
    print(f"report -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
