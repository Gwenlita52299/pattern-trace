# PatternTrace · 阶段6 收口报告：全局端到端校验 + Spec 差异比对

> 日期：2026-08-25
> 范围：`infra/verify_phase6.sh` 门禁、活系统（docker compose 六容器）全链路 E2E、
> `docs/specs/` 八份 spec 与实现逐条比对。
> 方法：门禁脚本实跑；E2E 对 `localhost:8000` 真实 API 执行（非 TestClient）；
> 比对结论经二次抽查核实（每条 P0/P1 均验证过源码）。

---

## 一、结论

| 项 | 结果 |
|---|---|
| verify_phase6.sh 门禁 | ✅ 19/19 通过 |
| 活系统全链路 E2E | ✅ 认证→建案→关联→分析→子图→报告→签名下载 全通 |
| mock 种子案例 ≤30s | ✅ 三档 high / low / no_match 均 <0.5s 出结论 |
| 真实 LLM 推理（ollama） | ⚠️ 管线机制正确（重试/防幻觉校验/终态）；qwen3:0.6b 因引用非法被校验拒绝，spec 档位（8b/30b）未在本机验证 |
| spec 一致性 | ❌ 有实质出入：P0×3、P1×20+，详见 §4 |

**E2E 共发现并已修复 4 个「测试全绿但部署必挂」的 P0 缺陷**——全部只在真实容器环境暴露：

1. **镜像缺少 fixture 数据**：Dockerfile 未 COPY `infra/`，`FixtureTxProvider`
   与 `GET /demo/addresses` 运行时读不到 `demo_txs.json` → 所有 analyze 500。
   （修复：`infra/Dockerfile.backend` 增加 `COPY infra/ infra/`）
2. **登录态 analyze 必然 FK 500**：`judgments.created_by` 外键指向 `users(id)`，
   代码却存 email（`app.py:565`）。匿名路径 user=None 为 NULL 所以从未触发；
   单测无真实 DB 也测不出。（修复：改为 `user["id"]`，与 reports 一致）
3. **PDF 报告在新部署必挂**：pyproject 加了 fpdf2 但 `uv.lock` 未更新，
   `uv sync --frozen` 装出的容器无 fpdf2。（修复：`uv lock` 重新生成）
4. **ollama provider 在容器必挂**：providers.py 运行时 `import httpx`，
   但 httpx 只在 dev extras。宿主机有 dev 依赖所以测试全绿。
   （修复：httpx 移入主依赖 + re-lock）

另修配置问题：compose `environment:` 不透传 `LLM_MODEL`（shell 变量只参与插值），
backend/worker 均已加 `- LLM_MODEL=${LLM_MODEL:-qwen3:30b-a3b}`。

### 门禁脚本修复（显示层）

- `verify_phase6.sh` 两处 pytest 调用的 CLI `-q` 与 pyproject `addopts="-q"`
  叠加成 `-qq`，隐藏了 "N passed" 汇总行 → 通过计数解析为空。去掉 CLI `-q`。
- `perf_phase6.py` RSS 打印把 macOS 的字节单位当 KB 除（显示 105280MB），
  按 `sys.platform` 归一后正确显示 103MB→122MB。

---

## 二、活系统 E2E 实测记录

环境：docker compose 全栈（db/pgvector、redis、ollama+qwen3、backend、worker、frontend），
`LLM_PROVIDER=ollama LLM_MODEL=qwen3:0.6b GRAPH_DATA_MODE=fixture(默认)`。

| 步骤 | 结果 |
|---|---|
| POST /auth/login（CSRF 头必需，值须为 `XMLHttpRequest`） | ✅ 200 |
| GET /demo/addresses | ✅ 200（修复 #1 前 500） |
| POST /cases + 关联地址 | ✅ 201 |
| POST /addresses/analyze | ✅ 202（修复 #2 前 500） |
| 轮询判决至终态 | ✅ completed / failed 均正确落终态与 error_code |
| evidence ⊆ 子图快照 ids（D3 反向校验） | ✅ 缓存命中判决 3 条 evidence 全部闭合 |
| 子图快照 | ✅ 41 个节点/边 ID |
| POST /cases/{id}/reports?format=pdf | ✅ 202（修复 #3 前 failed: no fpdf2） |
| 签名下载（exp/sig 缺失或过期拒绝） | ✅ 200 + %PDF 魔数；去参数 403/422 |
| 无 CSRF 头写请求 | ✅ 403 |

真实推理观察：清空判决缓存后 ollama qwen3:0.6b 全管线跑通约 28–33s，
模型把检索候选的字段名（`pattern_name` / `synth_hybrid_l4`）当作 evidence ID 引用，
被防幻觉校验连续拒绝 2 次 → `LLM_VALIDATION_FAILED` 终态。
**机制符合 llm-judge-spec §5 设计**（非法引用不得通过）；属小模型能力不足，
spec 预期档位为 Qwen3-30B-A3B / 8B。生产部署需配置足量模型。

---

## 三、Spec ↔ 实现差异（按严重度）

