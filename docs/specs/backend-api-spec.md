# Spec · backend/ — FastAPI 后端服务

> 模块路径：`pattern_trace/backend/`
> 技术栈：FastAPI / SQLAlchemy 2.0 / Alembic / Pydantic v2 / **Arq（async 任务队列）**
> 依赖模块：graph-builder、retrieval、llm-judge（**均为 backend 进程内 Python 包，见 D1**）
> 数据库：PostgreSQL 16 + pgvector
> 认证：access token（body 返回，前端内存持有）+ refresh token（HTTPOnly Cookie，7d，rotation）

> **实现状态（2026-08-25 校准）**：① analyze 主路径为 API 进程内
> `asyncio.create_task`（单实例默认形态）；arq worker 入口已在 `workers/worker.py`
> 就位并随 compose 部署，作为扩缩容切换项；q_llm 队列暂无生产者/消费者。
> ② 并发防重入用 DB partial unique index + 终态乐观守卫实现（非 Redis SETNX），
> 僵尸回收由每次 analyze 顺带触发（无独立定时进程）。③ refresh rotation 状态
> 已落 Redis（Lua 原子轮换），Redis 不可达时降级进程内。## 1. 目录结构

```
backend/
├── api/
│   ├── auth.py           # /auth/login, /auth/refresh, /auth/logout
│   ├── addresses.py      # /addresses/analyze, /addresses/{address}/subgraph
│   ├── patterns.py       # /patterns, /patterns/{id}
│   ├── judgments.py      # /judgments/{id}
│   ├── cases.py          # /cases CRUD + 关联地址 + 报告
│   ├── users.py          # /users 管理（admin only）
│   └── audit.py          # /audit-logs (admin only)
├── services/
│   ├── orchestration.py  # 判断编排：graph-builder → retrieval → llm-judge
│   ├── case_service.py   # 案件 CRUD + 报告任务编排
│   └── report_service.py # PDF/HTML 渲染（在 worker 进程执行）
├── core/
│   ├── config.py         # Pydantic Settings 环境变量
│   ├── security.py       # JWT / 密码哈希（bcrypt rounds=12）
│   ├── errors.py         # RFC 9457 Problem Details 统一错误处理
│   ├── rate_limit.py     # Redis 令牌桶
│   └── audit_log.py      # 审计日志中间件
├── workers/
│   ├── worker.py         # Arq WorkerSettings
│   └── tasks.py          # run_analysis / generate_report
├── graph_builder/        # D1: 进程内包（原 services/graph-builder）
├── retrieval/            # D1: 进程内包
├── llm_judge/            # D1: 进程内包
├── models/               # SQLAlchemy ORM models
├── schemas/              # Pydantic request/response schemas
└── main.py               # FastAPI app factory
```

## 2. 全局约定

### 错误响应格式

所有 4xx/5xx 响应采用 RFC 9457 `application/problem+json`：

```json
{
  "type": "https://docs.patterntrace.app/errors/validation",
  "title": "Validation Failed",
  "status": 422,
  "detail": "hops must be between 1 and 3",
  "instance": "/api/v1/addresses/analyze",
  "error_code": "VALIDATION_ERROR"
}
```

### 分页格式

所有列表接口统一响应包；`page_size` 上限 100：

```json
{"items": [...], "total": 123, "page": 1, "page_size": 20, "pages": 7}
```

Query params 支持 `page`, `page_size`, `sort_by`, `order=asc|desc`。

### 权限矩阵

| 操作 | anonymous | investigator | admin |
|---|---|---|---|
| POST /analyze | ✅（限演示白名单+配额） | ✅ | ✅ |
| GET judgments/subgraph/patterns | ✅ 只读 | ✅ | ✅ |
| cases CRUD / reports | ❌ 401 | ✅ | ✅ |
| users / audit-logs | ❌ 401 | ❌ 403 | ✅ |

## 3. API Endpoints

### POST /api/v1/auth/login

Request:
```json
{"email": "user@example.com", "password": "..."}
```

Response 200:
```json
{"access_token": "eyJ...", "token_type": "bearer", "expires_in": 900, "user": {"id": "uuid", "role": "investigator"}}
```

- access_token 由前端存于**内存**（不落 localStorage）
- refresh token 通过 `Set-Cookie: refresh_token=<jwt>; HttpOnly; Secure; SameSite=Lax; Path=/api/v1/auth` 下发（生产跨站部署改 `SameSite=None`）
- CSRF 防护：写请求要求自定义 header `X-Requested-With: XMLHttpRequest`（不可被表单伪造），配合 SameSite 双重防护

### POST /api/v1/auth/refresh

Request: 无 body，凭 Cookie 中 refresh_token。
Response 200: 同 login 结构；同时轮换新 refresh token 并作废旧 token。

