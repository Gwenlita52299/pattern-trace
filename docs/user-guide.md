# PatternTrace 使用说明

面向第一次接触本项目的操作者：从零启动，到走通「输入地址 → 出风险判断 → 导出报告」的完整流程。
架构与设计背景见 [README](../README.md)，30 秒演示分镜见 [demo-script.md](demo-script.md)。

---

## 一、启动项目

### 前置条件

- Docker Desktop（含 compose v2），内存建议 ≥ 8GB（跑本地 LLM 时更高）
- 无需 Python / Node 环境——一切在容器内运行

### 第 1 步：准备 `.env`

```bash
cp .env.example .env
```

编辑 `.env`，两个必填项：

```bash
JWT_SECRET=改成一串长随机字符          # 不允许弱默认，缺失时启动即失败
BOOTSTRAP_ADMIN_EMAIL=admin@patterntrace.local
BOOTSTRAP_ADMIN_PASSWORD=你的管理员密码   # 留空则系统里没有任何可登录账号
```

> bootstrap admin 只在**首次启动且 accounts 表为空**时创建。之后改这两个变量不会
> 重置密码；忘记密码可清库重来或用 `POST /api/v1/users` 再建账号。

### 第 2 步：启动六个容器

```bash
docker compose up --build -d
```

首次会拉取/构建镜像并执行数据库迁移，几分钟不等。服务清单：

| 服务 | 端口 | 作用 |
|---|---|---|
| db | 5432 | PostgreSQL 16 + pgvector（判决 / 案件 / 模式知识库） |
| redis | 6379 | 任务队列 · 判决缓存 · refresh rotation 状态 |
| llamacpp | 8080 | 本地 LLM 推理（可选 profile `local-llm`，默认不拉起，见 §四） |
| backend | 8000 | FastAPI REST API |
| worker | — | arq 工作进程（与 backend 同管线，扩缩容备用形态） |
| frontend | 3000 | Next.js Web 界面 |

### 第 3 步：健康检查

```bash
docker compose ps                    # 六个服务均应 healthy/running
curl -s localhost:8000/healthz       # 存活探针 → {"status":"ok"}
curl -s localhost:8000/readyz        # 就绪探针（含 db/redis 依赖）
```

然后打开：

- **http://localhost:3000** —— 操作界面
- **http://localhost:8000/docs** —— Swagger 交互文档

### 第 4 步：登录

右上角进入 `/login`，使用 `.env` 里的 bootstrap admin 邮箱密码。
不登录也能体验，但只能分析白名单内的演示地址；登录后可分析任意地址并使用案件管理。

---

## 二、第一个分析（离线，约 30 秒）

默认配置 `GRAPH_DATA_MODE=fixture`（内置演示子图，不出公网）。若 `LLM_PROVIDER`
还是默认的 `deepseek` 而 `.env` 里没有 `LLM_API_KEY`，判决会失败——离线体验请先把
`.env` 改成
`LLM_PROVIDER=mock` 并 `docker compose up -d backend worker` 重建两容器。

**方式 A：网页操作**

1. 打开首页，点击「演示地址」任意一个 chip（免登录可用）
2. 自动跳转 `/analyze/{id}`，页面轮询直至出结论
3. 结果区查看四档判断之一：**high / medium / low / no_match**，
   以及检索命中的相似模式、LLM 引用的证据（点证据可在图上高亮对应节点/边）

**方式 B：一键种子三档案例**

```bash
# 在容器内执行（脚本自带 mock/fixture 强制，幂等可重复跑）
docker compose exec backend python -m backend.services.seed_cases
```

产出三个案件：「Lazarus 高风险演示」(high)、「低风险普通钱包」(low)、
「无匹配模式地址」(no_match)，到案件列表页直接查看。

---

## 三、完整业务闭环：案件管理

登录后的推荐工作流（对应真实办案习惯）：

1. **建案**：`/cases` → 新建案件（标题 + 描述）
2. **关联地址**：进入案件详情，把待查 BTC 地址挂到案件下（可多个）
3. **发起分析**：案件详情页点「发起分析」，跳转首页并自动填入地址；
   分析完成后台账自动归属该链路
4. **导出报告**：案件详情选 PDF 或 HTML → 后端异步渲染（202）→
   完成后出现签名下载链接。链接带 HMAC 签名、**15 分钟过期**，
   过期后在页面重新生成即可

所有写操作（建案、关联、发起、导出）都会落入审计日志，admin 可在
`GET /api/v1/audit-logs` 查询。非本人资源一律 404（水平越权隔离）。

---

## 四、切换到真实推理与公网数据

### 本地 LLM（llama.cpp + Qwen3.8-27B）

本地推理走 llama.cpp 可选 profile（issue #63）。**首启会从 HuggingFace 下载
~16GB 模型**（unsloth UD-Q4_K_M 量化），请预留磁盘与时间；且 Docker Desktop
VM 内存需 ≥20GB：

```bash
docker compose --profile local-llm up -d    # 拉起 llamacpp（首启下载模型）
# .env 中设置：
#   LLM_PROVIDER=openai_compatible
#   LLM_MODEL=qwen3.8-27b                    # 必须与 llama-server --alias 一致
#   LLM_BASE_URL=http://llamacpp:8080/v1
docker compose up -d backend worker
```

