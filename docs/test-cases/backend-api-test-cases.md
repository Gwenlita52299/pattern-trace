# 后端 API 测试用例 · backend-api-spec

> 模块路径：`backend/`
> 执行环境：docker-compose up（db + redis + backend + worker）或 uvicorn 本地
> 基础 URL：`http://localhost:8000/api/v1`

---

## BE-01 登录成功返回 access_token 与用户信息

- **优先级**：P0
- **来源**: §3 auth/login

**前置条件**
- 已 seed 用户 `admin@test.com / AdminP@ss1`（role=admin）

**操作步骤**
1. 发送 `POST /api/v1/auth/login`
   ```json
   {"email": "admin@test.com", "password": "AdminP@ss1"}
   ```
2. 检查响应体

**预期结果**
- HTTP 200
- body 包含 `access_token`（JWT 格式，三段 base64）、`token_type="bearer"`、`expires_in=900`
- body.user.role = "admin"
- Set-Cookie 头包含 refresh_token 且带 HttpOnly 标记

---

## BE-02 错误密码返回 401 Problem Details

- **优先级**：P0
- **来源**：§3 auth/login + §2 全局错误格式

**前置条件**
- 已 seed 用户 inv@test.com

**操作步骤**
1. `POST /api/v1/auth/login` 密码传 "WrongPass"

**预期结果**
- HTTP 401
- Content-Type: `application/problem+json`
- body 符合 RFC 9457：包含 type/title/status/detail/instance/error_code 字段
- error_code = "INVALID_CREDENTIALS"（或同类语义）

---

## BE-03 不存在的邮箱同样返回 401（不泄露存在性）

- **优先级**：P1
- **来源**：安全设计最佳实践 + §5 权限矩阵

**前置条件**
- 系统中不存在 `ghost@nowhere.com`

**操作步骤**
1. `POST /api/v1/auth/login` email=ghost@nowhere.com 任意密码

**预期结果**
- HTTP 401
- 错误消息与"错误密码"场景完全一致（防止枚举攻击）

---

## BE-04 access token 调用受保护接口成功

- **优先级**：P0
- **来源**：§6 验收标准第 2 条

**前置条件**
- BE-01 已获得有效 token

**操作步骤**
1. `GET /api/v1/cases` 带 header `Authorization: Bearer <token>`
2. 检查响应

**预期结果**
- HTTP 200
- 返回分页包结构 `{items, total, page, page_size, pages}`

---

## BE-05 无 token 访问写操作返回 401

- **优先级**：P0
- **来源**：§6 验收标准第 7 条

**前置条件**
- 无认证态

**操作步骤**
1. `POST /api/v1/cases` 不带 Authorization header

**预期结果**
- HTTP 401
- problem+json 格式错误体

---

## BE-06 investigator 访问 admin-only 接口返回 403

- **优先级**：P0
- **来源**：§6 验收标准第 7 条、权限矩阵

**前置条件**
- 已登录 investigator 角色

**操作步骤**
1. `GET /api/v1/audit-logs` 用 investigator token

**预期结果**
- HTTP 403（非 401）
- error_code = "FORBIDDEN"

---

## BE-07 admin 可查询 audit-logs 并看到登录记录

- **优先级**：P0
- **来源**：§6 验收标准第 8 条

**前置条件**
- admin 登录并访问过若干接口

**操作步骤**
1. `GET /api/v1/audit-logs?page=1&page_size=20`

**预期结果**
- HTTP 200
- items 中至少有一条 action="login" 的记录
- 每条记录含 request_id/http_method/http_path/response_status/latency_ms 字段

---

## BE-08 analyze 免登录演示白名单地址返回 202

- **优先级**：P0
- **来源**：§3 addresses/analyze 成本控制

**前置条件**
- DEMO_ADDRESSES 白名单已配置且包含目标地址
- worker 进程运行中

**操作步骤**
1. `POST /api/v1/addresses/analyze` 传入白名单地址 + hops=3

