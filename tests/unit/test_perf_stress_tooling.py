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
from tests.performance import loadgen, pool, run_stress


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


# ------------------------------------------------- 开放模型生成器与拐点判定

def test_percentiles_interpolate_and_handle_empty():
    assert loadgen.percentiles([])["p95"] == 0.0
    assert loadgen.percentiles([7.0])["p99"] == 7.0
    series = [float(i) for i in range(1, 101)]          # 1..100
    got = loadgen.percentiles(series, qs=(50, 95, 99))
    assert abs(got["p50"] - 50.5) < 0.6
    assert abs(got["p95"] - 95.05) < 0.6
    assert abs(got["p99"] - 99.01) < 0.6


def test_classify_matches_spec_3_2():
    assert loadgen.classify(200, (), None) == "ok"
    assert loadgen.classify(200, ("nodes",), None) == "assert_missing_field"
    assert loadgen.classify(503, (), None) == "5xx"
    assert loadgen.classify(404, (), None) == "unexpected_4xx"   # 旧口径会漏掉
    assert loadgen.classify(429, (), None) == "unexpected_4xx"
    assert loadgen.classify(None, (), "ReadTimeout: x") == "timeout"
    assert loadgen.classify(None, (), "ConnectError: x") == "transport"


def test_schedule_is_open_model_and_deterministic():
    times = loadgen.schedule(rate=10, duration=10, warmup=0, poisson=False, seed=1)
    assert len(times) == 100                      # 10 RPS × 10s：到达数由 rate 决定
    assert times == sorted(times) and times[0] > 0
    assert abs(times[-1] - 10.0) < 0.2
    assert loadgen.schedule(10, 10, 0, False, 1) == times
    jittered = loadgen.schedule(10, 10, 0, True, 1)
    assert len(jittered) == 100 and jittered != times


def _step(rate, p95, err=0.0, achieved=None, gen_cpu=10.0):
    return {"target_rps": rate, "achieved_rps": achieved or rate,
            "error_rate": err, "p95_corrected_ms": p95,
            "knee_reasons": [], "knee": False,
            "generator_cpu_pct": gen_cpu}


def test_detect_knee_tail_latency_and_error_rate():
    # 尾延迟 3× 规则
    steps = [_step(25, 20.0), _step(50, 25.0), _step(100, 80.0)]
    knee = run_stress.detect_knee(steps, target_rps=25.0)
    assert knee["knee_at_rate"] == 100
    assert any("3×" in r for r in knee["reasons"])

    # 错误率抬头先触发
    steps = [_step(25, 20.0), _step(50, 21.0, err=0.02)]
    knee = run_stress.detect_knee(steps, target_rps=25.0)
    assert knee["knee_at_rate"] == 50


def test_detect_knee_reports_no_knee_and_generator_limit():
    steps = [_step(25, 20.0), _step(50, 22.0), _step(100, 24.0)]
    knee = run_stress.detect_knee(steps, target_rps=100.0)
    assert knee["knee_at_rate"] is None
    assert "未触拐点" in " ".join(knee["notes"])

    # 生成器饱和的档位必须被排除：它的校正延迟里混的是压测机排队，
    # 若计入拐点就会把「压测机不够」误报成「服务端到顶」
    limited = run_stress.detect_knee(
        [_step(25, 20.0), _step(50, 22.0, achieved=40.0, gen_cpu=92.0)],
        target_rps=50.0)
    assert limited["knee_at_rate"] is None
    assert limited["validated_max_rps"] == 25
    assert limited["invalid_steps"] == [50]
    assert any("压测机" in n for n in limited["notes"])


def test_generator_bound_step_keeps_its_own_reasons():
    steps = [_step(25, 20.0), _step(50, 22.0, gen_cpu=92.0)]
    run_stress.detect_knee(steps, target_rps=50.0)
    assert steps[1]["invalid_generator_bound"] is True
    assert steps[1]["knee"] is False
    assert any("生成器" in r for r in steps[1]["invalid_reasons"])


