# Graph Builder 测试用例 · graph-builder-spec

> 模块路径：`services/graph-builder/`（backend 进程内包，D1）
> 执行环境：Python 3.12 + mock Esplora fixture（不依赖公网）

---

## GB-01 五类终止条件——unspent 终止分支

- **优先级**：P0
- **来源**：§3 表格 unspent 行、§10 测试要点第 1 条

**前置条件**
- 种子地址拥有一个 UTXO（某交易输出给它），但没有任何交易消费它（该 UTXO 无 spent_by）

**操作步骤**
1. 调用 `builder.build(seed_address, hops=1)`
2. 检查返回的 SubgraphResult

**预期结果**
- `stats.unspent == 1`（展开单元 = 该无消费方的 UTXO）
- 该分支不产生向下展开的边（无 spent_by → 无消费交易 → 无出边）

---

## GB-02 out_of_range 按 block_time 判定

- **优先级**：P0
- **来源**：§3 out_of_range 行（已修正为 block_time 口径）

**前置条件**
- seed_block_time = 1700000000
- time_window_days = 90
- Mock 返回一笔 block_time = 1600000000 的交易（远早于窗口起点）

**操作步骤**
1. 调用 build 并检查 stats 与边

**预期结果**
- `stats.terminated_out_of_range >= 1`
- 该交易对应边标记 stopped_expansion
- 不向该交易输出地址继续扩展

---

## GB-03 窗口内交易正常扩展

- **优先级**：P0
- **来源**：GB-02 反向验证

**前置条件**
- 同上但 tx block_time = 1699990000（窗口内）

**操作步骤**
1. 调用 build

**预期结果**
- `terminated_out_of_range == 0`
- 正常生成后续地址节点与出边

---

## GB-04 block_time 缺失时不裁剪

- **优先级**：P1
- **来源**：容错语义——缺失数据不应误终止

**前置条件**
- Mock 交易的 block_time = None

**操作步骤**
1. 调用 build（time_window_days=90, seed_block_time 已设）

**预期结果**
- 该交易正常参与 BFS 扩展
- 无 out_of_range 统计增加

---

## GB-05 early_stop CoinJoin 识别

- **优先级**：P0
- **来源**：§3 early_stop 行、§4 coinjoin_txids 集合

**前置条件**
- GraphBuilder 初始化时传入 coinjoin_txids={"known_cj_txid"}
- Mock 返回该 txid 的交易

**操作步骤**
1. 调用 build hops=1

**预期结果**
- `stats.terminated_early_stop == 1`
- 对应边 is_stopped_expansion=True
- 不向交易内部展开

---

## GB-06 early_stop crosschain 带协议标签

- **优先级**：P0
- **来源**：§3 + §4 crosschain_tx_set 映射

**前置条件**
- crosschain_tx_set={"bridge_tx": "thorchain"}
- Mock 返回 bridge_tx

**操作步骤**
1. 调用 build

**预期结果**
- 对应边 `is_crosschain=True`
- 边上携带 `op_return_protocol="thorchain"`
- `terminated_early_stop >= 1`

---

## GB-07 seen_utxos 完整三元组去重

- **优先级**：P0
- **来源**：§5 禁止 hash 截断要求

**前置条件**
- Mock 一笔交易有 3 个 output：(idx=0, addr=A)、(idx=1, addr=A)、(idx=2, addr=B)

**操作步骤**
1. build hops=1
2. 收集所有 address 类型的节点 label

**预期结果**
- 地址 A 和 B 各自出现为独立节点
- 相同地址 A 出现在不同 output_index 时是**两个不同的 UTXO**，各自进入下一层队列；
  若后续被不同交易分别消费，会产生两条不同的下游分支
- seen_utxos 中条目数为 3（完整三元组）

---

## GB-08 快照语义——本轮新条目留待下一轮

- **优先级**：P0
- **来源**：§2 快照语义说明、§10 测试要点第 3 条

**前置条件**
- 构造链式数据 layer0→tx→addr1→tx→addr2→tx→addr3→tx→addr4

**操作步骤**
1. build hops=3
2. 检查最终节点集

**预期结果**
- addr4 不在结果中（depth 计数定义：种子=depth 0，每扩展一层 depth+1；hops=3 时最大 depth=3，addr4 位于 depth=4 被裁剪）
- 每层严格按快照处理，无同层递归展开

