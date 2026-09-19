# Graphormer 检索向量使用说明

> 数据状态：2026-09 迁移（migration 0009），知识库 = graphormer_v2 数据源
> （9,346 个 confirmed 候选闭包）。本文档描述当前生产链路中 Graphormer
> 的模型来源、输入编码、两个向量产物与检索接入方式。

## 1. 模型

| 项 | 值 |
|---|---|
| 权重 | `clefourrier/graphormer-base-pcqm4mv2`（HuggingFace） |
| 加载方式 | transformers 4.40 `GraphormerModel`，fairseq checkpoint 剥 `encoder.` 前缀 |
| 预训练数据 | PCQM4Mv2（分子图数据集），**未在比特币图上微调，零样本推理** |
| 设备 | MPS（macOS） |
| 上限 | max_nodes 512；ego 图截到 ≤15 入 + ≤15 出邻居 |

诚实边界：这是"借用分子图的通用图编码器"。节点/边类型映射是我们自定义
的 bucket（模型从未见过这些语义），它能 work 依赖度/位置编码捕捉的结构
信号可迁移；unseen-similarity 基准（conf-hit@10 99.0%，dev/5 轮验证）
实证了该用法在检索任务上有效。

## 2. 输入编码（把比特币图映射进分子接口）

Graphormer 的分子接口 → 我们的图（`ego_graphormer.py` / `run_g.py`）：

| 接口 | 映射 |
|---|---|
| `input_nodes`（atom slot, F=1） | 地址类型：`bc1`→0 / `3`开头→1 / 其他→2（pad=0，真实 id ≥1） |
| `in_degree` / `out_degree` | 度数 log 桶 1..8（cap 7） |
| `spatial_pos` | BFS 短路距离 1..5；>5 跳 = 16（不可达）；0 = self/pad |
| `input_edges`（最短路 5 槽 × 2） | 边特征 log 桶（含金额 val_bucket 1..6） |
| `attn_edge_type` | 全 0（multi_hop 模式未用） |

## 3. 两个向量产物（同一 checkpoint 的两个视角）

### 3.1 pooled（入库索引向量）

- 来源：`last_version/graphormer_test/seed_subgraph_features.npz` 的 `pool`
  （602,933 个闭包的 768d 节点嵌入池化），由 `run_g.py` 推理产出
- 检索向量 = `[768d pool | 16 标量 z]` → 784d，L2 归一化：
  - 标量 16 维 = `scal_features`（层级边计数 log10、layer1 等额占比 eq1、
    跨链/悬挂比例、深度均值、recv/sent/utxo 聚合、censored 占比），
    经全库 602,933 行的 `StandardScaler`（μ/σ 存于 npz）
- 9,346 个 confirmed 候选的向量存于
  `ingest/seed/graphormer_v2/derived/graphormer_pooled.npz`
  → 入库为 `patterns.graphormer_embedding vector(784)`（迁移 0009，HNSW）

### 3.2 ego（查询侧成员向量）

- 来源：`embeddings_full_full.npz` 的 `ego`（602,933 节点 per-node 768d），
  其中候选成员 union（12,643 地址）抽取为
  `derived/ego_members.npz`
- 用途：查询子图的成员 ego 向量**深度加权池化**（与基准 run_round 一致）：
  `q_pool = Σ w_i·ego[a_i] / Σ w_i, w_i = 1/(1+first_layer_i)`
  再拼标量 z 分数 → 784d 查询向量

同一 checkpoint 保证 stage1 召回与查询向量在同一嵌入空间（cos 可比的前提）。

## 4. 检索接入（pattern_trace）

```
backend/retrieval/graphormer.py      查询侧：ego 池化 + canonical 标量 z（查表，评测/已知地址）
backend/retrieval/graphormer_online.py 查询侧：在线 ego 前向（issue #82，live 全覆盖）
backend/retrieval/retriever.py       stage1 召回 + 精排组合 + 查询向量三级链
ingest/load_graphormer_candidates.py 入库侧：pooled 直写 + graphormer_model_id 锁
```

