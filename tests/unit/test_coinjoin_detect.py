"""backend.detection.coinjoin 启发式判定单测 — 无 CSV / ML / 聚类。

覆盖：
- extract_features / evaluate_rules：结构特征与规则语义。
- CoinJoinDetector.verdict：等额 Wasabi 式混币判为 CoinJoin；普通交易判否。
- 业务接线：GraphBuilder 在**无 coinjoin_txids 集合、无 CSV** 的情况下，
  仅凭交易结构判 spending_tx 为 CoinJoin → early_stop_wasabi。
全部为合成数据，不依赖外部数据与 DB。
"""
from dataclasses import dataclass

from backend.detection.coinjoin import (
    CoinJoinDetector,
    HeuristicConfig,
    evaluate_rules,
    extract_features,
)
from backend.graph_builder.builder import GraphBuilder


def out(addr: str, value: float) -> dict:
    return {"address": addr, "value": value}


def coinjoin_tx(txid: str = "cj_struct", *, n_in: int = 12, n_out: int = 12,
                amount: float = 0.1) -> dict:
    """等额 Wasabi 式混币：多输入、等额多输出、bech32、输入地址全唯一。"""
    inputs = [{"address": f"bc1q_in_{j}", "value": amount} for j in range(n_in)]
    outputs = [{"address": f"bc1q_out_{j}", "value": amount} for j in range(n_out)]
    return {"txid": txid, "inputs": inputs, "outputs": outputs,
            "total_input": amount * n_in, "total_output": amount * n_out, "fee": 0.0}


def normal_tx(txid: str = "normal") -> dict:
    """普通多输入输出：金额分散、输入地址混合、输出非 bech32 等额。"""
    inputs = [{"address": f"a{j}", "value": 0.2 if j == 0 else 0.1} for j in range(3)]
    outputs = [{"address": f"addr{j}", "value": 0.35 - 0.1 * j} for j in range(3)]
    total_in = sum(i["value"] for i in inputs)
    total_out = sum(o["value"] for o in outputs)
    return {"txid": txid, "inputs": inputs, "outputs": outputs,
            "total_input": total_in, "total_output": total_out, "fee": total_in - total_out}


# ---------------------------------------------------------------------------
# 特征与规则
# ---------------------------------------------------------------------------
class TestFeaturesAndRules:
    def test_coinjoin_equal_amount_features(self):
        feats = extract_features(coinjoin_tx())
        assert feats["input_count"] == 12 and feats["output_count"] == 12
        assert feats["equal_output_ratio"] == 1.0
        assert feats["output_entropy"] == 0.0
        assert feats["unique_input_ratio"] == 1.0
        assert feats["bech32_ratio"] == 1.0

    def test_coinjoin_passes_all_rules(self):
        rules = evaluate_rules(extract_features(coinjoin_tx()), HeuristicConfig())
        assert all(rules.values())

    def test_normal_tx_fails_core(self):
        # 普通交易 3 in / 3 out：低于新宽度阈值 10/10，core 直接拦截
        rules = evaluate_rules(extract_features(normal_tx()), HeuristicConfig())
        assert rules["fan_in_out"] is False
        assert rules["equal_outputs"] is False    # 金额分散，加权规则也拦截

    def test_single_input_many_equal_outputs_not_coinjoin(self):
        # 单选输出+多等额输出（airdrop/领奖类）不是 CoinJoin：扇入宽度不足
        tx = {"txid": "airdrop", "inputs": [{"address": "a", "value": 2.0}],
              "outputs": [{"address": "bc1q_o", "value": 0.5}] * 6}
        v = CoinJoinDetector().verdict(tx)
        assert v.rules["fan_in_out"] is False     # input_count=1 < min_inputs=2
        assert v.is_coinjoin is False


# ---------------------------------------------------------------------------
# 判定器
# ---------------------------------------------------------------------------
class TestDetector:
    def test_coinjoin_verdict_true(self):
        v = CoinJoinDetector().verdict(coinjoin_tx())
        assert v.is_coinjoin is True and v.core is True
        assert v.score >= 0.5

    def test_normal_verdict_false(self):
        v = CoinJoinDetector().verdict(normal_tx())
        assert v.is_coinjoin is False

    def test_config_override_tightens(self):
        strict = HeuristicConfig(equal_output_ratio=1.0, unique_input_ratio=1.0,
                                 bech32_ratio=1.0, max_fee_ratio=0.0, score_threshold=0.95)
        assert CoinJoinDetector(strict).is_coinjoin(coinjoin_tx()) is True
        # 普通交易在严格阈值下更应判否
        assert CoinJoinDetector(strict).is_coinjoin(normal_tx()) is False

    def test_module_convenience(self):
        from backend.detection.coinjoin import is_coinjoin
        assert is_coinjoin(coinjoin_tx()) is True
        assert is_coinjoin(normal_tx()) is False


