# Spec · ingest/ — 数据入库与知识库构建脚本

> 模块路径：`pattern_trace/ingest/`
> 数据源：btc_aml_forensics 已产出子图 Parquet 文件 + 公开地址标签清单
> 目标：将正负样本 pattern 写入 PostgreSQL pgvector 知识库

> **实现状态（2026-08-25 校准）**：基线 golden fixture 仅 3 个 seed，正样本主语料
> 由 spec 外新增的 `corpus_gen.py` 确定性合成（source=lazarus_synth，与 confirmed
> 区分）；负样本为 salary/exchange/dormant 三模板合成（live 抓取为 P2）；
> embedding 当前仅 SHA-256 stub（DB 元数据沿用占位模型名）。真实 Parquet 语料、
> 公开浏览器负样本与真 embedding provider 接入前，检索指标按合成口径解读。

## 1. 目录结构

```
ingest/
├── load_lazarus_subgraphs.py    # 从 Parquet 切图并写入 patterns 表
├── generate_negatives.py         # 构造普通钱包负样本子图
├── load_labels.py               # 地址标签 / CoinJoin txids / crosschain set 入库
├── compute_embeddings.py         # 批量计算语义 embedding + 结构特征向量
└── run_all.py                   # 一键执行全流程
```

## 2. 正样本来源

数据源路径（本地）：
```
/Users/gwenlita/Documents/bybit_rust/golden/python/results/step3_subgraph/
├── subgraph_nodes.parquet
├── subgraph_edges.parquet
└── _edges_batches/*
/Users/gwenlita/Documents/bybit_rust/golden/python/results/step2_label/
```

处理步骤：
1. 读取 Parquet → 按 seed_address 分组为独立子图
2. 过滤：保留节点数 ≥ 5 且包含混币器接触的子图
3. 为每个子图生成 canonical_subgraph JSON
4. 计算结构特征向量（复用 retrieval/specs 中的特征定义）
5. 生成语义描述文本 → 计算 embedding
6. 计算 WL 子树核指纹
7. 写入 patterns 表，evidence_grade = 'A'，source = 'lazarus_confirmed'

预期产出：~数千条正样本 pattern。

## 3. 负样本构造

- 从公开区块链浏览器获取普通钱包地址的交易数据
- 条件：无混币器接触、无黑名单标签、交易频率正常
- 按 3:1 负正比例构造同等规模子图
- 使用相同的特征计算和 embedding 流程
- evidence_grade = 'B', source = 'constructed_normal'

### 3a. 负样本隔离（重要）

负样本写入**独立的 `pattern_negatives` 表**（结构与 patterns 一致，无业务向量索引），仅用于：
- 检索阈值校准（区分"像洗钱"与"像正常"的分数分布）
- 评估脚本中的误报率测量
**不得进入 patterns 业务召回库**，避免把"正常钱包 pattern"作为候选喂给 judge。

## 4. 标签表加载

三张标签集合：

| 标签类型 | 来源 | 存储位置 |
|---|---|---|
| 混币器地址 | 公开清单 + Lazarus 项目沉淀 | addresses_meta.labels |
| CoinJoin 交易 ID | Wasabi / JoinMarket 公开标记 | coinjoin_txids (PostgreSQL table) |
| 跨链 OP_RETURN 协议映射 | Thorchain / 侧链协议公开文档 | crosschain_tx_set |

加载脚本将上述集合从 CSV/JSON 加载到 PostgreSQL，供 graph-builder 启动时读取到内存。

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
