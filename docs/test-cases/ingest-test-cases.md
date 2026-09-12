# Ingest 测试用例 · ingest-spec

> 模块路径：`ingest/`
> 数据源：本地 Parquet 文件 + 公开标签清单 CSV/JSON

---

## IG-01 run_all.py 一键执行产出正样本

- **优先级**：P0
- **来源**：§7 验收标准第 1 条
- **修订（2026-09）**：项目决策切换真实数据源后**无负样本**（负样本来源
  待接入真实数据，见 IG-04 停用说明），验收口径改为仅正样本

**前置条件**
- Parquet 数据文件存在于配置路径
- PostgreSQL pgvector 就绪

**操作步骤**
1. `python ingest/run_all.py`
2. 检查 patterns 表行数

**预期结果**
- 脚本无报错完成
- patterns 表有正样本：confirmed（source=lazarus_confirmed, grade=A）
  与 synthetic（source=lazarus_synth, grade=S）
- 总量数千条级别
- pattern_negatives 表为空（本阶段无负样本，不得有残留行）

---

## IG-02 Lazarus 子图按 seed_address 分组切图

- **优先级**：P0
- **来源**：§2 处理步骤第 1 条

**前置条件**
- Parquet 含多个 seed_address 的混合数据

**操作步骤**
1. 运行 load_lazarus_subgraphs.py
2. 查询 patterns 表 source=lazarus_confirmed 的 distinct canonical_subgraph->seed 数

**预期结果**
- 每个 seed_address 对应独立一条 pattern 记录
- 不同 seed 的子图数据未混淆

---

## IG-03 过滤保留 ≥ 5 节点且混币器接触

- **优先级**：P1
- **来源**：§2 过滤条件

**前置条件**
- 数据集中存在 3 节点子图和无混币器接触的子图

**操作步骤**
1. 运行入库脚本
2. 查询这些子图是否入库

**预期结果**
- 节点数 < 5 的子图不入库
- 无 mixer 接触的正样本不入库
- 符合条件的子图全部入库

---

## IG-04 负样本写入独立表隔离 — **已停用**

- **优先级**：~~P0~~ 已停用（2026-09）
- **来源**：§3a 负样本隔离（评审架构师 #16）
- **停用原因**：项目决策——真实数据阶段暂无负样本来源，合成负样本
  （generate_negatives）不再执行。隔离原则（负样本绝不入 patterns 业务召回库）
  仍然有效：pattern_negatives 表必须为空。待真实负样本来源接入后恢复本用例。

<details>
<summary>原用例（存档）</summary>

**前置条件**
- generate_negatives.py 已执行

**操作步骤**
1. `SELECT COUNT(*) FROM patterns WHERE source='constructed_normal'`
2. `SELECT COUNT(*) FROM pattern_negatives`

**预期结果**
- 步骤 1 结果 = 0（负样本不在业务召回库）
- 步骤 2 结果 > 0（负样本在独立表中）

</details>

---

## IG-05 负正比例 ≈ 3:1 — **已停用**

- **优先级**：~~P1~~ 已停用（2026-09）
- **来源**：§3 按 3:1 比例构造
- **停用原因**：同 IG-04——无负样本则无比例可言；run_all 汇总校验中
  的 IG-05 比例区间检查随负样本停用一并移除。

<details>
<summary>原用例（存档）</summary>

**前置条件**
- 正样本 N 条已知

**操作步骤**
1. 统计 pattern_negatives 行数 vs patterns 中 source=lazarus_confirmed AND grade=A 行数（统一正样本定义口径）

**预期结果**
- 负:正比例 ∈ [2.5, 3.5] 区间（允许 ±17% 波动）

</details>

---

## IG-06 负样本满足无混币器/无黑名单条件 — **已停用**

- **优先级**：~~P1~~ 已停用（2026-09）
- **来源**：§3 条件约束
- **停用原因**：同 IG-04——合成负样本不再生成，无样本可校验；
  待真实负样本来源（公开浏览器抓取）接入后恢复。

<details>
<summary>原用例（存档）</summary>

**前置条件**
- generate_negatives.py 完成

**操作步骤**
1. 抽样 100 条负样本
2. 交叉比对标签清单中的混币器地址集

**预期结果**
- 无一例接触混币器地址
- 无一例命中黑名单标签

</details>

---

## IG-07 标签集合入库

- **优先级**：P0
- **来源**：§4 标签表加载

**前置条件**
- 混币器地址清单 + CoinJoin txids JSON 就绪
- `ingest/seed/lazarus_btc_stolen_addresses.csv` 就绪（Lazarus 被盗地址标签）
- 跨链判定不再预备 crosschain_tx_set（issue #5 已移除）；不需要 op_returns CSV

**操作步骤**
1. `python ingest/load_labels.py`
2. 分别查询标签表

**预期结果**
- addresses_meta.labels 包含混币器标记
- addresses_meta 含 labels=["lazarus"] 条目（source=lazarus_stolen_csv），
  实测约 4.5k 条；与 coinjoin 重叠地址的 labels 为两者并集（不互相覆盖）
