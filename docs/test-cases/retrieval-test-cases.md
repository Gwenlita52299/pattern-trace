# Retrieval 测试用例 · retrieval-spec

> 模块路径：`services/retrieval/`（backend 进程内包）
> 执行环境：PostgreSQL pgvector 可用或 mock 向量库

---

## RT-01 特征向量提取核心维度正确

- **优先级**：P0
- **来源**：§3 结构特征向量定义

**前置条件**
- 构造已知属性的 SubgraphResult：node_count=45, edge_count=120, mixer_contact=3, crosschain=1

**操作步骤**
1. 调用 extract_features(nodes, edges)
2. 检查返回向量前 8 个元素

**预期结果**
- features[0]=45, features[1]=120
- features[2]（density）≈ 120/(45×44)
- features[5]=mixer_contact_count, features[7]=crosschain_count
- 总长度 = FEATURE_DIM (20)，不足维度补 0

---

## RT-02 特征向量归一化后存入 pgvector

- **优先级**：P1
- **来源**：§3 "归一化后存入 pgvector"

**前置条件**
- PostgreSQL pgvector 扩展可用

**操作步骤**
1. 提取特征并写入 patterns.structural_features
2. `SELECT structural_features FROM patterns WHERE id=<id>`

**预期结果**
- 向量维度与 FEATURE_DIM 一致
- 所有分量均为有限数值（无 NaN/Inf）

---

## RT-03 语义 embedding 文本序列化格式

- **优先级**：P1
- **来源**：§4 自然语言描述模板

**前置条件**
- 子图含混币器接触与分层结构

**操作步骤**
1. 调用 serialize_subgraph_to_text(subgraph)
2. 检查输出文本

**预期结果**
- 包含关键数字（节点数/边数/总 BTC 量/时间窗口天数）
- 包含结构描述关键词（CoinJoin/mixer/layers）
- 输出为英文自然语言句子

---

## RT-04 embedding 模型版本锁定校验

- **优先级**：P0
- **来源**：§5 修正版"模型版本必须与 ingest 写入时一致"

**前置条件**
- patterns.embedding_model = "text-embedding-3-small", dim=1536
- backend 进程 runtime 配置为 all-MiniLM-L6-v2 (dim=384)
- backend 定义启动时 fail-fast 校验钩子（检查 DB 中 embedding_model 与配置一致性）

**操作步骤**
1. 启动 backend 进程
2. 观察启动日志与退出行为

**预期结果**
- backend 启动时即 fail-fast 报错退出（非首次查询才报错）
- 错误消息说明模型版本不匹配（embedding_model ≠ configured_model）

---

## RT-05 混合召回权重从服务端读取

- **优先级**：P0
- **来源**：§5 修正版"禁止请求传入"

**前置条件**
- pytest 单测环境就绪（retrieval 为 backend 进程内包，无独立 HTTP 端点）
- w_struct=0.7 / w_semantic=0.3 在 Settings 中

**操作步骤**
1. 断言召回函数签名不含 w_struct/w_semantic 参数
2. 使用 SQLAlchemy 事件监听捕获实际执行的 SQL，断言权重值为参数化绑定

**预期结果**
- 函数签名中不存在可由外部注入的权重参数
- SQLAlchemy event listener 捕获的 SQL 中权重以绑定变量传入（非字符串拼接）
- 权重值始终为 Settings 配置的 0.7/0.3

---

## RT-06 数千条规模下精确扫描性能达标

- **优先级**：P1
- **来源**：§5 修正版"数千条规模接受精确顺序扫描"

**前置条件**
- patterns 表填充 ~5000 条测试向量

**操作步骤**
1. 执行一次混合召回查询
2. 记录耗时

**预期结果**
- 查询耗时 ≤ 50ms（精确扫描在此规模下足够）
- 返回 Top-20 结果按加权距离升序排列

---

## RT-07 两路 ANN 召回规模化方案预留

- **优先级**：P2
- **来源**：§5 修正版规模化演进方向

**前置条件**
- patterns 表 > 100K 行（模拟）

**操作步骤**
1. 启用 ANN 召回模式（两路 HNSW Top-N + RRF 融合）
2. 执行查询

**预期结果**
- 两路各取 Top-N 后融合排序
- 召回质量（recall@10）不低于精确扫描基线的 95%

---

## RT-08 HNSW 索引分别建在两个向量列

- **优先级**：P1
- **来源**：§5 修正版 DDL 说明

**前置条件**
- Alembic 迁移已执行

**操作步骤**
1. `\d patterns` 查看索引列表

