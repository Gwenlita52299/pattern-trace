# Infra 测试用例 · infra-spec

> 模块路径：`infra/`
> 执行环境：Docker Desktop / Fly.io CLI / GitHub Actions

---

## IF-01 docker compose up 一键启动全部服务

- **优先级**：P0
- **来源**：§5 验收标准第 1 条

**前置条件**
- `.env` 已配置 JWT_SECRET
- Docker Desktop 运行中

**操作步骤**
1. `docker compose up --build -d`
2. 轮询 `docker compose ps` 直至所有服务 healthy（超时上限 120s）

**预期结果**
- frontend/backend/worker/db/redis 全部 Up（healthy）
- ollama 容器 Up（healthy）；模型加载（qwen3:30b-a3b）为本地手动检查项，不阻塞 CI
- 无 restart loop

---

## IF-02 worker 使用 Arq 并显式配置 broker

- **优先级**：P0
- **来源**：§1 修订版 worker command + D2 决策 + 评审 #16

**前置条件**
- compose 已启动

**操作步骤**
1. `docker compose exec worker env | grep -E 'ARQ|REDIS'`
2. 观察日志确认队列连接成功

**预期结果**
- REDIS_URL=redis://redis:6379/0
- ARQ 队列 q_graph/q_llm 配置可见
- 日志无 "Connection refused to localhost:6379"

---

## IF-03 depends_on condition service_healthy 生效

- **优先级**：P1
- **来源**：§1 healthcheck 配置 + 评审 #17

**前置条件**
- 全新启动（清空 volumes）

**操作步骤**
1. `docker compose down && docker compose up -d backend`
2. 观察 backend 容器启动时序

**预期结果**
- backend 在 db 和 redis 健康检查通过后才启动
- 无 "connection refused" 启动报错
- 不依赖 sleep hack

---

## IF-04 JWT_SECRET 未注入时 compose 拒绝启动

- **优先级**：P0
- **来源**：§1 `${JWT_SECRET:?set-in-.env}` 强制注入 + 评审 #24

**前置条件**
- 删除 .env 文件或 unset JWT_SECRET

**操作步骤**
1. `docker compose config` 或 `docker compose up`

**预期结果**
- 报错 "JWT_SECRET: set-in-.env" 并退出
- 不以弱默认值静默启动

---

## IF-05 生产 Fly secrets 注入生效

- **优先级**：P0
- **来源**：§2 fly secrets set

**前置条件**
- flyctl 已认证且 app 创建

**操作步骤**
1. `fly secrets set DATABASE_URL=... JWT_SECRET=... LLM_API_KEY=...`
2. `fly deploy` 后 `fly ssh console -C "[ -n \"$JWT_SECRET\" ] && echo set || echo unset"`

**预期结果**
- 存在性检查输出 "set"（环境变量在容器内非空）
- 不回显密钥值到终端/日志

---

## IF-06 GitHub Actions PR 触发完整流水线

- **优先级**：P0
- **来源**：§3 CI workflow 定义

**前置条件**
- 代码已推送 GitHub 仓库

**操作步骤**
1. 创建 PR 触发 CI
2. 查看 Actions 页面各 job 状态

**预期结果**
- lint-test-backend：ruff + mypy + pytest 全绿
- lint-test-frontend：npm ci + type-check + test 全绿
- e2e job 在前两者通过后才运行

---

## IF-07 CI frontend job 校验 OpenAPI 类型一致性

- **优先级**：P1
- **来源**：frontend-spec §6 codegen 流程 + infra §3

**前置条件**
- CI workflow 包含 codegen 步骤

**操作步骤**
1. 故意在 frontend 中使用一个后端不存在的字段
2. 提交 PR

**预期结果**
- CI type-check 步骤失败并指出类型不匹配
- 合并被阻止

---

## IF-08 main 分支合并自动部署

- **优先级**：P0
- **来源**：§5 验收标准第 3 条

**前置条件**
- PR 通过全部 CI 后 merge 到 main

**操作步骤**
1. 观察 deploy job 执行
2. 访问生产 URL

