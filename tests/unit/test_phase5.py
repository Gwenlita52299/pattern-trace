"""阶段5 业务闭环单测 — CM-03/10、SEC-01/03、BE-23~27/37/48/49、REL-03/05。

需要 PostgreSQL 的用例以 requires_db 跳过；完整工作流（登录→建案→关联→
分析→导出报告）由 infra/verify_phase5.sh 的 e2e 脚本在真实环境收口。
"""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from backend.api.app import (
    create_app,
    get_db_engine,
    reset_stores,
    seed_user,
)
from backend.core.config import reset_settings

# 复用阶段4测试的公共助手（演示地址 / 清 judgments / 轮询终态）
from tests.unit.test_backend_api import (
    _clear_judgments,
    _demo_seed,
    _poll_terminal,
)


# ---------------------------------------------------------------------------
# 基础设施：可用性探测与夹具
# ---------------------------------------------------------------------------
def _db_ready() -> bool:
    try:
        from sqlalchemy import text

        with get_db_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


requires_db = pytest.mark.skipif(
    not _db_ready(), reason="PostgreSQL 未运行（verify_phase5.sh 中覆盖）")

try:
    import fpdf  # noqa: F401

    HAVE_FPDF = True
except ImportError:
    HAVE_FPDF = False


@pytest.fixture()
def api_client(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    # 钉住 fixture：本地 .env 若为 live，单测会走真实 Esplora（与 test_backend_api 对齐）
    monkeypatch.setenv("GRAPH_DATA_MODE", "fixture")
    monkeypatch.delenv("LLM_MOCK_SCENARIO", raising=False)
    reset_settings()
    reset_stores()
    client = TestClient(create_app())
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})  # SEC-03
    yield client
    reset_settings()
    reset_stores()


def _login(client, email: str) -> dict:
    """登录并返回带 CSRF 头的 Authorization headers。"""
    passwords = {"a@test.com": "PasswordA!123",
                 "b@test.com": "PasswordB!123",
                 "admin@test.com": "AdminP@ss1"}
    pw = passwords[email]
    r = client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}",
            "X-Requested-With": "XMLHttpRequest"}