---

## GB-09 max_total_nodes 硬上限裁剪

- **优先级**：P0
- **来源**：§8 规模裁剪表 max_total_nodes=200

**前置条件**
- Mock 种子有 300 笔扇出交易
- builder 设置 max_total_nodes=50

**操作步骤**
1. build hops=1

**预期结果**
- `len(result.nodes) <= 50`（硬上限不被突破）
- `stats.truncated_total_nodes` > 0（具体字段名以实现为准，此处指代被裁剪节点计数器）

---

## GB-10 max_nodes_per_layer 限制

- **优先级**：P0
- **来源**：§8 max_nodes_per_layer=50 默认值

**前置条件**
- 单层 100 笔消费交易，builder 设 max_nodes_per_layer=10

**操作步骤**
1. build hops=1

**预期结果**
- 本层最多展开 10 个新交易节点（**每层共享**预算，而非每队列条目）
- 超限的消费交易不计入后续队列展开（`truncated_per_layer` 增加）

---

## GB-11 fanout_truncate_threshold 折叠摘要节点

- **优先级**：P1
- **来源**：§8 fanout_truncate_threshold=20

**前置条件**
- Mock 一个地址有 > 20 个 output 的单笔交易

**操作步骤**
1. build 后检查该地址子树

**预期结果**
- 超过阈值的输出折叠为一个摘要节点（显示"N more outputs..."）
- 摘要节点不计入有效分析维度
- 补充断言：摘要节点计入 max_total_nodes 计数（防止折叠后仍突破硬上限）

---

## GB-12 queue_empty 正常结束

- **优先级**：P0
- **来源**：§3 queue_empty 行

**前置条件**
- Mock 种子地址没有任何交易（无任何拥有的 UTXO）

**操作步骤**
1. build hops=3

**预期结果**
- result.stats.queue_empty == True
- nodes 仅含种子一个节点
- edges 为空数组

---

## GB-13 全局 ID 规范 D3 一致性

- **优先级**：P0
- **来源**：D3 决策、backend §3 subgraph 接口

**前置条件**
- 任一成功构建的 SubgraphResult

**操作步骤**
1. 遍历所有 node.id
2. 遍历所有 edge.id
3. 检查 edge source/target 引用完整性

**预期结果**
- 所有节点 id 以 `addr:` 或 `tx:` 开头
- 所有边 id 以 `edge:` 开头且格式为 `edge:<src>-><dst>`
- 每个 edge.source 和 edge.target 都能在 node_ids() 中找到

---

## GB-14 hops 参数校验拒绝越界

- **优先级**：P0
- **来源**：validate_hops 函数 + D6 决策

**前置条件**
- GraphBuilder 模块可导入，无需外部依赖

**操作步骤**
1. 分别调用 validate_hops(0) / validate_hops(4) / validate_hops(-1)

**预期结果**
- 每次均抛 ValueError，消息含 "between 1 and 3"
- validate_hops(1/2/3) 正常通过

---

## GB-15 async-lru 缓存 key 含 base URL

- **优先级**：P1
- **来源**：§7.1 缓存策略修正（lru_cache 不能用于协程）

**前置条件**
- 配置两个不同 Esplora base URL

**操作步骤**
1. 用 URL_A 请求同一 txid → 缓存 miss
2. 用 URL_B 请求同一 txid → 观察 HTTP 请求是否发出

**预期结果**
- URL_B 发出新请求（缓存 key 不同）
- 同一 URL 下重复请求命中缓存不发 HTTP

---

## GB-16 Esplora 重试指数退避 + jitter

- **优先级**：P1
- **来源**：§7.1 容错重试策略

**前置条件**
- Mock Esplora 前 2 次 500，第 3 次 200
- 注入时钟/可替换 sleep 函数（如 `asyncio.sleep` 被 mock 为记录调用参数）

**操作步骤**
1. build 过程中捕获每次 sleep 的延迟参数

**预期结果**
- 共发出 3 次请求后成功
- sleep 参数依次为 [1s±30% jitter, 2s±30% jitter]（指数增长）
- 最终结果正确
- 不使用真实等待，单测执行 <100ms

---

## GB-17 circuit breaker 连续失败打开

