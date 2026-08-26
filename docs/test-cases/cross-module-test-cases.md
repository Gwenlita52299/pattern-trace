# 跨模块集成测试用例 · D1–D7 决策验证

> 覆盖模块间契约与端到端流程

---

## CM-01 D1 进程内包编排——analyze 全链路 happy path

- **优先级**：P0
- **来源**：spec-review-summary D1 + backend §3 处理流程

**前置条件**
- 全栈环境运行（backend+worker+db+redis+mock Esplora+mock LLM）
- 演示地址白名单含目标地址

**操作步骤**
1. POST /addresses/analyze {address, hops=3, time_window_days=90}
2. 用 poll_url 轮询至终态
3. GET /addresses/{address}/subgraph

**预期结果**
- 终态 status=completed
- risk_level ∈ 四档
- subgraph 接口返回与 judgment.subgraph 一致的数据
- 架构约束验证：import 边界架构测试通过（graph-builder/retrieval/llm-judge 无 HTTP client import，仅函数调用）——此断言由独立架构测试守护，本用例保留功能断言

---

## CM-02 D3 ID 契约端到端闭环

- **优先级**：P0
- **来源**：D3 决策

**前置条件**
- CM-01 完成

**操作步骤**
1. 脚本遍历 judgment.evidence 全集，逐一在 subgraph.nodes ∪ edges 中查找精确匹配（API 层结构断言）
2. 从 evidence 取一条 ID 在前端点击该 evidence（UI 断言，联动 FE-05）

**预期结果**
- evidence 中的每个 ID 都能在 subgraph.nodes ∪ edges 中精确匹配（脚本断言覆盖率=100%）
- 前端高亮命中正确元素（UI 层断言）
- 三处（builder 生成→judge 引用→前端渲染）ID 字符串完全一致

---

## CM-03 D5 状态机全生命周期

- **优先级**：P0
- **来源**：D5 决策

**前置条件**
- Mock LLM 正常返回
- Mock Esplora 注入延迟使 processing 阶段可观测
- pytest 集成测试环境可读取状态迁移事件表/日志（非仅 HTTP 轮询采样）

**操作步骤**
1. 发起 analyze 后读取状态迁移事件表中的完整序列
2. 对比合法状态机定义

**预期结果**
- 事件表记录的完整迁移序列为 queued → processing → completed（或 failed）
- 不出现非法跳变（如 queued 直接 completed）
- 终态后不再有新事件写入

---

## CM-04 失败路径——LLM 校验失败全链路降级

- **优先级**：P0
- **来源**：LJ-06 + BE-14 联动

**前置条件**
- 已登录 investigator（非白名单地址需认证才可达 LLM 阶段）
- Mock LLM 所有响应 evidence 均引用不存在 ID

**操作步骤**
1. 发起 analyze 并轮询至终态
2. 检查 DB 中 judgment 记录和 Redis 缓存

**预期结果**
- judgment.status=failed + error_code=LLM_VALIDATION_FAILED
- Redis 中无对应 cache_key 写入
- 前端显示错误卡与重试按钮（联动 FE-03）

---

## CM-05 D7 冷路径全链路延迟预算

- **优先级**：P1
- **来源**：D7 冷/热双口径

**前置条件**
- 清空所有 Redis 缓存
- Mock Esplora RTT=200ms、LLM 延迟=500ms

**操作步骤**
1. 对未预热地址发起 analyze
2. 记录从 POST 到终态总耗时

**预期结果**
- 总耗时 ≤ 20s（冷路径上限）
- 各阶段耗时分解符合预算表（graph ≤4s + retrieval ≤0.5s + LLM ≤4s + 序列化写入 ≤0.5s + 余量）

---

## CM-06 热路径 p95 ≤10s 全链路

- **优先级**：P0
- **来源**：tests-spec §5 eval_latency 目标 + D7

**前置条件**
- 10 个演示地址已预热

**操作步骤**
1. 每地址调用 analyze ≥30 次
2. 以服务端时间戳差（POST 接收时间到 status=completed 写入时间）计时
3. 缓存命中路径与冷路径分别统计 p50/p95/p99

**预期结果**
- 缓存命中路径服务端耗时 p95 ≤ 10000ms
- 冷路径单独报告（目标 ≤20s 由 CM-05 覆盖）
- eval_latency.py 输出直方图

---

## CM-07 builder_version 升级使下游缓存失效

- **优先级**：P1
- **来源**：BE-39 + LJ-09 联动

**前置条件**
- 地址 A 已有 gb-v1 的完整分析结果缓存
- 修改 BUILDER_VERSION 后需重启进程生效

**操作步骤**
1. 将 BUILDER_VERSION 升级为 gb-v2
2. 相同参数再次分析地址 A

**预期结果**
- graph-builder 重新执行 BFS（不走 Redis 子图缓存）
- llm-judge cache key 不同 → 重新调用 LLM
- 新 judgment 的 builder_version 字段 = gb-v2

---

## CM-08 embedding 模型升级触发全量重建提示

- **优先级**：P1
- **来源**：RT-04 + IG-08 联动

**前置条件**
- patterns 表已有 text-embedding-3-small 向量

**操作步骤**
1. 将 retrieval runtime 配置改为 all-MiniLM-L6-v2
2. 尝试发起检索

**预期结果**
- 启动或首次查询时报错拒绝服务
- 提示需要重新 ingest 全量重建

---

## CM-09 负样本隔离不影响业务召回

- **优先级**：P0
- **来源**：IG-04 + RT 联动

**前置条件**
- pattern_negatives 表有数据；patterns 表有正样本

**操作步骤**
1. 执行一次正常检索查询
2. 检查候选列表来源

**预期结果**
- 所有候选均来自 patterns 表（source=lazarus_confirmed）
- 无 constructed_normal 出现在候选中
- 负样本仅在校准脚本中被引用

---

## CM-10 audit_logs 覆盖关键写操作链路

- **优先级**：P1
- **来源**：BE-32 + 安全设计

**前置条件**
- investigator 执行了 login → create_case → associate_address → export_report 完整流程

**操作步骤**
1. admin 按 request_id 关联查询审计记录

**预期结果**
- 每步写操作均有 audit_log 条目
- 同一 request_id 可串联同一 HTTP 请求上下文
- action_result 均为 success

---

## CM-11 僵尸任务回收后前端正确展示

- **优先级**：P1
- **来源**：BE-40 + FE-03 联动

**前置条件**
- 人为 kill worker 使任务卡在 processing
- 回收阈值通过环境变量配置为测试环境值（如 ZOMBIE_TIMEOUT_S=15s）

**操作步骤**
1. 前端保持轮询页面打开
2. 等待回收定时器触发（测试环境 ≤15s + 定时器间隔 ≤15s）

**预期结果**
- 回收后 judgment 变为 failed(TASK_TIMEOUT)
- 前端下次轮询收到 failed → 显示错误卡
- 用户点击重试可重新入队

---

## CM-12 幂等性端到端——同参数重复提交

- **优先级**：P0
- **来源**：BE-12 + CM-01 联动

**前置条件**
- 系统空闲

**操作步骤**
1. 并发发送 5 个完全相同的 POST /analyze
2. 收集全部响应的 judgment_id

**预期结果**
- 所有响应指向同一个 judgment_id
- judgments 表新增行数 = 1
- LLM 调用只发生一次
