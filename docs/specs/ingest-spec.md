# Spec · ingest/ — 数据入库与知识库构建脚本

> 模块路径：`pattern_trace/ingest/`
> 数据源：btc_aml_forensics 已产出子图 Parquet 文件 + 公开地址标签清单
> 目标：将正负样本 pattern 写入 PostgreSQL pgvector 知识库

> **实现状态（2026-08-25 校准）**：基线 golden fixture 仅 3 个 seed，正样本主语料
> 由 spec 外新增的 `corpus_gen.py` 确定性合成（source=lazarus_synth，与 confirmed
> 区分）；负样本为 salary/exchange/dormant 三模板合成（live 抓取为 P2）；
> embedding 当前仅 SHA-256 stub（DB 元数据沿用占位模型名）。真实 Parquet 语料、
> 公开浏览器负样本与真 embedding provider 接入前，检索指标按合成口径解读。
>
> **issue #10（provenance 分离）**：知识库每行带 `provenance`（confirmed | synthetic |
> negative）区分「真实链上证据」与「合成结构模板」。合成样本 `provenance='synthetic'`，
> `evidence_grade='S'`，只作检索参考、不伪装成真实 Grade A 证据；负样本
> `provenance='negative'`。所有消费方（retrieval / LLM prompt / 前端 / 报告）据此
> 把合成 pattern 与真实确认样本区分对待。

## 1. 目录结构

```
ingest/
├── load_lazarus_subgraphs.py    # 从 Parquet 切图并写入 patterns 表
├── generate_negatives.py         # 构造普通钱包负样本子图
├── load_labels.py               # 地址标签 / CoinJoin txid 标记入库（无 crosschain CSV）
├── compute_embeddings.py         # 批量计算语义 embedding + 结构特征向量
└── run_all.py                   # 一键执行全流程
```

## 2. 正样本来源

数据源可切换（`settings.lazarus_subgraph_source`）：

- **cluster_k7（默认，真实数据）**：`ingest/seed/patterns/cluster_k7/<C>/` 按簇分目录，
  各含 `nodes/edges/seeds.parquet`（seeds 与节点表 `first_layer==0` 冗余，不单独读取）；
  节点表无 `direct_related_to_lazarus` 列，由 Lazarus 标签表回填
- **golden（基线 fixture）**：`/Users/gwenlita/Documents/bybit_rust/golden/python/results/step3_subgraph/`
  单文件布局（subgraph_nodes.parquet / subgraph_edges.parquet）

配套标签（§4）：`ingest/seed/lazarus_btc_stolen_addresses.csv` → addresses_meta
（labels=["lazarus"]），作为混币器/接触过滤证据并回填 direct_related_to_lazarus。
`ingest/seed/test_top20_*.csv` 为评测候选集，不入库。

处理步骤：
1. 读取 Parquet → 按 seed_address 分组为独立子图
2. 过滤：保留节点数 ≥ 5 且包含混币器接触的子图
3. 为每个子图生成 canonical_subgraph JSON
4. 计算结构特征向量（复用 retrieval/specs 中的特征定义）
5. 生成语义描述文本 → 计算 embedding
6. 计算 WL 子树核指纹
7. 写入 patterns 表，evidence_grade = 'A'，source = 'lazarus_confirmed'，provenance = 'confirmed'

预期产出：~数千条正样本 pattern（cluster_k7 实测 6899 seed → 4227 通过过滤）。

### 2a. 合成正样本（source=lazarus_synth，provenance=synthetic）

背景：本地 golden fixture 仅 3 个 seed，不满足检索阶段「数千条」规模要求。
`corpus_gen.py` 从已确认场景的拓扑特征（CoinJoin 入口 → 分层 peel / 扇出 → 跨链桥
逃逸）派生结构变体，确定性合成正样本并写入 `patterns` 表。