**预期结果**
- HTTP 202
- body 含 judgment_id (uuid)、status="queued"、poll_url
- poll_url 格式为 `/api/v1/judgments/<judgment_id>`

---

## BE-09 analyze 非白名单地址匿名请求被拒绝

- **优先级**：P0
- **来源**：§3 成本控制——免登录仅限白名单

**前置条件**
- 未登录
- 目标地址不在 DEMO_ADDRESSES 内但为合法 BTC 地址

**操作步骤**
1. `POST /api/v1/addresses/analyze` 传入该地址

**预期结果**
- HTTP 403
- problem+json 格式
- error_code = "DEMO_ADDRESS_REQUIRED"

---

## BE-10 analyze hops=4 返回 422 校验失败

- **优先级**：P0
- **来源**：§3 Validation hops 1–3（D6 决策）

**前置条件**
- 使用合法地址

**操作步骤**
1. `POST /api/v1/addresses/analyze` 传 `{"address":"bc1q...", "hops":4}`

**预期结果**
- HTTP 422
- detail 提及 "between 1 and 3"
- error_code = "VALIDATION_ERROR"

---

## BE-11 analyze 非法 BTC 地址返回 422

- **优先级**：P0
- **来源**：§3 address checksum 级校验

**前置条件**
- 准备一个格式像 BTC 但 checksum 错误的字符串

**操作步骤**
1. `POST /api/v1/addresses/analyze` 传 `{"address":"bc1qinvalidchecksum00000000000000000000000000000"}`

**预期结果**
- HTTP 422
- detail 说明地址校验失败（base58check/bech32 polymod 层面拒绝）

---

## BE-12 同参数并发提交只产生一条进行中任务

- **优先级**：P0
- **来源**：§3 analyze 幂等性要求

**前置条件**
- mock Esplora/LLM 注入延迟（≥5s）确保任务在窗口期内保持进行中状态
- pytest + httpx.AsyncClient 集成测试环境就绪

**操作步骤**
1. 使用 asyncio.gather 同时发送 ≥10 个完全相同的 POST /api/v1/addresses/analyze 请求（同 address/hops/time_window_days）
2. 对比所有响应的 judgment_id
3. 查询 judgments 表中该地址的进行中记录数

**预期结果**
- 所有响应返回相同 judgment_id（首个 202，后续 200 幂等复用）
- judgments 表中该参数组合仅有一条 status=queued/processing 的记录
- 反向用例：前一任务 completed 后再次提交相同参数 → 返回新 judgment_id（不复用）

---

## BE-13 轮询到 completed 终态包含完整字段

- **优先级**：P0
- **来源**：§3 GET /judgments/{id} completed 示例

**前置条件**
- BE-08 的分析已完成

**操作步骤**
1. `GET /api/v1/judgments/<id>` 轮询至 status=completed

**预期结果**
- body 含 risk_level ∈ {high,medium,low,no_match}
- matched_pattern_id 为 uuid 或 null
- evidence 为数组且每个元素以 addr:/tx:/edge: 前缀开头（D3）
- subgraph.nodes[].id 与 subgraph.edges[].id 均符合 D3 规范
- model/prompt_version/builder_version/latency_ms 均有值

---

## BE-14 轮询到 failed 终态包含 error_code 与 retry_count

- **优先级**：P0
- **来源**：§3 GET /judgments/{id} failed 示例、D5 状态机

**前置条件**
- `LLM_PROVIDER=mock` 环境变量已设置，mock 场景脚本配置为 `invalid_evidence_all_retries`
- MAX_RETRIES=3 语义：首调+最多2次重试=共3次调用，retry_count 记录重试次数（最大值=2）

**操作步骤**
1. 发起 analyze 并轮询至终态

**预期结果**
- 最终 status = "failed"
- body 含 error_code="LLM_VALIDATION_FAILED"、error_message、retry_count=2、failed_at 时间戳
- risk_level/confidence/reasoning 均为 null

---

## BE-15 终态后不再变更

- **优先级**：P1
- **来源**：§3 状态机说明

**前置条件**
- 存在一条 completed 的 judgment