def test_knee_without_server_pressure_is_environment_limited():
    """服务端 CPU 很低却出现延迟抬升 → 不能宣布容量上限（本轮最重要的口径修正）。"""
    steps = [_step(200, 100.0), _step(250, 400.0)]
    steps[1]["server_cpu_pct"] = 0.3
    knee = run_stress.detect_knee(steps, target_rps=250.0)
    assert knee["knee_at_rate"] is None                  # 不可信拐点不进容量结论
    assert knee["untrusted_knee_rates"] == [250]
    assert steps[1]["knee_trustworthy"] is False
    assert any("服务端 CPU" in r for r in steps[1]["knee_reasons"])
    assert run_stress.decide_status(knee, 250.0) == "environment_limited"

    # 服务端确实吃满时，拐点才成立
    steps[1]["server_cpu_pct"] = 92.0
    knee = run_stress.detect_knee(steps, target_rps=250.0)
    assert knee["knee_at_rate"] == 250
    assert run_stress.decide_status(knee, 250.0) == "below"


def test_decide_status_four_states():
    assert run_stress.decide_status({"knee_at_rate": 300}, 250) == "meets"
    assert run_stress.decide_status({"knee_at_rate": 100}, 250) == "below"
    assert run_stress.decide_status({"untrusted_knee_rates": [250]}, 250) == \
        "environment_limited"
    assert run_stress.decide_status({"validated_max_rps": 300}, 250) == "meets"
    assert run_stress.decide_status({"validated_max_rps": 200}, 250) == "indeterminate"


def test_backend_cpu_pct_parses_docker_stats():
    obs = {"docker_stats": "\n".join([
        "pattern_trace-backend-1,5.66%,1.248GiB / 7.748GiB",
        "pattern_trace-db-1,0.00%,106MiB / 7.748GiB"])}
    assert run_stress.backend_cpu_pct(obs) == 5.66
    assert run_stress.backend_cpu_pct({}) is None
    assert run_stress.backend_cpu_pct({"docker_stats": "garbage"}) is None
    m = run_stress._step_metrics(
        {"config": {"rate": 50.0}, "aggregate": {}, "generator": {}}, obs)
    assert m["server_cpu_pct"] == 5.66


# ------------------------------------------------------------ 回归对比

def _ladder_summary(tag, steps):
    return {"kind": "ladder", "tag": tag, "profile": "ladder(hot)",
            "pool_tier": "hot", "hardware": {"cpu_count": 10},
            "pool_meta": {"scale": {"judgments_total": 10501}}, "steps": steps}


def _lstep(rate, p95, p99=None, err=0.0, achieved=None, ok=True):
    return {"target_rps": rate, "p95_corrected_ms": p95,
            "p99_corrected_ms": p99 if p99 is not None else p95 * 1.2,
            "poll_p95_corrected_ms": p95, "error_rate": err,
            "achieved_rps": achieved if achieved is not None else rate,
            "knee_trustworthy": ok, "invalid_generator_bound": not ok}


def test_series_from_ladder():
    series = run_stress.series_from_ladder(_ladder_summary("t", [
        _lstep(100, 90.0), _lstep(200, 140.0)]))
    assert set(series) == {"100 RPS", "200 RPS"}
    assert series["200 RPS"]["p95"] == 140.0
    assert series["200 RPS"]["trustworthy"] is True


def test_compatibility_rejects_mismatched_runs():
    base = _ladder_summary("base", [_lstep(100, 90.0)])
    assert run_stress.compatibility(base, base) == []

    other_kind = {"kind": "locust", "tag": "x", "profile": "read", "pool_tier": "hot"}
    assert any("归档类型" in p for p in run_stress.compatibility(other_kind, base))

    other_tier = _ladder_summary("t2", [_lstep(100, 90.0)])
    other_tier["pool_tier"] = "cold"
    assert any("池档位" in p for p in run_stress.compatibility(other_tier, base))

    other_scale = _ladder_summary("t3", [_lstep(100, 90.0)])
    other_scale["pool_meta"] = {"scale": {"judgments_total": 100000}}
    assert any("数据规模" in p for p in run_stress.compatibility(other_scale, base))

    other_cpu = _ladder_summary("t4", [_lstep(100, 90.0)])
    other_cpu["hardware"] = {"cpu_count": 4}
    assert any("硬件核数" in p for p in run_stress.compatibility(other_cpu, base))