**预期结果**
- flyctl deploy 成功
- Vercel deploy hook 触发，前端自动更新
- 生产站点可访问且版本与 main HEAD 一致

---

## IF-09 Alembic release phase 迁移失败阻断部署

- **优先级**：P0
- **来源**：§4 修订版"迁移失败 → 阻断部署" + 评审 #25

**前置条件**
- 注入一个故意失败的迁移脚本

**操作步骤**
1. 推送到 main 触发部署
2. 观察 release phase 输出

**预期结果**
- alembic upgrade 失败
- Fly.io 部署中止，旧版本继续服务
- 数据库 schema 未被部分修改（无漂移）

---

## IF-10 Alembic advisory lock 防多实例并发

- **优先级**：P1
- **来源**：§4 修订版并发保护

**前置条件**
- 本地 docker-compose 或 CI job 环境（Fly release phase 每次部署仅执行一次迁移，无法构造两实例并发，故在本地验证）

**操作步骤**
1. 同时启动两个 alembic upgrade head 进程
2. 检查 alembic_version 表与迁移日志

**预期结果**
- 仅一个进程执行迁移 DDL，另一个等待 advisory lock
- 最终 schema 版本一致，无冲突错误

---

## IF-11 expand-contract 两阶段破坏性变更

- **优先级**:P2
- **来源**：§4 手动回滚流程说明

**前置条件**
- 需要删除某列的场景

**操作步骤**
1. Phase 1 (expand)：添加新列 + 双写，部署
2. Phase 2 (contract)：删除旧列，部署

**预期结果**
- 每个 phase 都可独立回滚
- 中间状态系统正常运行

---

## IF-12 生产 Redis TLS 连接 rediss://

- **优先级**：P1
- **来源**：§4 修订版 Upstash TLS 说明

**前置条件**
- 生产环境 Redis_URL 为 rediss:// 格式

**操作步骤**
1. backend 启动后执行一次缓存读写

**预期结果**
- 连接走 TLS 加密（wireshark/tcpdump 可验证）
- 无明文 6379 端口通信

---

## IF-13 Neon PgBouncer prepared statements 兼容

- **优先级**:P2
- **来源**：§4 SQLAlchemy 连接参数说明

**前置条件**
- DATABASE_URL 指向 Neon pooler endpoint

**操作步骤**
1. backend 执行多次参数化查询

**预期结果**
- 无 "prepared statement __stmt_... does not exist" 错误
- 连接池复用正常

---

## IF-14 Ollama 服务健康且模型已加载

- **优先级**：P1
- **来源**：§1 ollama 服务定义

**前置条件**
- docker compose up 已完成 ollama 容器运行中

**操作步骤**
1. `docker compose exec ollama ollama list`
2. `curl http://localhost:11434/api/tags`

**预期结果**
- 所需模型（qwen3:30b-a3b）已在列表中
- API 返回 200 且包含该模型条目

---

## IF-15 pgdata volume 持久化验证

- **优先级**：P1
- **来源**：§1 volumes: [pgdata]

**前置条件**
- 数据库中已有数据

**操作步骤**
1. `docker compose down`（不带 -v）
2. `docker compose up -d db`
3. 查询数据

**预期结果**
- 数据完好保留
- alembic_version 不变

---

## IF-16 E2E job 使用 mock fixture 而非公网

- **优先级**：P0
- **来源**：tests-spec 修订"E2E 必须跑在 mock Esplora/LLM 上"

**前置条件**
- CI e2e job 配置文件可查看

**操作步骤**
1. 检查 e2e job 的 docker-compose override 或 env 配置指向 mock
2. 在 CI job 中配置出网管控（DNS 黑名单 blockstream.info、api.openai.com；或 HTTP 代理拦截并计数外呼请求）
3. 运行一次完整 e2e pipeline

**预期结果**
- Esplora base URL 指向 mock server（如 wiremock/json-server）
- LLM_PROVIDER 指向 mock provider
- DNS/代理黑名单命中次数 = 0（证明无真实外呼）
- 测试全程无对 blockstream.info / api.openai.com 的网络连接
