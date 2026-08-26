"""GraphBuilder 单测 — 映射 docs/test-cases/graph-builder-test-cases.md GB-01~19。

统计字段名采用 bybit_rust 基线词表（unspent / out_of_range / early_stop_wasabi /
early_stop_crosschain / expanded / tx4_new_dst_hard_stop），与 GB 用例文本中的
terminated_* 旧名是同一概念的两种拼写；对齐门禁以基线词表为准。
"""
from dataclasses import dataclass, field

import pytest

from backend.graph_builder.builder import (
    BFSStats, DUST_THRESHOLD_BTC, GraphBuilder, SubgraphResult, validate_hops,
)
from backend.graph_builder.id_contract import edge_id, node_id

SEED = "bc1qseed0000000000000000000000000000000000000000000000qa"
V = 0.001  # 高于 dust 阈值的常规输出金额


@dataclass
class MockTx:
    txid: str
    outputs: list = field(default_factory=list)
    inputs: list = field(default_factory=list)
    block_time: float | None = None
    unspent_outputs: set = field(default_factory=set)


def out(addr: str, value: float = V) -> dict:
    return {"address": addr, "value": value}


def make_provider(tx_map: dict):
    return lambda addr: tx_map.get(addr, [])


# ---------------------------------------------------------------------------
# GB-14 hops 参数校验
# ---------------------------------------------------------------------------
class TestHopsValidation:
    def test_valid_range(self):
        for h in (1, 2, 3):
            validate_hops(h)  # no raise

    @pytest.mark.parametrize("bad", [0, -1, 4, 99, True])
    def test_out_of_range_rejected(self, bad):
        with pytest.raises(ValueError, match="between 1 and 3"):
            validate_hops(bad)


# ---------------------------------------------------------------------------
# GB-01 ~ GB-06 五类终止条件
# ---------------------------------------------------------------------------
class TestTerminationConditions:
    def test_gb01_unspent_terminates_branch(self):
        tx = MockTx(txid="tx1", outputs=[out("bc1qdst1")], unspent_outputs={0})
        result = GraphBuilder().build(SEED, make_provider({SEED: [tx]}), hops=1)
        assert result.stats.unspent == 1
        stopped = [e for e in result.edges if e.is_stopped_expansion]
        assert len(stopped) == 1
        # 分支不再展开：无 tx→dst 的第二条边
        assert all(e.target != node_id("address", "bc1qdst1") or e.source != stopped[0].target
                   for e in result.edges)

    def test_gb02_out_of_range_by_block_time(self):
        tx = MockTx(txid="old_tx", outputs=[out("bc1qold")], block_time=1600000000.0)
        result = GraphBuilder().build(
            SEED, make_provider({SEED: [tx]}), hops=1,
            time_window_days=90, seed_block_time=1700000000.0,
        )
        assert result.stats.out_of_range >= 1
        assert any(e.is_stopped_expansion for e in result.edges)

    def test_gb03_within_window_expands_normally(self):
        tx = MockTx(txid="recent_tx", outputs=[out("bc1qnew")], block_time=1699990000.0)
        result = GraphBuilder().build(
            SEED, make_provider({SEED: [tx]}), hops=1,
            time_window_days=90, seed_block_time=1700000000.0,
        )
        assert result.stats.out_of_range == 0
        assert len(result.edges) > 0
        dst_id = node_id("address", "bc1qnew")
        assert dst_id in result.node_ids()

    def test_gb04_missing_block_time_does_not_prune(self):
        tx = MockTx(txid="no_time_tx", outputs=[out("bc1qx")], block_time=None)
        result = GraphBuilder().build(
            SEED, make_provider({SEED: [tx]}), hops=1,
            time_window_days=90, seed_block_time=1700000000.0,
        )
        assert result.stats.out_of_range == 0

    def test_gb05_coinjoin_early_stop(self):
        builder = GraphBuilder(coinjoin_txids={"cj_tx"})
        tx = MockTx(txid="cj_tx", outputs=[out("bc1qcj")])
        result = builder.build(SEED, make_provider({SEED: [tx]}), hops=1)
        assert result.stats.early_stop_wasabi == 1
        stopped = [e for e in result.edges if e.is_stopped_expansion]
        assert len(stopped) >= 1
        assert all(e.is_remixer for e in stopped)

    def test_gb06_crosschain_early_stop_with_protocol(self):
        builder = GraphBuilder(crosschain_tx_set={"bridge_tx": "thorchain"})
        tx = MockTx(txid="bridge_tx", outputs=[out("bc1qbr")])
        result = builder.build(SEED, make_provider({SEED: [tx]}), hops=1)
        cross = [e for e in result.edges if e.is_crosschain]
        assert len(cross) == 1
        assert cross[0].op_return_protocol == "thorchain"
        assert result.stats.early_stop_crosschain == 1