> 「✓ 已核实」= 本报告作者亲自读源码确认；其余为模块比对代理结论（附文件:行号）。

### P0（破坏核心设计承诺）

| # | 模块 | 问题 | 位置 |
|---|---|---|---|
| 1 | backend-api | ✓ refresh token 同时在 login/refresh 响应 body 返回，HttpOnly Cookie 形同虚设，D4 的 XSS 威胁模型被绕过（代码注释自称「兼容阶段0调用方」） | app.py:452,498 |
| 2 | frontend | ✓ 组件全面使用 Tailwind 工具类，但 tailwindcss/postcss 未安装、globals.css 无定义 → 生产构建样式整体失效 | package.json, globals.css |
| 3 | infra | ✓ CI/deploy workflow 所有步骤带 `cd pattern_trace` 前缀，本目录即仓库根，CI 实际跑不起来（佐证：从未有 CI 运行记录） | ci.yml:46-47 等 |
| 4 | backend-api | ✓ PBKDF2-HMAC-SHA256 iterations=12 冒充 bcrypt cost 12（自述 MVP 骨架），强度远低于 spec 要求 | security.py:24 |

### P1（功能缺失或语义偏离）

**架构**
- ✓ arq 入口存在且 worker 容器健康，但 analyze 主路径是 API 进程内
  `asyncio.create_task`（orchestration docstring 自认），spec 的双队列
  q_graph/q_llm 中 q_llm 无任何生产者/消费者，job timeout/max_tries 不生效。
  （比对初报称「workers/ 目录不存在」，经核实为误报，以此为准）— workers/worker.py
- ✓ RefreshTokenStore 是进程内 dict：重启丢 rotation 状态（reuse detection 失效）、
  多实例不共享；models 里定义的 RefreshToken 表无任何代码使用 — security.py:79-108
- ✓ 无 CORS 中间件，前后端分离部署跨域不可用 — app.py 全文
- ✓ Fly.toml 无 release_command，迁移在 app 启动命令里执行且双进程组各自跑，
  env.py 亦无 advisory lock → 并发迁移风险 — fly.toml, migrations/env.py
- deploy workflow 与 CI 无门禁关联（CI 失败照样部署）— deploy.yml:3-12

**图构建（graph-builder-spec）**
- §7 整套 Esplora 容错层（L1/L2 缓存、退避重试、熔断、备用 provider、并发预算）
  实现完整且有单测，但生产编排走同步直连，未接线（死代码）— esplora.py vs orchestration.py
- out_of_range 终止条件：build() 未传 seed_block_time，真实管线恒不触发 — orchestration.py:175
- unspent 终止：live 模式 `_map_tx` 恒返回空 unspent_outputs，条件失效 — data_source.py:71
- Edge schema 缺 5 个 spec 字段（time_delta 两维、tx_fee_ratio、fanout_ratio、总量），
 连带 WL 分桶退化 — builder.py:52-65
- per-layer 裁剪实现为「每队列条目 50」而非 spec 的「每层共享 50」— builder.py:244,270

**检索（retrieval-spec）**
- 结构特征仅 8/20 维（layer_depth_distribution 等点名维度全缺），零填充充数 ✓ 已核实
- 特征未归一化，原始计数直接进余弦距离，大量级分量主导 ✓ 已核实 features.py:51
- embedding 仅 SHA-256 stub，但 DB 元数据记 "text-embedding-3-small" 名不符实 ✓

**API（backend-api-spec）**
- 缺 `GET /patterns/{id}` 详情端点；用户无 GET 列表
- 限流为进程内时间戳列表：非 Redis、多实例失效、登录用户无限流 ✓ app.py:94
- X-Forwarded-For 未处理，反代后所有匿名用户共享配额 ✓ app.py:543
- 第二个 `GET /audit-logs` 死路由注册（顺序依赖，脆弱）✓ app.py:1017

**前端（frontend-spec）**
- PatternCompare 双栏对比组件不存在；SWR 未用（手写 useEffect）
- 案件详情「发起分析」写 sessionStorage 并跳 `/?address=`，首页不读取 → 断链 ✓ 已核实 page.tsx:18-22
- poll_url 被忽略硬编码轮询路径（当前恰好一致，功能等价）
- frontend/Dockerfile：无锁文件安装 + 生产镜像跑 `npm run dev` ✓ 已核实

**测试（tests-spec）**
- Playwright E2E、integration/ 目录、eval_llm_judge、eval_latency、
  bench_graph_builder（Python vs numpy vs PyO3 对比表）五类资产无实现或被降级替代
  （替代物见 tests/evaluation/run_e2e_phase*.py 与 test_builder_perf.py）
- recall@10=1.0 的基线建立在合成模板族 + 查询向量取自身已存向量的理想假设上，
  含金量远低于 spec 设想的 Lazarus 留出集口径
- coverage fail_under=99 配置存在，但仓库内无可查证的覆盖率数据产物
- PERF-02 在 CI nightly 永远 [skip]（无灌库步骤，行数不足直接跳过）

