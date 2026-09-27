# Spec · tests/ — 测试策略与评估脚本

> 模块路径：`pattern_trace/tests/`
> 覆盖范围：后端单元 / 集成、前端单元、E2E 全流程、检索评估、性能基准

## 1. 目录结构

```
tests/
├── unit/
│   ├── test_graph_builder.py       # BFS 终止条件、去重、裁剪
│   ├── test_retrieval.py           # 特征计算、WL 核、pgvector 查询
│   ├── test_llm_judge.py           # JSON 解析、引用校验、重试逻辑
│   └── test_auth.py                # JWT 生成验证、密码哈希
├── integration/
│   ├── conftest.py                 # docker-compose 起 db+redis fixture
│   ├── test_api_analyze.py         # analyze 全流程 mock Esplora + LLM
│   ├── test_api_cases.py           # 案件 CRUD + 权限控制
│   └── test_ingest.py              # 入库脚本幂等性
├── e2e/
│   ├── seed_cases.spec.ts          # Playwright 种子案例全流程
│   └── auth.spec.ts               # 登录 → 查询 → 案件流程
├── evaluation/
│   ├── eval_retrieval.py           # recall@10 计算
│   ├── eval_llm_judge.py           # 四档判断一致率计算
│   └── eval_latency.py             # p95 延迟测量
└── benchmark/
    └── bench_graph_builder.py     # BFS 性能基准
```

## 2. 单元测试要点

### test_graph_builder.py

- 五类终止条件各自独立验证
- seen_utxos 复合键去重正确性
- 快照语义正确性（新条目不参与本轮展开）
- 扇出爆炸时规模裁剪生效
- early_stop 正确识别 CoinJoin 和跨链交易
- 与参考实现 Python 基线在同一测试集上结果一致

### test_llm_judge.py

- Mock LLM 返回合法 JSON → 正常解析
- Mock LLM 返回非法 JSON → 重试后成功或达到最大次数失败
- evidence 包含不存在的 txid → 校验拒绝并触发重试
- 缓存命中时不调用 LLM（mock 调用计数 = 0）
- prompt_version 变更后缓存 key 不同

### test_auth.py

- 正确密码登录返回 token
- 错误密码返回 401
- 过期 token 返回 401 且 refresh 成功
- investigator 无法访问 admin-only 接口

## 3. 集成测试

conftest.py 使用 docker-compose 启动真实 PostgreSQL + Redis，mock Esplora HTTP 和 LLM API。

test_api_analyze.py 流程：
1. POST /analyze with demo address → 202
2. Poll GET /judgments/{id} until completed or failed
3. Assert risk_level in ["high","medium","low","no_match"]
4. Assert every evidence ID exists in subgraph_snapshot nodes.id ∪ edges.id
5. Assert latency_ms — mock LLM 固定延迟下断言 < 5000；不做真实公网依赖（Esplora/LLM 均 mock fixture）

## 4. E2E 测试（Playwright）

seed_cases.spec.ts：

1. 打开首页，点击演示地址 chip "Lazarus confirmed"
2. 等待图谱画布渲染完成（React Flow 节点数 > 0）
3. 断言 VerdictCard 显示 HIGH 和 freeze
4. 点击 evidence 列表第一项 → 图谱对应节点高亮（class contains 'highlighted'）
5. 登录 → 创建案件 → 关联该地址 → 导出报告

## 5. 评估脚本

### eval_retrieval.py

```
数据：Lazarus 留出集 20%（~6K 地址）
指标：recall@10 = Top-10 中包含正确 pattern 的比例
目标：≥ 80%
输出：JSON 报告 {recall_at_10, mean_reciprocal_rank, per_class_breakdown}
```

### eval_llm_judge.py

```
数据：留出集 + 人工标注四档标签
指标：accuracy = 与人工标注一致的比例
对比模型：GPT-4o vs Qwen3-30B-A3B-Q4 vs Qwen3-8B-Q4
输出：按模型分列的准确率报告
```

### eval_latency.py

```
场景：10 个演示地址各调用 10 次 analyze
指标：p50 / p95 / p99 全链路延迟
目标：p95 ≤ 10s
输出：延迟分布直方图
```

## 6. 性能基准

bench_graph_builder.py：

```
场景：最坏情况 200 节点 × 5 出边 = 1000 边遍历
测量：纯 Python 循环 vs numpy 向量化 vs PyO3 Rust 绑定
输出：耗时对比表，用于决定是否需要 Rust 加速
触发条件：graph-builder > 3s 时启动 Rust PyO3 重写评估
```

## 7. 验收标准

- [ ] `bash infra/run_unit_tests.sh`（隔离库 `patterntrace_test`）全部通过且覆盖率 ≥ 80%
  - 不可直接 `pytest tests/unit`：单测含全表清理，指向开发库会删掉真实分析记录
  - 破坏性用例的库名守卫见 `scripts/db_guard.py`；CI 同样使用 `patterntrace_test`
- [ ] `pytest tests/integration/` 通过（docker-compose 环境）
- [ ] `npx playwright test` E2E 全部通过
- [ ] `python tests/evaluation/eval_retrieval.py` 输出 recall@10 ≥ 80%
- [ ] `python tests/evaluation/eval_llm_judge.py` 输出准确率 ≥ 85%
- [ ] `python tests/benchmark/bench_graph_builder.py` CPU-only 微基准（mock 数据 ≥100K 边）输出对比表；Rust 评估触发条件改为批量离线场景 >10K 任务/日，非在线延迟驱动
- [ ] E2E 全部跑在 mock Esplora / mock LLM fixture 上，不依赖公网
- [ ] 新增契约测试：SubgraphResult schema 快照 + D3 ID 规范校验（evidence ⊆ node/edge IDs）
- [ ] 集成测试延迟断言为软阈值（CI 抖动上报不失败）
