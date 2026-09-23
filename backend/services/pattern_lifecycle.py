"""模式知识库版本/审核/索引生命周期（issue #75）。

核心不变量：
1. ``patterns`` 行**始终代表当前生效版本**。编辑不碰它，只往
   ``pattern_revisions`` 写一条 draft；审核通过才把 draft COW 进当前行。
   因此「索引完成前继续使用上一 active 版本」是结构性保证——不存在
   「行被改成半更新状态、召回读到中间态」的窗口。
2. ``pattern_revisions`` 只 INSERT（+ 审核状态字段与索引元数据的回填），
   历史内容不可改写；编辑与回滚都产生新版本行。
3. 乐观锁以 ``patterns.revision`` 为基线：编辑/停用必须携带
   ``expected_revision``，与实际不符即 409（并发修改不覆盖）。同一基线上
   已存在待审核 draft 时同样 409（unique (pattern_id, revision) 兜底）。
4. ``source`` / ``provenance`` 不可编辑——版本化不得改变证据来源语义
   （验收：confirmed/synthetic/negative 语义保持不变）。

索引重算复用 ingest 的同一实现（structural_features / wl_fingerprint /
build_fingerprint / describe_subgraph / content_hash），保证库内向量与
查询侧落在同一空间。
"""
from __future__ import annotations

from datetime import UTC, datetime


class RevisionConflict(RuntimeError):
    """乐观锁冲突（expected_revision 过期或已有待审核 draft）。"""

    code = "REVISION_CONFLICT"


class PatternStateError(RuntimeError):
    """状态不允许该操作（如索引未完成就发布、非 draft 版本审核）。"""

    code = "PATTERN_STATE_INVALID"


# 可编辑内容字段：source/provenance 是证据来源语义，任何编辑都不得改写
EDITABLE_FIELDS = ("name", "description", "canonical_subgraph",
                   "evidence_grade")


def _revision_payload(rev, *, include_content: bool = False) -> dict:
    payload = {
        "id": rev.id,
        "revision": rev.revision,
        "status": rev.status,
        "origin": rev.origin,
        "index_status": rev.index_status,
        "index_error": rev.index_error,
        "index_metadata": rev.index_metadata or {},
        "name": rev.name,
        "source": rev.source,
        "provenance": rev.provenance,
        "evidence_grade": rev.evidence_grade,
        "content_hash": rev.content_hash,
        "embedding_model": rev.embedding_model,
        "change_note": rev.change_note,
        "created_by": rev.created_by,
        "created_at": rev.created_at.isoformat() if rev.created_at else None,
        "reviewed_by": rev.reviewed_by,
        "reviewed_at": rev.reviewed_at.isoformat() if rev.reviewed_at else None,
    }
    if include_content:
        payload["description"] = rev.description or ""
        payload["canonical_subgraph"] = rev.canonical_subgraph or {}
    return payload


def compute_index_fields(canon: dict, settings) -> dict:
    """按 ingest 同口径重算全部索引产物；graphormer 向量尽力而为。

    graphormer 在线前向依赖 optional torch/transformers：不可用时返回
    None 并把原因记进 index_metadata（该版本仍可被 hybrid 通道召回，
    只是缺 cos 通道），不把整个索引判为失败。
    """
    from ingest.common import content_hash, structural_features, wl_fingerprint
    from backend.retrieval.channels import build_fingerprint
    from backend.retrieval.embedding import build_provider
    from backend.retrieval.features import describe_subgraph

    nodes = list(canon.get("nodes") or [])
    edges = list(canon.get("edges") or [])
    provider = build_provider(settings)
    vector = provider.embed_batch([describe_subgraph(canon)])[0]

    fields = {
        "content_hash": content_hash(canon),
        "structural_features": structural_features(canon),
        "wl_fingerprint": wl_fingerprint(nodes, edges),
        "retrieval_fingerprint": build_fingerprint(nodes, edges),
        "semantic_embedding": vector,
        "embedding_model": settings.embedding_model,
        "embedding_dim": len(vector),
        "graphormer_embedding": None,
        "graphormer_model_id": None,
        "index_metadata": {"embedding_provider": settings.embedding_provider,
                           "graphormer_vector": "unavailable"},
    }
    try:
        from backend.retrieval.graphormer_online import query_vector_online

        gvec = query_vector_online(canon)
        if gvec is not None:
            fields["graphormer_embedding"] = [float(x) for x in gvec]
            fields["graphormer_model_id"] = settings.graphormer_model_name
            fields["index_metadata"]["graphormer_vector"] = "online"
    except Exception as exc:  # noqa: BLE001 — 缺 torch 等只降级，不失败
        fields["index_metadata"]["graphormer_vector"] = (
            f"unavailable: {type(exc).__name__}")
    return fields