**Ingest（ingest-spec）**
- 正样本实际来源是合成语料 1500 条（golden fixture 只有 3 seed），新增 spec 外
  corpus_gen.py；负样本为确定性三模板而非公开浏览器数据
- mixer 清单加载源缺失（MIXER_LABELS 认 "mixer" 标签但无写入路径）

### P2（择要）

- logout 204 带 body（违反 HTTP 语义）；refresh 响应缺 user 字段
- 分页无 sort_by/order；LoginRequest.password 按字符数而非字节数校验
- 错误 type_uri 默认 about:blank 而非 spec 文档 URL
- early_stop 摘要节点 id `tx:<txid>:overflow` 不符 D3 解析规则；builder 的
  value_ratio 恒 0 与 ingest 同位置边不一致；两侧 canonical 浮点精度口径不一
- VerdictCard 把 judgment id 前 8 位标为 `model {…}`；graph_safety.ts 前端零引用
- vercel.json installCommand 未用 npm ci（不可复现构建）
- tests/unit/__pycache__ 残留孤儿 .pyc

---

## 四、一致面（比对确认无需行动）

三队列 BFS + 快照语义、六项终止判定级联顺序、D3 ID 契约与 evidence 反向定位、
hops 1–3、混合召回权重服务端锁定、负样本隔离表、HNSW 双列索引、embedding_model
双重锁校验、JWT access/refresh TTL 与 Cookie 属性、CSRF 强制、审计日志覆盖写操作、
Idempotency-Key、签名 URL HMAC+15min、报告证据链内嵌（hash/model/prompt/builder）、
权限矩阵 401/403、Problem Details 格式、compose 六服务与健康检查、CI 三 job 骨架、
Fly/Vercel 平台分工、P2 明确不做清单与 product-design 逐字一致、mock 三档种子案例。

## 五、建议处理顺序

1. **发布前必须**：P0-1（response 去 refresh_token）、P0-2（Tailwind 安装）、
   P0-3（workflow 路径）、P0-4（bcrypt）；P1 的 CORS、release_command+advisory lock、
   refresh store 落 Redis。
2. **公网演示前建议**：Esplora 容错层接线或删码、out_of_range/unspent 补齐、
   前端发起分析断链、Dockerfile 用 npm ci+start。
3. **记录为已知偏差**（有意为之且有注释佐证）：进程内 asyncio 替代 arq 主路径、
   合成语料替代真实 Parquet、8 维特征 MVP——建议回写 spec 或在 spec 标注实现状态，
   保持文档与代码不脱节。

---

## 六、修复执行记录（2026-08-25 第二轮）

§五 三档全部完成。回归口径：`pytest tests/unit` 221 passed；
活系统 E2E 14/14 通过；`verify_phase6.sh` 19/19；前端 `next build`/`tsc` 干净，
产物 CSS 已含工具类。

| 项 | 修复方式 | 主要文件 |
|---|---|---|
| P0-1 | login/refresh 响应不再含 refresh_token，仅 HttpOnly Cookie；单测改走 Cookie 流转；前端裸 fetch 的 refresh 补上缺失的 CSRF 头 | app.py、test_auth_refresh.py、api.ts |
| P0-2 | tailwindcss v3 + postcss + autoprefixer，配置 content 扫描 src/** | frontend/tailwind.config.ts 等 |
| P0-3 | 移除 workflow 全部 `pattern_trace/` 前缀；auth 冒烟先建号再登录 | ci.yml、deploy.yml |
| P0-4 | 真 bcrypt cost 12；存量 pbkdf2$ 哈希保留验证兼容 | security.py、pyproject |
| P1 CORS | CORSMiddleware 显式白名单（默认 localhost:3000，生产走 CORS_ORIGINS），最后注册保证 preflight 不经 CSRF | app.py、config.py |
| P1 迁移 | fly.toml `[deploy] release_command`，app 进程去 alembic；env.py 加 pg_advisory_lock | fly.toml、migrations/env.py |
| P1 refresh store | Redis Lua 原子轮换（防并发重放分裂 token），不可达降级进程内；生产 Cookie 同步改 SameSite=None | security.py、app.py |
| 二档容错层 | LiveEsploraProvider 接线重试退避/熔断切备用/Redis L2（共享 CircuitBreaker 与缓存键规范） | graph_builder/data_source.py |
| 二档终止条件 | unspent 取自响应 vout spent 状态；编排层传 seed_block_time 使 out_of_range 生效（fixture/live 双模式） | data_source.py、orchestration.py |
| 二档前端断链 | 首页消费 `?address=` 与 sessionStorage 并清理 URL | frontend/src/app/page.tsx |
| 二档镜像 | npm ci + next start + NEXT_PUBLIC build arg + frontend/.dockerignore | frontend/Dockerfile、docker-compose.yml |
| 三档标注 | backend-api / ingest / retrieval / graph-builder 四份 spec 头部加「实现状态」注记 | docs/specs/*.md |

未处理（后续迭代）：deploy 与 CI 门禁联动、patterns/{id} 详情端点、限流迁 Redis、
X-Forwarded-For、死路由清理、Playwright/eval 资产补齐——见 §三 P1/P2 清单。