**操作步骤**
1. 连续 GET 该 judgment 两次，间隔 5 秒
2. 对比两次响应

**预期结果**
- 两次 status 均 = "completed"，无变化
- updated_at 不变（终态不可逆）

---

## BE-16 refresh rotation 正常轮换

- **优先级**：P0
- **来源**：§3 auth/refresh rotation 要求

**前置条件**
- 已登录并获得 refresh cookie

**操作步骤**
1. `POST /api/v1/auth/refresh`（凭 Cookie）
2. 检查新 Set-Cookie 与旧 token 关系

**预期结果**
- HTTP 200 返回新 access_token
- 新 Set-Cookie 下发新 refresh_token（值不同于旧的）
- 旧 refresh token 在服务端标记 revoked

---

## BE-17 reuse detection 撤销整个 token family

- **优先级**：P0
- **来源**：§3 rotation & reuse detection

**前置条件**
- 已完成一次 rotate（old→new）

**操作步骤**
1. 用已被 revoke 的旧 refresh token 再次调用 `/auth/refresh`

**预期结果**
- HTTP 401
- 整个 token family 被撤销
- 之后即使用最新 token 也无法 refresh，需重新登录

---

## BE-18 logout 清除 Cookie 并撤销 family

- **优先级**：P1
- **来源**：§3 auth/logout

**前置条件**
- 已登录

**操作步骤**
1. `POST /api/v1/auth/logout` 带有效 Bearer token

**预期结果**
- HTTP 204
- Set-Cookie 过期 refresh_token（Max-Age=0）
- 后续用同 family 的 refresh 调用返回 401

---

## BE-19 healthz 返回 200

- **优先级**：P0
- **来源**：§3 Health checks

**前置条件**
- backend 服务运行中

**操作步骤**
1. `GET /healthz`

**预期结果**
- HTTP 200，body `{"status": "ok"}`

---

## BE-20 readyz DB 断连时返回 503

- **优先级**：P1
- **来源**：§3 readyz 检查 DB+Redis

**前置条件**
- 停止 PostgreSQL 容器

**操作步骤**
1. `GET /readyz`

**预期结果**
- HTTP 503
- body 说明 DB unreachable

---

## BE-21 分页参数上限 page_size ≤ 100

- **优先级**：P1
- **来源**：§2 全局约定分页格式

**前置条件**
- patterns 表有数据；已认证用户 token 可用

**操作步骤**
1. `GET /api/v1/patterns?page=1&page_size=500`

**预期结果**
- HTTP 422
- detail 说明 page_size 上限为 100
- 不允许无限制拉取全表

---

## BE-22 patterns 列表支持 evidence_grade 过滤

- **优先级**:P1
- **来源**：§3 GET /patterns filters

**前置条件**
- patterns 表中有 grade A 和 B 数据各若干条

**操作步骤**
1. `GET /api/v1/patterns?evidence_grade=A`
2. 检查返回项

**预期结果**
- 所有返回项 evidence_grade = "A"
- total 只计 A 类数量

---

## BE-23 Idempotency-Key 防重复创建案件

- **优先级**：P1
- **来源**：§3 Cases POST 幂等性

**前置条件**
- investigator 已登录

**操作步骤**
1. `POST /cases` 带 `Idempotency-Key: key-abc-123` 和 body
2. 网络超时后用同一 Idempotency-Key 重试

**预期结果**
- 两次请求返回同一个 case_id
- cases 表只创建了一条记录
- 反例：同一 Idempotency-Key 但不同 body → HTTP 409 Conflict，detail 说明幂等键冲突

---

## BE-24 case_addresses 复合主键防重复关联

- **优先级**：P1
- **来源**：§4 数据模型 case_addresses PK (case_id, address)

**前置条件**
- 已创建案件 case-001

**操作步骤**
1. `POST /cases/case-001/addresses` body 含地址 X
2. 再次发送完全相同请求

**预期结果**
- 第一次 HTTP 201；第二次 HTTP 200（幂等跳过）
- case_addresses 表仍只有一行 (case-001, X)

