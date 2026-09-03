"""混合检索与 WL 精排 — retrieval-spec §1~§8。

流程：SubgraphResult → canonical dict
    → 结构特征 + 语义描述 embedding
    → pgvector 加权距离精确召回 Top-N（权重仅来自 Settings，禁止请求传入 §5）
    → 带属性 WL 子树核精排 Top-K（§6）
    → PatternCandidate（含差异说明提示）→ RetrievalResult

模型版本锁定：DB 中已写入向量的 embedding_model 必须与配置一致，
不一致直接拒绝服务（RT-04 / ingest-spec §5）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import bindparam, text

try:
    from .embedding import build_provider
    from .features import (
        describe_subgraph,
        generate_difference_note,
        structural_features,
        wl_subtree_similarity,
    )
except ImportError:  # 允许以脚本方式单独加载本模块
    from backend.retrieval.embedding import build_provider
    from backend.retrieval.features import (
        describe_subgraph,
        generate_difference_note,
        structural_features,
        wl_subtree_similarity,
    )

# 与 ingest canonical 同 schema 的键集——两个向量空间必须同构
_NODE_KEYS = ("id", "kind", "label", "first_layer", "total_received_btc",
              "total_sent_btc", "utxo_count", "direct_related_to_lazarus")
_EDGE_KEYS = ("id", "source", "target", "txid", "tx_layer", "value_ratio",
              "dst_value_btc", "total_num_inputs", "total_num_outputs",
              "is_stopped_expansion", "is_remixer", "is_crosschain",
              "op_return_protocol")


def validate_canonical_subgraph(canon: dict) -> dict:
    """CT-03 builder 输出校验层：拒绝引用缺失节点的边（ID 契约反向防御）。

    canonical dict 形态与 subgraphresult_to_canonical 一致；校验失败抛
    pydantic ValidationError，编排层按 TASK_FAILED 语义化失败并留痕。
    """
    from pydantic import BaseModel, Field, model_validator

    class _Edge(BaseModel):
        id: str
        source: str
        target: str
        model_config = {"extra": "allow"}

    class _Node(BaseModel):
        id: str
        kind: str
        model_config = {"extra": "allow"}

    class _Subgraph(BaseModel):
        seed_address: str = ""
        nodes: list[_Node] = Field(default_factory=list)
        edges: list[_Edge] = Field(default_factory=list)
        stats: dict = Field(default_factory=dict)

        @model_validator(mode="after")
        def _edges_reference_known_nodes(self):
            known = {n.id for n in self.nodes}
            dangling = [e.id for e in self.edges
                        if e.source not in known or e.target not in known]
            if dangling:
                raise ValueError(
                    f"edges reference unknown node ids: {dangling[:5]}")
            return self

    return _Subgraph.model_validate(canon).model_dump(exclude_none=True)


class EmbeddingModelMismatch(RuntimeError):
    """知识库向量与配置模型的版本不匹配（RT-04 fail-fast）。"""


@dataclass
class PatternCandidate:
    pattern_id: str
    name: str
    description: str = ""
    canonical_subgraph: dict = field(default_factory=dict)
    similarity_score: float = 0.0
    structural_similarity: float = 0.0
    semantic_similarity: float = 0.0
    wl_kernel_score: float = 0.0
    evidence_grade: str = "A"
    # issue #10：来源性质与证据等级分离——合成模板(source=lazarus_synth,
    # provenance=synthetic)只作结构检索参考，不会被解释为真实链上证据。
    source: str = "lazarus_confirmed"
    provenance: str = "confirmed"
    difference_note: str | None = None


@dataclass
class RetrievalResult:
    candidates: list[PatternCandidate] = field(default_factory=list)


def subgraphresult_to_canonical(result) -> dict:
    """GraphBuilder.SubgraphResult → canonical dict。

    键集与 ingest.common.to_canonical 产物一致，保证查询向量落在与
    知识库相同的空间（RT-02/RT-09 的前提）。
    """
    nodes = [{k: getattr(n, k) for k in _NODE_KEYS if hasattr(n, k)}
             for n in result.nodes]
    edges = [{k: getattr(e, k) for k in _EDGE_KEYS if hasattr(e, k)}
             for e in result.edges]
    seed_address = ""
    for n in nodes:
        if n.get("kind") == "address" and n.get("first_layer") == 0:
            seed_address = n.get("label") or ""
            break
    # issue #8：数据质量元数据。result 可能是无 stats 的桩（检索纯逻辑测试），
    # 一并兜底为完整数据默认值。
    stats = getattr(result, "stats", None)
    def _stat(name, default):
        return getattr(stats, name, default) if stats is not None else default
    return {
        "seed_address": seed_address,
        "nodes": sorted(nodes, key=lambda n: n.get("id", "")),
        "edges": sorted(edges, key=lambda e: e.get("id", "")),
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "max_first_layer": max((n.get("first_layer") or 0 for n in nodes),
                                   default=0),
            # issue #8：部分失败保留 + 数据质量元数据
            "data_quality": _stat("data_quality", "complete"),
            "requires_manual_review": bool(_stat("requires_manual_review", False)),
            "missing_branches": int(_stat("missing_branches", 0)),
            "source_errors": list(_stat("source_errors", [])),
        },
    }


def verify_embedding_model_lock(session, configured_model: str) -> None:
    """启动校验钩子：DB 向量模型 ≠ 配置模型时 fail-fast（RT-04）。"""
    rows = session.execute(text(
        "SELECT DISTINCT embedding_model FROM patterns "
        "WHERE embedding_model <> ''")).scalars().all()
    if not rows:
        return  # 空库尚未锁定
    mismatched = sorted(set(rows) - {configured_model})
    if mismatched:
        raise EmbeddingModelMismatch(
            f"embedding model version lock violated: DB has {mismatched}, "
            f"configured {configured_model!r}. "
            "换模型需全量重建并迁移（ingest-spec §5）")


def _rrf_fuse(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion（RT-07 两路 ANN 融合预留）。"""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for pos, pid in enumerate(ranking):
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (k + pos + 1)
    return scores


