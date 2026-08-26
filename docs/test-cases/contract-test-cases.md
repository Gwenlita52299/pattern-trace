# 前后端契约测试用例 · 跨模块契约

> 覆盖 OpenAPI 类型漂移、轮询协议边界、ID 契约反向校验
> 执行环境：CI pipeline + pytest

---

## CT-01 OpenAPI 反向漂移检测

- **优先级**:P1
- **来源**：FR-24 / IF-07 补充——反向漂移方向覆盖

**前置条件**
- CI workflow 含 openapi-typescript codegen + type-check 步骤

**操作步骤**
1. 在 backend schema 中新增一个必填字段（如 JudgmentResponse 新增 required 字段 new_field: str）
2. 提交 PR 触发 CI

**预期结果**
- CI 重新生成 types/api.d.ts
- frontend type-check 因现有调用点缺少 new_field 而失败
- 合并被阻止（与 IF-07 正向漂移形成双向守护）

---

## CT-02 轮询时序边界——不存在 ID 稳定 404

- **优先级**:P1
- **来源**：FR-18 补充——poll_url 契约

**前置条件**
- backend 服务运行中

**操作步骤**
1. 使用预生成的随机 UUID 调用 `GET /api/v1/judgments/<nonexistent_uuid>` 连续 10 次
2. 检查 poll_url 格式约定（相对路径 or 绝对 URL）
3. 前端 API client 解析 poll_url 时拼接 base URL

**预期结果**
- 所有 10 次请求均稳定返回 HTTP 404（不出现间歇 404/200 竞争）
- poll_url 为相对路径 `/api/v1/judgments/<id>`（前端负责拼接 origin）
- 前端对 404 的处理为显示错误卡（非无限轮询）

---

## CT-03 ID 契约反向校验——畸形 subgraph 引用防御

- **优先级**:P1
- **来源**：CM-02 补充——反向完整性验证

**前置条件**
- 构造畸形 subgraph 数据：edge.source 指向不存在的 node id

**操作步骤**
1. 将畸形 subgraph 注入 graph-builder 输出校验函数（单测）
2. 将相同数据注入前端 GraphCanvas 渲染流程（Playwright mock subgraph 响应）

**预期结果**
- builder 输出校验层拒绝该数据（抛出 ValidationError 或过滤无效边）
- 前端渲染层对无法解析的引用显示占位符而非白屏崩溃
- 两层的防御行为均有日志记录