# ---------------------------------------------------------------------------
# GB-07 / GB-08 去重与快照语义
# ---------------------------------------------------------------------------
class TestDedupAndSnapshot:
    def test_gb07_seen_utxos_full_triple_key(self):
        """复合键 (txid, output_index, address) — 相同地址不同 index 不遗漏。"""
        tx = MockTx(txid="multi_out", outputs=[
            out("bc1qa"), out("bc1qa"), out("bc1qb"),
        ])
        result = GraphBuilder().build(SEED, make_provider({SEED: [tx]}), hops=1)
        labels = {n.label for n in result.nodes if n.kind == "address"}
        assert {"bc1qa", "bc1qb"}.issubset(labels)
        # 完整三元组：A 的两个 output 各自记录
        assert len([e for e in result.edges if e.txid == "multi_out"
                    and e.target.startswith("addr:")]) >= 2

    def test_duplicate_edges_not_created(self):
        tx = MockTx(txid="dup_tx", outputs=[out("bc1qd")])
        provider = lambda a: [tx, tx]  # 模拟重复拉取
        result = GraphBuilder().build(SEED, provider, hops=1)
        edge_ids = [e.id for e in result.edges]
        assert len(edge_ids) == len(set(edge_ids))

    def test_gb08_snapshot_semantics_no_same_round_recursion(self):
        """链式数据 hops=3：addr4 位于 depth 不可达处，不出现。"""
        chain = {}
        for i in range(4):
            chain[f"bc1qlayer{i}"] = [MockTx(
                txid=f"tx_l{i}",
                inputs=[out(f"bc1qlayer{i}", V * 10)],
                outputs=[out(f"bc1qlayer{i+1}")],
                block_time=1700000000.0,
            )]
        result = GraphBuilder().build(
            "bc1qlayer0", make_provider(chain),
            hops=3, time_window_days=90, seed_block_time=1700000000.0,
        )
        all_labels = {n.label for n in result.nodes}
        assert "bc1qlayer4" not in all_labels
        assert "bc1qlayer3" in all_labels  # 边界内最后一层可达


# ---------------------------------------------------------------------------
# GB-09 ~ GB-11 规模裁剪
# ---------------------------------------------------------------------------
class TestScaleTruncation:
    def test_gb09_max_total_nodes_hard_cap(self):
        fanout = [MockTx(txid=f"f{i}", outputs=[out(f"bc1qfan{i}{'x' * 40}")]) for i in range(300)]
        builder = GraphBuilder(max_total_nodes=50)
        result = builder.build(SEED, make_provider({SEED: fanout}), hops=1)
        assert len(result.nodes) <= 50
        assert result.stats.truncated_total_nodes > 0

    def test_gb10_max_nodes_per_layer(self):
        many = [MockTx(txid=f"p{i}", outputs=[out(f"bc1qper{i}{'x' * 40}")]) for i in range(100)]
        builder = GraphBuilder(max_nodes_per_layer=10)
        result = builder.build(SEED, make_provider({SEED: many}), hops=1)
        tx_nodes = [n for n in result.nodes if n.kind == "transaction"]
        assert len(tx_nodes) <= 10
        assert result.stats.truncated_per_layer >= 90 - 10 + 1

    def test_gb11_fanout_fold_summary_node(self):
        outputs = [out(f"bc1qfold{i}{'x' * 30}", V) for i in range(25)]
        tx = MockTx(txid="wide_tx", inputs=[out("whale", 1.0)], outputs=outputs)
        result = GraphBuilder().build(SEED, make_provider({SEED: [tx]}), hops=1)
        summaries = [n for n in result.nodes if "more outputs" in n.label]
        assert len(summaries) == 1
        assert summaries[0].label == "5 more outputs..."
        assert result.stats.fanout_folded == 5

    def test_dust_outputs_filtered(self):
        tx = MockTx(txid="dusty", outputs=[
            out("bc1qreal", 0.001),
            out("bc1qdust", DUST_THRESHOLD_BTC),  # ≤ 阈值 → 整条丢弃（基线行为）
        ])
        result = GraphBuilder().build(SEED, make_provider({SEED: [tx]}), hops=1)
        assert node_id("address", "bc1qreal") in result.node_ids()
        assert node_id("address", "bc1qdust") not in result.node_ids()

    def test_self_transfer_edge_kept(self):
        """基线 golden row4：dst==src 的自转账边保留。"""
        tx = MockTx(txid="self_tx", outputs=[out(SEED, 0.002)])
        result = GraphBuilder().build(SEED, make_provider({SEED: [tx]}), hops=2)
        self_loop = [e for e in result.edges
                     if e.source.startswith("tx:") and e.target == node_id("address", SEED)]
        assert len(self_loop) == 1