class Retriever:
    """进程内检索服务（无独立 HTTP 端点，backend 直接调用）。"""

    def __init__(self, session, settings=None):
        self.session = session
        if settings is None:
            from backend.core.config import get_settings

            settings = get_settings()
        self.settings = settings

    # -- 入口 ------------------------------------------------------------
    def retrieve(self, subgraph, *, exclude_ids: list[str] | None = None,
                 k: int | None = None) -> RetrievalResult:
        """subgraph: SubgraphResult 或 canonical dict。"""
        canon = (subgraph if isinstance(subgraph, dict)
                 else subgraphresult_to_canonical(subgraph))
        if not canon.get("nodes"):
            return RetrievalResult()  # RT-18：空子图 → 明确的无匹配信号

        svec = structural_features(canon)
        evec = self._embed_canonical(canon)
        verify_embedding_model_lock(self.session, self.settings.embedding_model)

        recall_limit = self.settings.retrieval_recall_limit
        recalled = self._hybrid_recall(svec, evec,
                                       exclude_ids=exclude_ids or [],
                                       limit=recall_limit)
        if not recalled:
            return RetrievalResult()

        return self._rerank(canon, recalled, k or self.settings.retrieval_top_k)

    def _embed_canonical(self, canon: dict) -> list[float]:
        # 描述文本与入库侧共用同一实现（features.describe_subgraph），
        # 保证查询向量落在知识库同一 token 空间
        provider = build_provider(self.settings)
        return provider.embed_batch([describe_subgraph(canon)])[0]

    # -- 召回 -------------------------------------------------------------
    def _hybrid_recall(self, svec: list[float], evec: list[float], *,
                       exclude_ids: list[str], limit: int) -> list[dict]:
        """加权余弦距离精确扫描（数千条规模 <50ms，spec §5 修正版）。

        权重从 Settings 读取、SQL 绑定参数传入——函数签名不含任何
        可被外部注入的权重参数（RT-05）。
        """
        sql = text("""
            SELECT id, name, source, provenance, description, canonical_subgraph, evidence_grade,
                   1 - (structural_features <=> CAST(:sv AS vector)) AS struct_sim,
                   1 - (semantic_embedding  <=> CAST(:ev AS vector)) AS sem_sim,
                   :w_struct * (structural_features <=> CAST(:sv AS vector))
                 + :w_semantic * (semantic_embedding  <=> CAST(:ev AS vector)) AS dist
            FROM patterns
            WHERE embedding_model = :model
              AND id NOT IN :excluded
            ORDER BY dist
            LIMIT :limit
        """).bindparams(bindparam("excluded", expanding=True))
        rows = self.session.execute(sql, {
            "sv": str(svec), "ev": str(evec),
            "w_struct": self.settings.w_struct,
            "w_semantic": self.settings.w_semantic,
            "model": self.settings.embedding_model,
            "excluded": exclude_ids or [""],
            "limit": limit,
        }).mappings().all()
        return [dict(r) for r in rows]

    def _recall_ann(self, svec: list[float], evec: list[float], *,
                    exclude_ids: list[str], limit: int) -> list[dict]:
        """两路 ANN（各自 HNSW Top-N）+ RRF 融合 —— 规模化预留（RT-07 P2）。"""
        base = """
            SELECT id, name, source, provenance, description, canonical_subgraph, evidence_grade
            FROM patterns
            WHERE embedding_model = :model AND id NOT IN :excluded
            ORDER BY {col} <=> CAST(:vec AS vector)
            LIMIT :lim
        """

        def one(col: str, vec: str) -> list[dict]:
            sql = text(base.format(col=col)).bindparams(
                bindparam("excluded", expanding=True))
            rows = self.session.execute(sql, {
                "vec": vec, "model": self.settings.embedding_model,
                "excluded": exclude_ids or [""],
                "lim": max(limit, self.settings.retrieval_recall_limit),
            }).mappings().all()
            return [dict(r) for r in rows]

        by_struct = [r["id"] for r in one("structural_features", str(svec))]
        by_semantic = [r["id"] for r in one("semantic_embedding", str(evec))]
        fused = _rrf_fuse([by_struct, by_semantic])
        order = sorted(fused, key=fused.get, reverse=True)[:limit]
        by_id = {r["id"]: r for pair in (
            one("structural_features", str(svec)),
            one("semantic_embedding", str(evec))) for r in pair}
        return [by_id[pid] for pid in order if pid in by_id]

    # -- 精排 --------------------------------------------------------------
    def _rerank(self, query_canon: dict, recalled: list[dict],
                k: int) -> RetrievalResult:
        iterations = getattr(self.settings, "wl_iterations", 3)
        scored: list[tuple[float, float, float, dict]] = []
        for row in recalled:
            cand_canon = row["canonical_subgraph"] or {"nodes": [], "edges": []}
            wl = wl_subtree_similarity(query_canon, cand_canon,
                                       iterations=iterations)
            hybrid_sim = max(0.0, min(1.0 - row["dist"], 1.0))
            # 最终分：WL 结构核主导，混合召回相似度作平滑项
            final = round(0.6 * wl + 0.4 * hybrid_sim, 6)
            scored.append((final, wl, hybrid_sim, row))

        scored.sort(key=lambda t: (-t[0], t[3]["id"]))
        candidates = []
        for final, wl, hybrid_sim, row in scored[:k]:
            cand_canon = row["canonical_subgraph"] or {"nodes": [], "edges": []}
            candidates.append(PatternCandidate(
                pattern_id=row["id"],
                name=row["name"],
                description=row.get("description") or "",
                canonical_subgraph=cand_canon,
                similarity_score=max(0.0, min(final, 1.0)),
                structural_similarity=round(row["struct_sim"], 6),
                semantic_similarity=round(row["sem_sim"], 6),
                wl_kernel_score=round(wl, 6),
                evidence_grade=row.get("evidence_grade") or "A",
                source=row.get("source") or "lazarus_confirmed",
                provenance=row.get("provenance") or "confirmed",
                difference_note=generate_difference_note(query_canon, cand_canon),
            ))
        return RetrievalResult(candidates=candidates)
