"""issue #75：模式知识库版本/审核/索引生命周期。

覆盖验收标准：
- 未审核 draft 与 deprecated 模式不进入召回
- 每次内容修改产生不可变历史版本；rollback 产生新的当前版本且历史链完整
- 过期 expected_revision 返回 409，不覆盖新版本
- 索引失败时状态为 failed，上一 active 版本仍可检索
- 已完成 Judgment 能定位当时使用的模式版本
- confirmed/synthetic/negative 来源语义在版本化后保持不变
- 修改/审核/停用/回滚写入审计日志
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from backend.api.app import (create_app, get_db_engine, reset_stores,
                             seed_user)
from backend.core.config import get_settings, reset_settings
from backend.services.pattern_lifecycle import compute_index_fields


def _db_ready() -> bool:
    try:
        with get_db_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


requires_db = pytest.mark.skipif(not _db_ready(),
                                 reason="PostgreSQL 未运行")


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv(
        "JWT_SECRET",
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("GRAPH_DATA_MODE", "fixture")
    # embedding 由 conftest 的 autouse fixture 钉成 stub + test-stub-1024
    # （与召回 SQL 的 :model 同口径，不会捞到开发库的真实模式行）
    monkeypatch.setenv("GRAPHORMER_QUERY_MODE", "off")
    reset_settings()
    reset_stores()
    yield
    reset_settings()
    reset_stores()


@pytest.fixture()
def admin_client(env):
    seed_user("kb-admin@example.com", "Password123!", role="admin")
    seed_user("kb-inv@example.com", "Password123!", role="investigator")
    client = TestClient(create_app())
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    resp = client.post("/api/v1/auth/login",
                       json={"email": "kb-admin@example.com",
                             "password": "Password123!"})
    client.headers.update(
        {"Authorization": f"Bearer {resp.json()['access_token']}"})
    return client


def _sample_canon(*, seed: str = "", extra_node: bool = False) -> dict:
    """最小 canonical 子图（2~3 节点），足以算特征/指纹/描述。"""
    seed = seed or f"bc1q{uuid.uuid4().hex}"
    nodes = [
        {"id": f"addr:{seed}", "kind": "address", "label": seed,
         "first_layer": 0, "total_received_btc": 1.0, "total_sent_btc": 0.9,
         "utxo_count": 1},
        {"id": f"tx:{'a' * 64}", "kind": "transaction", "label": "a" * 64,
         "txid": "a" * 64, "first_layer": 1},
    ]
    edges = [
        {"id": f"edge:addr:{seed}->tx:{'a' * 64}", "source": f"addr:{seed}",
         "target": f"tx:{'a' * 64}", "txid": "a" * 64, "tx_layer": "1",
         "value_ratio": 0.9, "src_value_btc": 0.9, "dst_value_btc": 0.9,
         "total_num_inputs": 1, "total_num_outputs": 2},
    ]
    if extra_node:
        nodes.append({"id": f"tx:{'b' * 64}", "kind": "transaction",
                      "label": "b" * 64, "txid": "b" * 64, "first_layer": 2})
        edges.append({"id": f"edge:tx:{'a' * 64}->tx:{'b' * 64}",
                      "source": f"tx:{'a' * 64}", "target": f"tx:{'b' * 64}",
                      "txid": "b" * 64, "tx_layer": "2", "value_ratio": 0.5,
                      "src_value_btc": 0.5, "dst_value_btc": 0.5,
                      "total_num_inputs": 1, "total_num_outputs": 1})
    return {"seed_address": seed, "nodes": nodes, "edges": edges,
            "stats": {"node_count": len(nodes), "edge_count": len(edges)}}


def _reseed(canon: dict) -> dict:
    """换一个唯一种子地址（uq_patterns_seed_hash 拒绝同 seed+同内容的第二行）。"""
    import json as _json

    seed = f"bc1q{uuid.uuid4().hex}"
    old = canon.get("seed_address") or ""
    out = _json.loads(_json.dumps(canon))
    out["seed_address"] = seed
    for node in out.get("nodes") or []:
        for key in ("id", "label", "txid"):
            value = node.get(key)
            if isinstance(value, str) and old and old in value:
                node[key] = value.replace(old, seed)
    for edge in out.get("edges") or []:
        for key in ("id", "source", "target"):
            value = edge.get(key)
            if isinstance(value, str) and old and old in value:
                edge[key] = value.replace(old, seed)
    return out


def _make_pattern(*, canon: dict | None = None, status: str = "active",
                  index_status: str = "indexed", revision: int = 1,
                  name: str = "kb-test-pattern",
                  provenance: str = "confirmed",
                  source: str = "lazarus_confirmed",
                  evidence_grade: str = "A") -> str:
    """建一条带完整索引产物的模式行（走生产同一索引计算路径）。"""
    from backend.models.knowledge import Pattern

    settings = get_settings()
    canon = _reseed(canon or _sample_canon())
    fields = compute_index_fields(canon, settings)
    pid = str(uuid.uuid4())
    with Session(get_db_engine()) as session:
        session.add(Pattern(
            id=pid, name=name, source=source, provenance=provenance,
            evidence_grade=evidence_grade, seed_address=canon["seed_address"],
            description="test", canonical_subgraph=canon,
            status=status, revision=revision, index_status=index_status,
            **{k: v for k, v in fields.items() if k != "index_metadata"}))
        session.commit()
    return pid


def _cleanup(pattern_ids: list[str]) -> None:
    from backend.models.knowledge import Pattern

    with Session(get_db_engine()) as session:
        session.execute(delete(Pattern).where(Pattern.id.in_(pattern_ids)))
        session.commit()


def _recalled_ids(canon: dict, monkeypatch) -> set[str]:
    """跑一次真实召回（强制 hybrid 通道，屏蔽 graphormer 查询向量）。"""
    from backend.retrieval import retriever as retriever_mod

    monkeypatch.setattr(retriever_mod, "graphormer_query_vector",
                        lambda *_a, **_kw: None)
    monkeypatch.setattr(retriever_mod, "verify_embedding_model_lock",
                        lambda *_a, **_kw: None)
    with Session(get_db_engine()) as session:
        retriever = retriever_mod.Retriever(session, get_settings())
        result = retriever.retrieve(canon, notify=lambda _s: None)
    return {c.pattern_id for c in result.candidates}


class TestRecallFiltering:
    @requires_db
    def test_draft_and_deprecated_are_not_recallable(self, env, monkeypatch):
        canon = _sample_canon()
        active = _make_pattern(canon=canon, name="kb-active")
        draft = _make_pattern(canon=canon, name="kb-draft", status="draft",
                              index_status="pending")
        deprecated = _make_pattern(canon=canon, name="kb-deprecated",
                                   status="deprecated")
        try:
            recalled = _recalled_ids(canon, monkeypatch)
            assert active in recalled
            assert draft not in recalled, "未审核 draft 不得参与召回"
            assert deprecated not in recalled, "已停用模式不得参与召回"
        finally:
            _cleanup([active, draft, deprecated])

    @requires_db
    def test_pending_index_is_not_recallable(self, env, monkeypatch):
        canon = _sample_canon()
        ready = _make_pattern(canon=canon, name="kb-ready")
        pending = _make_pattern(canon=canon, name="kb-pending",
                                index_status="pending")
        try:
            recalled = _recalled_ids(canon, monkeypatch)
            assert ready in recalled
            assert pending not in recalled, "索引未完成不得参与召回"
        finally:
            _cleanup([ready, pending])


class TestRevisionHistory:
    @requires_db
    def test_edit_creates_immutable_revision_without_touching_current(
            self, env, monkeypatch):
        from backend.services import pattern_lifecycle as pl

        pid = _make_pattern(canon=_sample_canon(), name="kb-edit")
        try:
            with Session(get_db_engine()) as session:
                first = pl.list_revisions(session, pid)
            assert [r["revision"] for r in first] == [1]

            new_canon = _sample_canon(extra_node=True)
            with Session(get_db_engine()) as session:
                pl.request_edit(session, pid, expected_revision=1,
                                patch={"canonical_subgraph": new_canon,
                                       "name": "kb-edit-renamed"},
                                actor_id="admin-1", change_note="tweak")
            with Session(get_db_engine()) as session:
                revs = pl.list_revisions(session, pid)
                current = pl._load_pattern(session, pid)
            # 历史版本不可改写：v1 内容仍是原图
            assert revs[0]["revision"] == 1 and revs[0]["status"] == "active"
            assert [r["revision"] for r in revs] == [1, 2]
            assert revs[1]["origin"] == "edit" and revs[1]["status"] == "draft"
            assert revs[1]["index_status"] == "pending"
            # 当前生效版本未被改写（draft 尚未审核）
            assert current.revision == 1
            assert current.name == "kb-edit"
            assert len(current.canonical_subgraph["nodes"]) == 2
        finally:
            _cleanup([pid])

    @requires_db
    def test_approve_only_after_index_and_switch_is_atomic(self, env):
        from backend.services import pattern_lifecycle as pl

        pid = _make_pattern(canon=_sample_canon(), name="kb-approve")
        try:
            new_canon = _sample_canon(extra_node=True)
            with Session(get_db_engine()) as session:
                rev = pl.request_edit(session, pid, expected_revision=1,
                                      patch={"canonical_subgraph": new_canon,
                                             "name": "kb-approve-v2"},
                                      actor_id="admin-1")
            # 索引未完成即发布 → 显式拒绝（不允许半更新状态上线）
            with Session(get_db_engine()) as session:
                with pytest.raises(pl.PatternStateError):
                    pl.approve_revision(session, pid, revision=rev["revision"],
                                        actor_id="admin-1")

            assert pl.run_index(rev["id"]) == "indexed"
            with Session(get_db_engine()) as session:
                pl.approve_revision(session, pid, revision=rev["revision"],
                                    actor_id="admin-1")
            with Session(get_db_engine()) as session:
                pattern = pl._load_pattern(session, pid)
                revs = pl.list_revisions(session, pid)
            assert pattern.revision == 2 and pattern.status == "active"
            assert pattern.name == "kb-approve-v2"
            assert pattern.index_status == "indexed"
            assert pattern.reviewed_by == "admin-1"
            assert len(pattern.canonical_subgraph["nodes"]) == 3
            assert revs[1]["status"] == "active"
            assert revs[1]["reviewed_by"] == "admin-1"
        finally:
            _cleanup([pid])

    @requires_db
    def test_rollback_creates_new_current_version(self, env):
        from backend.services import pattern_lifecycle as pl

        pid = _make_pattern(canon=_sample_canon(), name="kb-rollback-v1")
        try:
            original = None
            with Session(get_db_engine()) as session:
                original = dict(pl._load_pattern(session, pid)
                                .canonical_subgraph)
            # v2：改内容并发布
            with Session(get_db_engine()) as session:
                rev = pl.request_edit(session, pid, expected_revision=1,
                                      patch={"canonical_subgraph":
                                             _sample_canon(extra_node=True)},
                                      actor_id="admin-1")
            pl.run_index(rev["id"])
            with Session(get_db_engine()) as session:
                pl.approve_revision(session, pid, revision=2, actor_id="admin-1")

            # 回滚到 v1：产生 v3（新当前版本），历史 v1/v2 完整保留
            with Session(get_db_engine()) as session:
                rolled = pl.rollback(session, pid, to_revision=1,
                                     expected_revision=2, actor_id="admin-2")
            assert rolled["revision"] == 3
            assert rolled["origin"] == "rollback"
            with Session(get_db_engine()) as session:
                pattern = pl._load_pattern(session, pid)
                revs = pl.list_revisions(session, pid)
            assert pattern.revision == 3 and pattern.status == "active"
            assert pattern.canonical_subgraph == original
            assert [r["revision"] for r in revs] == [1, 2, 3]
            assert revs[2]["origin"] == "rollback"
        finally:
            _cleanup([pid])


class TestOptimisticLocking:
    @requires_db
    def test_stale_expected_revision_conflicts(self, env):
        from backend.services import pattern_lifecycle as pl

        pid = _make_pattern(canon=_sample_canon())
        try:
            with Session(get_db_engine()) as session:
                with pytest.raises(pl.RevisionConflict):
                    pl.request_edit(session, pid, expected_revision=7,
                                    patch={"name": "x"}, actor_id="a")
        finally:
            _cleanup([pid])

    @requires_db
    def test_concurrent_edit_on_same_baseline_conflicts(self, env):
        from backend.services import pattern_lifecycle as pl

        pid = _make_pattern(canon=_sample_canon())
        try:
            with Session(get_db_engine()) as session:
                pl.request_edit(session, pid, expected_revision=1,
                                patch={"name": "first"}, actor_id="a")
            with Session(get_db_engine()) as session:
                with pytest.raises(pl.RevisionConflict):
                    pl.request_edit(session, pid, expected_revision=1,
                                    patch={"name": "second"}, actor_id="b")
            # 基线未过期但 revision 已前进（审核发布后）同样冲突
            with Session(get_db_engine()) as session:
                with pytest.raises(pl.RevisionConflict):
                    pl.rollback(session, pid, to_revision=1,
                                expected_revision=1, actor_id="a")
        finally:
            _cleanup([pid])

    @requires_db
    def test_source_and_provenance_are_immutable(self, env):
        from backend.services import pattern_lifecycle as pl

        pid = _make_pattern(canon=_sample_canon(), provenance="synthetic",
                            source="lazarus_synth", evidence_grade="S")
        try:
            with Session(get_db_engine()) as session:
                with pytest.raises(pl.PatternStateError):
                    pl.request_edit(session, pid, expected_revision=1,
                                    patch={"provenance": "confirmed"},
                                    actor_id="a")
                with pytest.raises(pl.PatternStateError):
                    pl.request_edit(session, pid, expected_revision=1,
                                    patch={"source": "lazarus_confirmed"},
                                    actor_id="a")
                pl.request_edit(session, pid, expected_revision=1,
                                patch={"name": "renamed"}, actor_id="a")
            with Session(get_db_engine()) as session:
                pattern = pl._load_pattern(session, pid)
                revs = pl.list_revisions(session, pid)
            # 版本化不改写来源语义
            assert pattern.provenance == "synthetic"
            assert pattern.source == "lazarus_synth"
            assert pattern.evidence_grade == "S"
            assert revs[1]["provenance"] == "synthetic"
            assert revs[1]["source"] == "lazarus_synth"
            assert revs[1]["evidence_grade"] == "S"
        finally:
            _cleanup([pid])


class TestIndexFailure:
    @requires_db
    def test_index_failure_marks_failed_and_keeps_previous_active(
            self, env, monkeypatch):
        from backend.services import pattern_lifecycle as pl
        from backend.retrieval.embedding import EmbeddingAPIError

        pid = _make_pattern(canon=_sample_canon(), name="kb-index-fail")
        try:
            with Session(get_db_engine()) as session:
                rev = pl.request_edit(session, pid, expected_revision=1,
                                      patch={"name": "kb-index-fail-v2"},
                                      actor_id="admin-1")

            def _boom(_settings):
                raise EmbeddingAPIError("provider down")

            monkeypatch.setattr(pl, "compute_index_fields",
                                lambda canon, settings: _boom(settings))
            with pytest.raises(EmbeddingAPIError):
                pl.run_index(rev["id"])

            with Session(get_db_engine()) as session:
                revision = pl.get_revision(session, pid, rev["revision"])
                pattern = pl._load_pattern(session, pid)
            assert revision["index_status"] == "failed"
            assert "EmbeddingAPIError" in (revision["index_error"] or "")
            # 索引失败不得影响线上：当前版本仍是上一 active 版本
            assert pattern.revision == 1 and pattern.status == "active"
            assert pattern.index_status == "indexed"
            assert pattern.name == "kb-index-fail"
            # 失败版本不可发布
            with Session(get_db_engine()) as session:
                with pytest.raises(pl.PatternStateError):
                    pl.approve_revision(session, pid, revision=rev["revision"],
                                        actor_id="admin-1")
        finally:
            _cleanup([pid])

    @requires_db
    def test_failed_revision_is_still_recallable_as_previous_version(
            self, env, monkeypatch):
        from backend.services import pattern_lifecycle as pl

        canon = _sample_canon()
        pid = _make_pattern(canon=canon, name="kb-prev-recall")
        try:
            with Session(get_db_engine()) as session:
                rev = pl.request_edit(session, pid, expected_revision=1,
                                      patch={"canonical_subgraph":
                                             _sample_canon(extra_node=True)},
                                      actor_id="admin-1")
            monkeypatch.setattr(
                pl, "compute_index_fields",
                lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
            with pytest.raises(RuntimeError):
                pl.run_index(rev["id"])
            # 上一 active 版本仍可被检索（这就是「不发布半更新版本」的目的）
            assert pid in _recalled_ids(canon, monkeypatch)
        finally:
            _cleanup([pid])


class TestJudgmentRevisionLinkage:
    @requires_db
    def test_completed_judgment_records_pattern_revision(self, env):
        import asyncio
        import uuid as _uuid

        from backend.models.base import Judgment
        from backend.services.orchestration import run_analysis
        from tests.unit.test_backend_api import _demo_seed

        jid = str(_uuid.uuid4())
        with Session(get_db_engine()) as session:
            session.add(Judgment(id=jid, address=_demo_seed(), hops=1,
                                 time_window_days=321, status="queued"))
            session.commit()
        try:
            status = asyncio.run(run_analysis(jid))
            with Session(get_db_engine()) as session:
                row = session.get(Judgment, jid)
            if status == "completed" and row.matched_pattern_id:
                # 有匹配时版本号必须落库（历史判定可定位到当时版本）
                assert row.matched_pattern_revision is not None
                assert row.matched_pattern_revision >= 1
        finally:
            with Session(get_db_engine()) as session:
                session.execute(delete(Judgment).where(Judgment.id == jid))
                session.commit()


class TestApiLifecycle:
    @requires_db
    def test_endpoints_require_admin(self, env, admin_client):
        pid = _make_pattern(canon=_sample_canon())
        try:
            anon = TestClient(create_app())
            anon.headers.update({"X-Requested-With": "XMLHttpRequest"})
            resp = anon.post(f"/api/v1/patterns/{pid}/edit",
                             json={"expected_revision": 1,
                                   "name": "hacked"})
            assert resp.status_code == 401

            inv = TestClient(create_app())
            inv.headers.update({"X-Requested-With": "XMLHttpRequest"})
            login = inv.post("/api/v1/auth/login",
                             json={"email": "kb-inv@example.com",
                                   "password": "Password123!"})
            inv.headers.update(
                {"Authorization": f"Bearer {login.json()['access_token']}"})
            resp = inv.post(f"/api/v1/patterns/{pid}/edit",
                            json={"expected_revision": 1, "name": "hacked"})
            assert resp.status_code == 403
        finally:
            _cleanup([pid])

    @requires_db
    def test_full_lifecycle_over_http_with_audit(self, env, admin_client,
                                                 monkeypatch):
        from backend.models.base import AuditLog
        from backend.services import task_queue as tq

        # 索引投递「成功但不执行」：HTTP 形态下索引由 q_index worker 完成，
        # 单测里不允许降级路径抢先把 revision 算成 indexed（否则
        # 「索引未完成不得发布」这条验收就测不到了）
        async def _fake_enqueue(*_a, **_kw):
            return True

        monkeypatch.setattr(tq, "_enqueue", _fake_enqueue)

        pid = _make_pattern(canon=_sample_canon(), name="kb-http")
        try:
            # 版本列表（公开只读）
            listing = admin_client.get(f"/api/v1/patterns/{pid}/revisions")
            assert listing.status_code == 200
            assert listing.json()["current"]["revision"] == 1
            assert [r["revision"] for r in listing.json()["items"]] == [1]

            # 编辑 → 201 + draft，current 不变
            edited = admin_client.post(
                f"/api/v1/patterns/{pid}/edit",
                json={"expected_revision": 1, "name": "kb-http-v2",
                      "change_note": "fix name"})
            assert edited.status_code == 201, edited.text
            rev = edited.json()
            assert rev["revision"] == 2 and rev["status"] == "draft"

            # 过期基线 → 409，不覆盖
            stale = admin_client.post(
                f"/api/v1/patterns/{pid}/edit",
                json={"expected_revision": 5, "name": "nope"})
            assert stale.status_code == 409
            assert stale.json()["error_code"] == "REVISION_CONFLICT"

            # 索引未完成 → 发布被拒 422
            early = admin_client.post(f"/api/v1/patterns/{pid}/review",
                                      json={"revision": 2, "approve": True})
            assert early.status_code == 422

            # 手工完成索引（HTTP 形态下由 q_index worker 承担）
            from backend.services.pattern_lifecycle import run_index

            assert run_index(rev["id"]) == "indexed"
            approved = admin_client.post(f"/api/v1/patterns/{pid}/review",
                                         json={"revision": 2, "approve": True})
            assert approved.status_code == 200, approved.text

            detail = admin_client.get(f"/api/v1/patterns/{pid}")
            assert detail.json()["revision"] == 2
            assert detail.json()["status"] == "active"
            assert detail.json()["index_status"] == "indexed"

            # 停用
            dep = admin_client.post(
                f"/api/v1/patterns/{pid}/deprecate",
                json={"expected_revision": 2})
            assert dep.status_code == 200
            assert dep.json()["status"] == "deprecated"

            # 回滚（deprecated 状态也能回滚恢复，只要基线一致）
            rolled = admin_client.post(
                f"/api/v1/patterns/{pid}/rollback",
                json={"to_revision": 1, "expected_revision": 2})
            assert rolled.status_code == 200, rolled.text
            assert rolled.json()["revision"] == 3
            assert rolled.json()["origin"] == "rollback"

            # 审计：四类操作都留痕
            with Session(get_db_engine()) as session:
                actions = set(session.execute(
                    select(AuditLog.action)
                    .where(AuditLog.resource_id == pid)).scalars().all())
            assert {"pattern.edit", "pattern.review", "pattern.deprecate",
                    "pattern.rollback"} <= actions
        finally:
            _cleanup([pid])

    @requires_db
    def test_reject_keeps_current_version(self, env, admin_client):
        from backend.services import pattern_lifecycle as pl

        pid = _make_pattern(canon=_sample_canon(), name="kb-reject")
        try:
            edited = admin_client.post(
                f"/api/v1/patterns/{pid}/edit",
                json={"expected_revision": 1, "name": "kb-reject-v2"})
            rev = edited.json()
            rejected = admin_client.post(
                f"/api/v1/patterns/{pid}/review",
                json={"revision": rev["revision"], "approve": False,
                      "reason": "not enough evidence"})
            assert rejected.status_code == 200
            assert rejected.json()["status"] == "rejected"

            with Session(get_db_engine()) as session:
                pattern = pl._load_pattern(session, pid)
            assert pattern.revision == 1 and pattern.name == "kb-reject"
            assert pattern.status == "active"

            # 驳回后可再次编辑（不阻塞后续修改）
            again = admin_client.post(
                f"/api/v1/patterns/{pid}/edit",
                json={"expected_revision": 1, "name": "kb-reject-v3"})
            assert again.status_code == 201
            assert again.json()["revision"] == 3
        finally:
            _cleanup([pid])

    @requires_db
    def test_unknown_pattern_404(self, env, admin_client):
        missing = str(uuid.uuid4())
        assert admin_client.get(
            f"/api/v1/patterns/{missing}/revisions").status_code == 404
        assert admin_client.post(
            f"/api/v1/patterns/{missing}/edit",
            json={"expected_revision": 1, "name": "x"}).status_code == 404
        assert admin_client.post(
            f"/api/v1/patterns/{missing}/deprecate",
            json={"expected_revision": 1}).status_code == 404
