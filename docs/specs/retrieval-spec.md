# Spec · services/retrieval — 混合检索与 WL 精排模块

> 模块路径：`pattern_trace/services/retrieval/`
> 数据库依赖：PostgreSQL pgvector (HNSW index)
> 输入：graph-builder 的 SubgraphResult
> 输出：Top-K 已证实洗钱 pattern 列表（供 LLM judge 使用）

> **实现状态（2026-08-25 校准）**：结构特征向量 MVP 实现 8 维核心特征，
> 其余维度零填充至 FEATURE_DIM=20（ingest 与查询两侧同函数，空间自洽但
> 区分度低于本 spec 的 ~20 维设计）；归一化未做，原始计数直接进余弦距离。
> 语义 embedding 为确定性 stub。扩维/归一化/真 embedding 属后续迭代项。

## 1. 职责

将输入子图转换为结构特征向量 + 语义 embedding，与知识库中的 pattern 进行混合召回和精排，返回最相似的 Top-K pattern 及其相似度分数。

## 2. 流程

```
SubgraphResult
    │
    ├─→ 结构特征提取 ──────────┐
    │   (图元计数/出入度分布/密度)  │
    │                            ├──→ pgvector HNSW 混合查询 → Top-20 召回
    └─→ 语义 embedding 计算 ────┘
                                     │
                                     ▼
                          WL 子树核精排 → Top-3~5
                                     │
                                     ▼
                          返回 pattern + similarity_score + 差异说明提示
```

## 3. 结构特征向量

从 SubgraphResult 提取以下特征（拼接为一个 float 向量）：

```python
[
    node_count,                  # 节点总数
    edge_count,                  # 边总数
    density,                     # edge_count / (node_count * (node_count-1))
    avg_out_degree,              # 平均出度
    max_out_degree,              # 最大出度
    mixer_contact_count,         # 接触混币器节点的数量
    stopped_expansion_count,     # early_stop 边数量
    crosschain_count,            # 跨链边数量
    layer_depth_distribution,    # [d0_count, d1_count, d2_count]
    value_concentration,         # top-5 output value / total output value
    chain_length_max,            # 最长链长度
    branching_factor_mean,       # 平均分叉因子
    # ... 共 ~20 维
]
```

归一化后存入 pgvector。

## 4. 语义 Embedding

将子图序列化为自然语言描述文本：

```
"A transaction subgraph rooted at an address with 45 nodes and 120 edges.
The root address received funds from 3 sources and sent to a known CoinJoin transaction.
After the mixer, outputs split into 12 addresses across 2 layers before reaching exchange addresses.
Total volume ~15 BTC across 90 days."
```

使用 embedding 模型（OpenAI text-embedding-3-small 或本地 sentence-transformers）编码为向量。

## 5. 混合召回

```sql
SELECT p.id, p.name,
       1 - (p.structural_features <=> :query_struct_vec) AS struct_sim,
       1 - (p.semantic_embedding <=> :query_semantic_vec) AS semantic_sim
FROM patterns p
ORDER BY (
    :w_struct * (p.structural_features <=> :query_struct_vec)
  + :w_semantic * (p.semantic_embedding <=> :query_semantic_vec)
)
LIMIT 20;
```

- 权重：`w_struct = 0.7`, `w_semantic = 0.3`（仅服务端配置读取，禁止请求传入；SQL 一律绑定参数）
- **性能说明（重要修正）**：两列加权和排序无法利用 HNSW 索引。知识库数千条规模下接受精确顺序扫描（实测 <50ms）；规模化后改为两路 ANN 召回（各自 HNSW Top-N）+ 应用层 RRF/加权融合。两个向量维度不同，DDL 中分别建独立 HNSW 索引供 ANN 路径使用
- embedding 模型版本必须与 ingest 写入时一致（patterns.embedding_model 锁定校验，不一致直接报错拒绝服务）
- 召回 Top-20 进入精排；负样本不入本召回库（见 ingest-spec §3a）

## 6. WL 子树核精排

- 对输入子图和每个候选 pattern 的 canonical_subgraph 计算**带属性的 WL 子树核相似度**
- 实现：自定义带属性 WL kernel——节点初始标签 = 类型 + script_type 分桶 + 金额数量级分桶 + time_delta 分桶；迭代聚合邻居多重集
- 相似度公式：各轮次子树标签多重集的 Jaccard 加权平均（networkx 的 `weisfeiler_lehman_graph_hash` 只输出哈希、无法给出连续分数，不可直接使用）
- 取 Top-3 ~ Top-5 作为最终候选；性能目标先 benchmark 再固化（纯 Python 20 候选 × 200 节点的 100ms 目标为乐观估计）
- 为每个候选生成差异说明提示（"输入子图比 pattern 多了 N 个中间节点"），附加到 LLM prompt 中
- subgraph 相似性比较前先做规范化序列化（节点按 ID 排序、浮点统一精度），保证 hash 与比较结果稳定

## 7. 输出格式

```python
@dataclass
class RetrievalResult:
    candidates: list[PatternCandidate]

@dataclass
class PatternCandidate:
    pattern_id: str
    name: str                     # e.g. "mixer_layering_3hop"
    description: str
    canonical_subgraph: dict      # 用于 LLM 上下文
    similarity_score: float       # 0–1
    structural_similarity: float
    semantic_similarity: float
    wl_kernel_score: float
    evidence_grade: str           # A or B
    difference_note: str | None   # 差异说明（给 LLM）
```

## 8. 性能目标

- 特征提取 + embedding 计算 ≤ 200ms
- pgvector 查询 ≤ 50ms（HNSW 索引命中）
- WL 精排 ≤ 100ms（20 候选 × 200 节点图对）
- 总计 ≤ 350ms（热路径；冷路径叠加 graph-builder 网络时间另计，见 backend 预算表）

## 9. 评估指标

- recall@10 ≥ 80%（Lazarus 留出集 20% 作测试）
- 精排后 Top-3 命中率（LLM 最终引用的 pattern 在 Top-3 中）

## 10. 测试要点

- [ ] 结构特征向量计算与 bybit_rust 一致
- [ ] pgvector HNSW 查询返回正确排序
- [ ] WL 核对同构图返回高分，异构图返回低分
- [ ] 差异说明文本能被 LLM 正确理解和使用
- [ ] 空子图 / 单节点子图不崩溃