**Rotation & Reuse Detection**：
- 每次 refresh 签发新 token，旧 token 标记 revoked
- 已撤销 token 再次使用 → 判定泄露，撤销整个 token family 并强制重新登录
- 存储于 Redis（key = token family id，TTL 7d），过期自动清理

### POST /api/v1/auth/logout

撤销当前 token family，清除 Cookie。204。

### POST /api/v1/addresses/analyze

Auth: 免登录（受成本控制约束，见下）

Request:
```json
{
  "address": "bc1qxy...",
  "hops": 3,
  "time_window_days": 90
}
```

Validation:
- address: BTC checksum 级校验（base58check / bech32 polymod，非正则）；长度 ≤ 62 字符
- hops: int, **1–3**, default 3（D6：与 BFS 三队列及特征向量维度一致，>3 返回 422）
- time_window_days: int, 7–365, default 90

**成本控制（匿名滥用防护）**：
- 免登录仅允许 `DEMO_ADDRESSES` 白名单内地址（env 配置）
- 登录用户可分析任意地址
- Rate limit：匿名 10 req/min/IP（X-Forwarded-For 仅信任 Fly.io/Vercel 代理层注入值）+ 匿名全局日配额 `ANON_DAILY_TASK_QUOTA`（默认 500 任务/天，超出返回 429）
- 429 响应带 `Retry-After` header，Problem Details 格式

Response 202 Accepted:
```json
{"judgment_id": "uuid", "status": "queued", "poll_url": "/api/v1/judgments/{uuid}"}
```

**幂等性**：同 `(address, hops, time_window_days)` 且已有进行中任务时直接返回该 judgment_id（200），不重复入队。Worker 内以 judgment_id 为锁 key 使用 Redis SETNX 防并发重入。

处理流程（Arq async task，D2）：
1. status → processing
2. 调用进程内 graph-builder 构建子图
3. 调用 retrieval 检索 Top-K pattern
4. 调用 llm-judge 结构化判断
5. 结果写入 judgments 表（evidence 校验通过才写入 completed）
6. 缓存到 Redis key `(builder_version, address, subgraph_hash, model, prompt_version)`

**任务可靠性**：
- Arq job timeout 120s，max_tries=2（仅网络类错误重试）
- 失败路径：status → failed，写入 error_code / error_message
- **僵尸回收**：定时任务每 60s 扫描 `status IN (queued,processing) AND updated_at < now() - interval '120 seconds'` → 标记 failed(`TASK_TIMEOUT`)
- 队列分离：`q_graph` 与 `q_llm` 独立并发配置，便于扩缩容

### GET /api/v1/addresses/{address}/subgraph

返回最新一条 completed judgment 的子图快照（支持 `?judgment_id=` 指定历史版本）：

```json
{
  "nodes": [
    {"id": "addr:bc1q...", "type": "address", "label": "Unknown Wallet", "balance_btc": 0.05},
    {"id": "tx:abc123...", "type": "transaction", "value_btc": 0.12, "timestamp": "..."}
  ],
  "edges": [
    {"id": "edge:addr:bc1q...->tx:abc...", "source": "addr:bc1q...", "target": "tx:abc...",
     "value_ratio": 0.8, "is_stopped_expansion": false}
  ]
}
```

节点/边 ID 规范（D3）：`addr:<address>` / `tx:<txid>` / `edge:<src_id>-><dst_id>`，由 graph-builder 生成，evidence 引用与前端高亮共用此 ID 空间。

### GET /api/v1/patterns

分页列表。Filters: `evidence_grade=A|B`，`search`（名称模糊）。响应为统一分页包。

### GET /api/v1/patterns/{id}

详情含 canonical_subgraph JSON 与版本字段。

### GET /api/v1/judgments/{id}

completed 示例：
```json
{
  "id": "uuid", "address": "bc1q...",
  "risk_level": "high", "matched_pattern_id": "uuid",
  "matched_pattern_name": "mixer_layering_3hop",
  "confidence": 0.87,
  "evidence": ["edge:addr:bc1q...->tx:abc...", "tx:def456..."],
  "reasoning": "子图呈现三层分层结构...",
  "recommended_action": "freeze",
  "subgraph": {...},
  "model": "Qwen/Qwen3-30B-A3B", "prompt_version": "v3", "builder_version": "gb-v1",
  "latency_ms": 6200,
  "status": "completed"
}
```

failed 时：
```json
{
  "id": "uuid", "address": "bc1q...", "status": "failed",
  "error_code": "LLM_VALIDATION_FAILED",
  "error_message": "evidence validation failed after 3 retries",
  "retry_count": 2, "failed_at": "2026-08-22T12:00:00Z"
}
```

状态机（D5）：`queued → processing → completed | failed`。终态后不再变更。

### Cases（登录必需，investigator/admin）