# ---------------------------------------------------------------------------
# GB-12 queue_empty 正常结束
# ---------------------------------------------------------------------------
class TestQueueEmpty:
    def test_gb12_empty_seed(self):
        result = GraphBuilder().build(SEED, make_provider({}), hops=3)
        assert result.stats.queue_empty is True
        assert len(result.nodes) == 1
        assert result.edges == []


# ---------------------------------------------------------------------------
# GB-13 D3 ID 规范一致性
# ---------------------------------------------------------------------------
class TestD3Integrity:
    @pytest.mark.parametrize("hops", [1, 2, 3])
    def test_gb13_id_contract(self, hops):
        txs_a = [
            MockTx(txid="cj_mix", outputs=[out("bc1qmixed")]),
            MockTx(txid="normal", inputs=[out("prev", 0.01)], outputs=[out("bc1qb"), out("bc1qc")],
                   block_time=1700000000.0),
        ]
        graph = {
            SEED: txs_a,
            "bc1qb": [MockTx(txid="deep", outputs=[out("bc1qd")], block_time=1700000000.0)],
        }
        builder = GraphBuilder(coinjoin_txids={"cj_mix"})
        result = builder.build(SEED, make_provider(graph), hops=hops)

        for nid in result.node_ids():
            assert nid.startswith(("addr:", "tx:")), nid
        for e in result.edges:
            assert e.id.startswith("edge:")
            assert e.id == f"edge:{e.source}->{e.target}"
            assert e.source in result.node_ids(), f"dangling source {e.id}"
            assert e.target in result.node_ids(), f"dangling target {e.id}"


# ---------------------------------------------------------------------------
# GB-19 部分失败 degraded 标志
# ---------------------------------------------------------------------------
class TestPartialFailure:
    def test_gb19_degraded_flag_and_other_branches_survive(self):
        dead = "bc1qdead" + "z" * 26
        # SEED 的交易输出指向坏地址 → 下一轮访问该分支时触发失败
        ok_tx = MockTx(txid="ok_tx", outputs=[out(dead)], block_time=None)

        def flaky_provider(addr):
            if addr == dead:
                raise ConnectionError("esplora unreachable")
            return [ok_tx] if addr == SEED else []

        result = GraphBuilder().build(SEED, flaky_provider, hops=2)
        assert result.stats.degraded is True
        # 失败分支之外的图结构正常产出
        assert node_id("transaction", "ok_tx") in result.node_ids()
        assert node_id("address", dead) in result.node_ids()


# ---------------------------------------------------------------------------
# 基线对齐：bybit_rust step3_sub2 决策级联转录 + golden fixture 统计核对
# ---------------------------------------------------------------------------
def reference_cascade(units: list[dict], coinjoin: set[str], crosschain: dict[str, str],
                      seed_block_time: float, window_days: int) -> dict[str, int]:
    """bybit_rust process_queue_batched Phase1/2 决策级联的忠实转录。
    每个 unit: {"spent_by": str|None, "in_window": bool|None, "txid": str}
    返回与基线 stats 键完全一致的计数字典。"""
    stats = dict.fromkeys(
        ["unspent", "out_of_range", "early_stop_wasabi",
         "early_stop_crosschain", "expanded", "tx4_new_dst_hard_stop"], 0)
    for u in units:
        if u["spent_by"] is None:                      # 基线 Phase 1
            stats["unspent"] += 1
        elif not u["in_window"]:                       # PatternTrace 口径：block_time 窗口
            stats["out_of_range"] += 1
        elif u["txid"] in coinjoin:                    # 基线 Phase 2
            stats["early_stop_wasabi"] += 1
        elif u["txid"] in crosschain:
            stats["early_stop_crosschain"] += 1
        else:
            stats["expanded"] += 1
    return stats


