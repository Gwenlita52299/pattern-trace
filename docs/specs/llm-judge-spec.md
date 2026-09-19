# Spec · services/llm-judge — LLM 结构化判断模块

> 模块路径：`pattern_trace/services/llm-judge/`
> 依赖：retrieval 输出的 PatternCandidate 列表 + graph-builder 的 SubgraphResult
> Provider：OpenAI / Anthropic / Ollama / vLLM（环境变量切换）
> 缓存：Redis key `(builder_version, address, subgraph_hash, model, prompt_version)`
> 部署形态：backend 进程内包（D1），非独立服务

## 1. 职责

接收子图和 Top-K 候选 pattern，调用 LLM 生成四档风险判断（JSON），并校验 evidence 引用合法性。

## 2. Provider 抽象

```python
class LLMClient(ABC):
    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        json_schema: dict | None = None,   # JSON Schema for structured output
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> str: ...

class OpenAIClient(LLMClient): ...
class AnthropicClient(LLMClient): ...
class OllamaClient(LLMClient): ...
class VLLMClient(LLMClient): ...

def get_llm_client() -> LLMClient:
    provider = os.environ["LLM_PROVIDER"]  # openai | anthropic | ollama | vllm
    ...
```

**Structured-output 能力契约**：各 provider 原生 JSON mode 能力不同（OpenAI json_schema / Anthropic 无原生 / Ollama format 参数）。抽象层契约定义为「尽力 structured output + 本地 Pydantic 硬校验兜底」——无论 provider 是否原生支持 schema，返回值都必须通过本地 Pydantic 校验才算有效；schema 校验失败计入同一重试循环。per-provider adapter 差异写入各自实现文档。

环境变量：
| 变量 | 示例值 |
|---|---|
| LLM_PROVIDER | deepseek（默认）/ openai_compatible（llama.cpp 本地推理，issue #63） |
| LLM_MODEL | deepseek-chat；本地为 llama-server --alias（qwen3.8-27b，UD-Q4_K_M） |
| LLM_BASE_URL | https://api.deepseek.com；本地为 http://llamacpp:8080/v1 |
| LLM_API_KEY | sk-... |

## 3. System Prompt

```
You are a blockchain forensics analyst. Given a transaction subgraph and candidate
money-laundering patterns, assess the risk level.

Rules:
1. Output ONLY valid JSON matching the provided schema.
2. Every ID in the "evidence" array MUST exist in the input subgraph nodes or edges.
3. Matching state is expressed ONLY by matched_pattern (null = no KB match), never by
   risk_level. If no pattern matches, judge the observed risk directly: "medium"/"high"
   for suspicious structure with recommended_action="review"; "low" only when no
   suspicious structure is observed.
4. Do not hallucinate transaction IDs or addresses.
5. Confidence is a float between 0.0 and 1.0.
6. reasoning must reference specific structural features of the input subgraph.
```

## 4. JSON Schema

```json
{
  "type": "object",
  "properties": {
    "risk_level": {"enum": ["high", "medium", "low"]},
    "matched_pattern": {"type": ["string", "null"]},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "evidence": {"type": "array", "items": {"type": "string"}},
    "reasoning": {"type": "string"},
    "recommended_action": {"enum": ["freeze", "monitor", "review", "none"]}
  },
  "required": ["risk_level", "matched_pattern", "confidence", "evidence", "reasoning", "recommended_action"]
}
```

## 5. 防幻觉四重机制

### 5.1 Prompt 约束
匹配状态与风险级别解耦（issue #72）：`no_match` 档已删除。`matched=null` 且
`risk_level ∈ {medium, high}` 语义为 flagged_no_pattern（风险成立但无知识库匹配），
硬校验强制 `recommended_action="review"`；`matched=null` 且 `low` = 观察到轻微接触
但无可疑结构，可放行。confidence 全档统一为「对风险等级成立的确信度」，跨档可比。
历史判定行中的旧 `no_match` 值保留原样不迁移（DB 无 CHECK 约束，前端 legacy 渲染）。

### 5.2 代码层引用校验
```python
def validate_evidence(evidence_ids: list[str], subgraph: SubgraphResult) -> bool:
    valid_ids = {n.id for n in subgraph.nodes} | {e.id for e in subgraph.edges}
    return all(eid in valid_ids for eid in evidence_ids)
```

如果校验失败 → 拒绝该输出，重试最多 3 次。重试时在 prompt 中追加错误提示：
`"Your previous response referenced invalid IDs: [...]. Only use IDs from the provided subgraph."`

### 5.3 结果缓存
- Redis key: `{address}:{subgraph_hash}:{model}:{prompt_version}`
- TTL: 7 天
- 相同输入 + 版本直接返回缓存结果

### 5.4 Prompt 版本化
- 每次修改 system prompt 或 JSON Schema 时递增版本号
- judgments 表记录 prompt_version 字段
- 评估结果与版本绑定，可追溯

## 6. 判断编排流程

```python
async def judge(subgraph, candidates, address) -> JudgmentResult:
    # 1. Check cache
    cache_key = make_cache_key(address, subgraph.hash, model, prompt_version)
    cached = redis.get(cache_key)
    if cached:
        return deserialize(cached)

    # 2. Build prompt with top-K patterns and subgraph summary
    messages = build_messages(subgraph, candidates)

    # 3. Call LLM with retry
    valid = False
    for attempt in range(MAX_RETRIES):
        raw = await llm.complete(messages, json_schema=SCHEMA)
        result = parse_and_validate(raw)   # Pydantic schema + evidence 双重校验
        if result is not None:
            valid = True
            break

    # 4. 仅校验通过才缓存并返回（修复原逻辑缺陷：失败结果不得入缓存）
    if not valid:
        raise JudgmentValidationError(...)   # 编排层捕获 → judgment.status = failed
    redis.setex(cache_key, TTL, serialize(result))
    return result
```

## 7. Thinking Mode 控制

- Qwen3 系列：在用户消息末尾追加 `/no_think` 关闭思考模式以降低延迟
- 如果需要保留 reasoning 用于审计：将 `<think>...</think>` 内容提取写入 audit_logs
- 生产默认关闭 thinking mode

## 8. 性能目标

- LLM 调用延迟 ≤ 10s p95（含重试一次）
- 首次 token ≤ 2s
- evidence 引用有效率 = 100%（硬校验通过才返回）
- 四档判断准确率 ≥ 85%（留出集验证）

## 9. 测试要点

- [ ] Provider 切换正常工作（mock 三种 provider 返回相同结构）
- [ ] JSON 解析失败触发重试
- [ ] evidence 引用越界被拦截且重试后修正
- [ ] 缓存命中时不调用 LLM
- [ ] prompt 版本变更后缓存 key 不同
- [ ] matched=null 且 risk∈{medium,high}（flagged_no_pattern）强制 review 动作
- [ ] confidence 在 [0,1] 范围内
