# Spec · services/graph-builder — BFS 子图构建模块

> 模块路径：`pattern_trace/services/graph-builder/`
> 参考实现：btc_aml_forensics (bybit_rust) `src/step3/step3_sub2_bfs_loop.rs`
> 数据源：Blockstream Esplora 公共 API
> 语言：MVP Python（asyncio + aiohttp + numpy）；P1 可选 Rust PyO3 绑定

> **实现状态（2026-08-25 校准）**：容错层已接线——live 模式走同步容错 provider
> （重试退避 / 429 Retry-After / 熔断切备用端点 / Redis L2，与异步 EsploraClient
> 共享熔断器与缓存键）；unspent 终止依据响应自带 vout spent 状态；out_of_range
> 由编排层传入 seed_block_time 生效。BFS 三队列元素已与 bybit_rust 基线一致改为
> **UTXO 元组**（见 §2）：每层的队列元素是 `(utxo_txid, output_index, owner)`，
> 展开单元是一次 UTXO 消费（spent_by 解析），而非旧实现的「地址节点」。per-layer
> 裁剪已修正为 spec 的「每层共享 N」而非「每队列条目 N」。
> 仍偏离本 spec 的一点：Edge 缺 time_delta/tx_fee_ratio/fanout_ratio 等特征字段
> （连带 WL 分桶以层深替代）。

## 1. 输入输出

### 输入

```python
@dataclass
class GraphBuilderInput:
    seed_address: str          # 用户输入的根地址
    hops: int = 3              # 最大跳数（API 上限 1–3，见 backend D6）
    time_window_days: int = 90 # 时间窗口（天）
    max_nodes_per_layer: int = 50
    max_total_nodes: int = 200
```

### 输出

```python
@dataclass
class SubgraphResult:
    nodes: list[Node]           # ≤ max_total_nodes，id 规范见下
    edges: list[Edge]           # 节点间有向边，含特征，每条边有稳定 id
    stats: BFSStats             # 终止条件统计、耗时、裁剪计数
```

**全局 ID 契约（D3）**：节点 `addr:<address>` / `tx:<txid>`；边 `edge:<src_id>-><dst_id>`。
该 ID 空间是 evidence 引用与前端高亮的唯一事实源。

## 2. 分层三队列 BFS

- **队列元素 = UTXO 元组** `(utxo_txid, output_index, owner)`，与基线
  `QueueEntry = (txid, n, block_height, tx_index, address)` 同构
  （PatternTrace 数据源用 block_time，故省略 block_height/tx_index）。
  `owner` 是拥有该 UTXO 的地址；展开单元是**一次 UTXO 消费**（spent_by 解析出消费交易）。
- `layer0_queue`: 种子地址 UTXO → 消费交易产生 tx2 边（depth=1）
- `layer1_queue`: tx2 输出 → 消费交易产生 tx3 边（depth=2）
- `layer2_queue`: tx3 输出 → 消费交易产生 tx4 边（depth=3）
- 最大深度 = hops（默认 3）
- **快照语义**：每轮处理开始时对队列取快照，展开新条目留待下一轮
- spent_by 解析：一个 UTXO `(txid, n)` 的消费交易 = 其 owner 交易列表中 input
  prevout == `(txid, n)` 的那一笔（Esplora vin 自带 prevout txid/vout；fixture
  由 `FixtureTxProvider._build_spent_by_ledger` 两遍法重建等价账本）。

## 3. 五类终止条件

| 条件 | 触发时机 | 行为 |
|---|---|---|
| unspent | UTXO 在数据范围内未被花费 | 记录节点状态，不生成出边 |
| out_of_range | spending tx block_time < seed_block_time − time_window_days*86400（早于窗口起点即停止） | 停止该分支扩展 |
| early_stop | spending tx ∈ coinjoin_txids 或 crosschain_tx_set | 生成 is_stopped_expansion=true 边，不向交易内部展开 |
| tx4_new_dst | depth=3 目标地址此前未见 | 硬停止该分支 |
| queue_empty | 三队列全部耗尽 | 正常结束 |

## 4. early-stop 交易级集合

标签表必须包含：

