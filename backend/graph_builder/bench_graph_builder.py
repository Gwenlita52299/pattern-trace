"""GB-24 · 纯 Python BFS 微基准（批量离线分析决策用，非在线延迟指标）。

用法:
    python -m backend.graph_builder.bench_graph_builder

生成 mock 数据（默认 ~120K 边），跑多轮取中位数，输出耗时表。
如安装了 Rust PyO3 扩展（pattern_trace_bfs），自动追加对比行——
当前 MVP 无该扩展，仅输出 Python 基线；扩展属于 P1 按需引入。
"""
from __future__ import annotations

import statistics
import time

from .builder import DUST_THRESHOLD_BTC, GraphBuilder


def _make_mock_txs(n_txs: int, outputs_per_tx: int, seed_addr: str) -> list[dict]:
    # 注资交易：向种子地址输出 n_txs 个 UTXO，作为每笔消费交易的输入来源（输出侧-only 根枚举）。
    funding = {
        "txid": "benchfund",
        "inputs": [],
        "outputs": [
            {"address": seed_addr, "value": 0.001} for _ in range(n_txs)
        ],
        "block_time": 1700000000.0,
    }
    spends = [
        {
            "txid": f"bench{i:08d}",
            "inputs": [
                {"address": seed_addr, "value": 0.001,
                 "prev_txid": "benchfund", "prev_vout": i}
            ],
            "outputs": [
                {"address": f"bc1qdst{i}_{j}", "value": 0.002}
                for j in range(outputs_per_tx)
            ],
            "block_time": 1700000000.0,
        }
        for i in range(n_txs)
    ]
    return [funding] + spends


def main(n_txs: int = 20_000, outputs_per_tx: int = 6, rounds: int = 5) -> None:
    total_edges = n_txs * outputs_per_tx

    # dict 版交易对象直接满足 builder 的鸭子类型访问；
    # 仅种子地址返回交易列表（注资交易 + 各消费交易），其余地址返回空（模拟数据终点）
    seed_addr = "bc1qbenchseed"
    txs = _make_mock_txs(n_txs, outputs_per_tx, seed_addr)

    def provider(addr: str):
        if addr != seed_addr:
            return []
        return [type("T", (), {
            "txid": t["txid"],
            "inputs": t["inputs"],
            "outputs": t["outputs"],
            "block_time": t["block_time"],
            "unspent_outputs": set(),
        })() for t in txs]

    # 放开规模裁剪以测真实 CPU 路径（生产默认值见 spec §8）
    builder = GraphBuilder(
        max_nodes_per_layer=n_txs,
        max_total_nodes=n_txs * (outputs_per_tx + 1),
        dust_threshold_btc=DUST_THRESHOLD_BTC,
    )

    print(f"dataset: {n_txs} txs × {outputs_per_tx} outputs ≈ {total_edges} edges")
    durations = []
    for r in range(rounds):
        t0 = time.perf_counter()
        result = builder.build(
            "bc1qbenchseed", provider, hops=3,
            time_window_days=90, seed_block_time=1700000000.0,
        )
        dt = time.perf_counter() - t0
        durations.append(dt)
        print(f"  round {r + 1}: {dt * 1000:8.1f} ms   "
              f"nodes={len(result.nodes)} edges={len(result.edges)}")

    med = statistics.median(durations)
    print("\n=== 结果（中位数） ===")
    print(f"python bfs : {med * 1000:8.1f} ms  ({total_edges / med / 1e3:.0f}K edges/s)")
    try:
        from pattern_trace_bfs import build_subgraph  # noqa: F401
        print("rust pyo3  : 已检测到扩展，可在此接入对比（P1 引入）")
    except ImportError:
        print("rust pyo3  : 未安装（P1 按需引入，见 docs/rust-migration-feasibility.md）")


if __name__ == "__main__":
    main()
