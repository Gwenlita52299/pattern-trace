"""Wasabi CoinJoin 启发式判定 — 基于交易结构，无 CSV / ML / 聚类依赖。

设计原则：一条交易是否 CoinJoin 由若干**确定性、可解释的规则**直接判定，所有规则只
作用于该交易自身的 inputs / outputs 结构（扇入扇出宽度、等额输出占比、输出金额熵、
输入地址唯一性、手续费占比、输出地址类型）。不读取任何外部标记文件（coinjoin_txids.csv）、
不训练聚类模型、不依赖向量库。—— 即「移除 csv 依赖，直接以启发式规则判别」。

规则集（名称、权重，见 HeuristicConfig）：
    fan_in_out      扇入/扇出宽度：input_count>=min_inputs 且 output_count>=min_outputs
    equal_outputs   等额输出：输出值==众数的占比 >= equal_output_ratio（CoinJoin 最强信号）
    low_entropy     输出金额熵 <= max_output_entropy（等额输出熵≈0）
    unique_inputs   输入地址唯一性 >= unique_input_ratio（混币汇集众多用户）
    low_fee         手续费占比 <= max_fee_ratio（金额 / 手续费相对比例）
    bech32          输出 bech32 占比 >= bech32_ratio（Wasabi 原生 segwit 输出）

判定：`core = fan_in_out`（硬条件，inputs/outputs ≥ 10）；`score` = 各规则按权重加权；
`is_coinjoin = core AND score >= score_threshold`。等额输出不作为硬条件——真实
大型异面额轮次的等额占比可低于 10%，仅在加权规则中贡献最强信号（0.25）。
所有阈值集中在 HeuristicConfig，
默认值按 Wasabi 等额 CoinJoin 的典型轮廓给出，可构造时覆盖（启发式，随生态演进调参）。
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

# 规则 → 权重（和=1.0）
RULE_WEIGHTS: dict[str, float] = {
    "fan_in_out": 0.20,
    "equal_outputs": 0.25,
    "low_entropy": 0.15,
    "unique_inputs": 0.15,
    "low_fee": 0.10,
    "bech32": 0.15,
}


@dataclass(frozen=True)
class HeuristicConfig:
    min_inputs: int = 10
    min_outputs: int = 10
    equal_output_ratio: float = 0.6       # 输出值==众数的占比下界
    max_output_entropy: float = 1.0       # 输出金额熵上界（等额≈0）
    unique_input_ratio: float = 0.8       # 输入地址唯一性下界
    max_fee_ratio: float = 0.05           # fee / total_input 上界
    bech32_ratio: float = 0.5             # 输出 bech32 占比下界
    score_threshold: float = 0.5          # 加权判定分阈值（core 通过后仍需达标）


@dataclass
class CoinJoinVerdict:
    is_coinjoin: bool
    score: float
    core: bool
    rules: dict[str, bool]
    features: dict[str, float]


# ---------------------------------------------------------------------------
# 取值工具：兼容 dict 与 SimpleNamespace / 任意对象（live provider/fixture）
# ---------------------------------------------------------------------------
def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _elem(elem, key, default=None):
    if isinstance(elem, dict):
        return elem.get(key, default)
    return getattr(elem, key, default)


def _entropy(values: list[float]) -> float:
    n = len(values)
    if n <= 1:
        return 0.0
    probs = [cnt / n for cnt in Counter(values).values()]
    return float(-sum(p * math.log2(p) for p in probs))


# ---------------------------------------------------------------------------
# 特征提取
# ---------------------------------------------------------------------------
def extract_features(tx) -> dict[str, float]:
    """提取单笔交易的结构特征（仅用该交易自身 inputs/outputs）。

    无地址输出（OP_RETURN）不计入输出侧特征；input 金额优先取 prevout 解析失败时
    回退 input.value（若字段缺失则以 0 计，只影响 fee_ratio，不影响等额/唯一性）。
    """
    inputs = _get(tx, "inputs") or []
    outputs = [o for o in (_get(tx, "outputs") or []) if _elem(o, "address")]
    n_in, n_out = len(inputs), len(outputs)

    input_vals = [v for i in inputs if (v := _elem(i, "value")) is not None]
    out_vals = [float(_elem(o, "value", 0.0) or 0.0) for o in outputs]

    total_in = _get(tx, "total_input")
    if total_in is None:
        total_in = sum(input_vals) if input_vals else 0.0
    total_in = float(total_in or 0.0)

    total_out = _get(tx, "total_output")
    if total_out is None:
        total_out = sum(out_vals)
    total_out = float(total_out or 0.0)

    fee = _get(tx, "fee")
    if fee is None:
        fee = total_in - total_out

    counts = Counter(out_vals)
    _mode_val, mode_count = counts.most_common(1)[0] if counts else (0.0, 0)
    addr_in = [_elem(i, "address") for i in inputs]

    return {
        "input_count": float(n_in),
        "output_count": float(n_out),
        "equal_output_ratio": (mode_count / n_out) if n_out else 0.0,
        "output_entropy": _entropy(out_vals),
        "unique_input_ratio": (len({a for a in addr_in if a}) / n_in) if n_in else 0.0,
        "fee_ratio": (float(fee or 0.0) / total_in) if total_in > 0 else 0.0,
        "bech32_ratio": (
            sum(1 for o in outputs if str(_elem(o, "address", "")).startswith(("bc1q", "bc1p"))) / n_out
            if n_out else 0.0
        ),
        "total_input": total_in,
        "total_output": total_out,
    }


# ---------------------------------------------------------------------------
# 规则判定
# ---------------------------------------------------------------------------
def evaluate_rules(feats: dict[str, float], cfg: HeuristicConfig) -> dict[str, bool]:
    return {
        "fan_in_out": (feats["input_count"] >= cfg.min_inputs
                       and feats["output_count"] >= cfg.min_outputs),
        "equal_outputs": feats["equal_output_ratio"] >= cfg.equal_output_ratio,
        "low_entropy": feats["output_entropy"] <= cfg.max_output_entropy,
        "unique_inputs": feats["unique_input_ratio"] >= cfg.unique_input_ratio,
        "low_fee": feats["fee_ratio"] <= cfg.max_fee_ratio,
        "bech32": feats["bech32_ratio"] >= cfg.bech32_ratio,
    }


def score_rules(rules: dict[str, bool]) -> float:
    return sum(RULE_WEIGHTS[name] for name, passed in rules.items() if passed)


class CoinJoinDetector:
    """启发式 CoinJoin 判定器。无状态，可复用；规则与阈值集中在 HeuristicConfig。"""

    def __init__(self, config: HeuristicConfig | None = None) -> None:
        self.config = config or HeuristicConfig()

    def verdict(self, tx) -> CoinJoinVerdict:
        feats = extract_features(tx)
        rules = evaluate_rules(feats, self.config)
        # core 仅 fan_in_out（真实案例：大型异面额轮次等额占比可低至 6%，
        # 等额作为硬门会漏判；它保留在加权规则中继续贡献最强信号）
        core = rules["fan_in_out"]
        score = score_rules(rules)
        is_coinjoin = core and score >= self.config.score_threshold
        return CoinJoinVerdict(is_coinjoin=is_coinjoin, score=score, core=core,
                               rules=rules, features=feats)

    def is_coinjoin(self, tx) -> bool:
        return self.verdict(tx).is_coinjoin


def is_coinjoin(tx, config: HeuristicConfig | None = None) -> bool:
    """便捷单笔判定。"""
    return CoinJoinDetector(config).is_coinjoin(tx)