---

## BE-25 case status 单向流转 open→investigating→closed

- **优先级**：P1
- **来源**：§3 PATCH /cases/{id}

**前置条件**
- 存在 open 状态案件

**操作步骤**
1. PATCH status=investigating → 应成功
2. 再 PATCH status=closed → 应成功
3. 尝试 PATCH status=open（回退）

**预期结果**
- 步骤 1、2 成功
- 步骤 3 返回 422，detail 说明状态只能单向推进

---

## BE-26 报告生成异步 202 + 轮询下载

- **优先级**：P0
- **来源**：§3 Reports 异步契约

**前置条件**
- 已有关联地址的案件
- worker 运行中

**操作步骤**
1. `POST /cases/{id}/reports?format=pdf` → 记录 report_id
2. 轮询 `GET /reports/{report_id}`
3. 完成后获取下载 URL

**预期结果**
- 第一步 HTTP 202
- 轮询最终 status=completed
- 下载 URL 在 15 分钟内有效，可成功下载 PDF 文件
- PDF 内容包含 judgment hash / model / prompt_version 元数据

---

## BE-27 报告并发限制单用户 ≤ 2

- **优先级**：P2
- **来源**：§3 Reports 单用户并发报告任务 ≤ 2

**前置条件**
- 同一用户已有 2 个 processing 报告（通过慢速 mock 渲染 worker 保持 processing 状态）

**操作步骤**
1. 再发起第三个报告请求

**预期结果**
- HTTP 429
- Retry-After header 有值
- problem+json 格式说明并发上限

---

## BE-28 rate limit 匿名 IP 维度触发 429

- **优先级**：P1
- **来源**：§3 成本控制 rate limit 10 req/min/IP

**前置条件**
- 清空 Redis rate limit keys

**操作步骤**
1. 从同一 IP 在 60 秒内发送 11 次 POST /analyze（白名单地址）

**预期结果**
- 前 10 次 HTTP 202
- 第 11 次 HTTP 429 + Retry-After header

---

## BE-29 anon daily quota 超限返回 429

- **优先级**：P1
- **来源**：§3 ANON_DAILY_TASK_QUOTA 默认 500

**前置条件**
- 启动时注入环境变量 ANON_DAILY_TASK_QUOTA=5 并重启进程生效
- 与 BE-28 rate limit 用例使用不同 Redis namespace 隔离

**操作步骤**
1. 第 6 次发起 analyze

**预期结果**
- HTTP 429
- error_code = "QUOTA_EXCEEDED"（区别于限流的 "RATE_LIMITED"）
- problem+json 说明 daily quota exceeded

---

## BE-30 CORS 白名单 origin 放行

- **优先级**：P1
- **来源**：§5 CORS allow_credentials=True 时 origin 必须显式

**前置条件**
- 生产配置 CORS_ORIGINS=["https://app.patterntrace.app"]

**操作步骤**
1. 从 `https://app.patterntrace.app` 页面发跨域请求
2. 检查响应头

**预期结果**
- Access-Control-Allow-Origin = "https://app.patterntrace.app"（精确匹配）
- Access-Control-Allow-Credentials = true

---

## BE-31 CORS 未知 origin 拒绝

- **优先级**：P1
- **来源**：§5 安全设计

**前置条件**
- 生产 CORS 白名单配置生效（同 BE-30）

**操作步骤**
1. 从 `https://evil.example.com` 发跨域请求

**预期结果**
- 响应不含 Access-Control-Allow-Origin 头（浏览器拦截）

---

## BE-32 audit_logs 记录写操作详情

- **优先级**：P1
- **来源**：§4 audit_logs 字段完备性（评审 #14）

**前置条件**
- investigator 创建了一个案件

**操作步骤**
1. admin 查询 audit_logs 过滤 action=create_case

**预期结果**
- 找到对应记录
- 字段完整：user_id/request_id/http_method/http_path/response_status/action_result=success/latency_ms/ip/user_agent/detail(jsonb)；action 枚举值引用 spec 取值表而非硬编码猜测

