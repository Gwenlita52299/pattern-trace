# Spec · stress-test — HTTP 层全链路压测

> 模块路径：`tests/performance/locustfile.py` · `tests/performance/seed_volume.py`
> 定位：真实 HTTP 栈（网络 + uvicorn + Docker bridge）下的并发压测，与
> `tests/performance/perf_phase6.py`（TestClient 进程内基准 PERF-01/02）互补，二者不可互相替代
> 执行档位：**手动触发**（不进 CI / nightly——压测结果机器敏感、噪声大，作为门禁会产生假失败）

## 1. 目标与非目标（分层边界）

分层边界是本 spec 的核心决策：**不同轮次测的是不同层，结论不得跨层混用。**

| 轮次 | 配置 | 回答的问题 | 结论适用范围 |
|---|---|---|---|
| 主轮 | `GRAPH_DATA_MODE=fixture` + `LLM_PROVIDER=mock` | API + PostgreSQL + Redis 的并发承载与劣化拐点 | 私有化部署的 API 层容量规划 |
| worker 专项 | 同上，mock provider 注入受控延迟 | arq 队列堆积 / 消费速率 / `reclaim_zombies` 是否成为瓶颈 | worker 扩缩容决策 |
| 参考轮 | 真实 LLM（deepseek/ollama） | 端到端吞吐 | **仅作参考值**——瓶颈大概率在外部服务的 rate limit，属容量规划而非本服务代码问题 |

**非目标**：前端渲染性能、soak 长稳测试（首版不做，RSS / PG 连接数列为观测项而非达标项）、真实公网数据源吞吐。

## 2. 前提条件

### 2.1 环境与参考硬件

所有指标绑定硬件口径，**不声明硬件的阈值没有意义**：

| 档位 | 规格 | 用途 |
|---|---|---|
| dev 档 | 压测执行机与被测服务同机（本机 compose） | 日常回归基线 |
| min-prod 档 | 4C8G，backend/worker/db/redis 分容器 | 发布前基准 |

启动：

```bash
GRAPH_DATA_MODE=fixture LLM_PROVIDER=mock docker compose up --build
curl -f http://localhost:8000/healthz   # 就绪检查
```

### 2.2 数据规模（不可空库压测）

空库压出的数字毫无参考价值——subgraph 查询性能强依赖数据量。口径与 PERF-02 对齐：

- `judgments` ≥ 10 000 行（status=completed，`subgraph_snapshot` 为真实体积 JSONB，触发 TOAST 压缩）
- `cases` ≥ 100（每案关联若干地址，供 `GET /cases/{id}` 的 N+1 关联查询压测）
- 灌数方式：`uv run python tests/performance/seed_volume.py`（幂等，可重复执行，只补差额）

### 2.3 已知行为约束（压测设计必须绕开或反向利用）

- **analyze 匿名限流**：不带 token 或超配额 → 429/403 是预期行为。STRESS-01/02 必须带 Bearer token；限流行为本身由 STRESS-03 反向验证
- **login 是 CPU 密集路径**（bcrypt cost 12）：单机 TPS 天然低（数十次/秒量级）。压测目的**不是冲吞吐**，而是验证登录风暴不会拖垮同进程的其他接口。常规流量压测不含 login task（仅 `on_start` 一次）
- **登录失败限流**（SEC-05）：仅失败计数，成功登录不受影响——压测账号密码必须正确，否则 5 次后触发 429 污染结果

### 2.4 依赖

- `uv sync --extra dev`（locust 在 dev extras）
- 被测服务必须以 compose 全栈运行（含 worker——STRESS-02 的 analyze 依赖 arq 消费）

## 3. 指标口径（基线优先）

### 3.1 基线对照制

绝对阈值只有一条有明确业务依据；其余指标走基线对照：

- **首轮**：产出基线，`locust --csv` 输出归档至 `output/stress/<YYYYMMDD>-<tag>/`，作为后续回归的对照物
- **回归轮**：与同硬件档位基线对比，**p95 劣化 > 30%** 需调查（可能来自代码变更或环境噪声，先复跑一次排除噪声）
- **唯一绝对阈值**：judgments 轮询 `GET /api/v1/judgments/{id}` **p95 ≤ 300ms**——前端 analyze 后以 ~200ms 间隔轮询，该接口延迟直接决定用户体感。首次基线校准时确认该阈值在网络栈下是否合理，不合理则修订本 spec 而非硬凑

### 3.2 错误率按状态码语义分类

- **计入错误率**：5xx、连接超时、读取超时
- **不计入错误率**（预期拒绝）：429（限流/配额）、403（demo 地址限制）——STRESS-03 中它们是预期产物
- 判定线：主轮各 VU 档位错误率 < 1%

### 3.3 服务端观测项（人工记录，非达标项）

压测工具只给客户端视角，瓶颈必须在服务端定位。每档 VU 稳态期记录：

| 观测点 | 命令 | 关注 |
|---|---|---|
| 容器资源 | `docker stats --no-stream` | backend/worker/db/redis CPU 与 RSS |
| PG 连接 | `psql -c "SELECT count(*), wait_event FROM pg_stat_activity GROUP BY 2"` | 连接数逼近 pool 上限、等待事件 |
| Redis | `redis-cli info stats` | ops/sec、rejected connections |
| arq 队列 | `redis-cli llen q_graph`（STRESS-02） | 堆积是否持续增长不回落 |