**`backend` 与 `worker` 的 `LLM_MODEL` 必须一致**：判决缓存的 key 含 model 名，
不一致会导致同图重复推理两次。另外模型能力有底线——过小的模型会把候选模式名当
evidence ID 引用，被防幻觉校验拒绝后落 `failed (LLM_VALIDATION_FAILED)` 终态，
这是设计行为不是 bug；请使用 8b 及以上档位。

### 公网链上数据

`.env` 设置 `GRAPH_DATA_MODE=live`（可选 `ESPLORA_API_URL`，默认 mempool.space，
故障时自动切 Blockstream 备用端点，带重试退避与 Redis 缓存）。live 模式受公网
延迟影响，深度 3 的分析通常需要数十秒。

---

## 五、API 直调要点

用 curl / Postman 直接调 API 时，有三条网页端帮你处理了、手调必须自己带的规则：

1. **认证**：`POST /api/v1/auth/login` 成功后响应体只有 access_token（15 分钟）；
   refresh token 只经 HttpOnly Cookie 下发。后续请求带
   `Authorization: Bearer <access_token>`
2. **CSRF**：**所有写请求**必须带头 `X-Requested-With: XMLHttpRequest`
   （值须逐字一致，包括 login 本身），否则 403
3. **限流**：匿名请求按 IP 限流；登录用户另有配额，超限返回 429

最小可用序列：

```bash
BASE=http://localhost:8000/api/v1
H='-H Content-Type:application/json -H X-Requested-With:XMLHttpRequest'

TOKEN=$(curl -s $H -d '{"email":"...","password":"..."}' $BASE/auth/login | jq -r .access_token)
ADDR=$(curl -s $BASE/demo/addresses | jq -r .addresses[0])

JID=$(curl -s $H -H "Authorization: Bearer $TOKEN" \
      -d "{\"address\":\"$ADDR\",\"hops\":3}" $BASE/addresses/analyze | jq -r .judgment_id)
curl -s -H "Authorization: Bearer $TOKEN" $BASE/judgments/$JID    # 轮询至 completed/failed
```

---

## 六、常用配置速查

完整默认值见 `backend/core/config.py` 与 [.env.example](../.env.example)：

| 变量 | 常用取值 | 说明 |
|---|---|---|
| `JWT_SECRET` | 长随机串 | **必填** |
| `BOOTSTRAP_ADMIN_*` | 邮箱 + 密码 | 首启建 admin；空则无账号 |
| `LLM_PROVIDER` | `deepseek`（默认，需 key）/ `openai_compatible`（llama.cpp）/ `mock` | mock 全离线秒出 |
| `LLM_MODEL` | 默认 `deepseek-chat`；llama.cpp 为 `qwen3.8-27b` | 两侧服务必须一致 |
| `GRAPH_DATA_MODE` | `fixture` / `live` | fixture 内置演示图 |
| `DEMO_SEEDS` | 地址 CSV | 匿名白名单；空则用内置 |
| `CORS_ORIGINS` | `https://your.app` | 生产填正式前端域名 CSV |
| `COOKIE_SECURE` | `true` | 前后端分域部署必开（Secure + SameSite=None） |

---

## 七、常见问题排查

| 现象 | 原因与处理 |
|---|---|
| 启动即退出，日志报 JWT_SECRET | `.env` 未设必填项；本项目拒绝弱默认 |
| 登录一直 401 | `BOOTSTRAP_ADMIN_PASSWORD` 首启时为空 → 没建过账号；清库重来或 API 建号 |
| 手调写接口 403 | 缺 `X-Requested-With: XMLHttpRequest` 头（值要逐字一致） |
| 分析终态 `failed`，error_code 含 fpdf2 / httpx | 本地镜像是老构建；`docker compose build` 后再 `up -d`（依赖已入 uv.lock） |
| 判决总落 `LLM_VALIDATION_FAILED` | 模型太小引用了不存在的 evidence；换 8b 以上，或先用 mock 验证管线 |
| 改了 `LLM_MODEL` 结论没变化 | 正常——缓存 key 含 model，新模型会重新推理；若想强制重跑可删 Redis 中 `gb-v1:*` 键 |
| `GET /demo/addresses` 500 | 老镜像缺 fixture 数据；`--build` 重建 backend 镜像 |
| 报告下载 403 | 签名 URL 已过 15 分钟有效期，回案件页重新生成 |
| 前端能开但请求全挂（跨域部署时） | 生产需 `CORS_ORIGINS=<前端域名>` 且 `COOKIE_SECURE=true`，改完重建 backend |
| 端口被占 | 改 `docker-compose.yml` 对应服务的 `ports:` 左侧宿主端口 |

---

## 八、停止与清理

```bash
docker compose down          # 停止并移除容器；数据卷保留（pgdata / llamacpp_models）
docker compose down -v       # 连数据一起删除——判决、案件、已拉取的模型权重全部丢失，不可恢复
```

只改了代码想让镜像生效：`docker compose up --build -d`（uv/pnpm 依赖层有缓存，
仅依赖变更时会重装）。
