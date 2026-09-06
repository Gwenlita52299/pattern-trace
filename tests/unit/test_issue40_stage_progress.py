"""issue #40：分析阶段进度上报 — worker 写 Redis，API 轮询读取。

- _report_stage best-effort：Redis 不可达不抛错
- _current_stage：processing 行读 Redis，终态行恒 None
"""
import pytest


class FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, value, ex=None):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)


@pytest.fixture
def fake_redis(monkeypatch):
    import redis as real_redis

    fake = FakeRedis()
    monkeypatch.setattr(
        real_redis.Redis, "from_url",
        classmethod(lambda cls, *a, **kw: fake))
    return fake


class TestReportStage:
    def test_writes_stage_key(self, fake_redis):
        from backend.core.config import get_settings
        from backend.services.orchestration import _report_stage

        _report_stage("j1", "llm_judging", get_settings())
        assert fake_redis.store["judgment:stage:j1"] == "llm_judging"

    def test_redis_unavailable_is_silent(self, monkeypatch):
        import redis as real_redis

        def boom(*a, **kw):
            raise ConnectionError("no redis")

        monkeypatch.setattr(real_redis.Redis, "from_url",
                            classmethod(boom))
        from backend.core.config import get_settings
        from backend.services.orchestration import _report_stage

        _report_stage("j2", "building_subgraph", get_settings())  # 不应抛错


class TestCurrentStage:
    def _row(self, status):
        from types import SimpleNamespace

        return SimpleNamespace(id="j1", status=status)

    def test_processing_row_reads_redis(self, fake_redis):
        from backend.api.app import _current_stage
        from backend.services.orchestration import _report_stage
        from backend.core.config import get_settings

        _report_stage("j1", "wl_rerank", get_settings())
        assert _current_stage(self._row("processing")) == "wl_rerank"

    def test_queued_row_without_stage_is_none(self, fake_redis):
        from backend.api.app import _current_stage

        assert _current_stage(self._row("queued")) is None

    def test_terminal_completed_row_is_none_even_with_stage_key(self, fake_redis):
        from backend.api.app import _current_stage
        from backend.core.config import get_settings
        from backend.services.orchestration import _report_stage

        _report_stage("j1", "llm_judging", get_settings())
        assert _current_stage(self._row("completed")) is None

    def test_failed_row_keeps_last_stage(self, fake_redis):
        """失败行的最后上报阶段即失败发生阶段（前端据实标注失败步骤）。"""
        from backend.api.app import _current_stage
        from backend.core.config import get_settings
        from backend.services.orchestration import _report_stage

        _report_stage("j1", "llm_judging", get_settings())
        assert _current_stage(self._row("failed")) == "llm_judging"

    def test_failed_row_without_stage_key_is_none(self, fake_redis):
        from backend.api.app import _current_stage

        assert _current_stage(self._row("failed")) is None


# ---------------------------------------------------------------------------
# e2e：完整管线的阶段序列（正例）与失败阶段归因（反例）
# 需要 PostgreSQL（fixture 图数据 + 真实 DB 状态机）
# ---------------------------------------------------------------------------
def _db_ready() -> bool:
    try:
        from sqlalchemy import text

        from backend.api.app import get_db_engine

        with get_db_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


requires_db = pytest.mark.skipif(
    not _db_ready(), reason="PostgreSQL 未运行")


