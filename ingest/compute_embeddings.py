"""批量计算语义 embedding + 结构特征向量 — ingest-spec §5 / IG-08~09/14~15。

provider 抽象：stub（默认，确定性 hash 向量，不调外部 API——IG-08 允许）/
openai 兼容接口留扩展位。模型版本锁定写入 embedding_model/embedding_dim，
换模型需全量重建并迁移（§5），缓存文件按模型校验，不匹配即失效。

幂等与续跑：
- DB 中已有向量的行直接跳过（IG-15 重跑只补缺失）
- 按 pattern_id 本地 JSON 缓存（IG-14），命中则不再调 provider
- embedding_fault_every>0 时每 N 次调用模拟一次 API 故障（IG-15 测试钩子）

两表都算：negatives 用于检索阈值校准（§3「相同的特征计算和 embedding 流程」）。
"""
from __future__ import annotations

import json
from pathlib import Path

try:  # 包内运行（python -m ingest.xxx）
    from .common import structural_features
except ImportError:  # 直接运行（python ingest/compute_embeddings.py）
    from common import structural_features

# provider 实现已上移到 backend 层（依赖方向保持 scripts → backend），
# 此处仅 re-export 维持既有调用方/测试的导入路径
from backend.retrieval.embedding import (  # noqa: F401
    EmbeddingAPIError,
    StubEmbedding,
    build_provider,
)


def _cache_path(cache_dir: Path, table_name: str, pattern_id: str) -> Path:
    return cache_dir / table_name / f"{pattern_id}.json"


def _load_cache(path: Path, model: str, dim: int) -> list[float] | None:
    if not path.exists():
        return None
    try:
        item = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if item.get("model") != model:  # 模型版本锁定：不匹配视为无效缓存
        return None
    vec = item.get("vector")
    if not isinstance(vec, list) or len(vec) != dim:
        return None
    return vec


def run(session, tables: tuple[type, ...] | None = None,
        verbose: bool = True) -> dict:
    from backend.core.config import get_settings
    from backend.models.knowledge import Pattern, PatternNegative

    s = get_settings()
    tables = tables or (Pattern, PatternNegative)
    provider = build_provider(s)
    cache_dir = Path(s.embedding_cache_dir)
    fault_every = s.embedding_fault_every
    batch_size = s.embedding_batch_size

    stats = {"computed": 0, "cache_hits": 0, "skipped_db": 0}
    for model in tables:
        table_name = model.__tablename__
        rows = (session.query(model.id, model.description, model.canonical_subgraph,
                              model.semantic_embedding)
                .filter(model.semantic_embedding.is_(None))
                .all())
        stats["skipped_db"] += session.query(model.id).filter(
            model.semantic_embedding.isnot(None)).count()

        pending = []  # [(row, description)]：无缓存的待计算行
        for row in rows:
            cached = _load_cache(_cache_path(cache_dir, table_name, row.id),
                                 s.embedding_model, s.embedding_dim)
            if cached is not None:
                stats["cache_hits"] += 1
                _write_vector(session, model, row.id, cached,
                              s.embedding_model, row.canonical_subgraph)
            else:
                pending.append((row, row.description))

        # 批量调 provider（spec §5 每批 100）；故障注入点在批间——
        # 已写入的行落库、已算的向量进缓存，重跑自然续传
        for i in range(0, len(pending), batch_size):
            chunk = pending[i:i + batch_size]
            if fault_every and provider.calls and provider.calls % fault_every == 0:
                session.commit()  # 先保住已完成的批次
                raise EmbeddingAPIError(
                    f"simulated API failure at batch {i // batch_size} "
                    f"(table={table_name}, fault_every={fault_every})")
            vectors = provider.embed_batch([text for _, text in chunk])
            for (row, _text), vec in zip(chunk, vectors):
                cache_path = _cache_path(cache_dir, table_name, row.id)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(
                    {"model": s.embedding_model, "dim": len(vec), "vector": vec}))
                _write_vector(session, model, row.id, vec,
                              s.embedding_model, row.canonical_subgraph)
                stats["computed"] += 1
        session.commit()

    if verbose:
        print(f"embeddings: {stats} (model={s.embedding_model} "
              f"dim={s.embedding_dim})")
    return stats


def _write_vector(session, model, row_id: str, vector: list[float],
                  model_name: str, canonical_subgraph: dict) -> None:
    from sqlalchemy import update

    session.execute(
        update(model)
        .where(model.id == row_id)
        .values(
            semantic_embedding=vector,
            structural_features=structural_features(canonical_subgraph),
            embedding_model=model_name,
            embedding_dim=len(vector),
        ))


if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.environ.setdefault("JWT_SECRET", "migration-placeholder")

    try:
        from .common import get_engine
    except ImportError:  # 直接运行回退
        from common import get_engine
    from sqlalchemy.orm import Session

    from backend.models.knowledge import Pattern, PatternNegative  # noqa: F401

    engine = get_engine()
    with Session(engine) as s:
        run(s)
