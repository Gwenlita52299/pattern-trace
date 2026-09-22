"""issue #73：持久化分析阶段 trace + 端到端可观测性。

覆盖验收标准：
- completed/failed 分析均生成根 trace 与阶段 span
- 每个 span 含 started_at/finished_at/duration_ms/status/attempt
- 失败 span 含稳定错误码，且不泄露凭据/载荷/prompt/思维链
- HTTP 请求创建的 trace ID 传递至异步 worker（judgment.trace_id → spans）
- Redis 不可用时最终 trace 仍可从数据库读取
- trace 端点权限与 GET judgments 一致（匿名只读），不存在 id 404
"""
from __future__ import annotations

import asyncio
import random
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from backend.api.app import create_app, get_db_engine, reset_stores
from backend.core.config import reset_settings
from backend.services import task_queue as tq
from backend.services.orchestration import run_analysis
from backend.services.tracing import (NullRecorder, SpanRecorder,
                                      reset_engine, sanitize_metadata)


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
    # stub embedding：trace 用例只关心 span 形态，不该走真网络
    monkeypatch.setenv("EMBEDDING_PROVIDER", "stub")
    monkeypatch.delenv("LLM_MOCK_SCENARIO", raising=False)
    monkeypatch.setenv("TASK_MAX_TRIES", "1")
    monkeypatch.setenv("TASK_RETRY_BASE_DELAY", "0")
    monkeypatch.setenv("TASK_RETRY_MAX_DELAY", "0")
    reset_settings()
    reset_engine()
    reset_stores()
    yield
    reset_settings()
    reset_engine()
    reset_stores()


@pytest.fixture()
def api_client(env):
    client = TestClient(create_app())
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    return client


def _new_judgment(*, status: str = "queued", trace_id: str | None = None,
                  hops: int = 1) -> str:
    """建一条 judgment。

    issue #73 的 trace 用例需要真实召回（否则空召回会跳过 wl_rerank 阶段），
    所以用 demo seed 地址；time_window_days 取唯一值避免与残留 active 行
    撞 uq_judgments_active_per_params。
    """
    from backend.models.base import Judgment
    from tests.unit.test_backend_api import _demo_seed

    jid = str(uuid.uuid4())
    with Session(get_db_engine()) as session:
        session.add(Judgment(id=jid, address=_demo_seed(),
                             hops=hops,
                             time_window_days=random.randint(30, 365),
                             status=status, trace_id=trace_id))
        session.commit()
    return jid


def _cleanup(jid: str) -> None:
    from backend.models.base import Judgment

    with Session(get_db_engine()) as session:
        session.execute(delete(Judgment).where(Judgment.id == jid))
        session.commit()


def _spans(jid: str) -> list:
    from backend.models.base import AnalysisSpan

    with Session(get_db_engine()) as session:
        return list(session.query(AnalysisSpan)
                    .filter(AnalysisSpan.judgment_id == jid)
                    .order_by(AnalysisSpan.id).all())


def _by_name(spans: list) -> dict:
    return {s.name: s for s in spans}