---

## BE-33 subgraph 接口返回最新 judgment 快照

- **优先级**：P1
- **来源**：§3 GET /subgraph 默认取最新 completed

**前置条件**
- 地址 X 有两条 completed judgments（不同时间）

**操作步骤**
1. `GET /addresses/X/subgraph`（不带 judgment_id 参数）

**预期结果**
- 返回的是 created_at 最新的那条快照数据

---

## BE-34 subgraph 接口支持指定历史 judgment_id

- **优先级**：P2
- **来源**：§3 ?judgment_id= 参数

**前置条件**
- 地址 X 有两条 completed judgments

**操作步骤**
1. `GET /addresses/X/subgraph?judgment_id=<older_id>`

**预期结果**
- 返回 older_id 对应的快照而非最新的

---

## BE-35 bootstrap admin 通过环境变量注入

- **优先级**：P1
- **来源**：§3 Users & Audit 首个 admin 引导

**前置条件**
- 全新数据库，BOOTSTRAP_ADMIN_EMAIL/PASSWORD 已设置

**操作步骤**
1. 启动 backend 服务
2. 用 BOOTSTRAP_ADMIN_EMAIL 登录

**预期结果**
- 登录成功，角色为 admin
- users 表中存在该记录且 is_active=true

---

## BE-36 users 管理 API 仅 admin 可调用

- **优先级**：P1
- **来源**：§3 GET/POST/PATCH /users admin only

**前置条件**
- 分别准备 investigator 与 admin token

**操作步骤**
1. investigator `POST /users` 创建用户
2. admin 执行同样请求

**预期结果**
- 步骤 1 → 403
- 步骤 2 → 201 成功创建

---

## BE-37 密码超过 72 bytes 返回校验错误

- **优先级**：P2
- **来源**：§4 users bcrypt 截断限制

**前置条件**
- admin 已登录，可调用 POST /users

**操作步骤**
1. `POST /api/v1/users` 密码设为 73 个 ASCII 字符 → 应返回 422
2. `POST /api/v1/users` 密码设为 80 个中日韩字符（>72 bytes）→ 也应返回 422

**预期结果**
- 两组数据均返回 HTTP 422（按字节非字符校验）
- detail 说明 max_length 72 bytes

---

## BE-38 缓存命中时跳过 LLM 调用

- **优先级**：P1
- **来源**：§3 处理流程第 6 步缓存逻辑

**前置条件**
- 同一地址第一次分析已完成

**操作步骤**
1. 用完全相同参数再次发起 analyze
2. 断言 mock provider 内置调用计数器（pytest fixture 暴露 `mock_llm.call_count`）

**预期结果**
- 第二次直接从 Redis 缓存返回
- `mock_llm.call_count` 不增加
- 判决内容与第一次一致
- 注意：缓存 key 含 subgraph_hash，必须配合固定数据 mock Esplora 保证 hash 一致

---

## BE-39 builder_version 变更使缓存失效

- **优先级**：P2
- **来源**：评审 P2-23 缓存 key 加 builder_version

**前置条件**
- 已缓存某地址结果（builder_version=gb-v1）
- 修改 BUILDER_VERSION 后需重启进程生效（测试步骤中注明）

**操作步骤**
1. 将 builder_version 升级为 gb-v2 后重新分析同一地址

**预期结果**
- 缓存 miss（key 不同），重新走完整管线
- LLM 调用计数增加

---

## BE-40 僵尸任务 120 秒后被回收为 failed

- **优先级**：P0
- **来源**：§3 任务可靠性僵尸回收机制

**前置条件**
- 单元层：freezegun/注入 clock 使 updated_at=now()-180s 的 judgment 可构造
- 集成层：docker-compose 环境 worker 运行中

**操作步骤**
1. 单元层：直接调用回收函数，断言筛选条件与字段写入
2. 集成层冒烟：手动插入一条超时 judgment，等待定时器触发后查询

