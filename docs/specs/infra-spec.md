# Spec · infra/ — 部署与 CI/CD

> 模块路径：`pattern_trace/infra/`
> 本地开发：Docker Compose 一键启动
> 生产：前端 Vercel / 后端 Fly.io / 数据库 Neon

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

  ollama:
    image: ollama/ollama:latest
    ports: ["11434:11434"]
    volumes: [ollama_models:/root/.ollama]
```

## 2. 生产部署

| 组件 | 平台 | 说明 |
|---|---|---|
| 前端 | Vercel | Next.js 自动部署 |
| 后端 API | Fly.io | Docker 部署，共享代码库镜像 |
| Celery Worker | Fly.io（同镜像） | 独立进程 |
| PostgreSQL + pgvector | Neon / Supabase | Serverless Postgres |
| Redis | Upstash / Fly.io Redis | 免费层可用 |
| Ollama (LLM) | 自托管 GPU 服务器或云 API | 或 fallback 到 OpenAI |

环境变量通过 Fly.io secrets 管理：
```bash
fly secrets set DATABASE_URL=... REDIS_URL=... JWT_SECRET=... LLM_API_KEY=...
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
      - run: npx playwright test

  deploy:
    if: github.ref == 'refs/heads/main'
    needs: e2e
    runs-on: ubuntu-latest
    steps:
      - name: Deploy backend to Fly.io
        run: flyctl deploy --config infra/fly.toml
      - name: Trigger Vercel deployment
        run: curl -X POST ${{ secrets.VERCEL_DEPLOY_HOOK }}
```

## 4. 数据库迁移

- Alembic 在 Fly.io **release phase** 单实例执行 `alembic upgrade head`
- 迁移脚本位于 `backend/migrations/versions/`
- CI 中使用空数据库测试迁移可重复执行
- release phase 迁移失败 → **阻断部署**（旧实例继续服务旧 schema，不产生漂移）
- 手动回滚流程：`fly releases rollback` + 按需 `alembic downgrade`（破坏性变更必须写成 expand-contract 两阶段）
- 多实例并发保护：alembic 迁移入口加 PostgreSQL advisory lock
- 生产 Redis（Upstash TLS）连接串使用 `rediss://`；Neon PgBouncer transaction pooling 下 SQLAlchemy 连接串需 `?prepared_statement_cache_size=0` 类等价配置

## 5. 验收标准

- [ ] `docker compose up --build` 一键启动所有服务
- [ ] GitHub Actions PR 触发完整测试流水线
- [ ] main 分支合并自动部署到 Fly.io + Vercel
- [ ] Alembic 迁移在空数据库上成功执行