class TestSuccessfulTrace:
    @requires_db
    def test_completed_analysis_writes_root_and_stage_spans(self, env):
        jid = _new_judgment(trace_id="trace-success-1")
        try:
            assert asyncio.run(run_analysis(jid)) == "completed"
            spans = _spans(jid)
            names = {s.name for s in spans}
            assert {"analysis", "building_subgraph", "retrieval_topk",
                    "llm_judging"} <= names

            root = next(s for s in spans if s.parent_id is None)
            assert root.name == "analysis"
            assert root.status == "completed"
            assert root.duration_ms is not None and root.duration_ms >= 0
            assert root.started_at is not None and root.finished_at is not None

            # 阶段 span 全部挂到根 span（trace 以 judgment 为根节点）
            children = [s for s in spans if s.parent_id == root.id]
            assert children, "阶段 span 必须归属根 span"
            assert {s.name for s in children} >= {"building_subgraph",
                                                 "retrieval_topk",
                                                 "llm_judging"}
            # 有召回才进精排（空召回在 retrieval_topk 后明确早退）
            recalled = (_by_name(spans)["retrieval_topk"].span_metadata
                        or {}).get("recalled") or 0
            assert ("wl_rerank" in names) == (recalled > 0)
            for span in spans:
                assert span.status in ("completed", "failed", "skipped")
                assert span.duration_ms is not None
                assert span.attempt == 1
                assert span.started_at is not None
                assert span.finished_at is not None
        finally:
            _cleanup(jid)

    @requires_db
    def test_span_metadata_carries_stage_facts(self, env):
        jid = _new_judgment(trace_id="trace-success-2")
        try:
            assert asyncio.run(run_analysis(jid)) == "completed"
            spans = _by_name(_spans(jid))
            build = spans["building_subgraph"].span_metadata or {}
            assert build["hops"] == 1 and build["time_window_days"] > 0
            assert "nodes" in build and "edges" in build
            assert build["data_quality"] in ("complete", "degraded")

            recall = spans["retrieval_topk"].span_metadata or {}
            assert recall["recall_limit"] > 0
            assert "recalled" in recall and "recall_mode" in recall

            if "wl_rerank" in spans:
                rerank = spans["wl_rerank"].span_metadata or {}
                assert rerank["top_k"] >= 1 and "candidates" in rerank

            judge = spans["llm_judging"].span_metadata or {}
            assert judge["model"] and judge["prompt_version"] \
                and judge["builder_version"]

            # fixture 数据源不发网络请求 → 上游 span 明确标 skipped
            assert spans["esplora_fetch"].status == "skipped"
            assert (spans["esplora_fetch"].span_metadata or {})["mode"] \
                == "fixture"
        finally:
            _cleanup(jid)

    @requires_db
    def test_trace_id_is_shared_by_all_spans(self, env):
        jid = _new_judgment(trace_id="trace-shared-9")
        try:
            assert asyncio.run(run_analysis(jid)) == "completed"
            spans = _spans(jid)
            assert spans and {s.trace_id for s in spans} == {"trace-shared-9"}
        finally:
            _cleanup(jid)


class TestFailedAndRetryingTrace:
    @requires_db
    def test_failed_analysis_records_stable_error_code(self, env, monkeypatch):
        monkeypatch.setenv("LLM_MOCK_SCENARIO", "timeout")
        reset_settings()
        jid = _new_judgment(trace_id="trace-fail-1")
        try:
            assert asyncio.run(run_analysis(jid)) == "failed"
            spans = _by_name(_spans(jid))
            root = next(s for s in _spans(jid) if s.parent_id is None)
            assert root.status == "failed"
            assert root.error_code == "LLM_PROVIDER_TIMEOUT"
            # 失败阶段可定位到子步骤（llm_judging），而不是只有整体失败
            assert spans["llm_judging"].status == "failed"
            assert spans["building_subgraph"].status == "completed"
        finally:
            _cleanup(jid)

    @requires_db
    def test_retry_attempt_is_recorded_per_run(self, env, monkeypatch):
        monkeypatch.setenv("LLM_MOCK_SCENARIO", "timeout")
        monkeypatch.setenv("TASK_MAX_TRIES", "2")
        reset_settings()
        jid = _new_judgment(trace_id="trace-retry-1")
        try:
            assert asyncio.run(run_analysis(jid, attempt=1)) == "retrying"
            first = [s for s in _spans(jid) if s.parent_id is None]
            assert len(first) == 1
            assert first[0].status == "retrying"
            assert first[0].error_code == "LLM_PROVIDER_TIMEOUT"
            assert first[0].attempt == 1

            assert asyncio.run(run_analysis(jid, attempt=2)) == "failed"
            roots = [s for s in _spans(jid) if s.parent_id is None]
            assert len(roots) == 2                    # 每次尝试一条根 trace
            assert roots[1].attempt == 2 and roots[1].status == "failed"
        finally:
            _cleanup(jid)