**预期结果**
- structural_features 列上有 hnsw 索引（vector_cosine_ops）
- semantic_embedding 列上有独立的 hnsw 索引
- 两列维度不同各自建索引成功

---

## RT-09 WL kernel 同构图返回高分

- **优先级**：P0
- **来源**：§6 WL 子树核精排、§10 测试要点

**前置条件**
- 构造图 A 与其同构副本图 B（节点 ID 不同但结构相同）

**操作步骤**
1. wl_subtree_similarity(A, B)

**预期结果**
- 相似度 ≥ 0.95（接近 1.0）

---

## RT-10 WL kernel 异构图返回低分

- **优先级**：P0
- **来源**：§6 + §10

**前置条件**
- 图 A 为链式结构；图 B 为星形结构

**操作步骤**
1. wl_subtree_similarity(A, B)

**预期结果**
- 相似度 ≤ 0.3

---

## RT-11 WL kernel 带属性区分不同类型节点

- **优先级**：P1
- **来源**：§6 修正版"节点初始标签 = type"

**前置条件**
- 图 A 与 B 结构相同但节点类型分布不同（如 A 有 mixer 节点 B 没有）

**操作步骤**
1. wl_subtree_similarity(A, B)

**预期结果**
- 相似度显著低于同构同类型场景（<0.8）
- 类型差异被捕捉而非忽略

---

## RT-12 WL 相似度空图边界情况

- **优先级**：P1
- **来源**：§10 测试要点最后一条

**前置条件**
- retrieval features 模块可导入

**操作步骤**
1. wl_subtree_similarity({"nodes":[],"edges":[]}, {"nodes":[],"edges":[]})
2. wl_subtree_similarity(empty, nonempty)

**预期结果**
- 双空图返回 1.0
- 空 vs 非空返回 0.0
- 不抛异常

---

## RT-13 cosine similarity 边界值

- **优先级**：P1
- **来源**：§5 SQL 中 <=> 余弦距离语义

**前置条件**
- retrieval features 模块可导入

**前置条件**
- 准备单位向量 a=[1,0], b=[0,1], c=[1,0]

**操作步骤**
1. cosine(a,c) / cosine(a,b)

**预期结果**
- cos(a,c)=1.0; cos(a,b)=0.0
- 结果始终 ∈ [0, 1]

---

## RT-14 差异说明文本生成

- **优先级**：P1
- **来源**：§6 "为每个候选生成差异说明提示"

**前置条件**
- 输入子图比候选 pattern 多 5 个中间节点

**操作步骤**
1. 调用 generate_difference_note(input_graph, candidate_graph)

**预期结果**
- 输出文本包含 "多了 N 个中间节点" 或等效英文表述
- N 数值准确反映节点数差值

---

## RT-15 Top-K 输出结构完整

- **优先级**：P0
- **来源**：§7 RetrievalResult dataclass 定义

**前置条件**
- 知识库中有 ≥ 5 条 patterns
- 固定 mock 向量与相似度阈值确保候选集确定

**操作步骤**
1. 执行一次完整检索流程

**预期结果**
- 返回 PatternCandidate 列表长度 ∈ [min(k_config, 合格候选数), k_config]（在固定数据下应为确定值 3–5 范围内）
- 每个候选包含 pattern_id/name/canonical_subgraph/similarity_score/evidence_grade/difference_note 字段
- similarity_score ∈ [0,1]

---

## RT-16 recall@10 ≥ 80%

- **优先级**：P0
- **来源**：§9 评估指标

**前置条件**
- Lazarus 留出集 20% 已加载（~6K 地址）
- eval_retrieval.py 就绪

**操作步骤**
1. 运行 `python tests/evaluation/eval_retrieval.py`

**预期结果**
- 输出 JSON 报告含 recall_at_10 ≥ 0.80
- 含 mean_reciprocal_rank 与 per_class_breakdown

---

## RT-17 精排后 Top-3 命中率追踪

- **优先级**：P1
- **来源**：§9 "LLM 最终引用的 pattern 在 Top-3 中"

**前置条件**
- 结合 llm-judge 评估脚本运行

**操作步骤**
1. 运行 eval_llm_judge.py 并统计引用命中率

**预期结果**
- 报告含 top_3_hit_rate 字段
- 目标值 ≥ 85%（与 LLM 准确率联动）

---

## RT-18 空子图/单节点不崩溃

- **优先级**：P1
- **来源**：§10 最后一条

**操作步骤**
1. 对空子图和仅含种子的子图分别执行特征提取与检索

**预期结果**
- 不抛异常
- 返回合理的默认值（density=0 等）或明确的"无法匹配"信号
