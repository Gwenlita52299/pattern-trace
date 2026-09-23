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
from tests.performance import pool, run_stress


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


# ------------------------------------------------------------ 报告渲染

def _summary(**overrides) -> dict:
    base = {
        "tag": "dev-read-hot", "profile": "read", "pool_tier": "hot",
        "host": "http://localhost:8000", "vu": 10, "duration": "60s",
        "git_sha": "abc1234", "guard": "passed",
        "pool_usable_as_baseline": True,
        "pool_meta": {"scale": {"judgments_total": 10501}, "sizes": {"hot": 500}},
        "pool_origin": "output/stress/pools/dev-pool.json",
        "observations_file": "observations.json",
        "locust": {"total_requests": 749, "total_failures": 0, "error_rate": 0.0,
                   "p95_ms": 89.0,
                   "by_name": {"GET /api/v1/judgments/[id] (hot pool)":
                               {"requests": 436, "failures": 0, "p95_ms": 12.0}}},
    }
    base.update(overrides)
    return base


def test_report_renders_self_contained_html():
    html = run_stress.render_html(_summary(), {"queues": {"q_analysis": "0"}},
                                  [{"Name": "GET /api/v1/cases", "Request Count": "10",
                                    "Failure Count": "0", "95%": "12",
                                    "Max Response Time": "20", "Requests/s": "0.2"}],
                                  [], [])
    assert "dev-read-hot" in html
    assert "✓ 全部达标" in html
    assert "12.0ms" in html or "12ms" in html
    assert 'class="verdict bad"' not in html
    assert "<script" not in html and "src=" not in html   # 自包含：无外部资源/脚本


def test_report_flags_failures_with_amber_not_red():
    html = run_stress.render_html(
        _summary(locust={"total_requests": 100, "total_failures": 5,
                         "error_rate": 0.05, "p95_ms": 900.0,
                         "by_name": {"GET /api/v1/judgments/[id] (hot pool)":
                                     {"requests": 60, "failures": 5, "p95_ms": 420.0}}}),
        {}, [], [], [{"Name": "GET /x", "Error": "unexpected 404", "Occurrences": "5"}])
    assert 'class="verdict bad"' in html
    assert "⚠ 存在未达标项" in html
    assert "热档轮询 p95 ≤ 300ms" in html and "420.0ms" in html
    assert "unexpected 404" in html
    assert "#f0b429" in html          # 琥珀只用于未达标
    assert "#e74c3c" not in html and "red" not in html


def test_report_escapes_hostile_names():
    html = run_stress.render_html(
        _summary(), {},
        [{"Name": "<script>alert(1)</script>", "Request Count": "1",
          "Failure Count": "0", "95%": "1", "Max Response Time": "1",
          "Requests/s": "0"}], [], [])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_report_normalizes_and_aggregates_endpoints():
    rows = [
        {"Name": "/api/v1/addresses/STRESS0001x/subgraph", "Request Count": "3",
         "Failure Count": "0", "95%": "10", "Max Response Time": "11", "Requests/s": "1"},
        {"Name": "/api/v1/addresses/STRESS0002x/subgraph", "Request Count": "2",
         "Failure Count": "1", "95%": "30", "Max Response Time": "44", "Requests/s": "2"},
        {"Name": "GET /api/v1/cases?page=3", "Request Count": "4", "Failure Count": "0",
         "95%": "5", "Max Response Time": "6", "Requests/s": "1"},
        {"Name": "Aggregated", "Request Count": "9", "Failure Count": "1",
         "95%": "30", "Max Response Time": "44", "Requests/s": "4"},
    ]
    items = {i["name"]: i for i in run_stress._aggregate_endpoints(rows)}
    sub = items["/api/v1/addresses/[addr]/subgraph"]
    assert sub["requests"] == 5 and sub["failures"] == 1
    assert sub["p95"] == 30 and sub["max"] == 44        # 组内最大（保守）
    assert "GET /api/v1/cases?page=N" in items
    assert "Aggregated" not in items                    # 聚合行不重复计入


def test_sparkline_handles_missing_history():
    assert "无历史采样" in run_stress._sparkline([])
    assert "无历史采样" in run_stress._sparkline([1.0])
    svg = run_stress._sparkline([0.0, 5.0, 2.0])
    assert svg.startswith("<svg") and "polyline" in svg
