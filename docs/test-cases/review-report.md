# 测试用例评审报告

> 评审日期：2026-08-23
> 评审方式：三个 subagent 并行评审 + 主 agent 汇总交叉确认
> - **前端测试工程师**：frontend-test-cases.md 及前端相关跨模块用例，侧重测试场景正确性与测试方法正确性
> - **后端测试工程师**：backend / graph-builder / retrieval / llm-judge / ingest / infra 全量，侧重场景与方法正确性
> - **高级全栈测试工程师**：全集完整性，识别缺失测试场景并按价值排序补充建议
> 修复状态：本报告中所有"高置信问题"与"P0/P1 补充场景"已于同日修复落地到各用例文档。

---

## 一、总体结论

现有 201 条用例对 specs 的映射密度很高、追踪矩阵意识好，但整体呈 **"happy path + 单点故障"** 结构：

- 安全维度仅有认证正向用例——越权访问、CSRF 服务端强制、JWT 攻击面几乎空白
- 可靠性只有"回收僵尸任务"，没有 worker 崩溃恢复与 Redis/LLM provider 故障降级契约
- 约 8 处 P0 用例存在前置不可达或断言不可判定问题，需修订后才能有效执行
- 部分用例的验证手段依赖不可控运行时操作（手动改内存 token、真实 sleep 等待）或工具能力错位（Profiler 判定数组引用）

---

## 二、多 agent 交叉确认的高置信问题（已全部修复）

| # | 用例 | 问题 | 修复方式 |
|---|------|------|----------|
| 1 | FE-03 | 前置不可达：匿名非白名单地址在入队前就被 403/422 拒绝（BE-09），走不到 LLM 校验失败阶段 | 前置条件补充"已登录 investigator" |
| 2 | BE-09 / BE-21 | "403 或 422"、"422 或截断为 100" 双解预期，自动化断言无法落地 | BE-09 钉死 403 + error_code=DEMO_ADDRESS_REQUIRED；BE-21 钉死 422 |
| 3 | LJ-05/06/10/11 + BE-14 | 同一校验函数断言"返回 None"与"抛异常"自相矛盾；retry_count 口径不一 | 统一契约：parse_and_validate 校验失败一律抛 JudgmentValidationError；MAX_RETRIES=3 定义为"首调+最多2次重试=共3次调用"，retry_count 记录重试次数（最大值2） |
| 4 | RT-04 / RT-05 / BE-43 | 前置写"retrieval 服务运行中"，但 D1 决策明确 retrieval 是 backend 进程内包，无 HTTP 入口；BE-43 与 RT-05 重复 | 改为函数级单测 + SQLAlchemy 事件监听断言参数化绑定；BE-43 合并入 RT-05 并在矩阵标注 |
| 5 | CM-03 / CM-06 / LJ-20 | 轮询采样无法保证捕捉每次状态跳变；固定 500ms mock 测 p95≤10s 是恒真断言；n=10 算 p95 ≈ 最大值 | 状态迁移写事件表读取完整序列；性能断言改为管线开销扣除法 + 样本 ≥30；冷/热路径分开统计 |
| 6 | BE-12 / CM-12 | 并发 2 个请求不足以证明竞态安全；幂等预期隐含唯一约束机制但无用例验证 | 慢速 mock 保证窗口期 + 并发 ≥10 + 补"任务完成后提交应新建"反例 + 补 DB partial unique index 用例（BE-46） |
| 7 | GB-20 / IG-08 | GB-20 前置用 Blockstream 公共端点与"不依赖公网"冲突；IG-08 调真实 OpenAI API 违反 mock 原则 | 改带请求计数器的 mock server；IG-08 改本地 embedding stub |
| 8 | traceability-matrix | FR-15 表述自相矛盾（备注说 CM 补充但 CM 中无 PatternCompare 用例）；FE-10/FE-32 未被任何 FR 行引用 | FR-15 显式标注"不计入分母"；FE-10 → FR-29、FE-32 → FR-30 映射补齐 |

---

## 三、各模块主要修订点

### 前端（FE）

**场景错误**
- FE-05 标题说"高亮节点"但步骤只测 edge ID → 拆分为 FE-05a（edge 高亮）与 FE-33（node 高亮）
- FE-24 防竞态步骤与 FE-01"点击即跳转"矛盾 → 改为分析页内重新发起分析的竞态场景

**方法错误**
- FE-10/11 "手动改内存 token 过期时间"不可操作 → 改为网络层 mock 401 触发 refresh 流程
- FE-26 React DevTools Profiler 无法证明"nodes 数组引用未被重建" → 改为单元测试引用比较
- FE-31 Lighthouse 不校验 aria-label 语义正确性 → 改为 axe-core + Playwright DOM 属性显式断言