## 4. 场景用例

沿用 `docs/test-cases/performance-test-cases.md` 的用例格式。

---

### STRESS-01 纯读负载基线

- **优先级**: P1

**前置条件**
- §2.1 环境就绪 + §2.2 数据已灌入
- 压测账号存在（locustfile `on_start` 幂等 seed）

**操作步骤**
1. `STRESS_PROFILE=read uv run locust -f tests/performance/locustfile.py --headless --csv baseline_read -u <VU> -r <VU/10s> -t 3m --host http://localhost:8000`
2. VU 阶梯：1 → 10 → 50 → 100 → 200，每档独立进程运行，稳态 2–3 分钟
3. 每档记录 §3.3 观测项
4. 找拐点：p95 陡增或错误率抬头的位置即当前硬件档位的承载上限

**任务权重**（模拟真实读流量）
| 任务 | 权重 | 说明 |
|---|---|---|
| `GET /judgments/{id}` | 60% | 轮询热路径，judgment id 取自压测案件的 `latest_judgment` 池 |
| `GET /addresses/{addr}/subgraph` | 25% | 大 JSONB 反序列化路径 |
| `GET /cases` | 10% | 分页列表 |
| `GET /patterns` | 5% | 知识库列表 |

**预期结果**
- 各档错误率（5xx+超时）< 1%
- 轮询 p95 ≤ 300ms（唯一绝对阈值）
- 首轮产出 CSV 基线归档

---

### STRESS-02 读写混合

- **优先级**: P1

**前置条件**
- 同 STRESS-01；**worker 容器运行中**（analyze 走 arq）

**操作步骤**
1. 同 STRESS-01 命令，改用 `STRESS_PROFILE=mixed`（叠加 analyze 5%）
2. analyze 任务：`POST /addresses/analyze`（种子地址 + Bearer）→ 202 → `GET /judgments/{id}` 轮询至终态
3. 观测 `q_graph` 队列长度曲线

**预期结果**
- **幂等复用正确**：并发撞同一 (address, hops, time_window) 只产生一个进行中任务，响应为 202（新建）或 200（复用），无 5xx
- `reclaim_zombies`（每次 analyze 触发）不成为 p95 主导项——若 analyze 202 响应 p95 随 VU 线性恶化且 PG 等待事件集中在该查询，即命中瓶颈
- `q_graph` 堆积在压测停止后回落至 0（fixture+mock 下任务秒级完成）

---

### STRESS-03 限流边界

- **优先级**: P2

**前置条件**
- 同 STRESS-01；**不携带 Bearer token** 的独立场景（`STRESS_PROFILE=anon`，locustfile 内匿名任务组）

**操作步骤**
1. 匿名 analyze 打非白名单地址 → 预期 403
2. 匿名 analyze 打白名单地址直至超配额 → 预期 429 + `Retry-After`
3. 高频失败登录 → 预期 429（SEC-05 爆破防护在压力下生效）

**预期结果**
- 三类预期拒绝状态码与语义正确，**无 5xx 泄漏**（限流实现本身在高并发下不崩）
- 匿名限流不误伤已登录用户（带 token 的 STRESS-01 类同机运行无 429）

---

### STRESS-04 Redis 降级 smoke

- **优先级**: P3（冒烟验证，不设吞吐指标）

**前置条件**
- 主轮环境；Redis 单实例（compose 形态）

**操作步骤**
1. `docker compose stop redis`
2. 读接口仍应 200（refresh 状态存 Redis 不可达的降级路径，BE 侧有进程内兜底）
3. `POST /addresses/analyze` → `dispatch_analysis` 降级为进程内执行（issue #22），任务应完成
4. `docker compose start redis` → 验证自动恢复

**预期结果**
- Redis 停机期间 API 不 5xx、analyze 任务最终 completed
- 已知边界：**worker 依赖 Redis 必然停摆**，降级只存在于 backend 进程内——单实例 compose 形态下的已知限制，不作为缺陷记录
- 本用例只回答「不崩溃 + 任务能完成」，不产生性能指标

## 5. 交付物

| 文件 | 职责 |
|---|---|
| `tests/performance/seed_volume.py` | 幂等灌数：万级 judgments（合成地址 + 真实体积快照）+ 百级 cases |
| `tests/performance/locustfile.py` | STRESS-01~03 场景（tag 区分 `read`/`write`/`anon`），`on_start` 幂等 seed 压测账号，`--csv` 输出 |

## 6. 验收标准

- [ ] `seed_volume.py` 执行后 judgments ≥ 10 000 行、cases ≥ 100；重复执行不产生重复数据
- [ ] `locust --headless -u 5 -r 1 -t 30s` 冒烟通过，CSV 产出且归档路径符合 §3.1
- [ ] STRESS-01 在 dev 档产出首份基线，轮询 p95 绝对阈值经首轮校准（达标，或修订阈值并记录依据）
- [ ] STRESS-02 幂等复用断言通过（并发无 5xx、无重复任务）
- [ ] STRESS-03 预期拒绝不计入错误率的口径已在 locustfile 中实现
- [ ] STRESS-04 冒烟通过（API 不 5xx + 降级任务完成）