### 查询向量三级链（issue #82）

`retriever.retrieve` 按 `graphormer_query_mode`（auto|online|ego|off）：

1. **online**（auto 默认）：`graphormer_online.query_vector_online(canon)`——
   canonical → addr→addr 投影（跨 tx 金额求和、attrs 取最大单笔，run_g 语义）
   → per-address ego 图（≤15+15，F=7/S=4 逐行照抄 run_g.py）→ 前向 →
   ego hidden → 1/(1+first_layer) 加权池化 + 16 标量 z → 784d。
   **live 新地址全覆盖**（不再依赖 ego 语料覆盖率）。任何异常 → None 自动降级
2. **ego**：ego_members.npz 查表池化（覆盖率 ≥50% 门槛，现状协议）
3. **hybrid**：20d 结构特征 + 文本描述 embedding 加权召回

在线对齐验证（scripts/verify_graphormer_alignment.py，FULL_ADJ min cos
≥0.99 为合入门）：
- FULL_ADJ 口径（全库邻接复刻）：cos = 1.0000（5/5）——编码逐位正确
- CLOSURE_ONLY 口径（仅闭包内邻接，live 现实上界）：cos ≈ 0.9747

### 召回（stage1，基准 STAGE1_N 协议）

- 查询向量可构造时：`_graphormer_recall` = 全索引精确 cosine 扫描 top-500
  （显式 `SET LOCAL enable_indexscan = off`，HNSW 近似可能漏边界候选）；
  SQL 按 `graphormer_model_id = settings.graphormer_model_name` 过滤（模型锁）
- 注意方向契约：`AS dist` = `<=>` 余弦**距离**，rerank 用
  `cos = 1 - dist` 还原相似度（0908c05 修复的反转 bug，勿回退）
- online/ego 均不可构造 → hybrid 召回（20 维结构特征 + 文本描述 embedding）

### 精排（benchmark 最终组合，conf-hit@10 99.0%）

```
final = 0.1·cos + 0.1·wljac + 0.8·ov
```

- cos 通道 = Graphormer pooled cosine（即 stage1 分数）
- wljac/fp/ov 从 `retrieval_fingerprint` JSONB 列计算（ingest 预计算，
  单一代码路径 `channels.build_fingerprint → scores_from_fingerprints`），
  避免回传 MB 级 canonical JSONB
- align 通道（基准用 per-node ego 贪心对齐）基准最终权重为 0，
  生产未启用——权重 >0 显式报错

## 5. 离线评估入口

- 索引向量矩阵 + seed 映射：`graphormer.index_vectors()`
- 基准脚本（对照实验的权威来源）：
  `last_version/graphormer_test/benchmark_unseen_sim.py`（dev 网格 + 5 轮验证）
- 报告：`last_version/graphormer_test/unseen_sim_report.md`

## 6. 已知限制（诚实边界）

1. 零样本：PCQM4Mv2 预训练权重对金融图是外推使用，输入 bucket 语义自定义；
2. ego 截断：查询/闭包图 >15 邻居被截，深链结构信息部分丢失；
3. 大闭包截断：249 个含巨型 hub（56.7 万出边）的闭包 canonical 边
   封顶 2,500 确定性截断——这 249 条的 wljac/fp 与基准完整口径有偏差，
   ov 通道（权重 0.8）不受影响；
4. 模型/向量版本锁（issue #82）：`patterns.graphormer_model_id` 列 +
   `_graphormer_recall` SQL 过滤 + 在线前向同权重前提。换模型需重建
   derived npz、全量重入库并更新 settings.graphormer_model_name
   （无增量迁移路径）；
5. 在线前向的额外边界：canonical 无 time_delta 字段，边 slot3 以 0 兜底
   （离线有真实时间差）；在线 ego 邻居上下文仅来自当前分析子图
   （离线是全库 6M 边）——CLOSURE_ONLY cos≈0.975 是 live 协议现实上界。
