"""issue #22 持久化队列集成测试。

覆盖：
- analyze/report 经 task_queue 投递：enqueue 成功走队列（无进程内执行），
  失败降级为进程内任务且任务照常完成（conftest 已强制降级路径）。
- worker 崩溃恢复：recover_stuck_tasks 把 DB 里卡 queued 的分析、卡
  processing 的报告重新 enqueue；processing 的分析不重投（防双跑）。
- worker job 函数 run_analysis / run_report 的返回契约。
"""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from sqlalchemy.orm import Session

import backend.services.task_queue as tq
from backend.api.app import create_app, get_db_engine, reset_stores
from backend.core.config import reset_settings
from tests.unit.test_backend_api import _clear_judgments, _demo_seed

# 模块导入时 conftest 尚未打补丁，这里是真实实现；
# 走队列路径的用例需要显式还原（conftest autouse 强制的是降级路径）
_REAL_ENQUEUE = tq._enqueue


def _db_ready() -> bool:
    try:
        from sqlalchemy import text

        with get_db_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


requires_db = pytest.mark.skipif(
    not _db_ready(), reason="PostgreSQL 未运行")


@pytest.fixture()
def api_client(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret-for-ci-only")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.delenv("LLM_MOCK_SCENARIO", raising=False)
    reset_settings()
    reset_stores()
    client = TestClient(create_app())
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    yield client
    reset_settings()
    reset_stores()


def _report_model():
    from backend.models.base import Report

    return Report


class FakePool:
    """记录 enqueue 调用的假 arq 连接池。"""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def enqueue_job(self, job_name, *args, **kwargs):
        self.calls.append((job_name, args, kwargs))


def _use_fake_pool(monkeypatch) -> FakePool:
    """还原真实 _enqueue 并把进程级 pool 换成 fake，绕过真实建连。"""
    pool = FakePool()
    monkeypatch.setattr(tq, "_enqueue", _REAL_ENQUEUE)
    monkeypatch.setattr(tq, "_pool", pool)
    return pool


# ---------------------------------------------------------------------------
# 投递路径：enqueue 优先，降级兜底
# ---------------------------------------------------------------------------
@requires_db
class TestDispatchPaths:
    def test_analyze_enqueued_when_queue_available(self, api_client,
                                                   monkeypatch):
        """Redis 可达：分析进持久化队列，进程内不执行（保持 queued）。"""
        _clear_judgments()  # 清掉历史 queued/processing 残行，避免被幂等复用
        pool = _use_fake_pool(monkeypatch)
        r = api_client.post("/api/v1/addresses/analyze",
                            json={"address": _demo_seed()})
        assert r.status_code == 202, r.text
        jid = r.json()["judgment_id"]
        assert any(name == "run_analysis" and args == (jid,)
                   and kw.get("_job_id") == f"run_analysis:{jid}"
                   for name, args, kw in pool.calls)
        # 队列路径下无 worker 消费，DB 行应停留在 queued（API 不再抢跑）
        from backend.models.base import Judgment

        try:
            with Session(get_db_engine()) as session:
                assert session.get(Judgment, jid).status == "queued"
        finally:
            with Session(get_db_engine()) as session:
                session.execute(delete(Judgment).where(Judgment.id == jid))
                session.commit()

    def test_report_enqueued_when_queue_available(self, api_client,
                                                  monkeypatch):
        from backend.api.app import seed_user
        from tests.unit.test_phase5 import _create_case, _login

        seed_user("a@test.com", "PasswordA!123")
        pool = _use_fake_pool(monkeypatch)
        auth = _login(api_client, "a@test.com")
        cid = _create_case(api_client, auth)
        r = api_client.post(f"/api/v1/cases/{cid}/reports",
                            params={"format": "html"}, headers=auth)
        assert r.status_code == 202, r.text
        rid = r.json()["report_id"]
        assert any(name == "run_report" and args == (rid,)
                   for name, args, _kw in pool.calls)
        try:
            with Session(get_db_engine()) as session:
                assert session.get(_report_model(), rid).status == "processing"
        finally:
            with Session(get_db_engine()) as session:
                session.execute(delete(_report_model())
                                .where(_report_model().id == rid))
                session.commit()

    def test_analyze_fallback_completes_without_redis(self, api_client):
        """Redis 不可达（conftest 强制失败）：降级进程内执行仍能到终态。"""
        _clear_judgments()
        r = api_client.post("/api/v1/addresses/analyze",
                            json={"address": _demo_seed()})
        assert r.status_code == 202, r.text
        jid = r.json()["judgment_id"]
        deadline = time.time() + 20
        status = "queued"
        while time.time() < deadline:
            status = api_client.get(f"/api/v1/judgments/{jid}").json()["status"]
            if status in ("completed", "failed"):
                break
            time.sleep(0.2)
        assert status == "completed", status


# ---------------------------------------------------------------------------
# worker 崩溃恢复
# ---------------------------------------------------------------------------
@requires_db
class TestWorkerRecovery:
    def test_recover_reenqueues_queued_judgments_and_processing_reports(
            self, monkeypatch):
        from backend.api.app import seed_user
        from backend.models.base import Case, Judgment, Report
        from workers.worker import collect_stuck, recover_stuck_tasks

        seed_user("recover@test.com", "Passw0rd!123")
        _clear_judgments()  # 清残行：同参数 partial unique index 会让 insert 报错
        with Session(get_db_engine()) as session:
            owner_id = session.execute(
                text("SELECT id FROM users WHERE email='recover@test.com'")
            ).scalar_one()

        jid = str(uuid.uuid4())
        rid = str(uuid.uuid4())
        cid = str(uuid.uuid4())
        # 同参数幂等的 partial unique index（BE-46）：processing 行用不同
        # hops 区分，否则两条 active 记录违反约束
        processing_jid = str(uuid.uuid4())
        try:
            with Session(get_db_engine()) as session:
                session.add(Case(id=cid, owner_id=owner_id,
                                 title="recover-test"))
                session.add(Judgment(id=jid, address=_demo_seed(),
                                     status="queued"))
                session.add(Judgment(id=processing_jid,
                                     address=_demo_seed(), hops=4,
                                     status="processing"))
                session.add(Report(id=rid, case_id=cid, format="html",
                                   status="processing"))
                session.commit()
                queued_ids, report_ids = collect_stuck(session)
            assert jid in queued_ids
            assert rid in report_ids

            pool = FakePool()
            asyncio.run(recover_stuck_tasks({"redis": pool}))
            assert any(name == "run_analysis" and args == (jid,)
                       for name, args, _kw in pool.calls)
            assert any(name == "run_report" and args == (rid,)
                       for name, args, _kw in pool.calls)
            # processing 的分析不重投：可能另一 worker 正在执行，重放会双跑
            analysis_targets = [args[0] for name, args, _kw in pool.calls
                                if name == "run_analysis"]
            assert processing_jid not in analysis_targets
        finally:
            with Session(get_db_engine()) as session:
                session.execute(delete(Report).where(Report.id == rid))
                session.execute(delete(Judgment).where(
                    Judgment.id.in_([jid, processing_jid])))
                session.execute(delete(Case).where(Case.id == cid))
                session.commit()


# ---------------------------------------------------------------------------
# worker job 函数契约
# ---------------------------------------------------------------------------
class TestJobContracts:
    def test_run_report_job_contract(self, monkeypatch):
        from workers.worker import run_report

        monkeypatch.setattr(
            "backend.services.report_service.generate_report",
            lambda rid: "completed")
        out = asyncio.run(run_report({}, "rid-1"))
        assert out == {"report_id": "rid-1", "status": "completed"}

    def test_run_analysis_job_contract(self, monkeypatch):
        from workers.worker import run_analysis

        async def fake_run(judgment_id):
            return "completed"

        monkeypatch.setattr(
            "backend.services.orchestration.run_analysis", fake_run)
        out = asyncio.run(run_analysis({}, "jid-1"))
        assert out == {"judgment_id": "jid-1", "status": "completed"}