**用途与限制**：合成样本是**结构模板**，用于补足结构检索的召回规模与多形态覆盖；
它们**不代表真实 Bitcoin 交易或真实案件证据**，仅作为检索参考，不得被解释为
真实链上确认证据。为此（issue #10）：
- `source = 'lazarus_synth'`，`provenance = 'synthetic'`；
- `evidence_grade = 'S'`（synthetic template），**不再是**真实样本的 `'A'` 语义；
- LLM 判断 prompt 会标记 `evidence_status: not_real_on_chain_evidence`，并要求模型
  **不得把候选自身当作区块链证据引用**；
- 前端知识库列表把此类 pattern 标注为「合成模板」，报告/评估指标将其与真实样本拆分统计。

真实 golden 场景由 `load_lazarus_subgraphs.py` 以 `source='lazarus_confirmed'`、
`provenance='confirmed'`、`evidence_grade='A'` 入库，二者永不混淆。

## 3. 负样本构造

> **已停用（2026-09，项目决策）**：真实数据阶段无负样本来源，合成负样本不再
> 生成，pattern_negatives 保持为空。隔离原则（§3a）继续有效；真实负样本来源
> （公开浏览器抓取，P2）接入后恢复本节与 IG-04/05/06。

- 从公开区块链浏览器获取普通钱包地址的交易数据
- 条件：无混币器接触、无黑名单标签、交易频率正常
- 按 3:1 负正比例构造同等规模子图
- 使用相同的特征计算和 embedding 流程
- evidence_grade = 'B', source = 'constructed_normal', provenance = 'negative'

### 3a. 负样本隔离（重要）

负样本写入**独立的 `pattern_negatives` 表**（结构与 patterns 一致，无业务向量索引），仅用于：
- 检索阈值校准（区分"像洗钱"与"像正常"的分数分布）
- 评估脚本中的误报率测量
**不得进入 patterns 业务召回库**，避免把"正常钱包 pattern"作为候选喂给 judge。

## 4. 标签表加载

标签集合（跨链判定不再来自标签表，issue #5 已移除 crosschain_tx_set）：

| 标签类型 | 来源 | 存储位置 |
|---|---|---|
| 混币器地址 | 公开清单 + Lazarus 项目沉淀 | addresses_meta.labels |
| CoinJoin 交易 ID | **交易结构级启发式判定**（`backend/detection/coinjoin.py`，无 CSV） | GraphBuilder 运行时按规则判别；`coinjoin_txids` 表为可选显式标记通道 |

加载脚本将上述集合加载到 PostgreSQL，供 graph-builder 启动时读取到内存。
CoinJoin 的**主判定**已改为启发式规则（见 graph-builder-spec §4），`coinjoin_txids` csv
不再作为来源（移除 csv 依赖）。
跨链协议判定（issue #5）改由 `backend/detection/crosschain.py::CrosschainDetector`
在运行时按 Esplora 交易字段（OP_RETURN / pegout）直接判别，**不读取
op_returns_interesting.csv，也不写入 crosschain_tx_set 表**。

## 5. Embedding 计算

- 模型：OpenAI text-embedding-3-small（或本地 sentence-transformers/all-MiniLM-L6-v2）
- 批量处理，每批 100 条
- 结果写入 patterns.semantic_embedding vector 列
- 结构特征向量写入 patterns.structural_features vector 列
- 同时写入 `embedding_model` 和 `embedding_dim` 元数据列（模型版本锁定，防止跨环境漂移；换模型需全量重建并迁移）

## 6. 幂等性

- 所有脚本支持重复运行不产生重复记录
- upsert key 改为 `(seed_address, content_hash)`（内容哈希 = canonical_subgraph 规范化序列化后的 sha256；`(name, source)` 在数千条 Lazarus 子图中极易同名碰撞，弃用）
- embedding 计算结果按 pattern_id 缓存到本地 JSON

## 7. 验收标准

- [ ] run_all.py 一键执行完成，patterns 表有正负样本数据
- [ ] 正负样本比例 ≈ 3:1（负:正）
- [ ] pgvector HNSW 索引创建成功
- [ ] 查询任一 pattern 可返回完整 canonical_subgraph 和两个向量
- [ ] 标签表数据可供 graph-builder 加载使用