# ---------------------------------------------------------------------------
# 业务接线：GraphBuilder 仅凭结构判定，无 coinjoin_txids / CSV
# ---------------------------------------------------------------------------
@dataclass
class MockTx:
    txid: str
    inputs: list = None
    outputs: list = None
    block_time: float | None = None


def fund_seed_txs(tx_map):
    """为 tx_map 中所有 input 的 prevout 自动补注资交易（输出侧-only 根枚举辅助）。"""
    from collections import defaultdict

    builder = defaultdict(list)
    for addr, txs in tx_map.items():
        builder[addr].extend(txs)
    existing = {getattr(t, "txid", None) for txs in builder.values() for t in txs}

    pending = defaultdict(dict)
    for addr, txs in tx_map.items():
        for tx in txs:
            for inp in (getattr(tx, "inputs", None) or []):
                ptxid = inp.get("prev_txid")
                if not ptxid or ptxid in existing:
                    continue
                pvout = inp.get("prev_vout", 0)
                owner = inp.get("address")
                value = inp.get("value", 0.1)
                pending[ptxid].setdefault(pvout, (owner, value))

    for ptxid, outs in pending.items():
        maxidx = max(outs)
        outputs = [{"address": None, "value": 0.0} for _ in range(maxidx + 1)]
        for idx, (owner, value) in outs.items():
            outputs[idx] = out(owner, value)
        funded_owners = set()
        for owner, _v in outs.values():
            if owner in funded_owners:
                continue
            funded_owners.add(owner)
            builder[owner].append(MockTx(txid=ptxid, outputs=outputs))
    return dict(builder)


def make_provider(tx_map):
    funded = fund_seed_txs(tx_map)
    return lambda addr: funded.get(addr, [])


SEED = "bc1qseed000000000000000000000000000000000000000000000000qa"


class TestBuilderWiring:
    def test_structural_coinjoin_triggers_early_stop_without_set(self):
        # SEED 拥有外部 UTXO (u1,0)；spender 是结构上的等额 CoinJoin，但**不在**
        # coinjoin_txids 集合里（默认空集），也无 CSV。启发式仍应判 CoinJoin。
        spender = MockTx(
            txid="cj_struct",
            inputs=[{"address": SEED, "value": 0.1, "prev_txid": "u1", "prev_vout": 0}]
                   + [{"address": f"bc1q_in_{j}", "value": 0.1,
                       "prev_txid": f"u{j}", "prev_vout": 0} for j in range(11)],
            outputs=[out(f"bc1q_out_{j}", 0.1) for j in range(12)],
        )
        builder = GraphBuilder()  # coinjoin_txids 默认空集
        result = builder.build(SEED, make_provider({SEED: [spender]}), hops=1)
        assert result.stats.early_stop_wasabi == 1
        stopped = [e for e in result.edges if e.is_stopped_expansion]
        assert len(stopped) == 1 and all(e.is_remixer for e in stopped)

    def test_normal_tx_not_stopped(self):
        spender = MockTx(
            txid="normal_tx",
            inputs=[{"address": SEED, "value": 0.3, "prev_txid": "u1", "prev_vout": 0},
                    {"address": "a2", "value": 0.1, "prev_txid": "u2", "prev_vout": 0},
                    {"address": "a3", "value": 0.1, "prev_txid": "u3", "prev_vout": 0}],
            outputs=[out("addr0", 0.25), out("addr1", 0.15), out("addr2", 0.05)],
        )
        builder = GraphBuilder()
        result = builder.build(SEED, make_provider({SEED: [spender]}), hops=1)
        # 未被误判为 CoinJoin：不产生 remixer / stopped 边，交易被正常展开成输出
        assert result.stats.early_stop_wasabi == 0
        assert not any(e.is_remixer or e.is_stopped_expansion for e in result.edges)
        assert result.stats.expanded + result.stats.tx4_new_dst_hard_stop >= 1