def _load_pattern(session, pattern_id: str, *, for_update: bool = False):
    from backend.models.knowledge import Pattern

    stmt = session.query(Pattern).filter(Pattern.id == pattern_id)
    if for_update:
        stmt = stmt.with_for_update()
    return stmt.one_or_none()


def _ensure_baseline_revision(session, pattern) -> None:
    """首次变更前把当前生效版本落成历史基线（幂等）。

    ingest 建库路径不写 pattern_revisions（issue #75 要求保存的是「被替换
    版本」），因此基线行在第一次编辑/回滚时才补录——补录的是**变更前**的
    真实内容与索引元数据，历史链完整且不可篡改。迁移回填过的行已有基线，
    这里直接跳过。
    """
    from backend.models.knowledge import PatternRevision

    exists = session.query(PatternRevision.id).filter(
        PatternRevision.pattern_id == pattern.id).first()
    if exists:
        return
    session.add(PatternRevision(
        pattern_id=pattern.id, revision=pattern.revision, status=pattern.status,
        origin="baseline", index_status=pattern.index_status,
        name=pattern.name, source=pattern.source,
        provenance=pattern.provenance, evidence_grade=pattern.evidence_grade,
        description=pattern.description,
        canonical_subgraph=pattern.canonical_subgraph,
        content_hash=pattern.content_hash,
        structural_features=pattern.structural_features,
        semantic_embedding=pattern.semantic_embedding,
        graphormer_embedding=pattern.graphormer_embedding,
        graphormer_model_id=pattern.graphormer_model_id,
        retrieval_fingerprint=pattern.retrieval_fingerprint,
        wl_fingerprint=pattern.wl_fingerprint,
        embedding_model=pattern.embedding_model,
        embedding_dim=pattern.embedding_dim,
        index_metadata={"baseline": True}))
    session.flush()


def _assert_no_pending_draft(session, pattern_id: str) -> None:
    """同一模式下只允许一个待审核版本。

    版本号取 max+1（不是 current+1）——被驳回的版本号不会被复用，历史
    链因此严格单调；但允许在驳回后继续编辑。
    """
    from backend.models.knowledge import PatternRevision

    pending = (session.query(PatternRevision.revision)
               .filter(PatternRevision.pattern_id == pattern_id,
                       PatternRevision.status == "draft").first())
    if pending:
        raise RevisionConflict(
            f"pattern already has a pending revision {pending[0]}; "
            "review or reject it first")


def _next_revision(session, pattern_id: str) -> int:
    from sqlalchemy import func, select

    from backend.models.knowledge import PatternRevision

    current = session.execute(
        select(func.max(PatternRevision.revision))
        .where(PatternRevision.pattern_id == pattern_id)).scalar_one_or_none()
    return int(current or 0) + 1