- coinjoin_txids 表有 Wasabi/JoinMarket 条目
- 不再创建查询 crosschain_tx_set 表（链路已删除）
- graph-builder 启动时可成功加载到内存 set/dict
- 跨链终止由运行时 CrosschainDetector 按交易字段判别

---

## IG-08 embedding 写入含版本元数据

- **优先级**：P0
- **来源**：§5 修订版 embedding_model/dim 列

**前置条件**
- compute_embeddings.py 配置 model=text-embedding-3-small
- 使用本地 embedding stub 或录制回放（recorded fixture），不调真实 OpenAI API

**操作步骤**
1. 执行脚本（stub 模式返回 1536 维向量）后查询任一 pattern

**预期结果**
- semantic_embedding 向量非空且维度 = 1536
- embedding_model 列 = "text-embedding-3-small"
- embedding_dim 列 = 1536

---

## IG-09 结构特征向量写入正确维度

- **优先级**：P1
- **来源**：§5 结构特征向量列

**前置条件**
- compute_embeddings.py 已执行完成

**操作步骤**
1. 查询任一 pattern 的 structural_features

**预期结果**
- 维度与 retrieval FEATURE_DIM 一致
- 分量均为有限数值

---

## IG-10 幂等性——重复运行不重复插入

- **优先级**：P0
- **来源**：§6 幂等性要求

**前置条件**
- run_all.py 已执行过一次

**操作步骤**
1. 再次运行 run_all.py
2. 对比前后 patterns 行数

**预期结果**
- 行数不变（upsert 覆盖而非新增）
- 不抛唯一约束冲突异常

---

## IG-11 upsert key 使用 (seed_address, content_hash)

- **优先级**：P0
- **来源**：§6 修订版 upsert key（弃用 name+source）

**前置条件**
- 数据集中有两个不同 seed 产生了同名 pattern name="mixer_layering_3hop"

**操作步骤**
1. 入库后查询同名 pattern 数量

**预期结果**
- 两条记录均存在（不被误判为重复）
- 各自 content_hash 不同且非空
- 联合唯一约束 (seed_address, content_hash) 生效

---

## IG-12 WL fingerprint 计算并存入

- **优先级**：P1
- **来源**：§2 处理步骤第 6 条

**前置条件**
- load_lazarus_subgraphs.py 已执行完成

**操作步骤**
1. 查询任一 pattern 的 wl_fingerprint 字段

**预期结果**
- 非空 JSONB 且结构可比较（如多重集 hash 列表）

---

## IG-13 pgvector HNSW 索引创建成功

- **优先级**：P0
- **来源**：§7 验收标准第 3 条

**前置条件**
- Alembic 或手动 DDL 已执行

**操作步骤**
1. `\di` 检查索引列表

**预期结果**
- structural_features 和 semantic_embedding 各有 hnsw 索引
- EXPLAIN 显示可用索引扫描路径（配合 `SET enable_seqscan=off` 或填充足量测试数据避免优化器选 Seq Scan）

---

## IG-14 embedding 缓存按 pattern_id 本地 JSON

- **优先级**:P2
- **来源**：§6 最后一条

**前置条件**
- compute_embeddings.py 已运行

**操作步骤**
1. 检查本地缓存目录
2. 清空 DB 后重跑（跳过 API 调用模式）

**预期结果**
- 缓存文件按 pattern_id 组织
- 重跑时从缓存读取不再调用 embedding API


## IG-15 Embedding API 失败断点续跑

- **优先级**:P1
- **来源**：IG-14 补充——异常路径覆盖

**前置条件**
- 本地 embedding stub 可模拟超时/限流响应
- 部分向量已写入 patterns 表

**操作步骤**
1. 使 stub 在处理第 N 条时返回 429
2. 重跑 compute_embeddings.py
3. 检查 API 调用次数与最终向量完整性

**预期结果**
- 重跑时已缓存的 pattern 不再调用 API（跳过已有向量的行）
- 仅补算缺失/失败的向量
- 全部完成后所有 pattern 的 semantic_embedding 非空且维度一致

---

## IG-16 Parquet 数据损坏容错

- **优先级**:P2
- **来源**：IG-01 补充——异常输入覆盖

**前置条件**
- 准备三种损坏 Parquet 文件：schema 字段缺失、类型漂移（期望 int64 实际 string）、空文件（0 行）

**操作步骤**
1. 分别以三种文件运行 load_lazarus_subgraphs.py
2. 检查错误输出与数据库写入

**预期结果**
- 三种场景均报清晰错误信息（指出具体文件与字段问题）
- 数据库零写入（不产生半截 patterns 记录）

---

## IG-17 ingest 双实例并发安全

- **优先级**:P2
- **来源**：IG-10 补充——并发幂等性

**前置条件**
- PostgreSQL pgvector 就绪
- upsert key (seed_address, content_hash) 唯一约束存在

**操作步骤**
1. 同时启动两个 run_all.py 实例处理相同数据集
2. 等待两个进程完成后统计 patterns 行数

**预期结果**
- 行数不变（唯一约束保证 upsert 幂等，或 advisory lock 保证串行执行）
- 无死锁或未处理异常导致进程崩溃
