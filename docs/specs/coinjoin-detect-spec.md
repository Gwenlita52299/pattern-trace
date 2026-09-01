# Spec · 交易结构级 Wasabi CoinJoin 启发式判定（无 CSV 依赖）

> 模块：`backend/detection/coinjoin.py`（核心） + `backend/graph_builder/builder.py`（接线）
> 目标：把 CoinJoin 判定做成**基于交易结构、可解释、确定性的启发式规则**，直接回答
> 「一笔 tx 是否是 CoinJoin」，**移除对 step1_coinjoin/coinjoin_txids.csv 的依赖**，
> 也无需无监督聚类 / ML / 向量库。

## 1. 设计动机

旧做法依赖外部导出的 `coinjoin_txids.csv`（由链外聚类/标记产生）作为 CoinJoin 事实来源。
本方案改为**运行时按交易自身结构判别**，不再读任何标记文件：一条消费交易只要在结构上
符合 CoinJoin 轮廓，GraphBuilder 即按 `early_stop_wasabi` 终止（`is_remixer` 边）。

## 2. 规则集与判定

规则全部作用于该交易自身的 inputs/outputs（`CoinJoinDetector.verdict(tx)`）：

| 规则 | 判定 | 权重 | 说明 |
|---|---|---|---|
| fan_in_out | `input_count>=min_inputs` 且 `output_count>=min_outputs` | 0.20 | 混币需要足够扇入扇出 |
| equal_outputs | 输出值==众数的占比 `>= equal_output_ratio` | 0.25 | 等额输出是 CoinJoin 最强信号 |
| low_entropy | 输出金额熵 `<= max_output_entropy` | 0.15 | 等额输出熵≈0 |
| unique_inputs | 输入地址唯一性 `>= unique_input_ratio` | 0.15 | 混币汇集众多用户 |
| low_fee | `fee/total_input <= max_fee_ratio` | 0.10 | 相对金额的手续费占比 |
| bech32 | 输出 bech32 占比 `>= bech32_ratio` | 0.15 | Wasabi 原生 segwit 输出 |

```
core = fan_in_out AND equal_outputs          # 硬条件
score = Σ(通过规则权重)
is_coinjoin = core AND score >= score_threshold
```

默认阈值集中在 `HeuristicConfig`：
`min_inputs=2, min_outputs=3, equal_output_ratio=0.6, max_output_entropy=1.0,
unique_input_ratio=0.8, max_fee_ratio=0.05, bech32_ratio=0.5, score_threshold=0.5`。
可通过构造 `CoinJoinDetector(config)` 覆盖（启发式随生态演进调参）。

> 防误判：单选输入+多等额输出（airdrop/领奖）因 `min_inputs=2` 不满足 `fan_in_out`，
> 不会被当作 CoinJoin。

## 3. 业务接线

`GraphBuilder._process_utxo` 对每条消费交易判定：

```python
elif (spending_tx.txid in self.coinjoin_txids
      or self.coinjoin_detector.is_coinjoin(spending_tx)):
    outcome = "early_stop_wasabi"
```

`coinjoin_txids` 集合（DB 表）保留为**可选显式标记通道**，可叠加启发式；主判定为启发式。
`spending_tx.txid in coinjoin_txids` 短路，命中集合时不再跑规则。

## 4. 兼容性

- 对 `send`/复杂 `output` 系列 tx，`equal_outputs`/`fan_in_out` 规则默认拦截，保持既有
  expanded 行为。
- 输入兼容 dict 与 SimpleNamespace（live Esplora / fixture provider）。无地址输出（OP_RETURN）
  不计入输出侧特征。

## 5. 验证

`tests/unit/test_coinjoin_detect.py`：特征/规则语义、判定器正反例、配置覆盖、以及
GraphBuilder 在**无 coinjoin_txids 集合、无 CSV** 下仅凭结构判 spending_tx 为 CoinJoin
（`early_stop_wasabi`、`is_remixer` 边）。