def _create_case(client, auth: dict, title="Case X", key=None) -> str:
    """建案并返回 case_id（断言 201）。"""
    headers = {**auth}
    if key:
        headers["Idempotency-Key"] = key
    r = client.post("/api/v1/cases", json={"title": title},
                    headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _clear_business() -> None:
    from backend.models.base import Case, CaseAddress, Report

    with Session(get_db_engine()) as session:
        session.execute(delete(Report))
        session.execute(delete(CaseAddress))
        session.execute(delete(Case))
        session.commit()


# ---------------------------------------------------------------------------
# 案件 CRUD 与幂等（BE-23/48、BE-25）
# ---------------------------------------------------------------------------
@requires_db
class TestCasesCRUD:
    def _clear_cases(self):
        from backend.models.base import Case, CaseAddress, Report

        with Session(get_db_engine()) as session:
            session.execute(delete(Report))
            session.execute(delete(CaseAddress))
            session.execute(delete(Case))
            session.commit()

    def test_be23_48_idempotency_key_semantics(self, api_client):
        self._clear_cases()
        seed_user("a@test.com", "PasswordA!123")
        auth = {"Authorization": "Bearer " + api_client.post(
            "/api/v1/auth/login",
            json={"email": "a@test.com",
                  "password": "PasswordA!123"}).json()["access_token"]}
        key = {"Idempotency-Key": "case-key-001"}

        first = api_client.post("/api/v1/cases", json={"title": "Case A"},
                                headers={**auth, **key})
        replay = api_client.post("/api/v1/cases", json={"title": "Case A"},
                                 headers={**auth, **key})
        conflict = api_client.post("/api/v1/cases", json={"title": "Case B"},
                                   headers={**auth, **key})

        assert first.status_code == 201
        assert replay.status_code == 200
        assert replay.json()["id"] == first.json()["id"]
        assert conflict.status_code == 409
        self._clear_cases()

    def test_be25_case_status_forward_only(self, api_client):
        self._clear_cases()
        seed_user("a@test.com", "PasswordA!123")
        auth = {"Authorization": "Bearer " + api_client.post(
            "/api/v1/auth/login",
            json={"email": "a@test.com", "password": "PasswordA!123"}
        ).json()["access_token"]}
        cid = _create_case(api_client, auth)

        p1 = api_client.patch(f"/api/v1/cases/{cid}",
                              json={"status": "investigating"}, headers=auth)
        p2 = api_client.patch(f"/api/v1/cases/{cid}",
                              json={"status": "closed"}, headers=auth)
        back = api_client.patch(f"/api/v1/cases/{cid}",
                                json={"status": "open"}, headers=auth)
        assert p1.status_code == 200 and p2.status_code == 200
        assert back.status_code == 422
        self._clear_cases()

    @pytest.mark.skipif(not _db_ready(), reason="PostgreSQL 未运行")
    def test_be49_invalid_address_rejected(self, api_client):
        self._clear_cases()
        seed_user("a@test.com", "PasswordA!123")
        auth = {"Authorization": "Bearer " + api_client.post(
            "/api/v1/auth/login",
            json={"email": "a@test.com", "password": "PasswordA!123"}
        ).json()["access_token"]}
        cid = _create_case(api_client, auth)
        r = api_client.post(f"/api/v1/cases/{cid}/addresses",
                            json={"addresses": ["not-a-valid-address"]},
                            headers=auth)
        assert r.status_code == 422
        missing = api_client.post("/api/v1/cases/nonexistent/addresses",
                                  json={"addresses": [_demo_seed()]},
                                  headers=auth)
        assert missing.status_code == 404


# ---------------------------------------------------------------------------
# SEC-01 水平越权隔离 + 留痕
# ---------------------------------------------------------------------------
@requires_db
class TestOwnership:
    def test_sec01_horizontal_access_denied_and_audited(self, api_client):
        seed_user("a@test.com", "PasswordA!123")
        seed_user("b@test.com", "PasswordB!123")
        auth_a, auth_b = _login(api_client, "a@test.com"), \
            _login(api_client, "b@test.com")
        case_id = _create_case(api_client, auth_a)

        get_r = api_client.get(f"/api/v1/cases/{case_id}", headers=auth_b)
        patch_r = api_client.patch(f"/api/v1/cases/{case_id}",
                                   json={"title": "hijack"}, headers=auth_b)
        assert get_r.status_code == 404      # 统一 404 不泄露存在性
        assert patch_r.status_code == 404

        # 越权拒绝在审计日志留痕（action=access_denied 经由 DB 直查）
        from sqlalchemy import text

        with Session(get_db_engine()) as session:
            rows = session.execute(text(
                "SELECT action FROM audit_logs "
                "WHERE action='access_denied' AND resource_id=:rid"),
                {"rid": case_id}).fetchall()
        assert rows, "access_denied 未留痕"


# ---------------------------------------------------------------------------
# 报告生命周期：异步导出 → 轮询 → 签名下载
# ---------------------------------------------------------------------------
@requires_db
class TestReports:
    def _completed_case(self, client) -> str:
        """建案 + 关联演示地址，并保证该地址有一条 completed judgment。"""
        seed_user("a@test.com", "PasswordA!123")
        auth = {"Authorization": "Bearer " + client.post(
            "/api/v1/auth/login",
            json={"email": "a@test.com", "password": "PasswordA!123"}
        ).json()["access_token"]}
        cid = _create_case(client, auth)
        client.post(f"/api/v1/cases/{cid}/addresses",
                    json={"addresses": [_demo_seed()]}, headers=auth)

        jid = self._analyze_demo(client)
        payload = client.get(f"/api/v1/judgments/{jid}").json()
        return cid, auth, payload

    def _analyze_demo(self, client) -> str:
        _clear_judgments()
        r = client.post("/api/v1/addresses/analyze",
                        json={"address": _demo_seed()})
        jid = r.json()["judgment_id"]
        deadline = time.time() + 20
        while time.time() < deadline:
            p = client.get(f"/api/v1/judgments/{jid}").json()
            if p.get("status") in ("completed", "failed"):
                break
            time.sleep(0.2)
        assert p["status"] == "completed", p
        return jid

    def test_report_lifecycle_and_signed_download(self, api_client):
        cid, auth, judgment = self._completed_case(api_client)
        r = api_client.post(f"/api/v1/cases/{cid}/reports",
                            params={"format": "html"}, headers=auth)
        assert r.status_code == 202
        rid = r.json()["report_id"]

        deadline = time.time() + 15
        report = {}
        while time.time() < deadline:
            report = api_client.get(f"/api/v1/reports/{rid}",
                                    headers=auth).json()
            if report["status"] in ("completed", "failed"):
                break
            time.sleep(0.2)
        assert report["status"] == "completed", report
        dl = report["download_url"]


        path_part, _, query = dl.partition("?")
        params = dict(p.split("=") for p in query.split("&"))
        resp = api_client.get(path_part, params=params)
        assert resp.status_code == 200
        body = resp.text
        assert "PatternTrace" in body and _demo_seed() in body
        # 证据链三元组出现在报告中（spec §3）
        assert judgment.get("model") in body

    def test_expired_signature_rejected(self, api_client):
        from backend.core.config import get_settings
        from backend.services.report_service import sign_download_token

        past_exp = int(time.time()) - 100
        sig = sign_download_token(
            "any-report", past_exp, get_settings().jwt_secret)
        r = api_client.get("/api/v1/reports/any-report/download",
                           params={"exp": past_exp, "sig": sig})
        assert r.status_code == 403

    def test_be27_concurrent_limit(self, api_client):
        seed_user("a@test.com", "PasswordA!123")
        auth = {"Authorization": "Bearer " + api_client.post(
            "/api/v1/auth/login",
            json={"email": "a@test.com", "password": "PasswordA!123"}
        ).json()["access_token"]}
        cid = _create_case(api_client, auth)

        from backend.models.base import Report

        with Session(get_db_engine()) as session:
            for i in range(2):
                session.add(Report(id=str(uuid.uuid4()), case_id=cid,
                                   format="html", status="processing",
                                   created_by=_user_id_of(api_client)))
            session.commit()

        r = api_client.post(f"/api/v1/cases/{cid}/reports",
                            params={"format": "html"}, headers=auth)
        assert r.status_code == 429
        assert r.headers.get("Retry-After")
        _clear_business()


def _user_id_of(client) -> str:
    from sqlalchemy import text

    with Session(get_db_engine()) as session:
        row = session.execute(text(
            "SELECT id FROM users WHERE email='a@test.com'")).first()
    return row[0] if row else "unknown"


# ---------------------------------------------------------------------------
# 用户管理 API（BE-36/37）
# ---------------------------------------------------------------------------
class TestUsersAdmin:
    def test_be36_role_gating(self, api_client):
        seed_user("a@test.com", "PasswordA!123")
        seed_user("admin@test.com", "AdminP@ss1", role="admin")
        user_auth = {"Authorization": "Bearer " + api_client.post(
            "/api/v1/auth/login",
            json={"email": "a@test.com", "password": "PasswordA!123"}
        ).json()["access_token"]}
        admin_auth = {"Authorization": "Bearer " + api_client.post(
            "/api/v1/auth/login",
            json={"email": "admin@test.com", "password": "AdminP@ss1"}
        ).json()["access_token"]}
        fresh_email = f"new-{uuid.uuid4().hex[:6]}@x.com"  # DB 跨测试运行持久
        forbidden = api_client.post("/api/v1/users", json={
            "email": fresh_email, "password": "LongEnough1!"},
            headers=user_auth)
        assert forbidden.status_code == 403

        ok = api_client.post("/api/v1/users", json={
            "email": fresh_email, "password": "LongEnough1!"},
            headers=admin_auth)
        assert ok.status_code == 201, ok.text

        dup = api_client.post("/api/v1/users", json={
            "email": fresh_email, "password": "LongEnough1!"},
            headers=admin_auth)
        assert dup.status_code == 409

    @pytest.mark.parametrize("bad_password", [
        "x" * 73,                                    # 73 ASCII 字节
        "密" * 80,                                   # 80 字符 = 240 bytes
    ])
    def test_be37_password_byte_length_rejected(self, api_client,
                                                bad_password):
        seed_user("admin@test.com", "AdminP@ss1", role="admin")
        admin_auth = {"Authorization": "Bearer " + api_client.post(
            "/api/v1/auth/login",
            json={"email": "admin@test.com", "password": "AdminP@ss1"}
        ).json()["access_token"]}
        r = api_client.post("/api/v1/users", json={
            "email": f"bytey-{uuid.uuid4().hex[:6]}@x.com",
            "password": bad_password}, headers=admin_auth)
        assert r.status_code == 422
        assert "bytes" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 审计链路（CM-10）与 CSRF 强制（SEC-03）
# ---------------------------------------------------------------------------
@requires_db
class TestAuditAndCsrf:
    def test_cm10_write_ops_audited_with_request_fields(self, api_client):
        seed_user("a@test.com", "PasswordA!123")
        login = api_client.post("/api/v1/auth/login",
                                json={"email": "a@test.com",
                                      "password": "PasswordA!123"})
        auth = {"Authorization":
                f"Bearer {login.json()['access_token']}"}
        _create_case(api_client, auth, title="audited-case")

        admin_auth = {"Authorization": "Bearer " + api_client.post(
            "/api/v1/auth/login",
            json={"email": "admin@test.com", "password": "AdminP@ss1"}
        ).json()["access_token"]}

        listed = api_client.get("/api/v1/audit-logs",
                                params={"action": "create_case"},
                                headers=admin_auth)
        assert listed.status_code == 200
        items = [i for i in listed.json()["items"]
                 if i["http_path"] == "/api/v1/cases"]
        assert items, "create_case 未被审计"
        item = items[0]
        for field in ("request_id", "http_method", "response_status",
                      "action_result", "latency_ms"):
            assert item[field] is not None, field

    def test_sec03_csrf_enforced_server_side(self):
        reset_settings()
        reset_stores()
        try:
            client = TestClient(create_app())  # 不带 X-Requested-With
            r = client.post("/api/v1/cases", json={"title": "x"})
            assert r.status_code == 403
            assert r.json()["error_code"] == "CSRF_CHECK_FAILED"
        finally:
            reset_stores()


# ---------------------------------------------------------------------------
# 可靠性语义（REL-03 / REL-05 / CM-03）
# ---------------------------------------------------------------------------
@requires_db
class TestReliabilitySemantics:
    @pytest.fixture(autouse=True)
    def _mock_llm(self, monkeypatch):
        """直连编排函数的测试必须显式锁定 mock provider——
        否则 Settings 可能回落到 ollama，ConnectError 会被归类为 PROVIDER_ERROR。"""
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        monkeypatch.setenv("JWT_SECRET", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
        monkeypatch.delenv("LLM_MOCK_SCENARIO", raising=False)
        reset_settings()
        reset_stores()
        yield
        reset_settings()
        reset_stores()

    def _enqueue(self, address: str) -> str:
        from backend.models.base import Judgment

        jid = str(uuid.uuid4())
        with Session(get_db_engine()) as session:
            session.add(Judgment(id=jid, address=address, hops=3,
                                 time_window_days=90, status="queued"))
            session.commit()
        return jid

    @pytest.mark.parametrize("scenario,expected_code", [
        ("timeout", "LLM_PROVIDER_TIMEOUT"),
        ("rate_limited", "LLM_PROVIDER_RATE_LIMITED"),
    ])
    def test_rel03_provider_failure_taxonomy(self, scenario, expected_code):
        import asyncio
        import os

        from backend.services.orchestration import run_analysis

        os.environ["LLM_MOCK_SCENARIO"] = scenario
        _clear_judgments()
        try:
            jid = self._enqueue(_demo_seed())
            result = asyncio.run(run_analysis(jid))
            assert result == "failed"

            from backend.models.base import Judgment

            with Session(get_db_engine()) as session:
                row = session.get(Judgment, jid)
            assert row.error_code == expected_code, row.error_code
        finally:
            os.environ.pop("LLM_MOCK_SCENARIO", None)

    def test_rel05_data_source_unavailable(self, monkeypatch):
        import asyncio

        import backend.services.orchestration as orch
        from backend.core.btc_address import encode_bech32m_address
        from backend.models.base import Judgment

        def _failing_provider(settings):
            def _raise(addr):
                raise ConnectionError("esplora down")
            return _raise, []

        monkeypatch.setattr(orch, "build_provider", _failing_provider)
        _clear_judgments()
        addr = encode_bech32m_address(b"rel05-unreachable-seed"[:20])
        jid = self._enqueue(addr)
        result = asyncio.run(orch.run_analysis(jid))
        assert result == "failed"
        with Session(get_db_engine()) as session:
            row = session.get(Judgment, jid)
        assert row.error_code == "ESPLORA_UNAVAILABLE"

    def test_cm03_state_transition_events(self, api_client):
        _clear_judgments()
        r = api_client.post("/api/v1/addresses/analyze",
                            json={"address": _demo_seed()})
        jid = r.json()["judgment_id"]
        payload = _poll_terminal(api_client, jid)
        assert payload["status"] == "completed"

        from sqlalchemy import text

        with Session(get_db_engine()) as session:
            events = session.execute(text(
                "SELECT from_status, to_status FROM judgment_events "
                "WHERE judgment_id = :jid ORDER BY id"),
                {"jid": jid}).fetchall()
        # issue #78：judgment_events 现在混有 stage:* 观测行——
        # 状态迁移与阶段观测区分开，本断言只看状态迁移序列
        seq = [(f, t) for f, t in events if not t.startswith("stage:")]
        assert seq == [("queued", "processing"), ("processing", "completed")]
