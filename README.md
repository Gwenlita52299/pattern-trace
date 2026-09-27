# PatternTrace

比特币地址资金链路溯源与洗钱模式识别平台：输入任意 BTC 地址，构建受控交易子图，
在知识库中检索相似模式（Wasabi / Lazarus / 混币中转等），由 LLM 给出结构化风险
判断（**high / medium / low / no_match** 四档），并支持案件管理与报告导出。

## 核心能力

| 能力 | 说明 |
|---|---|
| 子图构建 | BFS 三队列 + 五类终止条件（unspent / 时间窗 / 深度≤3 / 规模裁剪 / early-stop）；live 模式带重试退避、熔断切备用端点与 Redis 缓存 |
| 知识库 | Lazarus confirmed 案例切图入库（graphormer_v2 数据源，9,346 条 pattern），每条带 Graphormer pooled 向量 `vector(784)`（HNSW 索引）+ 检索指纹 JSONB（WL 多重集/金额/收款/UTXO/计数向量）；负样本独立表隔离，不参与召回 |
| 混合检索 | Graphormer pooled cosine 召回（9,346 规模走精确扫描）→ 三通道精排 `0.1·cos(Graphormer) + 0.1·wljac + 0.8·ov`（unseen-similarity 基准组合，通道权重 `channel_w_*` 可调）→ Top-K |
| LLM 判断 | DeepSeek 云端主推，Ollama 本地/OpenAI 兼容可切换，mock 可离线演示；JSON 结构化输出；evidence 统一引用地址节点（`addr:*`，防幻觉校验，非法引用重试后落 failed） |
| 分析流程 | 四阶段进度条（BFS 构建 → 混合检索 Top-K → WL kernel 精排 → LLM 判断）：worker 经 Redis 上报阶段，前端流势逐格推进，失败标注在具体阶段 |
| 可视化 | React Flow 分层画布、物证点击高亮完整链路（节点+相邻边+邻接节点）、节点折叠/展开、混币器琥珀框双编码 |
| 业务闭环 | 案件 CRUD、地址关联、异步 PDF/HTML 报告（HMAC 签名下载）、审计日志 |
| 认证体验 | 导航栏登录弹窗 + 用户中心下拉；整页刷新经 restoreSession 恢复会话；cases 页未登录内联提示、登录后自动刷新 |

## 架构

```
frontend (Next.js + React Flow)      backend (FastAPI)            workers (arq)
        │  /api/v1（Next rewrites 同源代理）│                          │
        ├──────────────────────────────► │  JWT access+refresh      │
        │                                ├──────────────────────────┤
        │                          PostgreSQL (pgvector)   Redis (queue/cache)
                                         DeepSeek API (或本地 llama.cpp)
```

浏览器只与前端同源通信（`next.config.mjs` rewrites 转发到后端）：refresh cookie
始终 same-site，登录态跨刷新可恢复；`API_PROXY_URL`（构建期注入）指定后端地址。

- `backend/graph_builder/`：子图构建核心（BFS 三队列 + 五类终止条件，D3 全局 ID 规范 `addr:*` / `tx:*` / `edge:*`）
- `backend/detection/`：CoinJoin / 跨链 OP_RETURN 运行时判定（模块化协议检测，取代旧跨链 CSV 标签库）
- `backend/retrieval/`：Graphormer 向量召回 + 检索指纹三通道（wljac / fp / ov）精排
- `backend/llm_judge/`：provider 抽象（deepseek / ollama / OpenAI 兼容 / mock）与防幻觉结构化判断
- `backend/services/`：分析编排、报告生成、种子案例
- `workers/worker.py`：arq 入口（analyze 默认进程内执行，扩缩容时切换队列形态）
- `ingest/`：Lazarus 切图、正负样本生成、embedding 计算
- `tests/unit/`：后端 460+ 条测试用例的自动化收口（含契约测试 CT-01~03）；前端 vitest 18 例（address-flow 变换 / api 状态机 / 轮询门控）
- `tests/performance/`：PERF-01/02 nightly 性能基准；压测脚本（locust + 灌库，spec 见 [docs/specs/stress-test-spec.md](docs/specs/stress-test-spec.md)）
- `infra/verify_phase*.sh`：各阶段门禁脚本（`verify_phase6.sh` 为发布门禁）

## 快速开始（本地一键启动）

前置：Docker、`.env` 中配置 `JWT_SECRET` 与 bootstrap admin 密码（参考 `.env.example`）。

```bash
docker compose up --build          # db / redis / backend / worker / frontend
open http://localhost:3000         # 前端
open http://localhost:8000/docs    # API 文档
```

迁移由 backend 容器自动执行；bootstrap admin 账号随首启 seed。默认 LLM 为
DeepSeek 云端判断，`.env` 中配置 `LLM_API_KEY`（离线演示见下）。

### 本地推理（可选）：llama.cpp + Qwen3.8-27B

不依赖云端 API 的本地判断走 llama.cpp（`--profile local-llm`）：

