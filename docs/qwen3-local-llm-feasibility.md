# PatternTrace · 本地 Qwen3 4-bit 量化模型可行性评估

> 评估对象：Qwen3-8B 4-bit（AWQ/GPTQ/GGUF Q4_K_M）与 Qwen3-30B-A3B 4-bit（Q4_K_M，MoE 3.3B 激活）
> 对照场景：PatternTrace llm-judge 模块（结构化 JSON 判断 + 证据引用校验）
> 日期：2026-08-22

## 1. 任务需求画像

| 维度 | PatternTrace llm-judge 要求 |
|---|---|
| 上下文长度 | 输入子图序列化 ~2K–8K tokens + Top-K pattern 上下文 + system prompt |
| 输出格式 | JSON Schema 强制：risk_level / matched_pattern / confidence / evidence[] / reasoning / recommended_action |
| 证据引用 | evidence[] 中的 ID 必须存在于输入子图（代码层硬校验，越界即重试） |
| 判断准确率目标 | ≥ 85%（四档与人工标注一致率） |
| 延迟目标 | 全链路 p95 ≤ 10s（子图构建 + 检索 + LLM） |
| 调用频率 | 单地址分析，非高并发；相同输入走 Redis 缓存 |

## 2. 候选模型对比

| 维度 | Qwen3-8B 4-bit | Qwen3-30B-A3B 4-bit (Q4_K_M) |
|---|---|---|
| 参数量 | 8.2B dense | 30.5B total / 3.3B activated (MoE) |
| 权重显存 | ~5–6 GB (AWQ) | ~17 GB (Q4_K_M) |
| 推荐 GPU | RTX 3060 12GB / RTX 4060 Ti 16GB / M2 Pro 16GB+ | RTX 4090 24GB / A100 40GB / M2 Ultra 32GB+ |
| 生成速度 (4090) | ~80–120 tok/s | ~40 tok/s（MoE 稀疏激活） |
| MMLU 4-bit 损失 | 74.7 → 69.3（-5.4） | 更小（大模型量化更稳定） |
| 结构化输出 | vLLM guided decoding / Ollama format json 均支持 | 同左；注意 2507-Instruct 版 + reasoning-parser 有兼容 bug |
| 上下文 | 32K native | 32K native / 128K YaRN |
| Thinking mode | 支持（`<think>` 分离） | 支持 |

## 3. 可行性判定

### 3.1 结构化 JSON 输出 — 可行 ✅

- vLLM 的 `response_format: json_schema`（guided decoding）在生成阶段强制合法 JSON，不依赖 prompt 遵循度，4-bit 量化不影响 grammar-based 约束。
- Ollama 的 `format: json` 同理。
- 注意事项：Qwen3-30B-A3B-Instruct-2507 + `--reasoning-parser qwen3` 存在 structured output 失效的已知 bug（vLLM issue #23315），需去掉 reasoning-parser 或使用非 2507 版本。
- 建议：生产部署用 vLLM + guided decoding；开发/演示用 Ollama。

### 3.2 证据引用准确率 — 有条件可行 ⚠️

- evidence[] 要求引用输入子图中真实存在的 txid / address，这是**精确字符串复制**任务，对量化损失相对不敏感（不是推理任务），但仍需评估。
- 8B 4-bit 的 MMLU 下降 5.4 分主要影响复杂推理；本任务的推理链较短（子图结构 → pattern 匹配 → 四档判断），8B 4-bit 可能够用，但需在留出集上验证。
- 30B-A3B 4-bit 因 MoE 稀疏激活，推理能力接近 14B dense，量化损失更小，是更安全的选择。
- **必须做**：用 Lazarus 留出集 20% 跑评估脚本，对比 FP16 vs Q4 的四档判断一致率与 evidence 有效率，达标后再切换。

### 3.3 延迟 — 可行 ✅

- 单次判断：输入 ~4K tokens（prefill）+ 输出 ~300–500 tokens。
- RTX 4090 + 30B-A3B Q4：prefill 快（3.3B 激活），生成 500 tok ≈ 12s（保守），加上 thinking mode 可关（`/no_think`）后输出更短。
- RTX 4090 + 8B AWQ：生成 500 tok ≈ 4–6s。
- 全链路 p95 ≤ 10s 目标：8B 稳定达标；30B-A3B 需关 thinking mode 或缩短 reasoning 才能稳定达标。
- 缓解：相同输入缓存命中直接返回；批量预取演示地址。

### 3.4 硬件成本 — 可行 ✅

| 方案 | 最低硬件 | 估算成本 |
|---|---|---|
| 8B 4-bit | RTX 3060 12GB / Mac M2 Pro 16GB | ¥2,000–5,000（已有则零成本） |
| 30B-A3B 4-bit | RTX 4090 24GB / Mac Studio M2 Ultra | ¥15,000+ 或云 GPU ~$0.5/hr |
| 云 GPU 按需 | RunPod / Vast.ai RTX 4090 | ~$0.3–0.5/hr，按需启动 |

## 4. 结论与建议

| 判定 | 说明 |
|---|---|
| **可行，推荐 Qwen3-30B-A3B Q4_K_M 作为 MVP 本地 LLM** | MoE 稀疏激活带来接近 14B dense 的推理能力，Q4 量化损失小，4090 单卡可跑，速度满足 p95 ≤ 10s |
| 8B 4-bit 作为最低配置备选 | 12GB 显存即可跑，但需验证判断准确率是否 ≥ 85% |
| 不建议低于 8B | 4B 及以下在复杂推理与指令遵循上损失过大 |

### 实施建议

1. **llm-judge provider 抽象已支持 Ollama/vLLM**，只需新增 `LLM_PROVIDER=ollama` + `LLM_MODEL=qwen3:30b-a3b` 配置。
2. **结构化输出**：vLLM 部署用 `response_format: json_schema`；Ollama 用 `format: json` + prompt 中嵌入 schema。
3. **关闭 thinking mode**（`/no_think`）以降低延迟；如需保留 reasoning 用于审计，将 `<think>` 内容写入 audit_logs 而非返回给前端。
4. **评估闭环**：W3 retrieval 基线跑通后，用同一留出集对比 GPT-4o / Qwen3-30B-A3B-Q4 / Qwen3-8B-Q4 的四档判断一致率，数据驱动选择。
5. **降级策略**：本地模型超时或 OOM → fallback 到云端 API（OpenAI/Anthropic），provider 抽象天然支持。
6. **注意 2507-Instruct 版 bug**：Qwen3-30B-A3B-Instruct-2507 + vLLM reasoning-parser 存在 structured output 失效问题，建议使用原版 Qwen3-30B-A3B 或去掉 reasoning-parser。
