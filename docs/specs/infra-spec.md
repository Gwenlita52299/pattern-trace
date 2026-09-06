# Spec · infra/ — 部署与 CI/CD

> 模块路径：`pattern_trace/infra/`
> 本地开发：Docker Compose 一键启动
> 生产：Docker Compose 私有化部署（自托管 DB/Redis/Ollama，LLM 可走云端兼容端点）

## 1. Docker Compose（本地开发）

```yaml
version: "3.9"
services:
  frontend:
    build: ../frontend
    ports: ["3000:3000"]
    environment:
      - NEXT_PUBLIC_API_URL=http://localhost:8000/api/v1

  backend:
    build: ../backend
    ports: ["8000:8000"]
    depends_on: [db, redis]
    environment:
      - DATABASE_URL=postgresql://pt:pt@db:5432/patterntrace
      - REDIS_URL=redis://redis:6379/0
      - LLM_PROVIDER=ollama
      - LLM_BASE_URL=http://ollama:11434

  worker:
    build: ../backend
    command: arq backend.workers.worker.WorkerSettings
    depends_on:
      db: {condition: service_healthy}
      redis: {condition: service_healthy}
    environment:
      - DATABASE_URL=postgresql://pt:pt@db:5432/patterntrace
      - REDIS_URL=redis://redis:6379/0
      - ARQ_QUEUE_GRAPH=q_graph
      - ARQ_QUEUE_LLM=q_llm
      - JWT_SECRET=${JWT_SECRET:?set-in-.env}   # 强制注入，弱默认值仅限 .env.example 示例

  db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: pt
      POSTGRES_PASSWORD: pt
      POSTGRES_DB: patterntrace
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U pt"]
      interval: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 5

  # issue #63：本地推理可选容器，生产不拉起；默认 LLM 走 deepseek 云端
  llamacpp:
    image: ghcr.io/ggml-org/llama.cpp:server
    profiles: ["local-llm"]
    ports: ["8080:8080"]
    volumes: [llamacpp_models:/root/.cache/llama.cpp]
```

## 2. 生产部署

| 组件 | 平台 | 说明 |
|---|---|---|
| 前端 | 私有化 docker compose 服务 | Next.js 镜像，构建期内联 `NEXT_PUBLIC_API_URL` |
| 后端 API | 私有化 docker compose 服务 | Docker 部署，共享代码库镜像 |
| Worker (arq) | 私有化 docker compose 服务（同镜像） | 独立进程 |
| PostgreSQL + pgvector | 自托管 `pgvector/pgvector:pg16` | compose 数据卷持久化 |
| Redis | 自托管 `redis:7-alpine` | 队列 + 缓存 |
| Ollama (LLM) | 自托管容器或外部兼容端点 | 或切换 DeepSeek/OpenAI 兼容 API |

环境变量经 `.env` + compose `environment:` 注入：
```bash
# .env  示例（参考 .env.example，勿纳入版本库）
JWT_SECRET=<64-char-random>
LLM_PROVIDER=ollama   # 或 deepseek
LLM_BASE_URL=http://ollama:11434
```

## 3. GitHub Actions CI/CD

```yaml
name: CI
on: [push, pull_request]

jobs:
  lint-test-backend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: pip install -r backend/requirements-dev.txt
      - run: ruff check backend/
      - run: mypy backend/
      - name: Integration tests
        run: |
          docker compose up -d db redis
          sleep 5
          pytest tests/backend/ -x

  lint-test-frontend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: {node-version: "20"}
      - run: cd frontend && npm ci && npm run type-check && npm test

  e2e:
    needs: [lint-test-backend, lint-test-frontend]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker compose up --build -d
      - run: curl -f http://localhost:8000/healthz && curl -f http://localhost:8000/readyz
```

CI 仅作验证门禁，不做云端发布；交付形态为 docker compose 私有化部署。

## 4. 数据库迁移

- Alembic 在 backend 容器启动时自动执行 `alembic upgrade head`（任一容器先起即建表）
- 迁移脚本位于 `backend/migrations/versions/`
- CI 中使用空数据库测试迁移可重复执行
- 迁移失败 → backend 容器退出，old 版本不提供错误 schema（不产生漂移）
- 手动回滚：down 容器 / 按需 `alembic downgrade`（破坏性变更必须写成 expand-contract 两阶段）
- 多实例并发保护：alembic 迁移入口加 PostgreSQL advisory lock
- 私有化数据库连接串即标准 `postgresql://pt:pt@db:5432/patterntrace`，无托管连接池特化

## 5. 验收标准

- [ ] `docker compose up --build` 一键启动所有服务
- [ ] GitHub Actions PR 触发完整测试流水线
- [ ] 可私有化部署：`.env` 注入密钥 + compose 一键起全套，无外部云依赖
- [ ] Alembic 迁移在空数据库上成功执行
