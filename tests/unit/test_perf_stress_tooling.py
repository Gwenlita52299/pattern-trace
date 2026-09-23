"""压测工具链单测 — 池文件口径与 mock 受控延迟接线（stress-test-spec §2.3/§2.4）。

这两处都是「错了不会报错、只会静默跑出假数据」的地方，所以必须有回归：
- 池文件分层口径错误 → 基线把热集当全表（系统性高估容量）
- LLM_MOCK_DELAY_MS 接线断了 → W1 专线注入不了延迟，队列压不出堆积
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.llm_judge.providers import MockLLMClient, get_llm_client
from tests.performance import pool


def test_pool_roundtrip_and_tiers(tmp_path):
    path = pool.write_pool(tmp_path / "p.json", hot=["a", "b"], cold=["c"],
                           addresses=["bc1"],
                           meta={"scale": {"judgments_total": 10}})
    loaded = pool.load_pool(path)
    assert loaded[pool.HOT] == ["a", "b"]
    assert loaded[pool.COLD] == ["c"]
    assert loaded[pool.ADDRESSES] == ["bc1"]
    assert loaded["meta"]["scale"]["judgments_total"] == 10


def test_pool_requires_both_tiers_for_baseline(tmp_path):
    hot_only = pool.write_pool(tmp_path / "hot.json", hot=["a"], cold=[])
    assert pool.pool_usable_as_baseline(pool.load_pool(hot_only)) is False
    both = pool.write_pool(tmp_path / "both.json", hot=["a"], cold=["b"])
    assert pool.pool_usable_as_baseline(pool.load_pool(both)) is True


def test_pool_rejects_malformed_tier(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"hot": [1, 2], "cold": []}))
    with pytest.raises(ValueError):
        pool.load_pool(bad)
    not_object = tmp_path / "list.json"
    not_object.write_text(json.dumps(["a"]))
    with pytest.raises(ValueError):
        pool.load_pool(not_object)


def test_pool_sample_and_address():
    payload = {"hot": ["h1"], "cold": ["c1", "c2"], pool.ADDRESSES: ["bc1"]}
    assert pool.sample(payload, pool.HOT) == "h1"
    assert pool.sample(payload, pool.COLD) in ("c1", "c2")
    assert pool.sample(payload, "missing") is None
    assert pool.sample_address(payload) == "bc1"
    assert pool.sample_address({}) is None


def test_mock_delay_env_wired(monkeypatch):
    """LLM_MOCK_DELAY_MS → MockLLMClient.fixed_delay_ms（W1 专线依赖）。"""
    settings = SimpleNamespace(llm_provider="mock", llm_model="test-model")
    monkeypatch.setenv("LLM_MOCK_DELAY_MS", "25")
    monkeypatch.setenv("LLM_MOCK_SCENARIO", "valid_high")
    client = get_llm_client(settings)
    assert isinstance(client, MockLLMClient)
    assert client.fixed_delay_ms == 25
    assert client.scenario == "valid_high"

    monkeypatch.delenv("LLM_MOCK_DELAY_MS", raising=False)
    assert get_llm_client(settings).fixed_delay_ms == 0
