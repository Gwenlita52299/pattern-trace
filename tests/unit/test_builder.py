"""GraphBuilder 单测 — 映射 docs/test-cases/graph-builder-test-cases.md GB-01~23。

队列语义采用 bybit_rust 基线（btc_aml_forensics reference/python step3_sub2_bfs_loop）：
三队列的元素是 **UTXO 元组** (utxo_txid, output_index, owner)，每个扩展单元 = 一个 UTXO 的
消费（spent_by 解析），与该 UTXO 的消费交易 / 其输出展开计入一个主状态计数器。
统计字段名采用基线词表（unspent / out_of_range / early_stop_wasabi /
early_stop_crosschain / expanded / tx4_new_dst_hard_stop）。
"""
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from backend.graph_builder.builder import (
    BFSStats, DUST_THRESHOLD_BTC, GraphBuilder, SubgraphResult, validate_hops,
)
from backend.graph_builder.id_contract import edge_id, node_id

SEED = "bc1qseed000000000000000000000000000000000000000000000000qa"
V = 0.001  # 高于 dust 阈值的常规输出金额


@dataclass
class MockTx:
    txid: str
    inputs: list = field(default_factory=list)   # {address, value, prev_txid, prev_vout}
    outputs: list = field(default_factory=list)  # {address, value}
    block_time: float | None = None
    unspent_outputs: set = field(default_factory=set)


def out(addr: str, value: float = V) -> dict:
    return {"address": addr, "value": value}


def vin(addr: str, value: float, prev_txid: str, prev_vout: int = 0) -> dict:
    """一个 input 消费 (prev_txid, prev_vout) 这个由 addr 拥有的 UTXO。"""
    return {"address": addr, "value": value,
            "prev_txid": prev_txid, "prev_vout": prev_vout}


def fund_seed_txs(tx_map: dict) -> dict:
    """为 tx_map 中所有 input 的 prevout 自动补注资交易（输出侧-only 根枚举辅助）。

    把 _owned_utxos 改为输出侧-only 后，它只枚举「输出到该地址」的 UTXO；而旧测试的
    根 UTXO 多源自 input 的 prevout（消费侧）。该辅助为每个 (prev_txid, prev_vout, owner)
    补一条由 prev_txid 创建、输出到 owner 的注资交易，使输出侧也能枚举到该根 UTXO，
    无需逐条改写测试。注资交易无 input，不会被当作消费交易，故不污染子图。
    """
    from collections import defaultdict

    builder: dict[str, list] = defaultdict(list)
    for addr, txs in tx_map.items():
        builder[addr].extend(txs)
    existing = {getattr(t, "txid", None) for txs in builder.values() for t in txs}

    pending: dict[str, dict[int, tuple[str, float]]] = defaultdict(dict)
    for addr, txs in tx_map.items():
        for tx in txs:
            for inp in getattr(tx, "inputs", None) or []:
                ptxid = inp.get("prev_txid")
                if not ptxid or ptxid in existing:
                    continue
                pvout = inp.get("prev_vout", 0)
                owner = inp.get("address")
                value = inp.get("value", V)
                pending[ptxid].setdefault(pvout, (owner, value))

    for ptxid, outs in pending.items():
        maxidx = max(outs)
        outputs = [{"address": None, "value": DUST_THRESHOLD_BTC} for _ in range(maxidx + 1)]
        for idx, (owner, value) in outs.items():
            outputs[idx] = out(owner, value)
        # 把注资交易加入每个输出 owner 的地址列表（一个创建交易可能注资多个地址）
        funded_owners: set[str] = set()
        for idx, (owner, _v) in outs.items():
            if owner in funded_owners:
                continue
            funded_owners.add(owner)
            builder[owner].append(MockTx(txid=ptxid, outputs=outputs))
    return dict(builder)


def make_provider(tx_map: dict):
    funded = fund_seed_txs(tx_map)
    return lambda addr: funded.get(addr, [])


class OutspendProvider:
    """最小 outspend 协议桩（issue #3 权威路径）：address_txs / outspend / get_tx。

    模拟 live 模式下「消费交易可能不在 owner 地址交易列表页内」的真实场景，
    用于验证 builder 以 outspend 作为 spent_by 权威来源而非地址扫描。
    """

    def __init__(self, tx_map, spent_by: dict, txs_by_id: dict):
        self.tx_map = tx_map          # addr -> [MockTx]
        self.spent_by = spent_by      # (txid, vout) -> spending_txid
        self.txs_by_id = txs_by_id    # txid -> MockTx（完整交易）

    def address_txs(self, addr):
        return self.tx_map.get(addr, [])

    def outspend(self, txid, vout):
        spender = self.spent_by.get((txid, vout))
        if spender is None:
            return SimpleNamespace(spent=False, txid=None, vin=None)
        return SimpleNamespace(spent=True, txid=spender, vin={"txid": txid, "vout": vout})

    def get_tx(self, txid):
        return self.txs_by_id.get(txid)


