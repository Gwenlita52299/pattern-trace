# PatternTrace · 测试用例文档索引

> 格式：每个测试场景包含「前置条件 / 操作步骤 / 预期结果」
> 覆盖率：按语义需求追踪矩阵衡量（见 traceability-matrix.md）
> 目标覆盖率：**≥ 99%**（对全部 specs 可验证需求）
> 来源：`../specs/*.md`（2026-08-22 评审修订版）

| 文档 | 覆盖模块 | 场景数 |
|---|---|---|
| [frontend-test-cases.md](frontend-test-cases.md) | frontend-spec | 34 |
| [backend-api-test-cases.md](backend-api-test-cases.md) | backend-api-spec | 50 |
| [graph-builder-test-cases.md](graph-builder-test-cases.md) | graph-builder-spec | 24 |
| [retrieval-test-cases.md](retrieval-test-cases.md) | retrieval-spec | 18 |
| [llm-judge-test-cases.md](llm-judge-test-cases.md) | llm-judge-spec | 20 |
| [ingest-test-cases.md](ingest-test-cases.md) | ingest-spec | 17 |
| [infra-test-cases.md](infra-test-cases.md) | infra-spec | 16 |
| [cross-module-test-cases.md](cross-module-test-cases.md) | 跨模块集成（D1–D7 决策） | 12 |
| [security-test-cases.md](security-test-cases.md) | 跨模块安全（越权/CSRF/JWT 攻击面） | 9 |
| [reliability-test-cases.md](reliability-test-cases.md) | 跨模块可靠性（崩溃恢复/降级/回滚） | 5 |
| [contract-test-cases.md](contract-test-cases.md) | 跨模块契约（OpenAPI 漂移/轮询边界/ID 反向校验） | 3 |
| [performance-test-cases.md](performance-test-cases.md) | 跨模块性能容量（nightly） | 2 |
| [project-schedule.md](project-schedule.md) | 项目排期（顺序版） | — |
| [review-report.md](review-report.md) | 评审报告（2026-08-23 三方评审汇总） | — |
| [traceability-matrix.md](traceability-matrix.md) | 需求→用例映射 + 覆盖率计算 | — |