class TestBaselineAlignment:
    GOLDEN_SEEDS = {
        # bybit_rust golden/python/results/step3_subgraph 场景还原：
        # 3 个种子地址、4 个 UTXO 扩展单元
        "bc1p2y0y336sgx7g4d8rhqq4na792xahkurw0rvjz3n7tmpt3gey5q8qka4flv":
            [("e1d78fc55604a15197fbcc508c1d2d376cac375549e1cd867c9c5215067e6c47", "crosschain")],
        "3EeX4v1jUkj6xnHHXzx7qg9Cz4zB3aejHf":
            [("8840a3cc2ff8743469c2906824bb4ea800c1a1312c696a98711ed93638111bfd", "crosschain")],
        "bc1qctulawhc0hdk00ypqpj3auwg8ac8980m533s35": [
            ("b4599950950e00b219269af32e14afc8a571d67b11c127e23a8b5500a6473c1a", "expanded"),
            ("feb3b133da2303771cc40785d1b26c2e08ad305195dad7a5e7977619b7b5f54c", "crosschain"),
        ],
    }
    CROSSCHAIN = {txid: "runes" for txid, kind in
                  [t for txs in GOLDEN_SEEDS.values() for t in txs] if kind == "crosschain"}

    def test_golden_fixture_termination_stats_match_baseline(self):
        expected = {
            "unspent": 0, "out_of_range": 0,
            "early_stop_wasabi": 0, "early_stop_crosschain": 3,
            "expanded": 1, "tx4_new_dst_hard_stop": 0,
        }
        # 参考级联在同一 fixture 上先行验证
        units = [{"spent_by": txid, "in_window": True, "txid": txid}
                 for txs in self.GOLDEN_SEEDS.values() for txid, _ in txs]
        assert reference_cascade(units, set(), self.CROSSCHAIN, 0.0, 90) == expected

        # 我方 builder 在等价 mock 上产生一致统计
        tx_map = {}
        for addr, txs in self.GOLDEN_SEEDS.items():
            entries = []
            for txid, kind in txs:
                if kind == "expanded":
                    entries.append(MockTx(
                        txid=txid,
                        inputs=[out("prev_utxo", 0.02)],
                        outputs=[out("326Lynz1EhXkpREH9SsUr51PoFinJwh84h", 0.003031),
                                 out("bc1pl670d6nemvhy968kmfxrq4qwmrhw8ldm88yfwmzc8uj5hytqxx7snshcvh", 0.004255),
                                 out(addr, 0.000135)],  # golden row4 自转账
                        block_time=1700000000.0,
                    ))
                else:
                    entries.append(MockTx(txid=txid))
            tx_map[addr] = entries

        builder = GraphBuilder(crosschain_tx_set=self.CROSSCHAIN)
        results = [builder.build(addr, make_provider(tx_map), hops=3)
                   for addr in self.GOLDEN_SEEDS]

        merged = dict.fromkeys(expected, 0)
        for r in results:
            for k, v in r.stats.termination_summary().items():
                merged[k] += v
        assert merged == expected
        # golden 结构特征：3 条 crosschain 停止边 + b45999 的 3 条扩展边（含自转账）
        all_edges = [e for r in results for e in r.edges]
        assert sum(e.is_crosschain for e in all_edges) == 3
        assert sum(not e.is_stopped_expansion and e.target.startswith("addr:") and e.txid.startswith("b45999")
                   for e in all_edges) == 3

    def test_counter_vocabulary_matches_baseline(self):
        assert set(BFSStats().termination_summary()) == {
            "unspent", "out_of_range", "early_stop_wasabi",
            "early_stop_crosschain", "expanded", "tx4_new_dst_hard_stop",
        }

    def test_unspent_vs_out_of_range_cascade_order(self):
        """级联顺序：unspent 先于 out_of_range 先于 early_stop（基线 Phase1→2）。"""
        tx = MockTx(txid="both", outputs=[out("bc1qz")], block_time=1000.0,
                    unspent_outputs={0})
        result = GraphBuilder(crosschain_tx_set={"both": "thorchain"}).build(
            SEED, make_provider({SEED: [tx]}), hops=1,
            time_window_days=90, seed_block_time=1700000000.0,
        )
        assert result.stats.unspent == 1
        assert result.stats.out_of_range == 0
        assert result.stats.early_stop_crosschain == 0