class DomainProvider:
    """实现领域级 resolve_spending_transaction 的头（issue #3 模块边界）。"""

    def __init__(self, tx_map, spent_by: dict, txs_by_id: dict):
        self.tx_map = tx_map
        self.spent_by = spent_by
        self.txs_by_id = txs_by_id

    def address_txs(self, addr):
        return self.tx_map.get(addr, [])

    def get_tx(self, txid):
        return self.txs_by_id.get(txid)

    def resolve_spending_transaction(self, txid, vout, owner=None):
        spender = self.spent_by.get((txid, vout))
        if spender is None:
            return SimpleNamespace(spent=False, spending_txid=None, spending_vin=None,
                                   spending_tx=None, txid=txid, vout=vout, owner=owner or "")
        spending_tx = self.txs_by_id.get(spender)
        spending_vin = None
        for i, inp in enumerate(getattr(spending_tx, "inputs", None) or []):
            if inp.get("prev_txid") == txid and inp.get("prev_vout") == vout:
                spending_vin = i
                break
        return SimpleNamespace(spent=True, spending_txid=spender, spending_vin=spending_vin,
                               spending_tx=spending_tx, txid=txid, vout=vout, owner=owner or "")


# ---------------------------------------------------------------------------
# GB-25 · issue #3：outspend 权威 spent_by（地址扫描会被分页遗漏 → 误判 unspent）
# ---------------------------------------------------------------------------
class TestOutspendAuthoritativeSpentBy:
    def test_outspend_unspent_leaf(self):
        """outspend 权威返回 spent=false → unspent 叶子，不产边。"""
        funding = MockTx(txid="tx_fund", outputs=[out(SEED, 0.5)])
        provider = OutspendProvider({SEED: [funding]}, {}, {"tx_fund": funding})
        result = GraphBuilder().build(SEED, provider, hops=1)
        assert result.stats.unspent == 1
        assert result.edges == []

    def test_outspend_spent_uses_get_tx_to_expand(self):
        """outspend spent=true → get_tx 取全量消费交易 → 正常展开（bc1qnew 入队下一层）。"""
        spender = MockTx(txid="sp1", inputs=[vin(SEED, 0.5, "u1")],
                         outputs=[out("bc1qnew")], block_time=1700000000.0)
        provider = OutspendProvider(fund_seed_txs({SEED: [spender]}), {("u1", 0): "sp1"}, {"sp1": spender})
        result = GraphBuilder().build(SEED, provider, hops=2)
        # 种子 UTXO 被权威解析为 spent（expanded），而非误判为 unspent；
        # bc1qnew 叶子在下一层正常计入 unspent，属正确语义。
        assert result.stats.expanded == 1
        assert node_id("transaction", "sp1") in result.node_ids()
        assert node_id("address", "bc1qnew") in result.node_ids()

    def test_spending_tx_missed_by_page_is_resolved_via_outspend(self):
        """核心回归：消费交易不在 owner 地址交易页内，旧地址扫描会误判 unspent。

        outspend 按 (txid, vout) 权威返回消费交易，get_tx 补齐全量 → 不再丢失分支。
        hops=1 下该消费交易被正确解析并展开（tx4 硬停止），而非误判为 unspent。
        """
        # SEED 拥有 (tx_fund, 0)；消费交易 sp_offpage 不在 address_txs(SEED) 返回页内
        funding = MockTx(txid="tx_fund", outputs=[out(SEED, 0.5)])
        spender = MockTx(txid="sp_offpage",
                         inputs=[vin(SEED, 0.5, "tx_fund", 0)],  # 消费 (tx_fund, 0)
                         outputs=[out("bc1qnew")], block_time=1700000000.0)
        provider = OutspendProvider(
            tx_map={SEED: [funding]},          # 页内无 sp_offpage（模拟分页遗漏）
            spent_by={("tx_fund", 0): "sp_offpage"},
            txs_by_id={"sp_offpage": spender},
        )
        result = GraphBuilder().build(SEED, provider, hops=1)
        assert result.stats.unspent == 0            # 不再把已花 UTXO 误判为 unspent
        assert node_id("transaction", "sp_offpage") in result.node_ids()
        assert node_id("address", "bc1qnew") in result.node_ids()

    def test_outspend_failure_degrades(self):
        """outspend 数据源异常 → 部分失败语义：degraded 置位（不整体失败）。"""
        spender = MockTx(txid="sp1", inputs=[vin(SEED, 0.5, "u1")],
                         outputs=[out("bc1qnew")])

        class FailOutspend(OutspendProvider):
            def outspend(self, txid, vout):
                raise ConnectionError("esplora unreachable")

        provider = FailOutspend(fund_seed_txs({SEED: [spender]}), {("u1", 0): "sp1"}, {"sp1": spender})
        result = GraphBuilder().build(SEED, provider, hops=1)
        assert result.stats.degraded is True

    def test_outspend_get_tx_failure_degrades(self):
        """outspend 已判 spent 但 get_tx 取全量失败 → 部分失败（degraded 置位）。"""
        spender = MockTx(txid="sp1", inputs=[vin(SEED, 0.5, "u1")],
                         outputs=[out("bc1qnew")])

        class FailGetTx(OutspendProvider):
            def get_tx(self, txid):
                raise ConnectionError("esplora unreachable")

        provider = FailGetTx(fund_seed_txs({SEED: [spender]}), {("u1", 0): "sp1"}, {"sp1": spender})
        result = GraphBuilder().build(SEED, provider, hops=1)
        assert result.stats.degraded is True

    def test_outspend_without_get_tx_falls_back_to_owner_scan(self):
        """极少数无 get_tx 的桩：outspend 给消费 txid 后，回退到 owner 交易列表按 txid 匹配。"""
        spender = MockTx(txid="sp1", inputs=[vin(SEED, 0.5, "u1")],
                         outputs=[out("bc1qnew")], block_time=1700000000.0)

        class OutspendNoGetTx:
            def __init__(self, tx_map, spent_by):
                self.tx_map = tx_map
                self.spent_by = spent_by

            def address_txs(self, addr):
                return self.tx_map.get(addr, [])

            def outspend(self, txid, vout):
                spender_id = self.spent_by.get((txid, vout))
                if spender_id is None:
                    return SimpleNamespace(spent=False, txid=None, vin=None)
                return SimpleNamespace(spent=True, txid=spender_id, vin={"txid": txid, "vout": vout})

            def __call__(self, addr):
                return self.tx_map.get(addr, [])

        provider = OutspendNoGetTx(fund_seed_txs({SEED: [spender]}), {("u1", 0): "sp1"})
        result = GraphBuilder().build(SEED, provider, hops=2)
        assert result.stats.degraded is False
        assert result.stats.expanded == 1
        assert node_id("transaction", "sp1") in result.node_ids()

    def test_seed_address_txs_unreachable_degrades_to_empty(self):
        """种子地址数据源不可达 → _owned_utxos 降级，仅保留种子节点。"""
        def dead_provider(addr):
            raise ConnectionError("esplora unreachable")

        result = GraphBuilder().build(SEED, dead_provider, hops=1)
        assert result.stats.degraded is True
        assert len(result.nodes) == 1
        assert result.edges == []

    def test_domain_provider_resolves_spending_tx(self):
        """builder 优先使用领域级 resolve_spending_transaction（issue #3 模块边界）。"""
        spender = MockTx(txid="sp1", inputs=[vin(SEED, 0.5, "u1")],
                         outputs=[out("bc1qnew")], block_time=1700000000.0)
        provider = DomainProvider(fund_seed_txs({SEED: [spender]}),
                                  {("u1", 0): "sp1"}, {"sp1": spender})
        result = GraphBuilder().build(SEED, provider, hops=2)
        assert result.stats.degraded is False
        assert result.stats.expanded == 1
        assert node_id("transaction", "sp1") in result.node_ids()

    def test_domain_provider_unspent_leaf(self):
        """领域接口遇 unspent 叶子 → 不再扩展。"""
        funding = MockTx(txid="tx_fund", outputs=[out(SEED, 0.5)])
        provider = DomainProvider({SEED: [funding]}, {}, {"tx_fund": funding})
        result = GraphBuilder().build(SEED, provider, hops=1)
        assert result.stats.unspent == 1
        assert result.edges == []

    def test_domain_provider_failure_degrades(self):
        """领域接口数据源异常 → 部分失败（degraded 置位）。"""
        spender = MockTx(txid="sp1", inputs=[vin(SEED, 0.5, "u1")],
                         outputs=[out("bc1qnew")])

        class FailDomain(DomainProvider):
            def resolve_spending_transaction(self, txid, vout, owner=None):
                raise ConnectionError("esplora unreachable")

        provider = FailDomain(fund_seed_txs({SEED: [spender]}),
                              {("u1", 0): "sp1"}, {"sp1": spender})
        result = GraphBuilder().build(SEED, provider, hops=1)
        assert result.stats.degraded is True


