# PatternTrace

比特币地址资金链路溯源与洗钱模式识别平台：输入任意 BTC 地址，构建受控交易子图，
在知识库中检索相似模式（Wasabi / Lazarus / 混币中转等），由 LLM 给出结构化风险
判断（**high / medium / low / no_match** 四档），并支持案件管理与报告导出。

## 核心能力

| 能力 | 说明 |
|---|---|
| 子图构建 | BFS 三队列 + 五类终止条件（unspent / 时间窗 / 深度≤3 / 规模裁剪 / early-stop）；live 模式带重试退避、熔断切备用端点与 Redis 缓存 |
| 知识库 | 正/负样本入库 + pgvector 语义向量 + 结构指纹；负样本独立表隔离，不参与召回 |
| 混合检索 | pgvector 加权召回 → 带属性 WL kernel 结构精排 → Top-K |
| LLM 判断 | DeepSeek 云端主推，Ollama 本地/OpenAI 兼容可切换，mock 可离线演示；JSON 结构化输出；evidence 必须反向存在于子图快照（防幻觉校验，非法引用重试后落 failed） |
| 可视化 | React Flow 分层画布、evidence 点击高亮、混币器红框双编码 |
| 业务闭环 | 案件 CRUD、地址关联、异步 PDF/HTML 报告（HMAC 签名下载）、审计日志 |

## 架构

```
frontend (Next.js + React Flow)      backend (FastAPI)            workers (arq)
        │  REST /api/v1                  │                          │
        ├──────────────────────────────► │  JWT access+refresh      │
        │                                ├──────────────────────────┤
        │                          PostgreSQL (pgvector)   Redis (queue/cache)
                                         DeepSeek API (或本地 Ollama)
```

- `backend/graph_builder/`：子图构建核心（D3 全局 ID 规范 `addr:*` / `tx:*` / `edge:*`）
- `backend/retrieval/`：结构指纹 + 向量混合检索
- `backend/llm_judge/`：provider 抽象与结构化判断
- `backend/services/`：分析编排、报告生成、种子案例
- `workers/worker.py`：arq 入口（analyze 默认进程内执行，扩缩容时切换队列形态）
- `ingest/`：Lazarus 切图、正负样本生成、embedding 计算
- `tests/unit/`：221 条测试用例的自动化收口（含契约测试 CT-01~03）
- `tests/performance/`：PERF-01/02 nightly 性能基准
- `infra/verify_phase*.sh`：各阶段门禁脚本（`verify_phase6.sh` 为发布门禁）

## 快速开始（本地一键启动）

前置：Docker、`.env` 中配置 `JWT_SECRET` 与 bootstrap admin 密码（参考 `.env.example`）。

```bash
docker compose up --build          # db / redis / ollama / backend / worker / frontend
open http://localhost:3000         # 前端
open http://localhost:8000/docs    # API 文档
```

迁移由 backend 容器自动执行；bootstrap admin 账号随首启 seed。离线演示
（不依赖公网与真实模型）：

```bash
# mock provider 三档种子案例（high / low / no_match），秒级出结论
LLM_PROVIDER=mock GRAPH_DATA_MODE=fixture python -m backend.services.seed_cases
```

> 提示：默认走 DeepSeek（`LLM_PROVIDER=deepseek`），无需本地模型。若改用
> `ollama` 本地推理，容器默认无模型，需拉取并让 backend/worker 使用同一模型名
> （判决缓存 key 含 model，两侧必须一致）：
>
> ```bash
> docker exec pattern_trace-ollama-1 ollama pull qwen3:8b
> LLM_PROVIDER=ollama LLM_MODEL=qwen3:8b docker compose up -d backend worker
> ```

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
| `LLM_PROVIDER` | `deepseek`（默认）/ `ollama` / OpenAI 兼容 / `mock`（测试与演示） |
| `LLM_MODEL` | 默认 `deepseek-chat`；本地 ollama 可换 `qwen3:8b` 等 |
| `LLM_BASE_URL` | `deepseek` 为 `https://api.deepseek.com`；`ollama` 为 `http://ollama:11434` |
| `GRAPH_DATA_MODE` | `fixture`（内置演示图，离线）/ `live`（Esplora 公网） |
| `ESPLORA_API_URL` | live 数据源，默认 `https://mempool.space/api`（自动切 Blockstream 备用） |
| `DEMO_SEEDS` | 匿名免登录白名单地址 CSV；空则用 fixture 内置 seed |
| `CORS_ORIGINS` | CORS 显式白名单 CSV；默认放行 `localhost:3000`，生产注入正式域名 |
| `COOKIE_SECURE` | 生产置 `true`（Cookie 带 Secure + SameSite=None，支持前后端分域） |

## 测试与发布门禁

```bash
bash infra/verify_phase6.sh       # 阶段6 门禁：单元+E2E+契约+性能+部署配置
uv run pytest tests/unit          # 单元 + 契约（CT-01~03）
python -m scripts.gen_api_types   # OpenAPI schema 变更后同步前端契约
uv run python tests/performance/perf_phase6.py PERF-01    # 报告容量基准
```

CI（`.github/workflows/ci.yml`）：PR 触发三个 job —— 后端 lint/test/codegen 守护、
前端 type-check、compose E2E 冒烟；`schedule` nightly 追加 PERF 性能档。
CI 只做验证门禁，不负责发布；交付形态是 docker compose 私有化部署（见下文）。

## 部署

本项目定位为**可私有化部署**：源码 + `docker compose` 一套编排，即可在自有主机
拉起全部服务，不依赖任何第三方公开云平台。

前置：Docker、`.env` 中配置 `JWT_SECRET`、bootstrap admin 与 LLM/数据源
（参考 `.env.example`）；`docker compose` 已透传全部运行时变量。

```bash
git clone <repo> && cd pattern_trace
cp .env.example .env          # 填写 JWT_SECRET / 生产 LLM / 数据源等
docker compose up --build     # db / redis / ollama / backend / worker / frontend
open http://localhost:3000
```

- **迁移自动执行**：backend 容器启动即 `alembic upgrade head`，无需单独步骤。
- **生产口径**：live 数据源 + DeepSeek 云端判断（见
  [docs/production-runbook.md](docs/production-runbook.md)）；如需本地推理可切 `ollama`。
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
| [docs/specs/](docs/specs/) | 八份模块 spec（grill-me 风格）+ 评审修订记录 |
| [docs/spec-comparison-report.md](docs/spec-comparison-report.md) | 全局端到端校验结果 + spec↔实现差异比对与修复记录 |
| [docs/project-schedule.md](docs/project-schedule.md) | 六阶段排期与门禁完成标志 |
| [docs/qwen3-local-llm-feasibility.md](docs/qwen3-local-llm-feasibility.md) | 本地 LLM 选型依据 |

## 明确不做（Roadmap · P2）

实时告警、graph2vec、历史数据回灌、多链支持、移动端、WebGL 大图渲染。
