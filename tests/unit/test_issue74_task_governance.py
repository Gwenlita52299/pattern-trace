"""issue #74：队列拆分、死信、重试、取消与运行状态治理。

覆盖验收标准：
- 分析与报告进入不同队列，可独立设置并发（worker settings）
- 报告队列堆积不阻塞分析（物理进程隔离，配置层面可验证）
- 同一业务 ID 重复投递最多一个执行方成功 claim
- 瞬时错误按配置重试，永久错误直接终态
- 重试耗尽生成死信并同步业务失败状态
- 管理员查看队列状态；死信重跑仅 admin 可用
- 取消语义：排队/执行中可取消，终态不可取消
- QUEUE_REQUIRED 下 Redis 故障拒绝投递（不静默降级）
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from backend.api.app import (create_app, get_db_engine, reset_stores,
                             seed_user)
from backend.core.config import reset_settings
from backend.services import task_queue as tq

_REAL_ENQUEUE = tq._enqueue


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
def api_client(monkeypatch):
    monkeypatch.setenv(
        "JWT_SECRET",
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("GRAPH_DATA_MODE", "fixture")
    monkeypatch.delenv("LLM_MOCK_SCENARIO", raising=False)
    # 重试退避在单测里置零，避免等待
    monkeypatch.setenv("TASK_RETRY_BASE_DELAY", "0")
    monkeypatch.setenv("TASK_RETRY_MAX_DELAY", "0")
    reset_settings()
    reset_stores()
    client = TestClient(create_app())
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    yield client
    reset_settings()
    reset_stores()


def _demo_seed() -> str:
    from tests.unit.test_backend_api import _demo_seed as _seed

    return _seed()


class FakePool:
    """记录 enqueue 调用的假 arq 池。"""

    def __init__(self, queued=None, raise_on_enqueue=None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._queued = queued or {}
        self._raise = raise_on_enqueue

    async def enqueue_job(self, job_name, *args, **kwargs):
        if self._raise is not None:
            raise self._raise
        self.calls.append((job_name, args, kwargs))

    async def queued_jobs(self, queue_name=None):
        return list(self._queued.get(queue_name, []))


def _queued_job(try_: int = 1, age_seconds: float = 0.0):
    return SimpleNamespace(
        job_try=try_,
        enqueue_time=datetime.now(UTC) - timedelta(seconds=age_seconds))


def _use_fake_pool(monkeypatch, **kw) -> FakePool:
    pool = FakePool(**kw)
    monkeypatch.setattr(tq, "_enqueue", _REAL_ENQUEUE)
    monkeypatch.setattr(tq, "_pool", pool)
    monkeypatch.setattr(tq, "_fail_until", 0.0)
    return pool


def _clear_judgments() -> None:
    from tests.unit.test_backend_api import _clear_judgments as _clear

    _clear()
    from backend.models.base import TaskDeadLetter

    with Session(get_db_engine()) as session:
        session.execute(delete(TaskDeadLetter))
        session.commit()


def _cleanup_judgment(jid: str) -> None:
    from backend.models.base import Judgment, TaskDeadLetter

    with Session(get_db_engine()) as session:
        session.execute(delete(Judgment).where(Judgment.id == jid))
        session.execute(delete(TaskDeadLetter).where(
            TaskDeadLetter.business_id == jid))
        session.commit()


def _enqueue_row(*, address: str | None = None, hops: int = 3,
                 status: str = "queued"):
    """直连 DB 建排队行。

    issue #74：address 默认用唯一伪地址——uq_judgments_active_per_params
    （address, hops, time_window_days）会让同一参数的 active 行互相冲突，
    而这些用例只关心状态机，不需要真实链上地址（API 层才校验格式）。
    """
    from backend.models.base import Judgment

    jid = str(uuid.uuid4())
    addr = address or f"bc1q{uuid.uuid4().hex}"
    with Session(get_db_engine()) as session:
        session.add(Judgment(id=jid, address=addr, hops=hops,
                             time_window_days=90, status=status))
        session.commit()
    return jid


# ---------------------------------------------------------------------------
# 1. 队列隔离与独立并发
# ---------------------------------------------------------------------------
class TestQueueIsolation:
    def test_analysis_and_report_use_distinct_queues(self):
        assert tq.analysis_queue_name() != tq.report_queue_name()
        assert tq.analysis_queue_name() == "q_analysis"
        assert tq.report_queue_name() == "q_report"

    def test_legacy_queue_env_still_honoured(self, monkeypatch):
        """拆分前部署用 ARQ_QUEUE_GRAPH；保留回退避免升级后任务投错队列。"""
        monkeypatch.delenv("ARQ_QUEUE_ANALYSIS", raising=False)
        monkeypatch.setenv("ARQ_QUEUE_GRAPH", "q_legacy")
        assert tq.analysis_queue_name() == "q_legacy"

    def test_worker_settings_are_independent(self):
        from workers.worker import (AnalysisWorkerSettings,
                                    ReportWorkerSettings)

        assert AnalysisWorkerSettings.queue_name != ReportWorkerSettings.queue_name
        assert AnalysisWorkerSettings.queue_name == "q_analysis"
        assert ReportWorkerSettings.queue_name == "q_report"
        assert [f.__name__ for f in AnalysisWorkerSettings.functions] \
            == ["run_analysis"]
        assert [f.__name__ for f in ReportWorkerSettings.functions] \
            == ["run_report"]
        # 分析并发可配且与报告隔离；报告默认单并发（CPU 密集渲染）
        assert AnalysisWorkerSettings.max_jobs >= 1
        assert ReportWorkerSettings.max_jobs == 1

    @requires_db
    def test_dispatch_routes_to_the_right_queue(self, api_client,
                                                monkeypatch):
        _clear_judgments()
        _use_fake_pool(monkeypatch)
        r = api_client.post("/api/v1/addresses/analyze",
                            json={"address": _demo_seed()})
        assert r.status_code == 202, r.text
        jid = r.json()["judgment_id"]
        try:
            assert tq._pool.calls[0][2]["_queue_name"] == "q_analysis"
        finally:
            _cleanup_judgment(jid)


# ---------------------------------------------------------------------------
# 2. 幂等 claim
# ---------------------------------------------------------------------------
@requires_db
class TestIdempotentClaim:
    def test_concurrent_execution_claims_once(self, api_client):
        """同一业务 ID 重复投递：只有一个执行方 claim 成功（DB 乐观守卫）。"""
        from backend.models.base import Judgment
        from backend.services.orchestration import run_analysis

        jid = _enqueue_row()
        try:
            first, second = asyncio.run(_gather_two(run_analysis, jid))
            assert sorted([first, second]) == ["completed", "skipped"]
            with Session(get_db_engine()) as session:
                row = session.get(Judgment, jid)
            assert row.status == "completed"
        finally:
            _cleanup_judgment(jid)

    def test_terminal_task_is_not_replayed(self, api_client):
        from backend.services.orchestration import run_analysis

        jid = _enqueue_row()
        try:
            assert asyncio.run(run_analysis(jid)) == "completed"
            assert asyncio.run(run_analysis(jid)) == "skipped"
        finally:
            _cleanup_judgment(jid)


async def _gather_two(fn, jid):
    return await asyncio.gather(fn(jid), fn(jid))


# ---------------------------------------------------------------------------
# 3. 瞬时/永久错误与死信
# ---------------------------------------------------------------------------
@requires_db
class TestRetryAndDeadLetter:
    def test_transient_failure_releases_to_queued(self, api_client,
                                                  monkeypatch):
        import os

        from backend.models.base import Judgment
        from backend.services.orchestration import run_analysis

        monkeypatch.setenv("LLM_MOCK_SCENARIO", "timeout")
        reset_settings()
        monkeypatch.setenv("TASK_MAX_TRIES", "3")
        reset_settings()
        os.environ.setdefault("LLM_MOCK_SCENARIO", "timeout")
        jid = _enqueue_row()
        try:
            assert asyncio.run(run_analysis(jid)) == "retrying"
            with Session(get_db_engine()) as session:
                row = session.get(Judgment, jid)
            assert row.status == "queued"
            assert row.error_code == "LLM_PROVIDER_TIMEOUT"
        finally:
            os.environ.pop("LLM_MOCK_SCENARIO", None)
            reset_settings()
            _cleanup_judgment(jid)

    def test_permanent_validation_failure_is_terminal(self, api_client):
        """校验失败是永久错误：直接终态，不重试。"""
        from backend.models.base import Judgment
        from backend.services.orchestration import run_analysis

        jid = _enqueue_row()
        try:
            with Session(get_db_engine()) as session:
                session.execute(text(
                    "UPDATE judgments SET mock_scenario='invalid_evidence_all_retries' "
                    "WHERE id=:i"), {"i": jid})
                session.commit()
            assert asyncio.run(run_analysis(jid)) == "failed"
            with Session(get_db_engine()) as session:
                row = session.get(Judgment, jid)
            assert row.status == "failed"
            assert row.error_code == "LLM_VALIDATION_FAILED"
        finally:
            _cleanup_judgment(jid)

    def test_exhausted_retries_archive_dead_letter(self, api_client,
                                                    monkeypatch):
        """重试耗尽 → 死信行 + 业务失败状态（worker 壳层职责）。"""
        import os

        from backend.models.base import Judgment, TaskDeadLetter
        from workers.worker import run_analysis as worker_run

        os.environ["LLM_MOCK_SCENARIO"] = "timeout"
        reset_settings()
        jid = _enqueue_row()
        try:
            out = asyncio.run(worker_run({"job_try": 3}, jid))
            assert out["status"] == "failed"
            assert out["error_code"] == "LLM_PROVIDER_TIMEOUT"
            with Session(get_db_engine()) as session:
                row = session.get(Judgment, jid)
                assert row.status == "failed"
                dead = session.execute(select(TaskDeadLetter).where(
                    TaskDeadLetter.business_id == jid)).scalars().first()
            assert dead is not None
            assert dead.task_type == "analysis"
            assert dead.attempts == 3
            assert dead.error_code == "LLM_PROVIDER_TIMEOUT"
            assert dead.queue == "q_analysis"
        finally:
            os.environ.pop("LLM_MOCK_SCENARIO", None)
            reset_settings()
            _cleanup_judgment(jid)

    def test_transient_failure_retries_instead_of_dead_letter(
            self, api_client, monkeypatch):
        """未耗尽时抛 arq Retry（留在队列），不写死信。"""
        import os

        from arq import Retry

        from backend.models.base import TaskDeadLetter
        from workers.worker import run_analysis as worker_run

        os.environ["LLM_MOCK_SCENARIO"] = "timeout"
        reset_settings()
        jid = _enqueue_row()
        try:
            with pytest.raises(Retry):
                asyncio.run(worker_run({"job_try": 1}, jid))
            with Session(get_db_engine()) as session:
                dead = session.execute(select(TaskDeadLetter).where(
                    TaskDeadLetter.business_id == jid)).scalars().first()
            assert dead is None
        finally:
            os.environ.pop("LLM_MOCK_SCENARIO", None)
            reset_settings()
            _cleanup_judgment(jid)

    def test_transient_error_classification(self):
        from backend.services.task_governance import is_transient

        assert is_transient("LLM_PROVIDER_TIMEOUT")
        assert is_transient("LLM_PROVIDER_RATE_LIMITED")
        assert is_transient("ESPLORA_UNAVAILABLE")
        assert not is_transient("LLM_VALIDATION_FAILED")
        assert not is_transient("LLM_PROVIDER_AUTH_FAILED")
        assert not is_transient("RENDER_FAILED")
        assert not is_transient(None)

    def test_retry_delay_is_exponential_and_capped(self, monkeypatch):
        from workers.worker import _retry_delay

        monkeypatch.setenv("TASK_RETRY_BASE_DELAY", "5")
        monkeypatch.setenv("TASK_RETRY_MAX_DELAY", "12")
        reset_settings()
        try:
            assert [_retry_delay(a) for a in (1, 2, 3, 4, 5)] \
                == [5.0, 10.0, 12.0, 12.0, 12.0]
        finally:
            reset_settings()


# ---------------------------------------------------------------------------
# 4. 取消
# ---------------------------------------------------------------------------
@requires_db
class TestCancellation:
    def test_queued_task_can_be_cancelled_and_is_not_executed(
            self, api_client):
        from backend.models.base import Judgment
        from backend.services.orchestration import run_analysis

        jid = _enqueue_row()
        try:
            assert asyncio.run(run_analysis(jid)) == "completed"
        finally:
            _cleanup_judgment(jid)

        jid = _enqueue_row(hops=3)
        try:
            from backend.services.task_governance import cancel_task

            with Session(get_db_engine()) as session:
                assert cancel_task(session, task_type="analysis",
                                   business_id=jid,
                                   actor="admin@test.com") == "cancelled"
            with Session(get_db_engine()) as session:
                assert session.get(Judgment, jid).status == "cancelled"
            # 取消是终态：worker 拿到消息后跳过，不执行管线
            assert asyncio.run(run_analysis(jid)) == "skipped"
            with Session(get_db_engine()) as session:
                assert session.get(Judgment, jid).status == "cancelled"
        finally:
            _cleanup_judgment(jid)

    def test_cancel_records_event(self, api_client):
        from backend.models.base import JudgmentEvent
        from backend.services.task_governance import cancel_task

        jid = _enqueue_row(hops=3)
        try:
            with Session(get_db_engine()) as session:
                cancel_task(session, task_type="analysis", business_id=jid,
                            actor="admin@test.com")
                events = session.execute(
                    select(JudgmentEvent.to_status).where(
                        JudgmentEvent.judgment_id == jid)).scalars().all()
            assert "cancelled" in events
        finally:
            _cleanup_judgment(jid)

    def test_terminal_task_cannot_be_cancelled(self, api_client):
        from backend.services.task_governance import cancel_task

        jid = _enqueue_row(hops=9, status="completed")
        try:
            with Session(get_db_engine()) as session:
                assert cancel_task(session, task_type="analysis",
                                   business_id=jid,
                                   actor="admin@test.com") == "not_cancellable"
        finally:
            _cleanup_judgment(jid)

    def test_cancel_endpoint_requires_admin(self, api_client):
        from tests.unit.test_phase5 import _login

        seed_user("a@test.com", "PasswordA!123")
        auth = _login(api_client, "a@test.com")
        r = api_client.post("/api/v1/admin/judgments/none/cancel",
                            headers=auth)
        assert r.status_code == 403

    def test_cancel_endpoint_admin_paths(self, api_client):
        from tests.unit.test_phase5 import _login

        seed_user("admin@test.com", "AdminP@ss1", role="admin")
        auth = _login(api_client, "admin@test.com")
        r = api_client.post("/api/v1/admin/judgments/none/cancel",
                            headers=auth)
        assert r.status_code == 404
        jid = _enqueue_row(hops=3)
        try:
            r = api_client.post(f"/api/v1/admin/judgments/{jid}/cancel",
                                headers=auth)
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "cancelled"
            # 重复取消 → 已终态
            r = api_client.post(f"/api/v1/admin/judgments/{jid}/cancel",
                                headers=auth)
            assert r.status_code == 409
        finally:
            _cleanup_judgment(jid)


# ---------------------------------------------------------------------------
# 5. 队列状态 + 死信治理
# ---------------------------------------------------------------------------
@requires_db
class TestQueueGovernance:
    def test_queue_status_reports_depth_and_oldest_wait(self, api_client,
                                                        monkeypatch):
        from tests.unit.test_phase5 import _login

        pool = _use_fake_pool(monkeypatch, queued={
            "q_analysis": [_queued_job(1, 30.0), _queued_job(2, 5.0)],
            "q_report": [_queued_job(1, 1.0)],
        })
        seed_user("admin@test.com", "AdminP@ss1", role="admin")
        auth = _login(api_client, "admin@test.com")
        r = api_client.get("/api/v1/admin/queues", headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["redis_available"] is True
        analysis = body["queues"]["analysis"]
        assert analysis["pending"] == 2
        assert analysis["retry"] == 1          # job_try=2 的算重试中
        assert analysis["oldest_waiting_seconds"] >= 29
        assert body["queues"]["report"]["pending"] == 1
        assert "active" in analysis
        assert body["dead_letters"]["unresolved"] >= 0
        assert pool is not None

    def test_queue_status_degrades_when_redis_down(self, api_client,
                                                   monkeypatch):
        """观测接口不因被观测对象故障而报错。"""
        from tests.unit.test_phase5 import _login

        _use_fake_pool(monkeypatch,
                       raise_on_enqueue=None)
        tq._pool._queued = {}  # noqa: SLF001

        async def _boom(queue_name=None):
            raise ConnectionError("redis down")

        monkeypatch.setattr(tq._pool, "queued_jobs", _boom)
        seed_user("admin@test.com", "AdminP@ss1", role="admin")
        auth = _login(api_client, "admin@test.com")
        r = api_client.get("/api/v1/admin/queues", headers=auth)
        assert r.status_code == 200
        assert r.json()["redis_available"] is False

    def test_queue_status_requires_admin(self, api_client):
        from tests.unit.test_phase5 import _login

        seed_user("a@test.com", "PasswordA!123")
        auth = _login(api_client, "a@test.com")
        assert api_client.get("/api/v1/admin/queues",
                              headers=auth).status_code == 403
        assert api_client.get("/api/v1/admin/dead-letters",
                              headers=auth).status_code == 403

    def test_dead_letter_list_and_requeue(self, api_client, monkeypatch):
        from backend.models.base import Judgment, TaskDeadLetter
        from backend.services.task_governance import record_dead_letter
        from tests.unit.test_phase5 import _login

        pool = _use_fake_pool(monkeypatch)
        seed_user("admin@test.com", "AdminP@ss1", role="admin")
        auth = _login(api_client, "admin@test.com")

        jid = _enqueue_row(hops=3)
        with Session(get_db_engine()) as session:
            session.execute(
                text("UPDATE judgments SET status='failed', "
                     "error_code='LLM_PROVIDER_TIMEOUT' WHERE id=:i"),
                {"i": jid})
            session.commit()
            record_dead_letter(session, task_type="analysis",
                               business_id=jid, queue="q_analysis",
                               attempts=3,
                               error_code="LLM_PROVIDER_TIMEOUT",
                               message="boom")
            dead_id = session.execute(select(TaskDeadLetter.id).where(
                TaskDeadLetter.business_id == jid)).scalar_one()
        try:
            r = api_client.get("/api/v1/admin/dead-letters", headers=auth)
            assert r.status_code == 200
            items = r.json()["items"]
            assert any(i["id"] == dead_id for i in items)
            entry = next(i for i in items if i["id"] == dead_id)
            assert entry["attempts"] == 3
            assert entry["queue"] == "q_analysis"

            r = api_client.post(
                f"/api/v1/admin/dead-letters/{dead_id}/requeue", headers=auth)
            assert r.status_code == 200, r.text
            assert r.json()["attempt"] == 4
            with Session(get_db_engine()) as session:
                assert session.get(Judgment, jid).status == "queued"
                assert session.get(TaskDeadLetter, dead_id).requeued_by \
                    == "admin@test.com"
            # 新 attempt 的 job_id 带后缀（绕开 arq 对旧 job key 的去重）
            assert any(name == "run_analysis"
                       and kw.get("_job_id") == f"run_analysis:{jid}:4"
                       for name, _args, kw in pool.calls)
            # 业务对象已不在 failed → 再次重跑被拒绝
            r = api_client.post(
                f"/api/v1/admin/dead-letters/{dead_id}/requeue", headers=auth)
            assert r.status_code == 409
        finally:
            _cleanup_judgment(jid)

    def test_requeue_unknown_dead_letter_404(self, api_client):
        from tests.unit.test_phase5 import _login

        seed_user("admin@test.com", "AdminP@ss1", role="admin")
        auth = _login(api_client, "admin@test.com")
        r = api_client.post(
            "/api/v1/admin/dead-letters/999999999/requeue", headers=auth)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# 6. QUEUE_REQUIRED：Redis 故障不得静默降级
# ---------------------------------------------------------------------------
@requires_db
class TestQueueRequired:
    def test_redis_down_rejects_dispatch_when_required(self, api_client,
                                                       monkeypatch):
        monkeypatch.setenv("QUEUE_REQUIRED", "true")
        reset_settings()
        _use_fake_pool(monkeypatch, raise_on_enqueue=ConnectionError("down"))
        try:
            r = api_client.post("/api/v1/addresses/analyze",
                                json={"address": _demo_seed()})
            assert r.status_code == 503, r.text
            assert "QUEUE_UNAVAILABLE" in r.text
        finally:
            reset_settings()

    def test_redis_down_falls_back_when_not_required(self, api_client,
                                                     monkeypatch):
        """开发默认：仍允许进程内降级（单实例无 Redis 的合法部署形态）。"""
        monkeypatch.setenv("QUEUE_REQUIRED", "false")
        reset_settings()
        _clear_judgments()
        _use_fake_pool(monkeypatch, raise_on_enqueue=ConnectionError("down"))
        try:
            r = api_client.post("/api/v1/addresses/analyze",
                                json={"address": _demo_seed()})
            assert r.status_code == 202, r.text
            jid = r.json()["judgment_id"]
            import time

            deadline = time.time() + 30
            status = "queued"
            while time.time() < deadline:
                status = api_client.get(
                    f"/api/v1/judgments/{jid}").json()["status"]
                if status in ("completed", "failed"):
                    break
                time.sleep(0.2)
            assert status == "completed", status
        finally:
            reset_settings()

    def test_inprocess_retry_reaches_terminal_state(self, api_client,
                                                    monkeypatch):
        """降级路径没有 worker 重排期，必须由进程内循环完成重试直到终态
        （否则任务停在 queued，等僵尸回收 120s 才收场）。"""
        import os

        from backend.models.base import Judgment

        monkeypatch.setenv("QUEUE_REQUIRED", "false")
        monkeypatch.setenv("TASK_MAX_TRIES", "2")
        os.environ["LLM_MOCK_SCENARIO"] = "timeout"
        reset_settings()
        _use_fake_pool(monkeypatch, raise_on_enqueue=ConnectionError("down"))
        try:
            jid = _enqueue_row(hops=3)
            asyncio.run(tq._run_inprocess_analysis(jid))
            with Session(get_db_engine()) as session:
                row = session.get(Judgment, jid)
            assert row.status == "failed"
            assert row.error_code == "LLM_PROVIDER_TIMEOUT"
        finally:
            os.environ.pop("LLM_MOCK_SCENARIO", None)
            reset_settings()
            _cleanup_judgment(jid)