def _draft_revision(session, pattern, *, patch: dict, origin: str,
                    actor_id: str | None, change_note: str | None):
    """按 patch 生成新 draft revision 行（内容变化后索引待重算）。"""
    from backend.models.knowledge import PatternRevision

    fields = {
        "name": pattern.name,
        "source": pattern.source,
        "provenance": pattern.provenance,
        "evidence_grade": pattern.evidence_grade,
        "description": pattern.description,
        "canonical_subgraph": pattern.canonical_subgraph,
    }
    unknown = set(patch) - set(EDITABLE_FIELDS)
    if unknown:
        raise PatternStateError(
            f"fields not editable: {sorted(unknown)} "
            f"(source/provenance are immutable evidence semantics)")
    for key, value in patch.items():
        if value is not None:
            fields[key] = value

    # content_hash 是内容的纯函数（不依赖 provider），必须在插入时就确定：
    # 它是版本的同一性标识，而索引产物（向量/指纹）要等异步任务。
    from ingest.common import content_hash as _content_hash

    revision_no = _next_revision(session, pattern.id)
    rev = PatternRevision(
        pattern_id=pattern.id, revision=revision_no, status="draft",
        origin=origin, index_status="pending", change_note=change_note,
        created_by=actor_id, content_hash=_content_hash(fields["canonical_subgraph"]),
        **fields)
    session.add(rev)
    session.flush()
    return rev


def request_edit(session, pattern_id: str, *, expected_revision: int,
                 patch: dict, actor_id: str | None = None,
                 change_note: str | None = None) -> dict:
    """创建待审核版本（不改变当前生效版本）。返回新 revision 的元数据。"""
    pattern = _load_pattern(session, pattern_id, for_update=True)
    if pattern is None:
        return {}
    if pattern.revision != expected_revision:
        raise RevisionConflict(
            f"expected_revision={expected_revision} but current is "
            f"{pattern.revision}; reload before editing")
    if any(key in patch for key in ("source", "provenance")):
        raise PatternStateError(
            "source/provenance are immutable evidence semantics")
    _ensure_baseline_revision(session, pattern)
    _assert_no_pending_draft(session, pattern_id)
    rev = _draft_revision(session, pattern, patch=patch, origin="edit",
                          actor_id=actor_id, change_note=change_note)
    pattern.last_editor_id = actor_id
    session.commit()
    return _revision_payload(rev, include_content=True)


def run_index(revision_id: int) -> str:
    """重算某 draft revision 的索引产物（worker / 降级路径调用）。

    独立 session：索引重算可能耗时数秒（embedding + graphormer 前向），
    不能占着调用方的连接。返回 indexed | failed。
    """
    from sqlalchemy.orm import Session

    from backend.core.config import get_settings
    from backend.models.knowledge import PatternRevision

    with Session(_engine()) as session:
        rev = session.get(PatternRevision, revision_id)
        if rev is None:
            # 索引任务指向不存在的 revision（被删/参数错）→ 明确留痕
            print(f"[pattern_index] revision {revision_id} not found; skipped")
            return "skipped"
        # 已终结（如审核期间被驳回）不再重算
        if rev.index_status == "indexed" or rev.status == "rejected":
            return rev.index_status
        try:
            fields = compute_index_fields(rev.canonical_subgraph or {},
                                          get_settings())
        except Exception as exc:  # noqa: BLE001 — 失败要留痕并交给死信治理
            rev.index_status = "failed"
            rev.index_error = f"{type(exc).__name__}: {exc}"[:2000]
            rev.index_metadata = {
                "failed_at": datetime.now(UTC).isoformat(),
                "error_type": type(exc).__name__,
            }
            session.commit()
            raise
        for key, value in fields.items():
            setattr(rev, key, value)
        rev.index_status = "indexed"
        rev.index_error = None
        session.commit()
        return "indexed"


def _engine():
    from sqlalchemy import create_engine

    from backend.core.config import get_settings

    url = get_settings().database_url.replace(
        "postgresql://", "postgresql+psycopg://")
    return create_engine(url, pool_pre_ping=True)