- `coinjoin_txids: set[str]`：已知 CoinJoin / Wasabi / 混币器交易 ID 集合
- `crosschain_tx_set: dict[str, str]`：跨链 OP_RETURN txid → protocol 映射（e.g. "thorchain"）

这些集合从 PostgreSQL 标签表加载到内存 set/dict。

## 5. 去重与节点状态

- `seen_utxos: set[tuple[str, int, str]]`：精确复合键 `(txid, output_index, address)`
- **禁止 hash 截断**——必须使用完整三元组防止碰撞导致节点遗漏

NodeState 字段：
```python
{
    "first_layer": bool,         # 是否为种子直接关联
    "layer_span": int,           # 距种子跳数
    "total_received_btc": float,
    "total_sent_btc": float,
    "utxo_count": int,
    "direct_related_to_lazarus": bool,
    "script_type": str,          # P2PKH / P2SH / Bech32 ...
}
```

## 6. Edge 特征 Schema

```python
{
    "id": str,                   # edge:<src_id>-><dst_id>（D3）
    "time_delta": {"block_height_diff": int, "tx_index_diff": int},
    "total_num_inputs": int,
    "total_num_outputs": int,
    "tx_fee_ratio": float,
    "fanout_ratio": float,
    "dst_value_btc": float,
    "tx_total_input_btc": float,
    "tx_total_output_btc": float,
    "value_ratio": float,
    "is_stopped_expansion": bool,
    "is_remixer": bool,
    "is_crosschain": bool,
    "op_return_protocol": str | None,
}
```

## 7. Esplora 数据获取

- Base URL: `https://blockstream.info/api`
- 关键接口：
  - `GET /address/{addr}/txs` → 地址最近交易列表
  - `GET /tx/{txid}` → 单笔交易详情（inputs/outputs）
- 并发控制：`asyncio.Semaphore(10)` 同时最多 10 个 HTTP 请求
- 缓存策略：
  - **`async-lru`**（`lru_cache` 不支持协程，禁止使用）maxsize=2048 缓存 tx 详情，key 含 Esplora base URL
  - Redis 二级缓存 TTL=24h
  - 演示地址启动时批量预取并写入缓存

### 7.1 容错与降级

- 重试：指数退避 + jitter，最多 3 次；429 按 `Retry-After` 等待
- 熔断：连续 5 次失败后 circuit breaker 打开 30s，期间快速失败
- 备用 provider：mempool.space（配置切换）；规模化可自托管 Electrs
- 部分失败语义：单分支数据获取失败 → 该分支标记 `stats.degraded=true` 并继续其余分支，不整体失败
- 并发预算：Semaphore(10) 仅对自托管端点使用；公共 Blockstream API 降至 5 并发以遵守限速

## 8. 规模裁剪规则

| 规则 | 默认值 | 说明 |
|---|---|---|
| max_hops | 3 | 对应原实现固定 3 层 |
| max_nodes_per_layer | 50 | 每层最多展开 50 个新节点 |
| max_total_nodes | 200 | 子图总节点硬上限 |
| fanout_truncate_threshold | 20 | 单节点出度超过此值折叠为摘要节点 |

## 9. 性能目标（D7 冷/热双口径）

- **热路径**（演示地址已预热）：BFS 计算耗时 ≤ 500ms（不含网络等待）
- **冷路径**（真实抓取）：含 Esplora 网络 RTT 全程 ≤ 4s（200 节点 / 5 并发估算），全链路预算见 backend-api-spec
- 内存峰值 < 50MB per task
- 无内存泄漏（连续调用 1000 次 RSS 不增长 > 10%）
- Rust PyO3 定位为**批量离线分析**加速选项（≥100K 边场景），非在线查询优化

## 10. 测试要点

- [ ] 五类终止条件各自独立验证
- [ ] seen_utxos 复合键去重正确（无碰撞遗漏）
- [ ] 快照语义正确（本轮新条目不参与本轮展开）
- [ ] 规模裁剪在扇出爆炸场景下生效
- [ ] early_stop 正确识别 CoinJoin 和跨链交易
- [ ] 与 bybit_rust Python 基线在同一测试数据集上结果一致