@pytest.fixture()
def api_client(monkeypatch):
    import time as _time

    from fastapi.testclient import TestClient

    from backend.api.app import create_app, reset_stores
    from backend.core.config import reset_settings

    monkeypatch.setenv(
        "JWT_SECRET",
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.delenv("LLM_MOCK_SCENARIO", raising=False)
    reset_settings()
    reset_stores()
    client = TestClient(create_app())
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    yield client
    _time.sleep(0.1)
    reset_settings()
    reset_stores()


def _demo_seed():
    from tests.unit.test_backend_api import _demo_seed as _seed

    return _seed()


def _clear_judgments(client=None):
    from tests.unit.test_backend_api import _clear_judgments as _clear

    _clear()


def _wait_terminal(client, jid, timeout=30):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/v1/judgments/{jid}").json()
        if payload["status"] in ("completed", "failed"):
            return payload
        time.sleep(0.1)
    raise AssertionError(f"judgment {jid} not terminal within {timeout}s")


@requires_db
class TestStagePipelineE2E:
    def test_full_pipeline_reports_all_four_stages_in_order(
            self, api_client, monkeypatch, fake_redis):
        """正例：四个阶段按管线顺序逐一上报。"""
        from backend.api.app import create_app
        from backend.models.base import Judgment

        _clear_judgments()
        recorded: list[tuple[str, str]] = []
        import backend.services.orchestration as orch

        monkeypatch.setattr(
            orch, "_report_stage",
            lambda jid, stage, settings: recorded.append((jid, stage)))
        # 知识库空表时 wl_rerank 不触发（空召回提前返回）——
        # 造一条确定性召回，保证精排阶段一定执行
        fake_row = {
            "id": "11111111-1111-1111-1111-111111111111",
            "name": "synth_stage_test", "dist": 0.2, "struct_sim": 0.8,
            "sem_sim": 0.8,
            "canonical_subgraph": {"nodes": [
                {"id": "addr:p1", "kind": "address"},
                {"id": "addr:p2", "kind": "address"},
            ], "edges": [
                {"id": "edge:addr:p1->addr:p2", "source": "addr:p1",
                 "target": "addr:p2"},
            ]},
            "evidence_grade": "A", "source": "synthetic",
            "provenance": "synthetic", "description": "stage test",
        }
        monkeypatch.setattr(
            orch.Retriever, "_hybrid_recall",
            lambda self, svec, evec, *, exclude_ids, limit: [fake_row])

        r = api_client.post("/api/v1/addresses/analyze",
                            json={"address": _demo_seed()})
        assert r.status_code == 202, r.text
        jid = r.json()["judgment_id"]
        payload = _wait_terminal(api_client, jid)
        assert payload["status"] == "completed", payload

        stages = [s for j, s in recorded if j == jid]
        assert stages == ["building_subgraph", "retrieval_topk",
                          "wl_rerank", "llm_judging"]

    def test_failed_at_llm_stage_is_attributed(
            self, api_client, monkeypatch, fake_redis):
        """反例：LLM 阶段失败 → 失败标在 llm_judging 而非第一阶段。"""
        from backend.api.app import create_app

        _clear_judgments()
        monkeypatch.setenv("LLM_MOCK_SCENARIO", "invalid_evidence_all_retries")

        r = api_client.post("/api/v1/addresses/analyze",
                            json={"address": _demo_seed()})
        assert r.status_code == 202, r.text
        jid = r.json()["judgment_id"]
        payload = _wait_terminal(api_client, jid)
        assert payload["status"] == "failed"
        assert payload["error_code"] == "LLM_VALIDATION_FAILED"
        assert payload["stage"] == "llm_judging"

    def test_failed_at_building_stage_is_attributed(
            self, api_client, monkeypatch, fake_redis):
        """反例：子图构建阶段失败 → 失败标在 building_subgraph。"""
        from backend.api.app import create_app
        from backend.services.orchestration import DataSourceUnavailable

        _clear_judgments()

        class BoomBuilder:
            def __init__(self, **kwargs):
                pass

            def build(self, *args, **kwargs):
                raise DataSourceUnavailable("tx provider down")

        import backend.services.orchestration as orch

        monkeypatch.setattr(orch, "GraphBuilder", BoomBuilder)

        r = api_client.post("/api/v1/addresses/analyze",
                            json={"address": _demo_seed()})
        assert r.status_code == 202, r.text
        jid = r.json()["judgment_id"]
        payload = _wait_terminal(api_client, jid)
        assert payload["status"] == "failed"
        assert payload["error_code"] == "ESPLORA_UNAVAILABLE"
        assert payload["stage"] == "building_subgraph"