- **优先级**：P1
- **来源**：§7.1 连续 5 次失败 → breaker 打开 30s

**前置条件**
- Mock Esplora 持续返回 500
- 注入时钟使 breaker 状态可即时推进
- 明确"连续 5 次失败"是否将内部重试计入（与实现口径一致）

**操作步骤**
1. 连续触发 6 次请求
2. 断言第 6 次请求不发 HTTP 直接快速失败
3. 推进注入时钟 +30s 后断言 breaker 进入 half_open

**预期结果**
- 第 6 次请求立即快速失败（不发 HTTP）
- breaker 状态为 open，open_until 时间戳 = now()+30s
- 期间日志记录 CIRCUIT_OPEN

---

## GB-18 备用 provider mempool.space 切换

- **优先级**：P2
- **来源**：§7.1 备用 provider

**前置条件**
- 主 provider Blockstream 持续超时触发熔断
- FALLBACK_PROVIDER=mempool.space 已配置

**操作步骤**
1. 熔断打开后推进注入时钟至 half_open 窗口
2. 发起请求观察实际使用的 base URL

**预期结果**
- 请求切换到 mempool.space base URL
- 数据结构兼容，build 结果一致

---

## GB-19 部分失败 degraded 标志

- **优先级**：P1
- **来源**：§7.1 部分失败语义

**前置条件**
- 3 个分支中 1 个分支的数据获取持续失败

**操作步骤**
1. build 完成

**预期结果**
- 其余 2 个分支正常完成
- `stats.degraded = True`
- 失败分支不影响整体结果可用性

---

## GB-20 公共 API 并发降至 Semaphore(5)

- **优先级**：P2
- **来源**：§7.1 并发预算说明

**前置条件**
- 使用带并发计数器的 mock server 替代公共端点（不依赖公网）

**操作步骤**
1. 并发发起 ≥10 个 build 请求
2. 从 mock server 侧读取 in-flight 峰值计数

**预期结果**
- 同时 in-flight 请求峰值 ≤ 5（Semaphore 生效）
- 配置 Semaphore(10) 后峰值 ≤ 10

---

## GB-21 Redis 二级缓存 TTL 24h

- **优先级**：P2
- **来源**：§7 缓存策略

**前置条件**
- Redis 可访问
- 进程内 async-lru 一级缓存已清空（或使用两个独立进程验证）

**操作步骤**
1. 进程 A 请求某 txid → 写入缓存
2. 清空进程 A 的内存 LRU 缓存（或由独立进程 B 发起第二次请求）
3. `TTL <key>` 检查过期时间并再次请求

**预期结果**
- TTL ≈ 86400s（±60s）
- 第二次请求命中 Redis 二级缓存不发 HTTP（排除一级 LRU 干扰）

---

## GB-22 内存泄漏检测连续 1000 次

- **优先级**：P1
- **来源**：§9 性能目标 RSS 增长约束

**前置条件**
- psutil 或 tracemalloc 可用

**操作步骤**
1. 循环调用 build 1000 次（mock provider）
2. 每次记录当前进程 RSS

**预期结果**
- 第 1000 次 RSS - 第 1 次 RSS ≤ 初始 RSS × 10%
- 无持续线性增长趋势

---

## GB-23 热路径 BFS 计算 ≤ 500ms

- **优先级**：P1
- **来源**：§9 D7 热路径口径

**前置条件**
- 演示地址已预热（Redis 二级缓存全命中；进程内 LRU 已清空以测真实路径）
- build 完全脱离网络 I/O（mock provider 本地运行）

**操作步骤**
1. 连续执行 build ≥50 次
2. 取 p95 耗时

**预期结果**
- p95 elapsed_ms ≤ 500（样本量 ≥50，统计稳定）

---

## GB-24 Rust PyO3 定位验证——批量场景基准

- **优先级**:P2
- **来源**：§9 最后一条 + tests-spec benchmark 修订

**前置条件**
- bench_graph_builder.py 就绪，使用 mock 数据 ≥100K 边

**操作步骤**
1. 运行纯 Python CPU-only 微基准
2. 如有 Rust 扩展则同时运行对比

**预期结果**
- 输出耗时对比表（非在线延迟指标）
- 结论仅用于批量离线分析决策，不驱动在线查询优化
