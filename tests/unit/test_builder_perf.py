"""GraphBuilder 性能与内存测试 — graph-builder-spec §9（GB-22 / GB-23）。"""
import statistics
import time
import tracemalloc

from backend.graph_builder.builder import GraphBuilder

from test_builder import MockTx, SEED, make_provider, out


def _fanout_fixture(n_txs: int) -> dict:
    return {SEED: [
        MockTx(
            txid=f"f{i}",
            inputs=[out("prev", 0.5)],
            outputs=[out(f"bc1qfan{i}{'x' * 40}", 0.002)],
            block_time=1700000000.0,
        ) for i in range(n_txs)
    ]}


def test_gb22_no_memory_growth_over_repeated_builds():
    """连续 1000 次 build（mock provider），traced 内存增长 ≤ 初始 10%。"""
    provider = make_provider(_fanout_fixture(60))
    builder = GraphBuilder(max_nodes_per_layer=50, max_total_nodes=200)

    def once():
        builder.build(SEED, provider, hops=3,
                      time_window_days=90, seed_block_time=1700000000.0)

    once()  # 预热：模块导入、dict 扩容等一次性开销不计入
    tracemalloc.start()
    once()
    _, baseline_peak = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()

    for _ in range(1000):
        once()

    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    growth = peak - baseline_peak
    assert growth <= max(baseline_peak * 0.10, 256 * 1024), (
        f"memory grew {growth} bytes vs baseline {baseline_peak}"
    )


def test_gb23_hot_path_p95_under_500ms():
    """预热后（无网络 I/O）连续 ≥50 次 build，p95 ≤ 500ms。"""
    provider = make_provider(_fanout_fixture(200))
    builder = GraphBuilder()  # 默认上限即目标规模
    durations = []
    for i in range(60):
        t0 = time.perf_counter()
        result = builder.build(SEED, provider, hops=3,
                               time_window_days=90, seed_block_time=1700000000.0)
        durations.append((time.perf_counter() - t0) * 1000)
        assert len(result.nodes) > 1
    p95 = sorted(durations)[int(len(durations) * 0.95)]
    assert p95 <= 500, f"p95={p95:.1f}ms exceeds 500ms budget"