```bash
docker compose --profile local-llm up --build
```

- 模型：`unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M`（~16GB），**首启自动从
  HuggingFace 下载，请预留磁盘与等待时间**；本地已有 HF 缓存时可把
  `~/.cache/huggingface` bind-mount 进 `llamacpp` 容器复用
- **内存要求**：Docker Desktop VM 需 ≥20GB（Settings → Resources），
  16GB 权重 + KV cache + 其余容器共占一台机器的统一内存
- backend/worker 切到 `LLM_PROVIDER=openai_compatible`、
  `LLM_BASE_URL=http://llamacpp:8080/v1`、`LLM_MODEL=qwen3.8-27b`
  （须与 llama-server `--alias` 一致——判决缓存 key 含 model，两侧必须同值）

### 离线演示（不依赖公网与真实模型）

```bash
# mock provider 三档种子案例（high / low / no_match），秒级出结论
LLM_PROVIDER=mock GRAPH_DATA_MODE=fixture python -m backend.services.seed_cases
```

### 仅跑后端开发环境

```bash
uv sync --extra dev
docker compose up -d db redis
uv run alembic upgrade head
JWT_SECRET=dev-secret uv run uvicorn backend.api.app:create_app --factory --reload
LLM_PROVIDER=mock GRAPH_DATA_MODE=fixture uv run python tests/evaluation/run_e2e_phase5.py
```

## 配置参考

环境变量经 `.env` 或 compose 注入，完整默认值见 `backend/core/config.py`：

| 变量 | 说明 |
|---|---|
| `JWT_SECRET` | **必填**，无弱默认（IF-04） |
| `BOOTSTRAP_ADMIN_EMAIL/PASSWORD` | 首个 admin 账号，空则不 seed |
| `LLM_PROVIDER` | `deepseek`（默认，生产推荐）/ OpenAI 兼容（llama.cpp 等本地推理）/ `mock`（测试与演示） |
| `LLM_MODEL` | 默认 `deepseek-chat`；本地推理为 llama-server `--alias`（如 `qwen3.8-27b`） |
| `LLM_BASE_URL` | 默认 `https://api.deepseek.com`；llama.cpp 为 `http://llamacpp:8080/v1` |
| `GRAPH_DATA_MODE` | `fixture`（内置演示图，离线）/ `live`（Esplora 公网） |
| `ESPLORA_API_URL` | live 数据源，默认 `https://mempool.space/api`（自动切 Blockstream 备用） |
| `ADDRESS_TX_COUNT_LIMIT` | live 模式地址活跃度预检阈值（默认 200）：analyze 建图前查 `/address/:addr/stats` 的 tx_count，超过即 422 `ADDRESS_TOO_ACTIVE`（issue #79） |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | 出站 HTTP 代理（httpx `trust_env` 自动拾取）；`NO_PROXY` 保护容器内网流量。受限网络下 live 模式访问公网 Esplora 必需；`NO_PROXY` 默认 `localhost,127.0.0.1` |
| `DEMO_SEEDS` | 匿名免登录白名单地址 CSV；空则用 fixture 内置 seed |
| `CORS_ORIGINS` | CORS 显式白名单 CSV；默认放行 `localhost:3000`（同源代理形态下仅直连后端时需要），生产注入正式域名 |
| `API_PROXY_URL` | 前端构建 arg：rewrites 转发目的地（compose 内 `http://backend:8000`；注意 rewrites 在 `next build` 时烘焙，须构建期注入） |
| `COOKIE_SECURE` | 生产置 `true`（Cookie 带 Secure + SameSite=None，支持前后端分域） |

## 测试与发布门禁

```bash
bash infra/verify_e2e_local.sh   # 完整 E2E 门禁（issue #78）：一条命令从全新
                                 # checkout 构建、初始化隔离 DB/KB、启动全部服务、
                                 # 跑通分析→判断→子图→PDF/HTML + 独立 worker 队列形态
bash infra/verify_phase6.sh      # 阶段6 门禁：单元+E2E+契约+性能+部署配置
bash infra/run_unit_tests.sh      # 单元 + 契约（CT-01~03）——**用这个，别直接 pytest**
bash infra/run_unit_tests.sh tests/unit/test_provider_config.py -q   # 也可只跑部分
python -m scripts.gen_api_types   # OpenAPI schema 变更后同步前端契约
uv run python tests/performance/perf_phase6.py PERF-01    # 报告容量基准
```

**单测必须指向专用库（必读）**：单测里有 `DELETE FROM judgments` 这类全表清理，
而 `DATABASE_URL` 默认就是开发库（与本地栈共用同一个 Postgres）——直接
`uv run pytest tests/unit` 会清掉开发库里正在查看的分析记录，`/analyze/<id>`
随即 404 且无法恢复。`bash infra/run_unit_tests.sh` 会自动建/刷新
`patterntrace_test` 并指向它（只建 schema，不拷数据，与 CI 的空库路径一致）。
`scripts/db_guard.py` 把这条约定变成机器可校验的：破坏性用例只允许库名为
`patterntrace_test` / `pt_e2e` / `*_test`，指向别的库直接报错并给出操作指引。

