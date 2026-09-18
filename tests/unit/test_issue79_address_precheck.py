"""issue #79 analyze 入口地址活跃度预检测试。

- LiveEsploraProvider.address_stats：单请求 /address/:addr/stats（_fetch 打桩）
- 预检 helper：超阈值 422 ADDRESS_TOO_ACTIVE、阈值内放行、
  Esplora 故障 fail-open、stats 缺字段放行、fixture 模式跳过
- 端点契约：live 模式下高活跃地址 analyze → 422（拒绝发生在落库前，无需 DB）
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api.app import _precheck_address_activity, reset_stores
from backend.core.config import reset_settings
from backend.core.errors import ProblemError
from backend.graph_builder.data_source import LiveEsploraProvider


# ---------------------------------------------------------------------------
# provider 层：address_stats 单请求路径
# ---------------------------------------------------------------------------
class TestAddressStats:
    def test_single_stats_request(self):
        """预检只发一次 GET /address/:addr/stats，不触发分页枚举。"""
        p = LiveEsploraProvider("https://mempool.space/api")
        p.calls: list[str] = []

        def _fetch(path: str):
            p.calls.append(path)
            return {"tx_count": 489724}

        p._fetch = _fetch
        stats = p.address_stats("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
        assert stats["tx_count"] == 489724
        assert p.calls == ["/address/1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa/stats"]


# ---------------------------------------------------------------------------
# helper 层：_precheck_address_activity
# ---------------------------------------------------------------------------
def _live_settings(tx_limit: int = 200):
    return SimpleNamespace(
        graph_data_mode="live",
        esplora_api_url="https://mempool.space/api",
        esplora_max_pages=40,
        address_tx_count_limit=tx_limit)


@pytest.fixture()
def no_redis(monkeypatch):
    """预检建 provider 不做真实 Redis ping（无 Redis 是合法形态）。"""
    monkeypatch.setattr("backend.graph_builder.data_source._sync_redis",
                        lambda: None)


class TestPrecheckHelper:
    def test_over_limit_raises_422(self, no_redis, monkeypatch):
        def fake_stats(self, address):
            return {"tx_count": 489724}

        monkeypatch.setattr(LiveEsploraProvider, "address_stats", fake_stats)
        with pytest.raises(ProblemError) as exc_info:
            _precheck_address_activity("bc1qseed", _live_settings())
        err = exc_info.value
        assert err.status == 422
        assert err.error_code == "ADDRESS_TOO_ACTIVE"
        assert "489724" in err.detail

    def test_under_limit_passes(self, no_redis, monkeypatch):
        def fake_stats(self, address):
            return {"tx_count": 42}

        monkeypatch.setattr(LiveEsploraProvider, "address_stats", fake_stats)
        assert _precheck_address_activity("bc1qseed", _live_settings()) is None

    def test_esplora_failure_is_fail_open(self, no_redis, monkeypatch):
        """预检本身失败放行：分页截断与 job timeout 是第二道防线。"""

        def broken_stats(self, address):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(LiveEsploraProvider, "address_stats", broken_stats)
        assert _precheck_address_activity("bc1qseed", _live_settings()) is None

    def test_missing_tx_count_passes(self, no_redis, monkeypatch):
        monkeypatch.setattr(LiveEsploraProvider, "address_stats",
                            lambda self, a: {"unrelated": 1})
        assert _precheck_address_activity("bc1qseed", _live_settings()) is None

    def test_fixture_mode_skips_provider(self, monkeypatch):
        """fixture 模式连 provider 都不应构建（确定性图无活跃度概念）。"""

        def forbidden_init(self, *args, **kwargs):
            raise AssertionError("fixture mode must not build a live provider")

        monkeypatch.setattr(LiveEsploraProvider, "__init__", forbidden_init)
        settings = SimpleNamespace(graph_data_mode="fixture",
                                   address_tx_count_limit=200)
        assert _precheck_address_activity("bc1qseed", settings) is None


# ---------------------------------------------------------------------------
# 端点契约：拒绝发生在 Judgment 落库前（无 DB 环境可验证）
# ---------------------------------------------------------------------------
class TestAnalyzePrecheck:
    def test_analyze_too_active_address_422(self, monkeypatch):
        addr = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"  # BIP173 示例地址
        monkey_env = {
            "GRAPH_DATA_MODE": "live",
            "JWT_SECRET":
                "0123456789abcdef0123456789abcdef"
                "0123456789abcdef0123456789abcdef",
            "LLM_PROVIDER": "mock",
        }
        saved = {k: os.environ.get(k) for k in monkey_env}
        os.environ.update(monkey_env)
        reset_settings()
        reset_stores()
        try:
            monkeypatch.setattr(
                LiveEsploraProvider, "address_stats",
                lambda self, address: {"tx_count": 489724})

            from backend.api.app import create_app, seed_user

            client = TestClient(create_app())
            client.headers.update({"X-Requested-With": "XMLHttpRequest"})
            seed_user("inv@test.com", "Passw0rd!123")
            resp = client.post("/api/v1/auth/login",
                               json={"email": "inv@test.com",
                                     "password": "Passw0rd!123"})
            client.headers.update({
                "Authorization":
                    f"Bearer {resp.json()['access_token']}"})

            r = client.post("/api/v1/addresses/analyze",
                            json={"address": addr})
            assert r.status_code == 422
            body = r.json()
            assert body["error_code"] == "ADDRESS_TOO_ACTIVE"
            assert "489724" in body["detail"]
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            reset_settings()
            reset_stores()
