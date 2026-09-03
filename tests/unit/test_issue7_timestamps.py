"""issue #7 —— Judgment 结论/数据时间戳 + 案件报告快照（immutable case report snapshots）。

覆盖验收标准：
  1. Judgment 增加 concluded_at / data_as_of；
  2. completed / failed Judgment 都记录结论时间；
  3. 同地址多次分析创建独立时间版本 Judgment，旧 Judgment 不被覆盖；
  4. CaseAddress.judgment_id 有明确写入逻辑（分析完成后回指最新 completed）；
  5. 报告生成时冻结 Judgment 与子图快照，后续重新分析不改变已生成报告；
  6. 报告证据链包含时间字段。

DB 用例复用 test_backend_api 的 requires_db 守卫（本环境 Postgres 可达即执行）。
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.api.app import _judgment_payload, get_db_engine
from backend.services.report_service import REPORTS_DIR, _fmt_time

from test_backend_api import (  # noqa: E402 — 复用同目录共享用例辅助
    _clear_judgments,
    _demo_seed,
    _login,
    _poll_terminal,
    requires_db,
)


# ---------------------------------------------------------------------------
# 纯逻辑：payload 与时间格式化
# ---------------------------------------------------------------------------
class TestJudgmentPayloadTimestamps:
    def _completed(self):
        return SimpleNamespace(
            id="j-1", address="addr:x", hops=3, time_window_days=90,
            status="completed", created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            concluded_at=datetime(2026, 1, 1, 12, 5, tzinfo=UTC),
            data_as_of=datetime(2026, 1, 1, 11, 58, tzinfo=UTC),
            subgraph_snapshot={"nodes": [], "edges": []},
            risk_level="high", matched_pattern_id=None, matched_pattern_name=None,
            confidence=0.9, evidence=["addr:x"], reasoning="r",
            recommended_action="block", model="m", prompt_version="v1",
            builder_version="v1", latency_ms=120, error_code=None,
            error_message=None, retry_count=0, failed_at=None,
        )

    def test_completed_payload_includes_time_fields(self):
        payload = _judgment_payload(self._completed())
        assert payload["concluded_at"] == "2026-01-01T12:05:00+00:00"
        assert payload["data_as_of"] == "2026-01-01T11:58:00+00:00"

    def test_failed_payload_includes_concluded_at(self):
        row = SimpleNamespace(
            id="j-2", address="addr:x", hops=1, time_window_days=90,
            status="failed", created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            concluded_at=datetime(2026, 1, 1, 12, 3, tzinfo=UTC),
            data_as_of=datetime(2026, 1, 1, 11, 58, tzinfo=UTC),
            subgraph_snapshot=None, risk_level=None, matched_pattern_id=None,
            matched_pattern_name=None, confidence=None, evidence=None,
            reasoning=None, recommended_action=None, model=None,
            prompt_version=None, builder_version=None, latency_ms=None,
            error_code="TASK_TIMEOUT", error_message="e", retry_count=1,
            failed_at=datetime(2026, 1, 1, 12, 3, tzinfo=UTC),
        )
        payload = _judgment_payload(row)
        assert payload["status"] == "failed"
        assert payload["concluded_at"] == "2026-01-01T12:03:00+00:00"
        assert payload["data_as_of"] == "2026-01-01T11:58:00+00:00"

    def test_fmt_time_falls_back(self):
        assert _fmt_time(None) == "-"
        assert _fmt_time("2026-01-01T12:00:00+00:00") == "2026-01-01T12:00:00+00:00"
        d = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        assert "_" not in _fmt_time(d).replace("+00:00", "")

    def test_fmt_time_missing_attr(self):
        class _Bare:
            pass
        assert _fmt_time(getattr(_Bare(), "concluded_at", None)) == "-"


# ---------------------------------------------------------------------------
# DB 用例：同地址多次分析 / 案件指针写入 / failed 结论时间 / 报告冻结
# ---------------------------------------------------------------------------
@pytest.fixture()
def api_client(monkeypatch):
    """mock LLM provider 的隔离客户端（沿用 test_backend_api/test_phase5 约定）。"""
    from fastapi.testclient import TestClient

    from backend.api.app import create_app, reset_stores
    from backend.core.config import reset_settings

    monkeypatch.setenv("JWT_SECRET", "test-secret-for-ci-only")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.delenv("LLM_MOCK_SCENARIO", raising=False)
    reset_settings()
    reset_stores()
    client = TestClient(create_app())
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})  # SEC-03
    yield client
    reset_settings()
    reset_stores()


def _new_case_with_address(api_client, addr) -> str:
    cid = api_client.post("/api/v1/cases",
                          json={"title": "issue7", "description": ""}).json()["id"]
    r = api_client.post(f"/api/v1/cases/{cid}/addresses",
                        json={"addresses": [addr]})
    assert r.status_code in (200, 201)
    return cid


@requires_db
def test_issue7_multiple_analyses_are_time_versioned(api_client):
    _clear_judgments()
    addr = _demo_seed()
    # 第一次分析
    r1 = api_client.post("/api/v1/addresses/analyze",
                         json={"address": addr, "hops": 1})
    jid1 = r1.json()["judgment_id"]
    p1 = _poll_terminal(api_client, jid1)
    assert p1["status"] == "completed", p1
    assert p1["concluded_at"] and p1["data_as_of"]  # 结论/数据时间戳已回填

    # 第二次分析（不同 hops）→ 新的独立 judgment 行
    r2 = api_client.post("/api/v1/addresses/analyze",
                         json={"address": addr, "hops": 2})
    jid2 = r2.json()["judgment_id"]
    assert jid2 != jid1
    p2 = _poll_terminal(api_client, jid2)
    assert p2["status"] == "completed", p2
    assert p2["concluded_at"] and p2["data_as_of"]

    # 旧 judgment 未被新分析覆盖，仍返回其原始结论时间
    old = api_client.get(f"/api/v1/judgments/{jid1}").json()
    assert old["id"] == jid1
    assert old["concluded_at"] == p1["concluded_at"]


@requires_db
def test_issue7_case_address_judgment_id_written(api_client):
    _clear_judgments()
    _login(api_client)
    addr = _demo_seed()
    cid = _new_case_with_address(api_client, addr)

    r = api_client.post("/api/v1/addresses/analyze",
                        json={"address": addr, "hops": 1})
    jid = r.json()["judgment_id"]
    p = _poll_terminal(api_client, jid)
    assert p["status"] == "completed", p

    from backend.models.base import CaseAddress

    with Session(get_db_engine()) as session:
        link = session.execute(
            select(CaseAddress).where(CaseAddress.case_id == cid,
                                      CaseAddress.address == addr)
        ).scalar_one()
        assert link.judgment_id == jid  # 明确写入：关联地址回指最新 completed


@requires_db
def test_issue7_failed_records_concluded_at(api_client, monkeypatch):
    _clear_judgments()
    monkeypatch.setenv("LLM_MOCK_SCENARIO", "invalid_evidence_all_retries")
    r = api_client.post("/api/v1/addresses/analyze",
                        json={"address": _demo_seed()})
    jid = r.json()["judgment_id"]
    p = _poll_terminal(api_client, jid)
    assert p["status"] == "failed", p
    assert p["concluded_at"]  # failed 终态同样记录结论时间


@requires_db
def test_issue7_report_freezes_judgment_after_reanalysis(api_client):
    """报告生成一次落盘；后续重新分析不改变已生成报告内容。"""
    _clear_judgments()
    _login(api_client)
    addr = _demo_seed()
    cid = _new_case_with_address(api_client, addr)

    def _generate_report() -> str:
        rid = api_client.post(f"/api/v1/cases/{cid}/reports?format=html",
                              json={}).json()["report_id"]
        deadline = time.time() + 15
        while time.time() < deadline:
            st = api_client.get(f"/api/v1/reports/{rid}").json()
            if st["status"] == "completed":
                return rid
            if st["status"] == "failed":
                raise AssertionError(f"report failed: {st}")
            time.sleep(0.3)
        raise AssertionError("report timeout")

    # 首次分析并生成报告（冻结 judgment 快照）
    r1 = api_client.post("/api/v1/addresses/analyze",
                         json={"address": addr, "hops": 1})
    jid1 = r1.json()["judgment_id"]
    assert _poll_terminal(api_client, jid1)["status"] == "completed"
    rid = _generate_report()
    content1 = (REPORTS_DIR / f"{rid}.html").read_text()
    assert f"judgment:{jid1[:16]}" in content1  # 报告冻结了第一次的 judgment

    # 重新分析（新 judgment，指针前进）→ 已生成报告不变
    r2 = api_client.post("/api/v1/addresses/analyze",
                         json={"address": addr, "hops": 2})
    jid2 = r2.json()["judgment_id"]
    assert jid2 != jid1
    assert _poll_terminal(api_client, jid2)["status"] == "completed"

    content2 = (REPORTS_DIR / f"{rid}.html").read_text()
    assert content2 == content1  # 报告文件未被重写（仍引用第一次 judgment）