**E2E 环境隔离（必读）**：E2E 全部走独立 compose project（`-p pt-e2e`）、
独立 DB（名 `pt_e2e`，seed 脚本拒绝向其他库名写入）、独立 Redis DB index
（`/1`，判决缓存与开发环境隔离）与独立报告卷。`run_e2e_phase4.py` 的
`_clear_judgments` 会**全表 DELETE judgments**——绝不可将 E2E 脚本指向
开发或生产数据库；该约束现由 `scripts/db_guard.py` 在代码里强制
（库名不是 `pt_e2e` / `*_test` 即报错），不再只靠这句提醒。
fixture KB seed（`infra/seed_kb_fixture.py`）与判决缓存隔离保证
KB 变更后 E2E 不会命中旧 verdict 假通过。

CI（`.github/workflows/ci.yml`，Node 24）：PR 触发三个 job —— 后端 lint/test/codegen 守护、
前端 lint + type-check + vitest + build、**完整 E2E**（`verify_e2e_local.sh`：
分析→判断→报告真实走一遍，主流程经前端同源代理 `:13000`，含停 worker 队列形态
证明，失败自动上传 compose 日志与产物并清理本次 project 资源）；
`schedule` nightly 追加 PERF 性能档。
CI 只做验证门禁，不负责发布；交付形态是 docker compose 私有化部署（见下文）。

## 部署

本项目定位为**可私有化部署**：源码 + `docker compose` 一套编排，即可在自有主机
拉起全部服务，不依赖任何第三方公开云平台。

前置：Docker、`.env` 中配置 `JWT_SECRET`、bootstrap admin 与 LLM/数据源
（参考 `.env.example`）；`docker compose` 已透传全部运行时变量。
受限网络部署（容器无法直连公网 Esplora/LLM API）时，在 `.env` 配置
`HTTP_PROXY/HTTPS_PROXY` 指向可达出口，容器出站流量统一经代理（见配置参考）。

```bash
git clone <repo> && cd pattern_trace
cp .env.example .env          # 填写 JWT_SECRET / LLM_API_KEY（deepseek）/ 数据源等
docker compose up --build     # db / redis / backend / worker / frontend
open http://localhost:3000
```

- **迁移自动执行**：backend 容器启动即 `alembic upgrade head`，无需单独步骤。
- **生产口径**：live 数据源 + DeepSeek 云端判断（见
  [docs/production-runbook.md](docs/production-runbook.md)）；如需本地推理可加
  `--profile local-llm`（首启下载 ~16GB 模型，内存 ≥20GB，详见快速开始一节）。
- 生产加固项（`COOKIE_SECURE`、`CORS_ORIGINS` 等）由 `.env` 注入，`docker compose`
  自行按需编排反向代理，不绑定任何平台。

## 安全基线

- 密码哈希 bcrypt cost 12（存量 pbkdf2 哈希兼容验证，平滑过渡）
- JWT access(15min) + refresh rotation（reuse detection，状态存 Redis 原子轮换；
  Redis 不可达降级进程内），refresh **仅经 HttpOnly Cookie 下发、绝不出现在响应 body**
- 写请求强制 `X-Requested-With`（CSRF 服务端强制校验，非依赖前端自觉）
- CORS 显式白名单：`allow_credentials=True` 时禁止通配符
- 水平越权隔离：非本人案件/报告一律 404 并落审计日志
- 报告下载 URL：HMAC-SHA256 签名 + 15 分钟过期
- 生产（`COOKIE_SECURE=true`）：Cookie 附 Secure + SameSite=None，支持分域部署

## 演示

30 秒演示流程（种子案例）见 [docs/demo-script.md](docs/demo-script.md)，
含演示视频录制分镜与口播稿。

## 文档索引

| 文档 | 内容 |
|---|---|
| [docs/user-guide.md](docs/user-guide.md) | 使用说明：从启动到完整业务闭环、API 直调要点与故障排查 |
| [docs/production-runbook.md](docs/production-runbook.md) | 生产运行手册：当前配置、启动/回收、手工复现链路与排障 |
| [docs/specs/](docs/specs/) | 十份模块 spec（grill-me 风格，含 coinjoin / crosschain 检测 spec）+ 评审修订记录 |
| [docs/spec-comparison-report.md](docs/spec-comparison-report.md) | 全局端到端校验结果 + spec↔实现差异比对与修复记录 |
| [docs/project-schedule.md](docs/project-schedule.md) | 六阶段排期与门禁完成标志 |
| [docs/qwen3-local-llm-feasibility.md](docs/qwen3-local-llm-feasibility.md) | 本地 LLM 选型依据 |

## 明确不做（Roadmap · P2）

实时告警、graph2vec、历史数据回灌、多链支持、移动端、WebGL 大图渲染。
