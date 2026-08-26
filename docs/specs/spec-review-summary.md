# Specs 评审汇总 · 2026-08-22

> 评审角色：高级架构师 / 高级前端工程师 / 高级后端工程师（并行评审）
> 改进执行：高级全栈工程师（主 agent）
> 状态：全部 P0/P1 问题已回写至对应 spec 文档

## 一、跨文档架构决策（本次定稿）

| # | 决策 | 解决的评审问题 |
|---|------|----------------|
| D1 | **模块化单体**：graph-builder / retrieval / llm-judge 为 backend 进程内 Python 包（防腐层接口），非独立微服务；MVP 不引入服务间 RPC | 架构师 #1 |
| D2 | **任务队列 Celery → Arq**（Redis-backed、async-native），消除同步 Worker 与全异步管线的执行模型冲突 | 架构师 #2 |
| D3 | **全局 ID 规范**：`addr:<address>` / `tx:<txid>` / `edge:<src>-><dst>`，由 graph-builder 统一生成；evidence 引用与前端高亮共用该 ID 空间 | 架构师 #3、后端 #22 |
| D4 | **认证方案定稿**：access token 返回 body（前端存内存）+ refresh token 走 HTTPOnly Cookie（SameSite=Lax dev / None+Secure prod）+ CSRF 自定义 header + Rotation & Reuse Detection | 前端 P0-1、后端 #7/#8、架构师 #10 |
| D5 | **judgment 状态机**：`queued → processing → completed / failed`；failed 含 error_code/error_message；僵尸任务 120s 回收 | 前端 P0-2/3、后端 #1/#2 |
| D6 | **hops 上限收敛为 1–3**，与 BFS 三队列和特征向量维度一致 | 架构师 #4 |
| D7 | **延迟预算改为冷/热缓存双口径**：热路径 p95 ≤ 10s（演示地址预热），冷路径 ≤ 20s（Esplora 网络 RTT 主导） | 后端 #4、架构师 #26 |

## 二、按严重度的问题清单（评审原文要点）

### P0（阻断开发）
- 服务部署拓扑矛盾（进程 vs 微服务未定义）→ D1 已定稿
- Celery 同步模型与异步管线冲突 → D2 已定稿
- 边无稳定 ID 导致 evidence 校验/前端高亮断裂 → D3 已定稿
- hops=5 与三队列硬编码冲突 → D6 已定稿
- pgvector 双向量加权和无法走 HNSW（性能声明错误）→ retrieval-spec 已修正为精确扫描 + 扩展方案
- LLM 校验失败仍写缓存 → llm-judge-spec 已修正为仅校验通过才缓存
- 免登录 analyze 成本滥用面 → backend-spec 增加白名单 + 全局日配额
- 认证机制前后端矛盾 → D4 已定稿
- judgment 无失败态、轮询无终止条件 → D5 + 前端轮询协议已补齐
- cases CRUD 接口缺口 → backend-spec 补全 GET/PATCH/addresses/reports 全套契约

### P1（上线前必须）
- 异步任务幂等性 + 僵尸回收 + acks_late → backend-spec 处理流程已补
- p95 预算无法闭合 → D7 冷/热双口径
- subgraph_snapshot TOAST 膨胀（300–800KB/行）→ LZ4 压缩 + 读取预算标注
- 错误响应无统一规范 → RFC 9457 Problem Details
- Refresh Rotation / bcrypt 参数 / rate limit 维度 / SQL 注入面 / 报告资源限制 → 各文件已补
- case_addresses 复合主键 / audit_logs 字段 / addresses_meta↔judgments 关系 → 数据模型已修
- Esplora 容错（重试/熔断/降级）→ graph-builder-spec 新增 §7.1
- 时间窗口终止条件语义错误（时间 vs 高度混用）→ 已统一为 block_time 口径
- lru_cache 不能用于协程 → async-lru 替代
- WL hash ≠ 相似度分数 → 自定义带属性 WL kernel + Jaccard 公式
- Embedding 模型版本漂移 → patterns 表增加 embedding_model/dim 列并强制锁定
- 负样本污染业务召回库 → 独立 pattern_negatives 表仅用于校准
- matched_pattern 名字弱引用 → 改 pattern_id FK + 内容哈希版本化
- Compose worker 缺 broker 配置 / 无健康检查 / JWT 弱密钥 / Alembic 失败策略 → infra-spec 已修

### P2（体验与稳健性）
- 前端轮询退避/超时/取消、GraphCanvas 大图性能配置、四态 UI 矩阵、middleware 保护规则、OpenAPI codegen 流程、分页 URL 同步、a11y 双编码 → frontend-spec 已补
- 分页上限/排序/响应包、Idempotency-Key、401 vs 403、refresh 契约、缓存 key 加 builder_version、ingest upsert key、测试断言口径、BTC 地址 checksum 校验、subgraph_hash 规范化序列化、admin 用户管理 API → 对应文件已补
- Rust 加速重新定位为批量离线场景（在线瓶颈在网络 IO）；基准改为 ≥100K 边 CPU 微基准 → tests/graph-builder spec 已修

## 三、遗留事项（不在本轮 spec 内解决）
- SSE/WebSocket 替代轮询：预留升级接口，MVP 后评估
- 移动端适配策略：MVP 桌面优先，移动端只读
- 审计日志哈希链防篡改：P2 backlog