**方法低效**
- FE-02 人工观察"不经过 processing 状态"不可靠 → 改 Playwright 断言零 POST + 无 loading 态闪现
- FE-12 人工录 Performance 判 fps 重复性差 → Playwright + CDP tracing 自动化
- FE-28 真实等待 90s → 超时阈值环境变量化（测试环境 3–5s）+ fake timers 单测

### 后端 API（BE）

- API 路径书写不统一（BE-10/11 写 `POST /analyze` 缺 `/api/v1` 前缀）→ 全文统一完整路径
- BE-14 mock LLM 注入机制未说明 → 明确 `LLM_PROVIDER=mock` + 场景脚本切换方式
- BE-21 分页双解 → 钉死 422
- BE-23 幂等键只测相同 body → 补"同 key 不同 body 应 409"反例
- BE-29 quota 修改需重启未说明；两种 429 未区分 → 明确 env 注入重启方式 + 断言专属 error_code
- BE-32 "http_method=http_path" 笔误 → 更正为分立字段断言
- BE-37 "80 个字符"与"72 bytes"口径不一 → 用 73 ASCII 字符 + 多字节字符两组数据分别验证
- BE-40 等 60s 定时器扫描 → 拆冻结时钟单元测试 + 一条集成冒烟

### Graph Builder（GB）

- GB-16/17/18 依赖真实 sleep 时序 → 统一注入时钟 / 可替换 sleep，断言计算出的退避参数与状态转移逻辑
- GB-18 二义前置（"等待半开 或 手动配置 fallback"）→ 拆成两个独立用例各自单一前置
- GB-20 公共端点与 mock 原则冲突 → mock server 请求计数器断言 in-flight ≤ Semaphore 上限
- GB-21 进程内 async-lru 一级缓存与 Redis 二级无法区分 → 跨进程第二次请求验证 Redis 层
- GB-23 n=10 取 p95 统计不稳 → ≥50 次 + 注明预热层级与无网络 I/O 前提

### Retrieval / LLM-Judge / Ingest

- GT-03 编号笔误（矩阵映射到 RT-03）→ 更名为 RT-03
- RT-15 Top-K 3–5 条输出取决于阈值与候选数 → 固定 mock 向量与阈值后断言
- RT-16/17 离线评估不适合常规 CI 回归 → 标记为 nightly/发布前 eval，加数据快照版本锁定
- IG-05 正样本分母口径不一（IG-01 用 source=lazarus_confirmed，此处用 grade=A）→ 统一为 source=lazarus_confirmed AND grade=A
- IG-13 小表 Seq Scan 使 EXPLAIN 断言不稳定 → 配合大表数据或 SET enable_seqscan=off
- IG-08 真实 OpenAI API → 本地 embedding stub（维度/元数据断言不变）

### Infra / 跨模块（IF / CM）

- IF-01 "等待 60 秒后 ps"脆弱 → 轮询 healthcheck 直至 healthy（带超时）；ollama 模型加载列为本地检查项
- IF-05 容器内密钥回显风险 → 改存在性检查不回显内容
- IF-10 构造不出两实例并发迁移 → 改本地并发两个 alembic 进程验证 advisory lock
- IF-16 "检查配置指向 mock"不能证明无真实外呼 → CI 加 DNS/代理黑名单断言零命中
- CM-01 "全程无跨进程 RPC"不可外部观测 → 改 import 边界架构测试 + 本用例保留功能断言
- CM-02 步骤只取第一条 ID 但预期要求全集匹配 → ID 全集校验下沉为脚本遍历断言，前端仅保留 UI 点击断言
- CM-06 p95 统计口径修正（见高置信问题 #5）

---

## 四、建议补充的测试场景（已全部新增）

### P0（8 条，上线前必须）

| 新用例 ID | 场景 | 所在文档 |
|-----------|------|----------|
| SEC-01 | 水平越权：investigator A 直接 GET/PATCH B 的案件与报告 → 403/404 + 审计留痕 | security-test-cases.md |
| SEC-02 | Refresh Cookie 安全属性全集（SameSite/Secure/Path） | security-test-cases.md |
| SEC-03 | 后端 CSRF 强制拒绝（不带 X-Requested-With 的跨站写请求 → 403） | security-test-cases.md |
| SEC-04 | JWT 攻击面：篡改签名 / alg:none / 过期 / 改 role payload 均拒绝 | security-test-cases.md |
| BE-47 | judgment 非法状态跳变防御：终态重放 worker 消息被守卫拒绝 | backend-api-test-cases.md |
| REL-01 | Worker 崩溃恢复：kill -9 后 queued 任务重启续跑，不悬挂不丢失 | reliability-test-cases.md |
| REL-02 | Redis 不可用降级：analyze/rate-limit/缓存的 fail-open/closed 显式契约 | reliability-test-cases.md |
| REL-03 | LLM provider 超时/429 与校验失败 error_code 区分 | reliability-test-cases.md |