def test_build_comparison_self_compare_is_clean():
    """spec 验收：同一份自比必须报 0%（否则回归门禁会假报警）。"""
    summary = _ladder_summary("t", [_lstep(100, 90.0), _lstep(200, 140.0, err=0.002)])
    series = run_stress.series_from_ladder(summary)
    comparison = run_stress.build_comparison(summary, summary, series, series)
    assert comparison["verdict"] == "clean"
    assert comparison["regressions"] == 0
    assert all(row.get("p95_delta") in (0.0, None) for row in comparison["rows"])


def test_build_comparison_flags_regressions():
    base = _ladder_summary("base", [_lstep(100, 100.0, err=0.0, achieved=100.0)])
    cand = _ladder_summary("cand", [_lstep(100, 140.0, err=0.02, achieved=70.0)])
    comparison = run_stress.build_comparison(
        cand, base, run_stress.series_from_ladder(cand),
        run_stress.series_from_ladder(base))
    assert comparison["verdict"] == "regressed"
    reasons = " ".join(comparison["rows"][0]["regressions"])
    assert "p95 劣化 +40.0%" in reasons          # >30% 阈值
    assert "错误率上升" in reasons
    assert "吞吐下降" in reasons


def test_build_comparison_handles_added_removed_and_untrusted():
    base = _ladder_summary("base", [_lstep(100, 100.0), _lstep(200, 150.0)])
    cand = _ladder_summary("cand", [_lstep(100, 150.0), _lstep(300, 200.0)])
    comparison = run_stress.build_comparison(
        cand, base, run_stress.series_from_ladder(cand),
        run_stress.series_from_ladder(base))
    by_key = {row["key"]: row for row in comparison["rows"]}
    assert by_key["300 RPS"]["status"] == "added"
    assert by_key["200 RPS"]["status"] == "removed"

    # 数据本身无效的档位：仍列出劣化，但必须带 note 提示不可当真
    base = _ladder_summary("base", [_lstep(100, 100.0)])
    cand = _ladder_summary("cand", [_lstep(100, 300.0, ok=False)])
    comparison = run_stress.build_comparison(
        cand, base, run_stress.series_from_ladder(cand),
        run_stress.series_from_ladder(base))
    row = comparison["rows"][0]
    assert row["status"] == "regressed" and row["trustworthy"] is False
    assert "数据本身无效" in row["note"]


def test_aggregate_endpoints_excludes_warmup_and_locust_aggregate():
    rows = [
        {"Name": "warmup:login", "Request Count": "10", "Failure Count": "0",
         "95%": "210", "99%": "210", "Max Response Time": "220", "Requests/s": "0.1"},
        {"Name": "GET /api/v1/cases", "Request Count": "5", "Failure Count": "0",
         "95%": "12", "99%": "12", "Max Response Time": "13", "Requests/s": "0.1"},
    ]
    names = {i["name"] for i in run_stress._aggregate_endpoints(rows)}
    assert names == {"GET /api/v1/cases"}


def test_step_metrics_extracts_without_judging():
    m = run_stress._step_metrics({
        "config": {"rate": 50.0},
        "aggregate": {"achieved_rps": 49.5, "error_rate": 0.0,
                      "corrected_ms": {"p95": 30.0, "p99": 60.0},
                      "latency_ms": {"p95": 28.0}},
        "generator": {"cpu_utilization_pct": 12.0, "queued_arrivals": 0,
                      "peak_inflight": 8},
        "by_endpoint": {run_stress.POLL_ENDPOINT: {
            "corrected_ms": {"p95": 31.0, "p99": 61.0}, "error_rate": 0.0}},
        "samples_measured": 2970,
    })
    assert m["target_rps"] == 50.0 and m["achieved_rps"] == 49.5
    assert m["poll_p95_corrected_ms"] == 31.0 and m["poll_p99_corrected_ms"] == 61.0
    assert m["knee"] is False and m["knee_reasons"] == []   # 判定不在这一步
