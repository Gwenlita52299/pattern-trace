# LLM Judge 测试用例 · llm-judge-spec

> 模块路径：`services/llm-judge/`（backend 进程内包，D1）
> 执行环境：Mock LLM provider（不依赖公网 API）

---

## LJ-01 Provider 抽象切换正常工作

- **优先级**：P0
- **来源**：§2 Provider 抽象 + §9 测试要点第 1 条

**前置条件**
- 四种 provider 均使用实例注入方式（pytest 参数化 fixture），mock 返回合法 JSON
- 另保留一条集成冒烟用例验证 env 变量切换生效

**操作步骤**
1. 依次注入四种 provider 实例并调用 judge()
2. 对比输出结构 schema（不比内容）

**预期结果**
- 四种 provider 均返回结构一致的 JudgmentResult（Pydantic schema 校验通过）
- 集成冒烟：修改环境变量后重启进程生效，无需改代码

---

## LJ-02 JSON Schema 校验——合法输入通过

- **优先级**：P0
- **来源**：§4 JSON Schema 定义

**前置条件**
- Mock LLM 返回完整合法 JSON（所有 required 字段、枚举值正确）

**操作步骤**
1. parse_and_validate(raw, valid_ids)

**预期结果**
- 返回 JudgmentResult 实例
- risk_level ∈ 枚举；confidence ∈ [0,1]；recommended_action ∈ 枚举

---

## LJ-03 非法 JSON 触发重试

- **优先级**：P0
- **来源**：§5.2 重试机制 + §9 第 2 条

**前置条件**
- Mock LLM 第一次返回 "not json at all"，第二次返回合法 JSON

**操作步骤**
1. judge() 执行
2. 检查 LLM 调用计数与最终结果

**预期结果**
- LLM 被调用 2 次
- 第二次成功解析并返回有效 JudgmentResult
- 无异常抛出

---

## LJ-04 重试时 prompt 附加错误提示

- **优先级**：P1
- **来源**：§5.2 重试消息格式

**前置条件**
- 同 LJ-03 场景

**操作步骤**
1. 截获第二次调用的 messages 参数

**预期结果**
- messages 中包含 "Your previous response referenced invalid IDs"
- 包含具体非法 ID 列表或占位符

---

## LJ-05 evidence 引用越界被拦截

- **优先级**：P0
- **来源**：§5.2 代码层校验 + §8 evidence 有效率 =100%

**前置条件**
- valid_ids = {"tx:abc", "addr:def"}
- Mock 返回 evidence=["tx:nonexistent"]

**操作步骤**
1. parse_and_validate(raw, valid_ids)

**预期结果**
- 抛出 JudgmentValidationError（统一契约：校验失败一律抛异常）
- 不产生缓存写入

---

## LJ-06 三次重试全部失败进入 failed 终态

- **优先级**：P0
- **来源**：§5.2 MAX_RETRIES=3 + 编排层失败处理

**前置条件**
- Mock LLM 所有响应的 evidence 都引用不存在的 ID

**操作步骤**
1. judge() 执行
2. 捕获异常

**预期结果**
- LLM 调用计数 = 3（首调 + 最多2次重试；MAX_RETRIES=3 表示总调用上限）
- 抛出 JudgmentValidationError（与其他校验用例统一契约）
- 缓存中无该 key 的条目（仅校验通过才缓存）
- 上层编排将 judgment.status 设为 failed，retry_count=2

---

## LJ-07 缓存命中时不调用 LLM

- **优先级**：P0
- **来源**：§5.3 结果缓存 + §9 第 4 条

**前置条件**
- Redis/内存缓存中已有某 key 的有效结果

**操作步骤**
1. 用相同参数再次调用 judge()
2. 检查 LLM 调用计数

**预期结果**
- 直接返回缓存值
- LLM mock 调用计数 = 0

---

## LJ-08 prompt_version 变更后缓存 key 不同

- **优先级**：P0
- **来源**：§5.4 版本化 + §9 第 5 条

**前置条件**
- 已用 PROMPT_VERSION=v3 缓存了结果

**操作步骤**
1. 将 PROMPT_VERSION 改为 v4
2. 相同子图参数再次 judge()

**预期结果**
- build_cache_key 输出不同字符串
- 缓存 miss，LLM 重新被调用

---

## LJ-09 builder_version 变更使缓存失效

- **优先级**：P1
- **来源**：build_cache_key 签名含 builder_version 参数

**前置条件**
- 已有 builder_version=gb-v1 的缓存

**操作步骤**
1. 以 builder_version=gb-v2 调用相同参数

**预期结果**
- cache key 不同 → miss → 重新调用 LLM

---

## LJ-10 no_match 时 recommended_action 必须为 review

- **优先级**：P0
- **来源**：§3 System Prompt rule 3