**预期结果**
- status 变为 failed
- error_code = "TASK_TIMEOUT"
- failed_at 时间戳为回收时刻

---

## BE-41 Problem Details 所有 4xx/5xx 一致

- **优先级**：P0
- **来源**：§2 全局约定错误格式

**前置条件**
- backend 服务运行中；500 通过 mock 内抛异常的注入钩子制造（如 `DEBUG_FORCE_500=true`）
- 注意：error_code 是项目扩展字段（RFC 9457 无此字段）

**操作步骤**
1. 依次触发 400（畸形 JSON）/401/403/404/422/429/500
2. 检查每次响应 Content-Type 与字段完整性

**预期结果**
- 所有错误响应 Content-Type 均为 application/problem+json
- 每个响应体均含 type/title/status/detail/instance 五个 RFC 字段 + error_code 项目扩展字段

---

## BE-42 404 不存在的 judgment

- **优先级**：P1
- **来源**：§3 GET /judgments/{id}

**前置条件**
- backend 服务运行中

**操作步骤**
1. `GET /api/v1/judgments/nonexistent-id`

**预期结果**
- HTTP 404
- problem+json 格式

---

## BE-43 SQL 注入防护——检索权重不可由请求注入

- **优先级**：P1
- **来源**：§5 权重仅服务端配置

**前置条件**
- retrieval 服务运行中

**操作步骤**
1. 构造请求试图将 w_struct 作为 query param 注入恶意 SQL
2. 观察 SQL 日志与行为

**预期结果**
- w_struct/w_semantic 始终从服务端 Settings 读取
- 请求参数被忽略
- SQL 日志显示参数化绑定（非字符串拼接）

---

## BE-44 X-Forwarded-For 仅信任代理层

- **优先级**：P2
- **来源**：§3 rate limit X-Forwarded-For 信任边界

**前置条件**
- 本地 nginx/Caddy 反代 fixture 模拟可信代理（uvicorn `--proxy-headers --forwarded-allow-ips=127.0.0.1`）

**操作步骤**
1. 绕过代理直连 uvicorn 端口，伪造 XFF header 发请求 → 应被忽略
2. 经反代发请求（真实 client IP 由代理注入）→ rate limit 以代理注入 IP 计算

**预期结果**
- 后端忽略客户端伪造的 XFF 值
- rate limit 以可信反向代理/网关注入的真实 client IP 计算
- 无法通过伪造 header 绕过限流

---

## BE-45 subgraph_snapshot LZ4 TOAST 压缩生效

- **优先级**：P2
- **来源**：§4 数据模型 snapshot 估算 300–800KB 行启用 LZ4

**前置条件**
- PostgreSQL shell 可访问
- 已确认列定义含 `ALTER TABLE judgments ALTER COLUMN subgraph_snapshot SET COMPRESSION lz4`
- 新插入行需 VACUUM FULL 后才可查询压缩标记

**操作步骤**
1. 插入一条 > 300KB 的 subgraph_snapshot 测试行
2. VACUUM FULL judgments
3. 执行 `SELECT pg_column_compression(subgraph_snapshot) FROM judgments WHERE id='<test_id>'`

**预期结果**
- pg_column_compression 返回 'l'（lz4）
- GET /judgments/{id} 响应时间 p95 ≤ 300ms（采样 ≥10 次）


## BE-46 同参数幂等提交依赖 DB partial unique index

- **优先级**:P0
- **来源**：BE-12 补充——验证机制本身而非仅行为表现

**前置条件**
- PostgreSQL shell 或 pytest DB fixture 就绪

**操作步骤**
1. 检查 judgments 表存在 partial unique index（如 `ON judgments (address, hops, time_window_days) WHERE status IN ('queued','processing')`）
2. 在两个并发事务中同时 INSERT 相同参数的 queued 行

**预期结果**
- 第二个 INSERT 因唯一约束冲突失败
- 应用层捕获冲突并复用已有 judgment_id
- 移除索引后重复此操作应产生两条记录（证明索引是幂等保障的关键机制）

---

