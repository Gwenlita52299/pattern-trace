# 安全测试用例 · 跨模块安全

> 覆盖认证边界、越权访问、注入防护、敏感信息泄露
> 执行环境：docker-compose 全栈 + pytest 集成测试

---

## SEC-01 水平越权访问案件与报告

- **优先级**:P0
- **来源**：BR-03 权限矩阵补充——水平越权

**前置条件**
- 已创建两个 investigator 用户 A 与 B，各自拥有独立案件
- investigator A 的 token 可用

**操作步骤**
1. investigator A 使用 `GET /api/v1/cases/{case_id_of_B}` 访问 B 的案件
2. investigator A 使用 `PATCH /api/v1/cases/{case_id_of_B}` 修改 B 的案件状态
3. investigator A 使用 B 的报告 report_id 尝试下载

**预期结果**
- 步骤 1 返回 HTTP 403 或 404（按 spec 定义的隔离策略）
- 步骤 2 同样被拒绝，B 的案件数据未被修改
- 步骤 3 报告下载被拒绝
- 以上每次拒绝均在 audit_logs 中留痕（action=access_denied）

---

## SEC-02 Refresh Token Cookie 安全属性全集

- **优先级**:P0
- **来源**：BR-05 补充——SameSite/Secure/Path 完整性

**前置条件**
- BE-01 登录成功获得 Set-Cookie 响应

**操作步骤**
1. 解析 Set-Cookie 头中 refresh_token 的全部属性
2. 从第三方域页面发起跨站请求验证 Cookie 是否被携带

**预期结果**
- Set-Cookie 包含 HttpOnly ✓
- Set-Cookie 包含 Secure ✓（生产环境强制；dev 环境可豁免）
- Set-Cookie 包含 SameSite=Lax 或 Strict ✓
- Set-Cookie 包含 Path=/api/v1/auth（限定作用域）✓
- 跨站请求不携带 refresh_token cookie

---

## SEC-03 后端 CSRF 强制拒绝

- **优先级**:P0
- **来源**：FR-21 反向验证——服务端必须主动校验而非依赖前端自觉附加

**前置条件**
- 已登录用户（refresh_token cookie 有效）

**操作步骤**
1. 发送 `POST /api/v1/auth/logout` 不带 `X-Requested-With` header 且凭 Cookie 认证
2. 发送 `POST /api/v1/cases` 同样缺少该 header

**预期结果**
- 两个请求均返回 HTTP 403
- error_code = "CSRF_CHECK_FAILED" 或等效
- 操作未执行（无 logout/无 case 创建）

---

## SEC-04 JWT 攻击面——伪造与算法攻击

- **优先级**:P0
- **来源**：BR-03/04 补充——token 完整性

**前置条件**
- BE-01 获得有效 access_token 作为基准
- JWT_SECRET 已知（测试环境）

**操作步骤**
1. 篡改 payload 中 role 字段为 "admin" 后用正确密钥重签 → 发送请求
2. 构造 `alg:none` 的空签名 token → 发送请求
3. 使用过期 exp 时间戳的有效签名 token → 发送请求
4. 用错误密钥签名的 token → 发送请求

**预期结果**
- 场景 1：如果 role 是从 DB 读取而非仅信 JWT claim 则正常（或返回 403）；如果仅信 JWT 则此场景应通过 spec 确认是否允许（记录设计决策）
- 场景 2–4 均返回 HTTP 401 + problem+json
- error_code 区分 TOKEN_EXPIRED / INVALID_SIGNATURE 等

---

## SEC-05 登录爆破防护

- **优先级**:P1
- **来源**：BR-12 补充——登录端点限流

**前置条件**
- 已知有效邮箱 inv@test.com
- Redis rate limit 就绪

**操作步骤**
1. 从同一 IP 在 60 秒内发送 10 次 `POST /auth/login` 密码均错误
2. 第 11 次使用正确密码尝试登录

**预期结果**
- 触发 IP 维度限流后第 11 次请求返回 429 + Retry-After header
- 所有失败响应的错误消息一致，不泄露用户存在性
- 限流窗口过后可恢复正常登录

---

## SEC-06 敏感信息泄露扫描

- **优先级**:P1
- **来源**：安全设计最佳实践

**前置条件**
- docker-compose 全栈运行中
- 可访问应用日志与数据库

**操作步骤**
1. 触发各类错误响应（400/401/403/422/429/500），检查响应体
2. 检查 backend/worker 日志输出
3. 查询 audit_logs 表 detail(jsonb) 字段

**预期结果**
- 错误响应体不含 JWT_SECRET、LLM API key、密码明文、完整 access_token/refresh_token
- 日志不含上述敏感值
- audit_logs.detail 中 request/response 快照已做脱敏处理

---

## SEC-07 分页参数极端值统一校验

- **优先级**:P2
- **来源**：BE-21 补充——输入验证完备性

**前置条件**
- backend 服务运行中；已认证用户 token 可用

**操作步骤**
1. 分别发送以下参数组合的 GET /api/v1/patterns：
   - page=0, page_size=20
   - page=-1, page_size=20
   - page=1, page_size=0
   - page=1, page_size=-10
   - page=abc, page_size=def
   - page=999999999999999999999, page_size=100

**预期结果**
- 所有非法组合均返回 HTTP 422 + problem+json
- 错误信息指明具体参数问题
- 不产生服务器内部错误（500）

---

## SEC-08 地址规范化行为

- **优先级**:P2
- **来源**：BE-11 补充——输入归一化

**前置条件**
- backend 服务运行中

**操作步骤**
1. `POST /api/v1/addresses/analyze` 地址为全大写 bech32（BC1Q...）
2. `POST /api/v1/addresses/analyze` 地址为混合大小写但 checksum 正确
3. `POST /api/v1/addresses/analyze` 地址为 >200 字符的超长字符串

**预期结果**
- 步骤 1–2：bech32 大小写混写应被接受并规范化存储（或统一拒绝，以实现为准，记录决策）
- 步骤 3：返回 HTTP 422（超长拒绝）
- 所有合法变体在 DB 中以同一规范化形式存储（幂等去重生效）

---

## SEC-09 多标签页认证态一致性

- **优先级**:P2
- **来源**：FR-19/FR-20 补充——跨标签页状态同步

**前置条件**
- 浏览器打开两个标签页均处于已登录状态
- 标签页 A 位于 `/cases`

**操作步骤**
1. 在标签页 B 点击登出按钮
2. 切换到标签页 A 执行任意需认证操作（如刷新 `/cases` 页面）

**预期结果**
- 标签页 A 收到 401 → refresh 也失败 → 自动跳转到 `/login`
- 无残留内存态导致的异常渲染或报错
