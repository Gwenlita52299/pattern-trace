"""backend-api 单测 — BE 用例可自动化子集（阶段4）。

需要 PostgreSQL 的用例以 requires_db 跳过保护：纯逻辑断言在任何环境可跑；
DB 行为由 infra/verify_phase4.sh 在真实库上收口。
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import UTC

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from backend.api.app import (
    close_db_engine,
    create_app,
    get_db_engine,
    reset_stores,
    seed_user,
)
from backend.core.config import reset_settings


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _db_available() -> bool:
    try:
        from sqlalchemy import text as _t

        from backend.api.app import get_db_engine

        with get_db_engine().connect() as conn:
            conn.execute(_t("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


requires_db = pytest.mark.skipif(
    not _db_available(), reason="PostgreSQL 未运行（verify_phase4.sh 中覆盖）")


@pytest.fixture()
def api_client(monkeypatch):
    """mock LLM provider 的隔离客户端；settings 缓存按环境变量重建。"""
    monkeypatch.setenv("JWT_SECRET", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.delenv("LLM_MOCK_SCENARIO", raising=False)
    reset_settings()
    reset_stores()
    client = TestClient(create_app())
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})  # SEC-03
    yield client
    reset_settings()
    reset_stores()


def _login(client, email="inv@test.com", role="investigator") -> dict:
    """登录并把 Bearer token 挂到该 client 的后续请求上。"""
    seed_user(email, "Passw0rd!123", role=role)
    resp = client.post("/api/v1/auth/login",
                       json={"email": email, "password": "Passw0rd!123"})
    body = resp.json()
    client.headers.update({"Authorization": f"Bearer {body['access_token']}"})
    return body


def _demo_seed() -> str:
    from backend.graph_builder.data_source import FixtureTxProvider

    return FixtureTxProvider.load().seed_addresses[0]


def _poll_terminal(client, jid, timeout_s=15.0) -> dict:
    deadline = time.time() + timeout_s
    payload = {}
    while time.time() < deadline:
        payload = client.get(f"/api/v1/judgments/{jid}").json()
        if payload.get("status") in ("completed", "failed"):
            return payload
        time.sleep(0.2)
    return payload


def _clear_judgments() -> None:
    from backend.core.config import get_settings
    from backend.models.base import Judgment

    with Session(get_db_engine()) as session:
        session.execute(delete(Judgment))
        session.commit()
    # 判决缓存在 Redis（跨测试进程存活）——不清会导致失败路径用例
    # 命中上次的成功判决，绕过 LLM 直接 completed
    try:
        import redis

        client = redis.Redis.from_url(get_settings().redis_url,
                                      socket_connect_timeout=1)
        keys = list(client.scan_iter(match="gb-v1:*", count=100))
        if keys:
            client.delete(*keys)
    except Exception:  # noqa: BLE001, S110 — 无 Redis 时缓存本就是进程内的
        pass


# ---------------------------------------------------------------------------
# 错误契约与认证（无 DB 依赖）
# ---------------------------------------------------------------------------
class TestAuthAndErrors:
    def test_be01_login_shape(self, api_client):
        seed_user("admin@test.com", "AdminP@ss1", role="admin")
        r = api_client.post("/api/v1/auth/login",
                            json={"email": "admin@test.com",
                                  "password": "AdminP@ss1"})
        assert r.status_code == 200
        body = r.json()
        assert body["token_type"] == "bearer"
        assert len(body["access_token"].split(".")) == 3
        assert body["user"]["role"] == "admin"

    def test_be02_wrong_password_problem_details(self, api_client):
        seed_user("inv@test.com", "RightPass1")
        r = api_client.post("/api/v1/auth/login",
                            json={"email": "inv@test.com",
                                  "password": "WrongPass"})
        assert r.status_code == 401
        assert r.headers["content-type"].startswith("application/problem+json")
        body = r.json()
        for key in ("type", "title", "status", "detail", "instance",
                    "error_code"):
            assert key in body, key
        assert body["error_code"] == "INVALID_CREDENTIALS"

    def test_be03_unknown_email_same_response(self, api_client):
        seed_user("inv@test.com", "RightPass1")
        wrong_pw = api_client.post("/api/v1/auth/login",
                                   json={"email": "inv@test.com",
                                         "password": "x"}).json()["detail"]
        ghost = api_client.post("/api/v1/auth/login",
                                json={"email": "ghost@nowhere.com",
                                      "password": "x"}).json()["detail"]
        assert wrong_pw == ghost  # 防枚举

    def test_be05_write_without_token_401(self, api_client):
        r = api_client.post("/api/v1/cases", json={"title": "x"})
        assert r.status_code == 401
        assert r.headers["content-type"].startswith("application/problem+json")

    def test_be06_investigator_admin_endpoint_403(self, api_client):
        _login(api_client)
        r = api_client.get("/api/v1/audit-logs")
        assert r.status_code == 403
        assert r.json()["error_code"] == "FORBIDDEN"

    def test_be42_unknown_judgment_404(self, api_client):
        _login(api_client)
        r = api_client.get("/api/v1/judgments/nonexistent-id")
        assert r.status_code == 404
        assert r.json()["error_code"] == "NOT_FOUND"


class TestAnalyzeValidation:
    def test_be10_hops_out_of_range_422(self, api_client):
        _login(api_client)
        r = api_client.post("/api/v1/addresses/analyze",
                            json={"address": _demo_seed(), "hops": 4})
        assert r.status_code == 422
        assert "between 1 and 3" in r.json()["detail"]

    def test_be11_bad_checksum_address_422(self, api_client):
        _login(api_client)
        # 合法 charset 内破坏末位字符：单字符替换必然破坏 polymod 校验和
        # （issue #24 后校验顺序细化，含非法字符的串会先报 invalid character）
        seed = _demo_seed()
        bad = seed[:-1] + ("q" if seed[-1] != "q" else "p")
        r = api_client.post("/api/v1/addresses/analyze",
                            json={"address": bad})
        assert r.status_code == 422
        assert "checksum" in r.json()["detail"].lower()

    def test_be09_anon_non_demo_address_403(self, api_client):
        r = api_client.post("/api/v1/addresses/analyze",
                            json={"address":
                                  "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"})
        # 合法校验和的经典 BIP173 示例地址，但不在演示白名单内
        assert r.status_code == 403
        assert r.json()["error_code"] == "DEMO_ADDRESS_REQUIRED"

    def test_be28_anon_rate_limit_429_with_retry_after(self):
        # 限流检查先于 DB 写入——无 Postgres 环境同样可验证（BE-28）
        import os

        monkey_env = {"ANON_RATE_PER_MIN": "3", "JWT_SECRET":
                      "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "LLM_PROVIDER": "mock"}
        saved = {k: os.environ.get(k) for k in monkey_env}
        os.environ.update(monkey_env)
        reset_settings()
        reset_stores()
        try:
            client = TestClient(create_app())
            client.headers.update({"X-Requested-With": "XMLHttpRequest"})
            addr = _demo_seed()
            codes = []
            retry_after = None
            for _ in range(5):
                resp = client.post("/api/v1/addresses/analyze",
                                   json={"address": addr})
                codes.append(resp.status_code)
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
            assert 429 in codes                      # 第 4 次起触发
            assert all(c in (202, 500) for c in codes[:3])  # 前 3 次未被限流
            assert retry_after is not None           # 带 Retry-After 头
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            reset_settings()
            reset_stores()


# ---------------------------------------------------------------------------
# 分析管线（需 DB）
# ---------------------------------------------------------------------------
class TestPipeline:
    @requires_db
    def test_be08_analyze_demo_address_202(self, api_client):
        _clear_judgments()
        r = api_client.post("/api/v1/addresses/analyze",
                            json={"address": _demo_seed()})
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "queued"
        assert body["poll_url"] == f"/api/v1/judgments/{body['judgment_id']}"

    @requires_db
    def test_be13_poll_completed_full_fields(self, api_client):
        _clear_judgments()
        r = api_client.post("/api/v1/addresses/analyze",
                            json={"address": _demo_seed(), "hops": 2})
        jid = r.json()["judgment_id"]
        payload = _poll_terminal(api_client, jid)

        assert payload["status"] == "completed", payload
        assert payload["risk_level"] in {"high", "medium", "low", "no_match"}
        assert payload["model"] and payload["prompt_version"] \
            and payload["builder_version"]
        assert isinstance(payload["latency_ms"], int)
        snapshot_ids = ({n["id"] for n in payload["subgraph"]["nodes"]}
                        | {e["id"] for e in payload["subgraph"]["edges"]})
        assert len(snapshot_ids) > 0
        for eid in payload["evidence"]:  # D3 前缀 + 引用闭合（BE-13/验收6）
            prefix = eid.split(":", 1)[0]
            assert prefix in ("addr", "tx", "edge"), eid
            assert eid in snapshot_ids
        for node in payload["subgraph"]["nodes"]:
            assert node["id"].startswith(("addr:", "tx:"))

    @requires_db
    def test_be14_failed_path_error_contract(self, api_client, monkeypatch):
        _clear_judgments()
        monkeypatch.setenv("LLM_MOCK_SCENARIO", "invalid_evidence_all_retries")
        r = api_client.post("/api/v1/addresses/analyze",
                            json={"address": _demo_seed()})
        jid = r.json()["judgment_id"]
        payload = _poll_terminal(api_client, jid)

        assert payload["status"] == "failed", payload
        assert payload["error_code"] == "LLM_VALIDATION_FAILED"
        assert payload["retry_count"] == 2      # MAX_RETRIES=3 → 重试 2 次
        assert payload["failed_at"]
        assert payload["risk_level"] is None
        assert payload["confidence"] is None

    @requires_db
    def test_be12_idempotent_same_params_single_active(self, api_client,
                                                       monkeypatch):
        _clear_judgments()

        async def _slow(judgment_id):
            await asyncio.sleep(30)  # 保持进行中窗口；测试结束不等待

        monkeypatch.setattr("backend.services.orchestration.run_analysis",
                            _slow)
        addr = _demo_seed()
        r1 = api_client.post("/api/v1/addresses/analyze",
                             json={"address": addr})
        r2 = api_client.post("/api/v1/addresses/analyze",
                             json={"address": addr})
        assert r1.status_code == 202
        assert r2.status_code == 200
        assert r1.json()["judgment_id"] == r2.json()["judgment_id"]

        from sqlalchemy import func as sa_func
        from sqlalchemy import select as sa_select

        from backend.api.app import get_db_engine
        from backend.models.base import Judgment

        with Session(get_db_engine()) as session:
            active = session.execute(
                sa_select(sa_func.count(Judgment.id))
                .where(Judgment.address == addr)).scalar_one()
            # 清理慢任务残留，避免阻塞后续用例的 partial index
            session.execute(delete(Judgment).where(Judgment.address == addr))
            session.commit()
        assert active == 1

    @requires_db
    def test_be47_replay_after_terminal_refused(self, api_client):
        _clear_judgments()
        r = api_client.post("/api/v1/addresses/analyze",
                            json={"address": _demo_seed()})
        jid = r.json()["judgment_id"]
        payload = _poll_terminal(api_client, jid)
        assert payload["status"] == "completed"

        from sqlalchemy.orm import Session

        from backend.api.app import get_db_engine
        from backend.models.base import Judgment
        from backend.services.orchestration import run_analysis

        result = asyncio.run(run_analysis(jid))
        assert result == "skipped"  # 终态重放被状态守卫拒绝
        with Session(get_db_engine()) as session:
            status = session.get(Judgment, jid).status
        assert status == "completed"

    @requires_db
    def test_be40_zombie_reclaimed_as_timeout(self, api_client):
        from datetime import datetime, timedelta

        from sqlalchemy.orm import Session

        from backend.api.app import get_db_engine
        from backend.models.base import Judgment
        from backend.services.orchestration import reclaim_zombies

        jid = "zombie-test-0001"
        cutoff = datetime.now(UTC) - timedelta(seconds=180)
        with Session(get_db_engine()) as session:
            session.add(Judgment(id=jid, address=_demo_seed(),
                                 hops=1, time_window_days=90,
                                 status="processing"))
            session.commit()
            # 绕过 ORM onupdate 直写过期 updated_at
            session.execute(text(
                "UPDATE judgments SET updated_at = :ts WHERE id = :jid"),
                {"ts": cutoff, "jid": jid})
            session.commit()

            reclaimed = reclaim_zombies(session, older_than_seconds=120)
            assert jid in reclaimed
            row = session.get(Judgment, jid)
            assert row.status == "failed"
            assert row.error_code == "TASK_TIMEOUT"
            assert row.failed_at is not None

    @requires_db
    def test_be33_34_subgraph_latest_and_historical(self, api_client):
        _clear_judgments()
        from backend.core.btc_address import encode_bech32m_address
        from backend.models.base import Judgment

        snap_old = {"nodes": [{"id": "addr:old", "kind": "address",
                               "first_layer": 0}], "edges": []}
        snap_new = {"nodes": [{"id": "addr:new", "kind": "address",
                               "first_layer": 0}], "edges": []}
        addr = encode_bech32m_address(
            hashlib.sha256(b"subgraph-demo").digest()[:20])
        with Session(get_db_engine()) as session:
            session.add(Judgment(id="j-old", address=addr, hops=1,
                                 time_window_days=90, status="completed",
                                 subgraph_snapshot=snap_old))
            session.commit()
            session.add(Judgment(id="j-new", address=addr, hops=1,
                                 time_window_days=90, status="completed",
                                 subgraph_snapshot=snap_new))
            session.commit()

        _login(api_client)
        latest = api_client.get(
            f"/api/v1/addresses/{addr}/subgraph").json()
        assert latest["nodes"][0]["id"] == "addr:new"  # 默认最新 completed

        older = api_client.get(
            f"/api/v1/addresses/{addr}/subgraph",
            params={"judgment_id": "j-old"}).json()
        assert older["nodes"][0]["id"] == "addr:old"

    @requires_db
    def test_be21_22_patterns_pagination_and_filter(self, api_client):
        _login(api_client)
        r = api_client.get("/api/v1/patterns",
                           params={"page_size": 500})
        assert r.status_code == 422  # page_size 上限 100

        r = api_client.get("/api/v1/patterns",
                           params={"evidence_grade": "A", "page_size": 5})
        assert r.status_code == 200
        body = r.json()
        for key in ("items", "total", "page", "page_size", "pages"):
            assert key in body
        assert all(item["evidence_grade"] == "A" for item in body["items"])

    @requires_db
    def test_be20_readyz_503_when_db_down(self, api_client, monkeypatch):
        monkeypatch.setenv("DATABASE_URL",
                           "postgresql://pt:pt@localhost:5999/none")
        reset_settings()
        close_db_engine()
        try:
            r = api_client.get("/readyz")
            assert r.status_code == 503
            assert "db unreachable" in r.json()["detail"]
        finally:
            reset_settings()
            close_db_engine()


# ---------------------------------------------------------------------------
# 状态机纯逻辑（D5 / BE-47）
# ---------------------------------------------------------------------------
class TestStateMachineGuards:
    def test_terminal_states_immutable(self):
        from backend.models.base import InvalidStateTransition, assert_transition

        assert_transition("queued", "processing")
        assert_transition("processing", "completed")
        assert_transition("processing", "failed")
        with pytest.raises(InvalidStateTransition, match="terminal"):
            assert_transition("completed", "queued")
        with pytest.raises(InvalidStateTransition, match="terminal"):
            assert_transition("failed", "processing")
