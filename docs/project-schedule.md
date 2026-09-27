# PatternTrace · 项目排期（顺序版）

> 依据：`docs/product-design.md` 6 周路线图 + `docs/test-cases/`（201 条用例、评审报告、追踪矩阵）
> 原则：只定先后顺序，不绑定具体时间；每阶段以「完成标志」作为进入下一阶段的门禁。
> 创建日期：2026-08-23

---

## 总体依赖链

```
基础设施 → 图谱构建 → 数据入库 → 检索 → LLM判断+前端 → 业务闭环 → 测试发布
```

严格串行推进；各模块测试用例随开发同步执行，不后置。

---

## 阶段 0 · 基础设施与骨架

- monorepo 骨架
- docker-compose 一键启动（含 worker / db / redis / ollama）
- DB schema（Alembic）
- JWT 认证（access + refresh rotation）

**完成标志**
- 本地一键启动全服务 healthy
- 登录 / 受保护接口跑通

**并行测试**：infra-test-cases.md（IF-01 ~ IF-16）

---

## 阶段 1 · 图谱构建核心

- BFS 子图构建器（三队列）
- 五类终止条件（unspent / out_of_range / 深度 / 规模裁剪 / early-stop）
- 全局 ID 规范（D3：addr / tx / edge）

**完成标志**
- 输入地址返回受控子图
- 终止条件统计与参考实现基线对齐

**并行测试**：graph-builder-test-cases.md（GB-01 ~ GB-24）

---

## 阶段 2 · 数据入库

- Esplora 数据源接入
- Lazarus 数据切图（seed_address 分组）
- 正 / 负样本生成（pattern_negatives 独立表）
- pgvector 入库

**完成标志**
- 知识库可查询
- 正负样本比例达标

**并行测试**：ingest-test-cases.md（IG-01 ~ IG-17）

---

## 阶段 3 · 检索服务

- 结构指纹特征向量
- 混合召回（pgvector + 结构过滤）
- WL kernel 精排

**完成标志**
- recall@10 基线跑通并记录

**并行测试**：retrieval-test-cases.md（RT-01 ~ RT-18）

---

## 阶段 4 · LLM 判断 + 前端可视化

**后端**
- LLM provider 抽象（Ollama / OpenAI 兼容）
- JSON Schema 结构化输出
- 引用校验（evidence ID 必须存在于子图快照）
- 判断状态机（D5：queued → processing → completed / failed）

**前端**
- 图谱画布（React Flow）
- Verdict 面板 + evidence 高亮
- 轮询（退避 / 超时 / 取消）

**完成标志**
- 端到端「地址 → 子图 → 判断 → 可视化」跑通

**并行测试**
- llm-judge-test-cases.md（LJ-01 ~ LJ-20）
- frontend-test-cases.md（FE-01 ~ FE-34）
- backend-api-test-cases.md（BE-01 ~ BE-50）

---

## 阶段 5 · 业务闭环

- 案件管理（CRUD + 地址关联 + 状态流转）
- 报告导出（异步生成 + 轮询下载）
- 审计日志
- 3 个种子案例（high / low / no_match）

**完成标志**
- 完整工作流可用：登录 → 建案 → 关联地址 → 分析 → 导出报告

**并行测试**
- cross-module-test-cases.md（CM-01 ~ CM-12）
- security-test-cases.md（SEC-01 ~ SEC-09）
- reliability-test-cases.md（REL-01 ~ REL-05）

---

## 阶段 6 · 测试与发布

- 单元 / 集成 / E2E 测试收口
- CI/CD（PR 三 job 流水线 + 验证门禁）
- docker compose 私有化部署
- README + 演示视频

**完成标志**
- `docker compose up` 一键拉起全套（可私有化交付）
- 种子案例 30 秒出结论

**并行测试**
- contract-test-cases.md（CT-01 ~ CT-03）
- performance-test-cases.md（PERF-01 ~ PERF-02，nightly 档）

---

## 明确不做（P2，写入 README 路线图）

实时告警、graph2vec、历史数据回灌、多链支持、移动端、WebGL 大图渲染。