### P1（11 条）

| 新用例 ID | 场景 | 所在文档 |
|-----------|------|----------|
| SEC-05 | 登录爆破防护：高频错误密码触发限流，响应仍不泄露用户存在性 | security-test-cases.md |
| SEC-06 | 敏感信息泄露扫描：错误响应/日志/audit detail 中不得出现密钥与完整 token | security-test-cases.md |
| BE-48 | Idempotency-Key 同 key 不同 body → 409 | backend-api-test-cases.md |
| BE-49 | case_addresses 异常关联（不存在 case → 404；非法地址 → 422） | backend-api-test-cases.md |
| BE-50 | 报告下载 URL 过期与吊销 | backend-api-test-cases.md |
| CT-01 | OpenAPI 反向漂移检测：后端新增必填字段时 CI type-check 失败 | contract-test-cases.md |
| CT-02 | 轮询时序边界：假 UUID 稳定 404；poll_url 相对路径拼接契约 | contract-test-cases.md |
| CT-03 | ID 契约反向校验：畸形 subgraph 引用不存在节点 → 拒绝/占位而非白屏 | contract-test-cases.md |
| REL-04 | 部署回滚兼容性：expand 阶段新 schema + 旧代码并行读写正常 | reliability-test-cases.md |
| IG-15 | Embedding API 失败断点续跑：超时/限流后重跑仅补算缺失向量 | ingest-test-cases.md |
| PERF-01 | 大案件（50–100 地址）报告生成容量：耗时/内存/文件完整性 | performance-test-cases.md |

### P2（8 条）

| 新用例 ID | 场景 | 所在文档 |
|-----------|------|----------|
| SEC-07 | 分页极端值（page=0/负数/page_size=0/非整数）统一 422 | security-test-cases.md |
| SEC-08 | 地址规范化：bech32 大小写混写/大写前缀/超长字符串归一化行为 | security-test-cases.md |
| SEC-09 | 多标签页认证态一致性：一页登出后另一页行为 | security-test-cases.md |
| REL-05 | Esplora 主备全失败：任务进入明确 failed/degraded 终态 | reliability-test-cases.md |
| IG-16 | Parquet 数据损坏容错：schema 缺失/类型漂移报清晰错误且零写入 | ingest-test-cases.md |
| IG-17 | ingest 双实例并发：upsert 唯一键/advisory lock 保证行数不变 | ingest-test-cases.md |
| PERF-02 | 万级 judgment 表查询性能：subgraph 与列表查询走索引 p95 达标 | performance-test-cases.md |
| FE-34 | PatternCompare 双栏差异标注冒烟（FR-15） | frontend-test-cases.md |

---

## 五、执行方式总建议

1. **立即修订**（纯文档改动，本次已完成）：统一 API 路径书写、修复 GT-03 编号、统一 parse_and_validate 失败契约、统一 retry_count 语义、钉死 BE-09/21 双解预期
2. **测试分层**：
   - BE 系列 → pytest + httpx.AsyncClient 集成层（testcontainers/docker-compose），手动 curl 仅冒烟
   - FE 系列 → Playwright + MSW route mock
   - 所有涉及计数/时序/TTL/熔断/退避的用例统一"注入时钟 + mock 计数器"模式，禁止真实 sleep 等待
3. **性能拆档**：CI 冒烟阈值 + nightly 基准两档；p95 样本量 ≥30；明确计时起点终点与统计口径
4. **安全优先**：P0 补充清单第 1–4 条可基本封住越权与 token 攻击两类最大事故面

---

## 六、修复清单汇总

| 文件 | 修订用例 | 新增用例 |
|------|----------|----------|
| frontend-test-cases.md | FE-02,03,05,10,11,12,18,23,24,26,28,29,31,32 | FE-33, FE-34 |
| backend-api-test-cases.md | BE-09,10,11,12,14,18,21,22,23,27,29,32,37,38,40,41,43,44,45 | BE-46–50 |
| graph-builder-test-cases.md | GB-08,09,11,16,17,18,20,21,23 | — |
| retrieval-test-cases.md | GT-03→RT-03, RT-04,05,15,16,17 | — |
| llm-judge-test-cases.md | LJ-01,05,06,10,11,20 | — |
| ingest-test-cases.md | IG-01,05,08,13 | IG-15,16,17 |
| infra-test-cases.md | IF-01,05,10,16 | — |
| cross-module-test-cases.md | CM-01,02,03,04,06,07,11,12 | — |
| security-test-cases.md | （新建） | SEC-01–09 |
| reliability-test-cases.md | （新建） | REL-01–05 |
| contract-test-cases.md | （新建） | CT-01–03 |
| performance-test-cases.md | （新建） | PERF-01,02 |
| traceability-matrix.md | 全文更新映射与覆盖率 | 新增模块需求行 |
