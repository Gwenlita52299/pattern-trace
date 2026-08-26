# PatternTrace · 模块 Spec 文档索引

> 按仓库结构逐模块编写，grill-me 风格（职责边界 / 输入输出 / 验收标准 / 测试要点）
> 设计报告：`../PatternTrace_前后端分离设计报告.md`
> **2026-08-22 评审修订**：三方（架构/前端/后端）评审后已修复全部 P0/P1 问题，详见 [spec-review-summary.md](spec-review-summary.md)

| 模块 | Spec 文件 | 职责 |
|---|---|---|
| frontend/ | [frontend-spec.md](frontend-spec.md) | Next.js SPA：页面路由、GraphCanvas、VerdictCard、Zustand store、API client |
| backend/ | [backend-api-spec.md](backend-api-spec.md) | FastAPI：REST endpoints、JWT+rotation、Arq 异步编排、数据模型、安全 |
| services/graph-builder/ | [graph-builder-spec.md](graph-builder-spec.md) | BFS 三队列 + 五类终止条件 + 全局 ID 契约 + Esplora 容错获取 |
| services/retrieval/ | [retrieval-spec.md](retrieval-spec.md) | 结构特征向量 + 语义 embedding + 混合召回 + 带属性 WL 精排 |
| services/llm-judge/ | [llm-judge-spec.md](llm-judge-spec.md) | Provider 抽象 + JSON Schema + 防幻觉四重机制 + 校验通过才缓存 |
| ingest/ | [ingest-spec.md](ingest-spec.md) | Lazarus 切图 + 负样本隔离 + 标签加载 + embedding 版本锁定 |
| infra/ | [infra-spec.md](infra-spec.md) | Docker Compose(healthcheck) + Fly.io/Vercel 部署 + GitHub Actions CI/CD |
| tests/ | [tests-spec.md](tests-spec.md) | 单元/集成/E2E 测试策略 + 检索/LLM 评估脚本 + 性能基准 |

## 关键跨文档决策（详见 review summary）

- **D1** graph-builder / retrieval / llm-judge 为 backend 进程内包，非独立微服务
- **D3** 全局 ID 规范 `addr:*` / `tx:*` / `edge:*`，evidence 与前端高亮共用
- **D4** 认证：内存 access token + HTTPOnly refresh cookie + rotation
- **D5** judgment 状态机 `queued → processing → completed | failed`
- **D6** hops 上限 1–3（与 BFS/特征维度一致）
- **D7** 延迟预算冷/热双口径：热 p95 ≤10s，冷 ≤20s