def approve_revision(session, pattern_id: str, *, revision: int,
                     actor_id: str | None = None) -> dict:
    """审核通过：把已索引的 draft COW 成当前生效版本。"""
    from backend.models.knowledge import PatternRevision

    pattern = _load_pattern(session, pattern_id, for_update=True)
    if pattern is None:
        return {}
    rev = (session.query(PatternRevision)
           .filter(PatternRevision.pattern_id == pattern_id,
                   PatternRevision.revision == revision).one_or_none())
    if rev is None:
        return {}
    if rev.status != "draft":
        raise PatternStateError(
            f"revision {revision} is {rev.status}, only draft can be approved")
    if rev.index_status != "indexed":
        raise PatternStateError(
            f"revision {revision} index_status={rev.index_status}; "
            "cannot publish before indexing completes")
    if revision != pattern.revision + 1:
        raise RevisionConflict(
            f"revision {revision} is stale (current {pattern.revision})")

    now = datetime.now(UTC)
    for field in ("name", "description", "canonical_subgraph", "content_hash",
                  "structural_features", "semantic_embedding",
                  "graphormer_embedding", "graphormer_model_id",
                  "retrieval_fingerprint", "wl_fingerprint",
                  "embedding_model", "embedding_dim"):
        setattr(pattern, field, getattr(rev, field))
    pattern.revision = rev.revision
    pattern.status = "active"
    pattern.index_status = "indexed"
    pattern.reviewed_by = actor_id
    pattern.reviewed_at = now
    rev.status = "active"
    rev.reviewed_by = actor_id
    rev.reviewed_at = now
    session.commit()
    return _revision_payload(rev)


def reject_revision(session, pattern_id: str, *, revision: int,
                    actor_id: str | None = None,
                    reason: str | None = None) -> dict:
    from backend.models.knowledge import PatternRevision

    pattern = _load_pattern(session, pattern_id, for_update=True)
    if pattern is None:
        return {}
    rev = (session.query(PatternRevision)
           .filter(PatternRevision.pattern_id == pattern_id,
                   PatternRevision.revision == revision).one_or_none())
    if rev is None:
        return {}
    if rev.status != "draft":
        raise PatternStateError(
            f"revision {revision} is {rev.status}, only draft can be rejected")
    rev.status = "rejected"
    rev.reviewed_by = actor_id
    rev.reviewed_at = datetime.now(UTC)
    if reason:
        rev.change_note = f"{rev.change_note or ''}\n[rejected] {reason}".strip()
    session.commit()
    return _revision_payload(rev)


def deprecate(session, pattern_id: str, *, expected_revision: int,
              actor_id: str | None = None) -> dict:
    """停用：当前版本退出召回（历史版本链不受影响）。"""
    pattern = _load_pattern(session, pattern_id, for_update=True)
    if pattern is None:
        return {}
    if pattern.revision != expected_revision:
        raise RevisionConflict(
            f"expected_revision={expected_revision} but current is "
            f"{pattern.revision}")
    pattern.status = "deprecated"
    pattern.last_editor_id = actor_id
    session.commit()
    return {"id": pattern.id, "revision": pattern.revision,
            "status": pattern.status, "index_status": pattern.index_status}