# ---------------------------------------------------------------------------
# GB-26 · issue #3「输入为 txid」：从交易输出侧建立根 UTXO，按 outspend 展开
# ---------------------------------------------------------------------------
class TestBuildFromTxid:
    def test_unspent_output_leaf(self):
        """种子交易 T0 有一个未花输出 → 该根 UTXO 判 unspent，并连到 T0。"""
        seed_tx = MockTx(txid="T0", outputs=[out("bc1qA", 0.5)])
        provider = OutspendProvider({}, {}, {"T0": seed_tx})
        result = GraphBuilder().build_from_txid("T0", provider, hops=2)
        assert result.stats.unspent == 1
        assert node_id("transaction", "T0") in result.node_ids()
        assert node_id("address", "bc1qA") in result.node_ids()

    def test_spent_output_expands_via_outspend(self):
        """T0 输出被消费 → outspend 解析消费交易，get_tx 补齐全量 → 正常展开。"""
        seed_tx = MockTx(txid="T0", outputs=[out("bc1qA", 0.5)])
        spender = MockTx(txid="S0", inputs=[vin("bc1qA", 0.5, "T0", 0)],
                         outputs=[out("bc1qB", 0.4)], block_time=1700000000.0)
        provider = OutspendProvider({}, {("T0", 0): "S0"}, {"T0": seed_tx, "S0": spender})
        result = GraphBuilder().build_from_txid("T0", provider, hops=2)
        assert result.stats.expanded == 1
        assert node_id("transaction", "S0") in result.node_ids()
        assert node_id("address", "bc1qB") in result.node_ids()

    def test_multiple_outputs_form_multiple_root_branches(self):
        """T0 有两个可追踪输出 → 两个独立根 UTXO 分支（未花 → unspent×2）。"""
        seed_tx = MockTx(txid="T0", outputs=[out("bc1qA", 0.5), out("bc1qB", 0.3)])
        provider = OutspendProvider({}, {}, {"T0": seed_tx})
        result = GraphBuilder().build_from_txid("T0", provider, hops=2)
        assert result.stats.unspent == 2
        assert node_id("address", "bc1qA") in result.node_ids()
        assert node_id("address", "bc1qB") in result.node_ids()

    def test_excludes_op_return_and_dust_outputs(self):
        """OP_RETURN（无地址）与 dust 输出不成为根 UTXO。"""
        seed_tx = MockTx(txid="T0", outputs=[
            out("bc1qA", 0.5),
            {"address": None, "value": 1.0},            # OP_RETURN / 不可追踪
            out("bc1qDust", DUST_THRESHOLD_BTC),         # dust
        ])
        provider = OutspendProvider({}, {}, {"T0": seed_tx})
        result = GraphBuilder().build_from_txid("T0", provider, hops=2)
        assert result.stats.unspent == 1
        assert node_id("address", "bc1qA") in result.node_ids()
        assert node_id("address", "bc1qDust") not in result.node_ids()

    def test_d3_id_integrity_for_txid_seed(self):
        """种子交易模式产出的边端点均在节点集内，id 遵守 D3 契约。"""
        seed_tx = MockTx(txid="T0", outputs=[out("bc1qA", 0.5)])
        spender = MockTx(txid="S0", inputs=[vin("bc1qA", 0.5, "T0", 0)],
                         outputs=[out("bc1qB", 0.4)], block_time=1700000000.0)
        provider = OutspendProvider({}, {("T0", 0): "S0"}, {"T0": seed_tx, "S0": spender})
        result = GraphBuilder().build_from_txid("T0", provider, hops=2)
        assert result.edges
        for e in result.edges:
            assert e.id == f"edge:{e.source}->{e.target}"
            assert e.source in result.node_ids()
            assert e.target in result.node_ids()

    def test_get_tx_failure_degrades(self):
        """种子交易数据源不可达 → 降级，不产节点（部分失败语义）。"""
        class FailTx(OutspendProvider):
            def get_tx(self, txid):
                raise ConnectionError("esplora unreachable")

        provider = FailTx({}, {}, {"T0": MockTx(txid="T0", outputs=[out("bc1qA", 0.5)])})
        result = GraphBuilder().build_from_txid("T0", provider, hops=2)
        assert result.stats.degraded is True
        assert len(result.nodes) == 0

    def test_get_tx_unavailable_degrades(self):
        """provider 未实现 get_tx 协议 → 交易种子无法取详情 → 降级。"""
        result = GraphBuilder().build_from_txid("T0", make_provider({}), hops=2)
        assert result.stats.degraded is True
        assert len(result.nodes) == 0

    def test_get_tx_returning_none_degrades(self):
        """get_tx 查无该交易 → 降级（数据源缺失语义）。"""
        provider = OutspendProvider({}, {}, {})  # txs_by_id 无 "T0"
        result = GraphBuilder().build_from_txid("T0", provider, hops=2)
        assert result.stats.degraded is True
        assert len(result.nodes) == 0


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
# GB-01 ~ GB-06 五类终止条件（以 UTXO 消费单元为单位）
# ---------------------------------------------------------------------------
class TestTerminationConditions:
    def test_gb01_unspent_utxo_no_spender(self):
        """种子地址拥有一个 UTXO（某交易输出给它），但没有任何交易消费它 → unspent。"""
        funding = MockTx(txid="tx_fund", outputs=[out(SEED, 0.5)])
        result = GraphBuilder().build(
            SEED, make_provider({SEED: [funding]}), hops=1)
        assert result.stats.unspent == 1
        # 该 UTXO 无消费交易 → 不产生任何向下展开的边
        assert result.edges == []

    def test_gb02_out_of_range_by_block_time(self):
        """SEED 的 UTXO 被一笔早于时间窗起点的交易消费 → out_of_range。"""
        spender = MockTx(
            txid="old_tx", inputs=[vin(SEED, 0.5, "u1")], outputs=[out("bc1qold")],
            block_time=1600000000.0,
        )
        result = GraphBuilder().build(
            SEED, make_provider({SEED: [spender]}), hops=1,
            time_window_days=90, seed_block_time=1700000000.0,
        )
        assert result.stats.out_of_range == 1

    def test_gb03_within_window_expands_normally(self):
        spender = MockTx(
            txid="recent_tx", inputs=[vin(SEED, 0.5, "u1")],
            outputs=[out("bc1qnew")], block_time=1699990000.0,
        )
        result = GraphBuilder().build(
            SEED, make_provider({SEED: [spender]}), hops=1,
            time_window_days=90, seed_block_time=1700000000.0,
        )
        assert result.stats.out_of_range == 0
        assert len(result.edges) > 0
        dst_id = node_id("address", "bc1qnew")
        assert dst_id in result.node_ids()

    def test_gb04_missing_block_time_does_not_prune(self):
        spender = MockTx(
            txid="no_time_tx", inputs=[vin(SEED, 0.5, "u1")],
            outputs=[out("bc1qx")], block_time=None,
        )
        result = GraphBuilder().build(
            SEED, make_provider({SEED: [spender]}), hops=1,
            time_window_days=90, seed_block_time=1700000000.0,
        )
        assert result.stats.out_of_range == 0

    def test_gb05_coinjoin_early_stop(self):
        builder = GraphBuilder(coinjoin_txids={"cj_tx"})
        spender = MockTx(
            txid="cj_tx", inputs=[vin(SEED, 0.5, "u1")], outputs=[out("bc1qcj")])
        result = builder.build(SEED, make_provider({SEED: [spender]}), hops=1)
        assert result.stats.early_stop_wasabi == 1
        stopped = [e for e in result.edges if e.is_stopped_expansion]
        assert len(stopped) == 1
        assert all(e.is_remixer for e in stopped)

    def test_gb06_crosschain_early_stop_with_protocol(self):
        builder = GraphBuilder(crosschain_tx_set={"bridge_tx": "thorchain"})
        spender = MockTx(
            txid="bridge_tx", inputs=[vin(SEED, 0.5, "u1")], outputs=[out("bc1qbr")])
        result = builder.build(SEED, make_provider({SEED: [spender]}), hops=1)
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
        spender = MockTx(txid="multi_out", inputs=[vin(SEED, 2.0, "u1")], outputs=[
            out("bc1qa"), out("bc1qa"), out("bc1qb"),
        ])
        result = GraphBuilder().build(SEED, make_provider({SEED: [spender]}), hops=1)
        labels = {n.label for n in result.nodes if n.kind == "address"}
        assert {"bc1qa", "bc1qb"}.issubset(labels)
        # 完整三元组：A 的两个不同 output_index 各自入账
        assert len([e for e in result.edges if e.txid == "multi_out"
                    and e.target.startswith("addr:")]) >= 2

    def test_duplicate_edges_not_created(self):
        spender = MockTx(txid="dup_tx", inputs=[vin(SEED, 0.5, "u1")],
                         outputs=[out("bc1qd")])
        provider = lambda a: [spender, spender]  # 模拟重复拉取（同一消费交易）
        result = GraphBuilder().build(SEED, provider, hops=1)
        edge_ids = [e.id for e in result.edges]
        assert len(edge_ids) == len(set(edge_ids))

    def test_gb08_snapshot_semantics_no_same_round_recursion(self):
        """链式数据 hops=3：addr4 位于 depth 不可达处，不出现。"""
        chain = {
            SEED: [MockTx(txid="tx_l0", inputs=[vin(SEED, V * 2, "u0")],
                          outputs=[out("bc1qlayer1")], block_time=1700000000.0)],
            "bc1qlayer1": [MockTx(txid="tx_l1",
                                  inputs=[vin("bc1qlayer1", V, "tx_l0", 0)],
                                  outputs=[out("bc1qlayer2")],
                                  block_time=1700000000.0)],
            "bc1qlayer2": [MockTx(txid="tx_l2",
                                  inputs=[vin("bc1qlayer2", V, "tx_l1", 0)],
                                  outputs=[out("bc1qlayer3")],
                                  block_time=1700000000.0)],
            "bc1qlayer3": [MockTx(txid="tx_l3",
                                  inputs=[vin("bc1qlayer3", V, "tx_l2", 0)],
                                  outputs=[out("bc1qlayer4")],
                                  block_time=1700000000.0)],
        }
        result = GraphBuilder().build(
            SEED, make_provider(chain),
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
        # 每个消费交易花费**各自独立**的 UTXO，模拟扇出爆炸触发总节点硬上限
        fanout = [MockTx(txid=f"f{i}", inputs=[vin(SEED, 0.5, f"u{i}")],
                         outputs=[out(f"bc1qfan{i}{'x' * 40}")])
                  for i in range(300)]
        builder = GraphBuilder(max_total_nodes=50)
        result = builder.build(SEED, make_provider({SEED: fanout}), hops=1)
        assert len(result.nodes) <= 50
        assert result.stats.truncated_total_nodes > 0

    def test_gb10_max_nodes_per_layer(self):
        many = [MockTx(txid=f"p{i}", inputs=[vin(SEED, 0.5, f"u{i}")],
                       outputs=[out(f"bc1qper{i}{'x' * 40}")])
                for i in range(100)]
        builder = GraphBuilder(max_nodes_per_layer=10)
        result = builder.build(SEED, make_provider({SEED: many}), hops=1)
        tx_nodes = [n for n in result.nodes if n.kind == "transaction"]
        assert len(tx_nodes) <= 10
        assert result.stats.truncated_per_layer > 0

    def test_gb11_fanout_fold_summary_node(self):
        outputs = [out(f"bc1qfold{i}{'x' * 30}", V) for i in range(25)]
        spender = MockTx(txid="wide_tx", inputs=[vin(SEED, 1.0, "u1")],
                         outputs=outputs)
        result = GraphBuilder().build(SEED, make_provider({SEED: [spender]}), hops=1)
        summaries = [n for n in result.nodes if "more outputs" in n.label]
        assert len(summaries) == 1
        assert summaries[0].label == "5 more outputs..."
        assert result.stats.fanout_folded == 5

    def test_dust_outputs_filtered(self):
        spender = MockTx(txid="dusty", inputs=[vin(SEED, 0.5, "u1")], outputs=[
            out("bc1qreal", 0.001),
            out("bc1qdust", DUST_THRESHOLD_BTC),  # ≤ 阈值 → 整条丢弃（基线行为）
        ])
        result = GraphBuilder().build(SEED, make_provider({SEED: [spender]}), hops=1)
        assert node_id("address", "bc1qreal") in result.node_ids()
        assert node_id("address", "bc1qdust") not in result.node_ids()

    def test_self_transfer_edge_kept(self):
        spender = MockTx(txid="self_tx", inputs=[vin(SEED, 1.0, "u1")],
                         outputs=[out(SEED, 0.002)])
        result = GraphBuilder().build(SEED, make_provider({SEED: [spender]}), hops=2)
        self_loop = [e for e in result.edges
                     if e.source.startswith("tx:") and e.target == node_id("address", SEED)]
        assert len(self_loop) == 1


# ---------------------------------------------------------------------------
# GB-12 queue_empty 正常结束
# ---------------------------------------------------------------------------
class TestQueueEmpty:
    def test_gb12_no_owned_utxo_seed(self):
        # 种子地址没有任何交易 → 无拥有的 UTXO → 三队列空 → queue_empty
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
        graph = {
            SEED: [
                MockTx(txid="cj_mix", inputs=[vin(SEED, 0.5, "u1")],
                       outputs=[out("bc1qmixed")]),
                MockTx(txid="normal", inputs=[vin(SEED, 1.0, "u2")],
                       outputs=[out("bc1qb"), out("bc1qc")],
                       block_time=1700000000.0),
            ],
            "bc1qb": [MockTx(txid="deep", inputs=[vin("bc1qb", 0.5, "normal", 0)],
                             outputs=[out("bc1qd")], block_time=1700000000.0)],
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
        ok_spend = MockTx(txid="ok_tx", inputs=[vin(SEED, 0.5, "u1")],
                          outputs=[out(dead)], block_time=None)
        # SEED 的一个 UTXO 在获取交易时触发失败；SEED 的 (u1,0) 由注资交易补足
        def flaky_provider(addr):
            if addr == dead:
                raise ConnectionError("esplora unreachable")
            if addr == SEED:
                return fund_seed_txs({SEED: [ok_spend]})[SEED]
            return []

        result = GraphBuilder().build(SEED, flaky_provider, hops=2)
        assert result.stats.degraded is True
        # 失败分支之外的图结构正常产出
        assert node_id("transaction", "ok_tx") in result.node_ids()
        assert node_id("address", dead) in result.node_ids()


# ---------------------------------------------------------------------------
# 基线对齐：bybit_rust step3_sub2 决策级联 + 计数词表核对
# ---------------------------------------------------------------------------
class TestBaselineAlignment:
    """在封闭 UTXO 图上验证基线决策级联（逐单元计数）与词表对齐。

    拓扑（每个 seed 拥有一个待消费 UTXO，UTXO 恰好计入一个主状态）：
      - S1 的 UTXO 被 crosschain 交易消费 → early_stop_crosschain
      - S2 的 UTXO 被 expanded 交易消费，其两个输出各被 crosschain 消费
        → early_stop_crosschain ×2 与 expanded ×1（并附带两个跨链子分支）
      - S3 的 UTXO 未被任何交易消费 → unspent
      - S4 的 UTXO 被一笔远早于窗口的交易消费 → out_of_range
    合计 => unspent=1, out_of_range=1, crosschain=3, expanded=1。
    """
    CROSSCHAIN = {"x_a": "thorchain", "x_b": "runes", "x_c": "mayachain"}

    def _graph(self) -> dict:
        return {
            "bc1qs1": [MockTx(txid="x_a", inputs=[vin("bc1qs1", 0.5, "u_s1")])],
            "bc1qs2": [
                MockTx(txid="exp", inputs=[vin("bc1qs2", 1.0, "u_s2")],
                       outputs=[out("bc1qb", 0.6), out("bc1qc", 0.3)],
                       block_time=1700000000.0),
            ],
            "bc1qb": [MockTx(txid="x_b", inputs=[vin("bc1qb", 0.6, "exp", 0)])],
            "bc1qc": [MockTx(txid="x_c", inputs=[vin("bc1qc", 0.3, "exp", 1)])],
            "bc1qs3": [MockTx(txid="fund3", outputs=[out("bc1qs3", 0.4)])],
            "bc1qs4": [
                MockTx(txid="old_tx", inputs=[vin("bc1qs4", 0.5, "u_s4")],
                       outputs=[out("bc1qold")], block_time=1500000000.0),
            ],
        }

    def test_closed_graph_termination_stats_match_cascade(self):
        builder = GraphBuilder(crosschain_tx_set=self.CROSSCHAIN)
        results = [builder.build(addr, make_provider(self._graph()), hops=3,
                                 time_window_days=90, seed_block_time=1700000000.0)
                   for addr in ("bc1qs1", "bc1qs2", "bc1qs3", "bc1qs4")]
        merged = dict.fromkeys(
            ["unspent", "out_of_range", "early_stop_wasabi",
             "early_stop_crosschain", "expanded", "tx4_new_dst_hard_stop"], 0)
        for r in results:
            for k, v in r.stats.termination_summary().items():
                merged[k] += v
        assert merged == {
            "unspent": 1,              # S3 的 UTXO 无消费交易
            "out_of_range": 1,         # S4 被 old_tx 消费（远早于窗口）
            "early_stop_wasabi": 0,
            "early_stop_crosschain": 3,  # S1, S2 的两个输出分支
            "expanded": 1,             # S2
            "tx4_new_dst_hard_stop": 0,
        }
        all_edges = [e for r in results for e in r.edges]
        assert sum(e.is_crosschain for e in all_edges) == 3
        assert sum(not e.is_stopped_expansion and e.txid == "exp"
                   for e in all_edges) >= 1

    def test_counter_vocabulary_matches_baseline(self):
        assert set(BFSStats().termination_summary()) == {
            "unspent", "out_of_range", "early_stop_wasabi",
            "early_stop_crosschain", "expanded", "tx4_new_dst_hard_stop",
        }

    def test_unspent_vs_out_of_range_cascade_order(self):
        """级联顺序：unspent 先于 out_of_range 先于 early_stop（基线 Phase1→2）。"""
        spender = MockTx(txid="both", inputs=[vin(SEED, 0.5, "u1")],
                         outputs=[out("bc1qz")],
                         block_time=1000.0)
        result = GraphBuilder(crosschain_tx_set={"both": "thorchain"}).build(
            SEED, make_provider({SEED: [spender]}), hops=1,
            time_window_days=90, seed_block_time=1700000000.0,
        )
        # 唯一 UTXO u1 被消费，因此不是 unspent；消费交易早于窗口 → out_of_range
        assert result.stats.unspent == 0
        assert result.stats.out_of_range == 1
        assert result.stats.early_stop_crosschain == 0


def test_unspent_utxo_with_same_txid_different_vout_not_collapsed():
    """完整三元组 (txid, vout, addr)：同 txid 不同 vout 是不同 UTXO，互不折叠。

    一个交易输出两个 index 到同一地址 A → 这两个是不同 UTXO。D3 下 A 只有一个
    地址节点与一条 tx→A 边（地址级节点去重），但两个 UTXO 各自进入下一层队列，
    若被后续交易分别消费则会产生不同的下游分支。这里验证两个 UTXO 均被入队。
    """
    spender = MockTx(
        txid="multi", inputs=[vin(SEED, 2.0, "u1")],
        outputs=[out("bc1qa", 0.01), out("bc1qa", 0.01), out("bc1qb", 0.01)])
    # bc1qa 的两个 UTXO (multi,0) 与 (multi,1) 都被后续交易消费 → 各自展开
    consumer = {
        "bc1qa": [
            MockTx(txid="sp0", inputs=[vin("bc1qa", 0.01, "multi", 0)],
                   outputs=[out("bc1qb", 0.009)]),
            MockTx(txid="sp1", inputs=[vin("bc1qa", 0.01, "multi", 1)],
                   outputs=[out("bc1qb", 0.009)]),
        ],
    }
    def provider(addr):
        # SEED 拥有 (u1,0)，由注资交易补足；multi 是 SEED 的消费交易，也是 bc1qa 的接收交易
        if addr == SEED:
            return funded_seed
        if addr == "bc1qa":
            return [spender] + consumer["bc1qa"]
        return consumer.get(addr, [])
    funded_seed = fund_seed_txs({SEED: [spender]})[SEED]
    result = GraphBuilder().build(SEED, provider, hops=2)
    # 两个不同 vout 的 UTXO 都被消费 → 产生两条不同的 tx→A 边（深度可达）
    sp_tx_nodes = [n.id for n in result.nodes if n.kind == "transaction"]
    assert f"tx:{'sp0'}" in sp_tx_nodes and f"tx:{'sp1'}" in sp_tx_nodes
    # D3 边按 (source,target) 去重：multi→A 只有一条，但两个消费分支都保留了
    assert sum(e.source == node_id("transaction", "multi")
               and e.target == node_id("address", "bc1qa") for e in result.edges) == 1