| Endpoint | 说明 |
|---|---|
| `GET /cases` | 分页列表，filter `status=open\|investigating\|closed` |
| `POST /cases` | 创建。支持 `Idempotency-Key` header（服务端缓存同 key 响应 24h 防重复创建） |
| `GET /cases/{id}` | 详情，含 addresses 数组（每项含关联 judgment 摘要） |
| `PATCH /cases/{id}` | 更新标题/描述/status（open→investigating→closed 单向流转） |
| `POST /cases/{id}/addresses` | 关联地址数组，`(case_id, address)` 复合唯一，重复添加幂等跳过 |
| `DELETE /cases/{id}/addresses/{address}` | 解除关联 |

### Reports

`POST /cases/{id}/reports?format=pdf|html` — **异步**（202 返回 `{report_id, poll_url}`；PDF 渲染 CPU 密集，在 worker 队列执行）：
- 案件地址数上限 50；单用户并发报告任务 ≤ 2
- `GET /reports/{report_id}` 轮询 → completed 后返回带签名的临时下载 URL（15 分钟有效）
- 报告内嵌 judgment hash / model / prompt_version 形成证据链

### Users & Audit

- `GET/POST/PATCH /api/v1/users`（admin only）：创建/禁用/角色变更
- 首个 admin 通过环境变量 `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD` 在首次启动时 seed
- `GET /api/v1/audit-logs`：admin only，分页 + 时间范围筛选

### Health checks

- `GET /healthz` — 进程存活，200
- `GET /readyz` — 检查 DB + Redis 连通性，任一失败返回 503

## 4. 数据模型

- users (uuid pk, email unique, hashed_password bcrypt(12), role enum, is_active bool, created_at)
  - 密码输入校验 max_length=72 bytes（bcrypt 截断限制）
- addresses_meta (address text pk, chain default 'btc', labels jsonb, last_analyzed_at, latest_judgment_id fk judgments ON DELETE SET NULL)
- patterns (uuid pk, name, description, canonical_subgraph jsonb, structural_features vector, semantic_embedding vector, embedding_model text NOT NULL, embedding_dim int NOT NULL, wl_fingerprint jsonb, content_hash sha256 unique, evidence_grade enum A/B, source, created_at)
- judgments (uuid pk, address fk, subgraph_snapshot jsonb LZ4 压缩, subgraph_hash, hops, risk_level nullable, matched_pattern_id fk patterns nullable, confidence nullable float, evidence jsonb, reasoning text nullable, model, prompt_version, builder_version, latency_ms, status enum queued/processing/completed/failed, error_code nullable, error_message nullable, retry_count int default 0, created_by nullable fk users, created_at, updated_at)
  - snapshot 估算 300–800KB/行 → 启用 LZ4 TOAST 压缩；GET 读取预算 ≤ 300ms；规模化演进方向：独立快照表或对象存储
- cases (uuid pk, title, description, status enum open/investigating/closed, created_by fk users)
- case_addresses (**PK (case_id, address)**, case_id fk ON DELETE CASCADE, address, judgment_id fk nullable ON DELETE SET NULL)
- reports (uuid pk, case_id fk, format enum pdf/html, status enum processing/completed/failed, storage_key, created_at)
- audit_logs (bigserial pk, user_id nullable, request_id uuid, http_method, http_path, response_status int, action, resource_type, resource_id, action_result enum success/failure, detail jsonb, latency_ms int, ip, user_agent, created_at)

## 5. 安全设计

- JWT：access 15min（内存持有）/ refresh 7d HTTPOnly Cookie + rotation + reuse detection
- CSRF：SameSite cookie + `X-Requested-With` 自定义 header 校验
- CORS：`allow_credentials=True` 时 origin 必须为显式白名单（禁止 `*`）
- 角色：investigator 读写案件；admin 管理用户与审计日志；权限不足返回 403（未认证 401）
- 所有写操作写 audit_logs
- Rate limiting：Redis 令牌桶；匿名 IP 维度 + 登录 user_id 维度双轨
- Pydantic v2 输入校验，所有字段强类型；retrieval 混合召回权重从服务端配置读取，禁止由请求传入（SQL 一律绑定参数）
- 密钥管理：生产经 Fly secrets 注入；compose 开发默认值仅限本地且显式标注

## 6. 验收标准

- [ ] docker-compose up 一键启动，readyz 通过后才接收流量
- [ ] JWT 登录 → 内存 access token 调用需认证接口成功；refresh rotation 生效
- [ ] POST /analyze 返回 202 → 轮询 → completed 或 failed 终态；失败态含 error_code
- [ ] 同参数并发提交只产生一条进行中 judgment
- [ ] evidence 中每个 ID 都能在 subgraph_snapshot 的 nodes.id ∪ edges.id 中找到
- [ ] 写操作未登录返回 401；investigator 访问 admin 接口返回 403
- [ ] 所有错误响应符合 RFC 9457 格式
- [ ] admin 可查询 audit_logs 并看到登录记录