## BE-47 judgment 非法状态跳变防御

- **优先级**:P0
- **来源**：BR-22 状态机终态不可变补充

**前置条件**
- 存在一条 status=completed 的 judgment
- 可访问 worker 任务消息队列或直接调用编排函数

**操作步骤**
1. 对该 completed judgment 重放 worker 任务消息（或调用编排处理函数传入相同 task payload）
2. 查询该 judgment 当前状态

**预期结果**
- 编排层状态守卫拒绝写入（不发生 completed→queued 回退）
- DB 中该 judgment status 保持 completed 不变
- 日志记录状态跳变拒绝事件

---

## BE-48 Idempotency-Key 同 key 不同 body 冲突

- **优先级**:P1
- **来源**：BE-23 补充——幂等键实现的常见缺陷点

**前置条件**
- investigator 已登录
- Idempotency-Key 存储机制运行中

**操作步骤**
1. `POST /api/v1/cases` 带 `Idempotency-Key: key-conflict-test` 和 body A（title="Case A"）
2. 用同一 Idempotency-Key 但 body B（title="Case B"）再次请求

**预期结果**
- 步骤 2 返回 HTTP 409 Conflict
- detail 说明幂等键与请求体不匹配
- cases 表中仅有 body A 创建的记录

---

## BE-49 case_addresses 异常关联

- **优先级**:P1
- **来源**：BE-24 补充——异常路径覆盖

**前置条件**
- investigator 已登录

**操作步骤**
1. `POST /api/v1/cases/nonexistent-case-id/addresses` 关联合法地址 X
2. `POST /api/v1/cases/<valid-case>/addresses` 关联非法 BTC 地址 "not-a-valid-address"

**预期结果**
- 步骤 1 返回 HTTP 404（案件不存在）
- 步骤 2 返回 HTTP 422（地址校验失败）
- case_addresses 表无新增行

---

## BE-50 报告下载 URL 过期与吊销

- **优先级**:P1
- **来源**：BE-26 补充——URL 生命周期管理

**前置条件**
- BE-26 已完成，获得一个有效的报告下载 URL

**操作步骤**
1. 将下载 URL 的过期时间手动设置为已过期（或等待 15 分钟 TTL 到期）
2. 再次访问该 URL
3. 将报告所属 case 设为 closed 后再访问 URL

**预期结果**
- 过期后访问返回 403 或 410（URL 失效）
- case closed 后 URL 仍可访问（closed 仅限制编辑不限制查看），或按 spec 定义的吊销策略返回对应错误码

---

## BE-51 Judgment 结论时间戳与不可变案件报告快照（issue #7）

- **优先级**：P1
- **来源**：GitHub issue #7 —— history Judgment 时间版本化 + 报告冻结快照

**前置条件**
- 已分析过某地址（completed），并通过 `GET /api/v1/judgments/:id` 获取结论

**操作步骤**
1. 对同一地址发起 2 次分析（不同 hops），各轮询至终态
2. 检查两次分析返回的 `concluded_at` / `data_as_of`
3. 再查第一次的 judgment，确认其结论内容与时间未被第二次分析覆盖
4. 创建案件并关联该地址，分析完成后查 `case_addresses.judgment_id`
5. 对该案件生成报告，记录报告内容；随后重新分析该地址，再次读取已生成报告

**预期结果**
- 每次分析产生独立 judgment 行，completed/failed 均写入 `concluded_at`；completed 还写入 `data_as_of`（链上数据时间点）
- 旧 judgment 的结论时间/内容在新分析后保持不变（时间版本化，不覆盖）
- 新分析完成后，关联案件的 `case_addresses.judgment_id` 回指该地址的最新 completed judgment
- 已生成报告（文件）在重新分析后内容不变（引用首次 judgment / subgraph_hash），下载与重生成不再重查地址全局最新 Judgment
- 报告证据链包含 `judgment_id / subgraph_hash / model / prompt_version / builder_version / risk_level / confidence / evidence / reasoning / recommended_action / concluded_at / data_as_of`

---
