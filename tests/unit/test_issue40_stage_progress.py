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

    def test_terminal_row_is_none_even_with_stage_key(self, fake_redis):
        from backend.api.app import _current_stage
        from backend.core.config import get_settings
        from backend.services.orchestration import _report_stage

        _report_stage("j1", "llm_judging", get_settings())
        assert _current_stage(self._row("completed")) is None
        assert _current_stage(self._row("failed")) is None