class TestRedisUnavailable:
    @requires_db
    def test_trace_still_readable_when_redis_is_down(self, env, monkeypatch):
        # Redis 不可达：实时阶段通知静默失败（best-effort），
        # 但持久化 trace 必须完整——这就是 issue 要求 DB 作为历史事实源的原因
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
        reset_settings()
        jid = _new_judgment(trace_id="trace-noredis-1")
        try:
            assert asyncio.run(run_analysis(jid)) == "completed"
            spans = _by_name(_spans(jid))
            assert spans["analysis"].status == "completed"
            assert spans["llm_judging"].status == "completed"
            assert spans["analysis"].trace_id == "trace-noredis-1"
        finally:
            _cleanup(jid)


class TestTraceEndpoint:
    @requires_db
    def test_trace_endpoint_shape_and_anonymous_read(self, env, api_client):
        jid = _new_judgment(trace_id="trace-endpoint-1")
        try:
            assert asyncio.run(run_analysis(jid)) == "completed"
            resp = api_client.get(f"/api/v1/judgments/{jid}/trace")
            assert resp.status_code == 200, resp.text        # 匿名只读放行
            body = resp.json()
            assert body["judgment_id"] == jid
            assert body["trace_id"] == "trace-endpoint-1"
            assert body["status"] == "completed"
            assert body["total_duration_ms"] is not None
            names = {s["name"] for s in body["spans"]}
            assert "analysis" in names and "llm_judging" in names
            assert body["stage_duration_ms"]["llm_judging"] is not None
        finally:
            _cleanup(jid)

    @requires_db
    def test_trace_endpoint_surfaces_failure_code(self, env, api_client,
                                                  monkeypatch):
        monkeypatch.setenv("LLM_MOCK_SCENARIO", "timeout")
        reset_settings()
        jid = _new_judgment(trace_id="trace-endpoint-fail")
        try:
            assert asyncio.run(run_analysis(jid)) == "failed"
            body = api_client.get(f"/api/v1/judgments/{jid}/trace").json()
            assert body["status"] == "failed"
            assert body["error_code"] == "LLM_PROVIDER_TIMEOUT"
            failed = [s for s in body["spans"] if s["status"] == "failed"]
            assert failed and failed[0]["error_code"] == "LLM_PROVIDER_TIMEOUT"
        finally:
            _cleanup(jid)

    def test_trace_endpoint_unknown_judgment_404(self, api_client):
        resp = api_client.get(f"/api/v1/judgments/{uuid.uuid4()}/trace")
        assert resp.status_code == 404
        assert resp.json()["error_code"] == "NOT_FOUND"