def rollback(session, pattern_id: str, *, to_revision: int,
             expected_revision: int, actor_id: str | None = None,
             change_note: str | None = None) -> dict:
    """回滚：把历史版本变成**新的当前版本**（立即生效，历史链保持完整）。

    索引元数据来自历史版本（当时已 indexed）：若 embedding 模型已变化则
    复用会落在失效空间，此时标记 pending 交给索引任务重算后再发布。
    """
    from backend.core.config import get_settings
    from backend.models.knowledge import PatternRevision

    pattern = _load_pattern(session, pattern_id, for_update=True)
    if pattern is None:
        return {}
    if pattern.revision != expected_revision:
        raise RevisionConflict(
            f"expected_revision={expected_revision} but current is "
            f"{pattern.revision}")
    source_rev = (session.query(PatternRevision)
                  .filter(PatternRevision.pattern_id == pattern_id,
                          PatternRevision.revision == to_revision)
                  .one_or_none())
    if source_rev is None:
        return {}

    _ensure_baseline_revision(session, pattern)
    _assert_no_pending_draft(session, pattern_id)
    settings = get_settings()
    reusable = (source_rev.index_status == "indexed"
                and source_rev.embedding_model == settings.embedding_model)
    new_rev = PatternRevision(
        pattern_id=pattern_id,
        revision=_next_revision(session, pattern_id),
        status="active" if reusable else "draft",
        origin="rollback",
        index_status="indexed" if reusable else "pending",
        change_note=change_note or f"rollback to revision {to_revision}",
        created_by=actor_id,
        name=source_rev.name, source=source_rev.source,
        provenance=source_rev.provenance,
        evidence_grade=source_rev.evidence_grade,
        description=source_rev.description,
        canonical_subgraph=source_rev.canonical_subgraph,
        content_hash=source_rev.content_hash,
        structural_features=source_rev.structural_features,
        semantic_embedding=source_rev.semantic_embedding,
        graphormer_embedding=source_rev.graphormer_embedding,
        graphormer_model_id=source_rev.graphormer_model_id,
        retrieval_fingerprint=source_rev.retrieval_fingerprint,
        wl_fingerprint=source_rev.wl_fingerprint,
        embedding_model=source_rev.embedding_model,
        embedding_dim=source_rev.embedding_dim,
        index_metadata={"rolled_back_from": to_revision},
    )
    session.add(new_rev)
    session.flush()

    if reusable:
        pattern.revision = new_rev.revision
        pattern.status = "active"
        pattern.index_status = "indexed"
        pattern.name = new_rev.name
        pattern.description = new_rev.description
        pattern.canonical_subgraph = new_rev.canonical_subgraph
        pattern.content_hash = new_rev.content_hash
        pattern.structural_features = new_rev.structural_features
        pattern.semantic_embedding = new_rev.semantic_embedding
        pattern.graphormer_embedding = new_rev.graphormer_embedding
        pattern.graphormer_model_id = new_rev.graphormer_model_id
        pattern.retrieval_fingerprint = new_rev.retrieval_fingerprint
        pattern.wl_fingerprint = new_rev.wl_fingerprint
        pattern.embedding_model = new_rev.embedding_model
        pattern.embedding_dim = new_rev.embedding_dim
        pattern.reviewed_by = actor_id
        pattern.reviewed_at = datetime.now(UTC)
    pattern.last_editor_id = actor_id
    session.commit()
    return _revision_payload(new_rev, include_content=True)


def list_revisions(session, pattern_id: str) -> list[dict]:
    from backend.models.knowledge import PatternRevision

    rows = (session.query(PatternRevision)
            .filter(PatternRevision.pattern_id == pattern_id)
            .order_by(PatternRevision.revision)).all()
    payloads = [_revision_payload(r) for r in rows]
    if payloads:
        return payloads
    pattern = _load_pattern(session, pattern_id)
    if pattern is None:
        return []
    # 基线尚未落库（从未被编辑过）→ 合成当前版本条目，避免前端看到空历史
    return [{
        "id": None, "revision": pattern.revision, "status": pattern.status,
        "origin": "current", "index_status": pattern.index_status,
        "index_error": None, "index_metadata": {},
        "name": pattern.name, "source": pattern.source,
        "provenance": pattern.provenance,
        "evidence_grade": pattern.evidence_grade,
        "content_hash": pattern.content_hash,
        "embedding_model": pattern.embedding_model,
        "change_note": None, "created_by": None,
        "created_at": (pattern.created_at.isoformat()
                       if pattern.created_at else None),
        "reviewed_by": pattern.reviewed_by,
        "reviewed_at": (pattern.reviewed_at.isoformat()
                        if pattern.reviewed_at else None),
    }]


def get_revision(session, pattern_id: str, revision: int) -> dict | None:
    from backend.models.knowledge import PatternRevision

    row = (session.query(PatternRevision)
           .filter(PatternRevision.pattern_id == pattern_id,
                   PatternRevision.revision == revision).one_or_none())
    return _revision_payload(row, include_content=True) if row else None


def pending_revision_id(session, pattern_id: str) -> int | None:
    """最近一条待索引的 draft revision（投递索引任务用）。"""
    from backend.models.knowledge import PatternRevision

    row = (session.query(PatternRevision)
           .filter(PatternRevision.pattern_id == pattern_id,
                   PatternRevision.index_status == "pending")
           .order_by(PatternRevision.revision.desc()).first())
    return row.id if row else None