**前置条件**
- Mock 返回 risk_level="no_match" 但 recommended_action="freeze"

**操作步骤**
1. parse_and_validate(raw, valid_ids)

**预期结果**
- 抛出 JudgmentValidationError（违反规则 3）
- 编排层捕获异常后触发重试

---

## LJ-11 no_match 时 matched_pattern 必须为 null

- **优先级**：P1
- **来源**：§3 System Prompt rule 3 语义一致性

**前置条件**
- Mock 返回 no_match + matched_pattern="mixer_layering"

**操作步骤**
1. parse_and_validate

**预期结果**
- 抛出 JudgmentValidationError，编排层捕获后触发重试

---

## LJ-12 confidence 越界拒绝

- **优先级**：P1
- **来源**：§4 confidence minimum=0 maximum=1

**前置条件**
- Mock 返回 confidence=1.5

**操作步骤**
1. parse_and_validate

**预期结果**
- None（超出 [0,1]）

---

## LJ-13 thinking mode 提取 `<think>` 内容

- **优先级**：P2
- **来源**：§7 Thinking Mode 控制

**前置条件**
- Mock Qwen3 返回包含 `<think>reasoning here</think>{"risk_level":...}` 格式响应

**操作步骤**
1. extract_thinking(raw_response)

**预期结果**
- 第一返回值为 "reasoning here"（去空白）
- 第二返回值为去掉 think 块后的纯 JSON 文本

---

## LJ-14 无 think 标签时原文返回

- **优先级**：P2
- **来源**：同上

**前置条件**
- Mock 返回不含 `<think>` 的纯 JSON

**操作步骤**
1. extract_thinking(raw)

**预期结果**
- 第一返回值为空字符串 ""
- 第二返回值为原始文本

---

## LJ-15 subgraph_hash 规范化序列化稳定性

- **优先级**：P0
- **来源**：canonical_subgraph_hash 函数 + 架构师 P2-25

**前置条件**
- 构造同一逻辑子图的两种表示（字段顺序不同、label 不同、浮点精度差异在 round(8) 内）

**操作步骤**
1. 分别计算 canonical_subgraph_hash
2. 对比两个 hash

**预期结果**
- 两 hash 完全一致（规范化消除了顺序和 label 差异）
- 浮点差异 < 1e-8 不影响 hash

---

## LJ-16 canonical hash 忽略 label 字段

- **优先级**：P1
- **来源**：规范化白名单设计

**前置条件**
- 两个子图仅节点 label 不同

**操作步骤**
1. 计算 hash 并比较

**预期结果**
- hash 相同（label 属于展示信息不影响结构判断）

---

## LJ-17 Structured-output 能力降级——Anthropic 无原生 schema

- **优先级**：P1
- **来源**：§2 能力契约修正"尽力 structured output + 本地硬校验"

**前置条件**
- provider=anthropic（mock），模型不支持原生 json_schema 参数

**操作步骤**
1. judge() 正常执行

**预期结果**
- AnthropicClient 在 prompt 中内嵌 schema 说明而非 API 参数
- 本地 Pydantic 校验兜底仍然生效
- 合法输出正常通过

---

## LJ-18 缓存 TTL 7 天过期

- **优先级**：P2
- **来源**：§5.3 TTL: 7 天

**前置条件**
- Redis 可访问，写入一条缓存并设 TTL

**操作步骤**
1. `TTL <cache_key>` 检查

**预期结果**
- TTL ≈ 604800s (±120s)
- 手动 expire 后再次请求重新调用 LLM

---

## LJ-19 System Prompt 包含防幻觉核心规则

- **优先级**：P1
- **来源**：§3 System Prompt 全文

**前置条件**
- judge() 可截获 messages

**操作步骤**
1. 检查 system role 内容

**预期结果**
- 含 "Output ONLY valid JSON"
- 含 "Every ID ... MUST exist in the input subgraph"
- 含 "If no pattern matches, output risk_level=no_match and recommended_action=review"
- 含 "Do not hallucinate transaction IDs or addresses"
- 含 confidence 范围说明

---

## LJ-20 性能目标 p95 ≤ 10s（热路径）

- **优先级**：P1
- **来源**：§8 LLM 调用延迟目标 + D7 冷热口径

**前置条件**
- Mock LLM 固定延迟 500ms
- 计时采用管线开销扣除法：pipeline_overhead = total_elapsed - mock_delay

**操作步骤**
1. 连续调用 judge ≥30 次
2. 每次记录总耗时并扣除已知 mock 延迟得管线自身开销
3. 取管线开销 p95

**预期结果**
- 管线开销 p95 ≤ 500ms（扣除 mock 500ms 后剩余部分）
- 总耗时含一次重试预算时 ≤ 10s
- 流式首 token 指标仅在真实 provider 场景测量，mock 场景不适用