class TestTraceIdPropagation:
    @requires_db
    def test_request_trace_id_reaches_worker(self, env, api_client,
                                             monkeypatch):
        """验收：HTTP 请求创建的 trace ID 能传递至异步 worker。"""
        from tests.unit.test_backend_api import _demo_seed

        async def _no_queue(*args, **kwargs):
            return False    # 投递失败 → dispatch 走进程内降级路径

        monkeypatch.setattr(tq, "_enqueue", _no_queue)
        monkeypatch.setattr(tq, "_fail_until", 0.0)
        monkeypatch.setattr(tq, "_pool", None)

        header_trace = "req-trace-abcdef12"
        resp = api_client.post(
            "/api/v1/addresses/analyze",
            json={"address": _demo_seed(), "hops": 1,
                  "time_window_days": random.randint(30, 365)},
            headers={"X-Request-Id": header_trace})
        assert resp.status_code == 202, resp.text
        jid = resp.json()["judgment_id"]
        assert resp.headers["X-Trace-Id"] == header_trace
        try:
            from backend.models.base import Judgment

            with Session(get_db_engine()) as session:
                assert session.get(Judgment, jid).trace_id == header_trace

            # 降级路径在后台 loop 执行；等它落 span（trace_id 必须一致）
            deadline = time.time() + 20
            spans: list = []
            while time.time() < deadline:
                spans = _spans(jid)
                if any(s.name == "analysis" for s in spans):
                    break
                time.sleep(0.2)
            assert spans, "worker 侧未产出 span"
            assert {s.trace_id for s in spans} == {header_trace}
        finally:
            _cleanup(jid)

    def test_invalid_trace_id_header_is_replaced(self, api_client):
        from tests.unit.test_backend_api import _demo_seed

        resp = api_client.post(
            "/api/v1/addresses/analyze",
            json={"address": _demo_seed(),
                  "time_window_days": random.randint(30, 365)},
            headers={"X-Request-Id": "bad id!!<script>"})
        assert resp.status_code in (200, 202), resp.text
        trace_id = resp.headers["X-Trace-Id"]
        assert trace_id != "bad id!!<script>"
        assert len(trace_id) == 36                  # 生成的 uuid4
        assert "trace_url" in resp.json()


class TestMetadataRedaction:
    def test_sanitize_redacts_secrets_and_payloads(self):
        clean = sanitize_metadata({
            "nodes": 12,
            "authorization": "Bearer sk-secret",
            "api_key": "sk-secret",
            "prompt": "full prompt text",
            "messages": [{"role": "user", "content": "chain of thought"}],
            "thinking": "raw reasoning",
            "response": {"body": "raw"},
            "url": "https://api.example.com?api_key=sk-secret",
            "nested": {"cookie": "session=abc", "limit": 5},
            "long": "x" * 1000,
        })
        assert clean["nodes"] == 12
        assert clean["nested"]["limit"] == 5
        for key in ("authorization", "api_key", "prompt", "messages",
                    "thinking", "response", "url"):
            assert clean[key] == "[REDACTED]", key
        assert clean["nested"]["cookie"] == "[REDACTED]"
        assert clean["long"].endswith("…") and len(clean["long"]) < 400

    def test_empty_metadata_is_none(self):
        assert sanitize_metadata(None) is None
        assert sanitize_metadata({}) is None

    @requires_db
    def test_persisted_metadata_has_no_sensitive_keys(self, env):
        jid = _new_judgment()
        try:
            assert asyncio.run(run_analysis(jid)) == "completed"
            for span in _spans(jid):
                meta = span.span_metadata or {}
                for key in ("authorization", "api_key", "prompt", "messages",
                            "thinking", "response", "headers"):
                    assert key not in meta
        finally:
            _cleanup(jid)

    def test_error_code_is_stable_not_message(self):
        from backend.services.tracing import _error_code_for

        class Boom(RuntimeError):
            code = "LLM_PROVIDER_TIMEOUT"

        assert _error_code_for(Boom("secret payload in message")) \
            == "LLM_PROVIDER_TIMEOUT"
        assert _error_code_for(ValueError("secret payload")) == "ValueError"


class TestRecorderDisabled:
    def test_null_recorder_is_noop(self):
        rec = NullRecorder()
        with rec.span("x") as span:
            span.set(a=1)
            span.set_status("failed", error_code="X")
        assert rec.enabled is False

    @requires_db
    def test_span_recorder_survives_write_failure(self, env, monkeypatch):
        # 观测写入失败不得阻塞分析：engine 指向不可用端口时 span 调用仍安静返回
        monkeypatch.setenv("DATABASE_URL",
                           "postgresql://pt:pt@127.0.0.1:1/patterntrace")
        reset_settings()
        reset_engine()
        rec = SpanRecorder("judgment-x", "trace-x")
        with rec.span("analysis", root=True):
            pass
        assert rec.root_span_id is None    # 写失败 → 无 id，但没抛异常
