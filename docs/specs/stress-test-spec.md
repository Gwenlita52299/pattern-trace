# Spec · stress-test — HTTP 层全链路压测

> 模块路径：`tests/performance/run_stress.py`（单一入口）· `tests/performance/locustfile.py` ·
> `tests/performance/seed_volume.py` · `tests/performance/pool.py`
> 定位：真实 HTTP 栈（网络 + uvicorn + Docker bridge）下的并发压测，与
> `tests/performance/perf_phase6.py`（TestClient 进程内基准 PERF-01/02）互补，二者不可互相替代
> 执行档位：**手动触发**（不进 CI / nightly——压测结果机器敏感、噪声大，作为门禁会产生假失败）
> 修订记录见 §7。当前落地状态：P0 已实现，P1/P2 为待实现项（文中标注）。

## 1. 目标与非目标（分层边界）

### 1.0 容量目标与 SLO（待生产数据校准的假设）

没有目标的压测只能产出数字，不能产出结论。目标必须从流量模型推导，而不是拍出来；
下列推导基于**私有化部署的团队规模假设**，接入真实流量统计后必须重算（并在归档里替换）：

| 输入 | 假设值 | 来源/理由 |
|---|---|---|
| 同时在线调查员 | 20 人 | 私有化单团队部署 |
| 单次初筛的分析时长 | 10–60s | 子图 + 检索 + LLM 全链路（product-design p95 ≤ 10s 是热路径目标） |
| 前端轮询间隔 | ~200ms | 既有事实（`GET /judgments/{id}` 轮询热路径） |
| → 单次分析产生的轮询 | ~5 RPS | 1 / 0.2s |
| → 峰值轮询负载 | ~100 RPS | 20 人 × 5 RPS（最坏情形：全员同时初筛） |
| → 叠加其他读（subgraph/cases/patterns ~25%） | **~125 RPS 峰值** | 读路径为主 |
| 按 **2× 峰值**验证 | **250 RPS** | AWS Well-Architected 通行做法：按预期峰值 2 倍验证 |
| analyze 写入负载 | ~0.07 RPS | 20 人 × 每人 5 分钟一次；RPS 可忽略但 worker 资源占用重 |

**据此固化的目标（判定口径见 §3.3）**：

- 在 **250 RPS 混合读负载**（轮询占 60%）下，`GET /judgments/{id}` 热档 **p95 ≤ 300ms**、**p99 ≤ 1s**
- 各档**错误率 < 1%**（§3.2 口径）
- 停止压测后 **60s 内队列深度回落至 0**
- 阶梯中若某档不达标，该档即为**当前容量上限**；拐点判定见 §3.5

> 校准说明：以上是「必须撑住的负载」而非「猜测的容量」。若阶梯显示 250 RPS 远未触拐点，
> 则记录余量倍数；若提前触拐点，则把该点写为容量上限并给出扩容结论——两种结果都是有价值的结论。
> 校准依据必须写进归档 `summary.json`（`capacity_target` 字段）。

分层边界是本 spec 的核心决策：**不同轮次测的是不同层，结论不得跨层混用。**

| 轮次 | profile | 配置 | 回答的问题 | 结论适用范围 | 状态 |
|---|---|---|---|---|---|
| 主轮 | `read` / `mixed` / `anon` | `GRAPH_DATA_MODE=fixture` + `LLM_PROVIDER=mock` | API + PostgreSQL + Redis 的并发承载与劣化拐点 | 私有化部署的 API 层容量规划 | P0 |
| W1 分析队列专线 | `queue-analysis` | 同主轮 + `LLM_MOCK_DELAY_MS` 注入受控延迟 | `q_analysis` 堆积 / 消费速率 / `reclaim_zombies` 在受控延迟下的行为 | analysis worker 扩缩容决策 | P1 |
| W2 报告队列专线 | `queue-report` | 同主轮，`ARQ_REPORT_MAX_JOBS=1` | `q_report` 排队时间与堆积回落；报告渲染对 API 的挤占 | report worker 并发度决策 | P1 |
| W3 索引队列专线 | `queue-index` | stub 与真实 provider 两档 | `q_index` 并发重算、与分析 worker 的资源争用、embedding 429 语义 | index worker 并发度与限额决策 | P1 |
| 参考轮 | — | 真实 LLM（deepseek/ollama） | 端到端吞吐 | **仅作参考值**——瓶颈大概率在外部服务的 rate limit，属容量规划而非本服务代码问题 | P1 |

**非目标**：前端渲染性能、soak 长稳测试（首版不做，RSS / PG 连接数 / `analysis_spans` 增长列为观测项而非达标项）、真实公网数据源吞吐。

**已知容量风险（观测但不达标）**：模式索引重算单次实测 73–180s（首次 graphormer 模型加载占绝大部分），
`ARQ_INDEX_MAX_JOBS=2`；它与分析 worker 争 CPU 与 embedding 配额，是本服务当前最大的容量未知数。

## 2. 前提条件

### 2.1 环境与参考硬件

所有指标绑定硬件口径，**不声明硬件的阈值没有意义**：

| 档位 | 规格 | 用途 |
|---|---|---|
| dev 档 | 压测执行机与被测服务同机（本机 compose） | 日常回归基线 |
| min-prod 档 | 4C8G，backend/worker/db/redis 分容器 | 发布前基准 |

启动与就绪检查：

```bash
GRAPH_DATA_MODE=fixture LLM_PROVIDER=mock docker compose up --build
curl -f http://localhost:8000/healthz
```

**环境守卫（强制）**：被测服务必须在 `fixture` + `mock` 下运行，否则观测到的既有真实数据源 RTT
也有真实 LLM 的 rate limit 与费用，数字与结论都不可用。`run_stress.py` 在跑任何档位前执行守卫，
不通过即拒绝运行。

- dev 档：守卫读取 `docker compose exec backend env` 校验 `GRAPH_DATA_MODE` / `LLM_PROVIDER`
- 远程档：无 compose 上下文，必须显式传 `--allow-non-fixture` 并在归档里记录该事实；
  默认不带此参数即拒绝运行，避免误连生产形态

### 2.2 数据规模（不可空库压测）

空库压出的数字毫无参考价值——subgraph 查询性能强依赖数据量。口径与 PERF-02 对齐：

- `judgments` ≥ 10 000 行（status=completed，`subgraph_snapshot` 为真实体积 JSONB，触发 TOAST 压缩）
- `cases` ≥ 100（每案关联若干地址，供 `GET /cases/{id}` 的 N+1 关联查询压测）
- 灌数方式：`uv run python tests/performance/seed_volume.py`（幂等，可重复执行，只补差额）
- 清理：`uv run python tests/performance/seed_volume.py --cleanup`（删除 STRESS 命名空间数据与案件；
  压测结束后必须执行，否则会污染 `GET /cases`、本地演示与其他测试）

### 2.3 轮询 id 池：分层抽样（不可用热集代替全表）

`GET /api/v1/judgments/{id}` 是唯一有绝对阈值的读路径，其延迟强依赖工作集是否在 PG 缓存内。
只从案件关联地址取 id（约数百个）会让读数全部落在热集，系统性高估容量。因此：

- id 池由 `run_stress.py pool` 直接从 `judgments` 表抽样生成，**分两档**：
  - **热档**：`created_at` 最近 1 小时内已完成判定（模拟刚分析完的轮询）
  - **冷档**：全表随机（模拟历史案件回看）
- 两档分别记录 p95，不合并统计；基线归档里必须同时存在两档数字
- 池以 JSON 文件传递（`output/stress/pools/<tag>-pool.json`），locust 侧经 `STRESS_ID_POOL_FILE` 读取；
  未提供池文件时退化为旧的 API 派生池，并在输出中标注 `pool=derived(hot-only)`——只允许用于冒烟，不得作为基线

### 2.4 已知行为约束与故障注入通道（压测设计必须绕开或反向利用）

- **analyze 匿名限流**：不带 token 或超配额 → 429/403 是预期行为。STRESS-01/02 必须带 Bearer token；限流行为本身由 STRESS-03 反向验证
- **login 是 CPU 密集路径**（bcrypt cost 12）：单机 TPS 天然低（数十次/秒量级）。压测目的**不是冲吞吐**，而是验证登录风暴不会拖垮同进程的其他接口（smoke 实测单次 ~220ms）。常规流量压测不含 login task（仅 `on_start` 一次）
- **登录失败限流**（SEC-05）：仅失败计数，成功登录不受影响——压测账号密码必须正确，否则 5 次后触发 429 污染结果
- **LLM 受控延迟**（W1 依赖）：`LLM_MOCK_DELAY_MS`（仅 `LLM_PROVIDER=mock` 生效）让 mock provider 每次调用前 sleep 指定毫秒，用于把队列堆积拉到可观测区间
- **mock 场景注入**（STRESS-07 依赖）：`POST /addresses/analyze` 的 `mock_scenario` 字段（仅 mock 生效）可强制 `timeout` / `rate_limited` / `invalid_json_then_valid` 等失败路径，用于验证重试分类与死信归档

### 2.5 依赖

- `uv sync --extra dev`（locust 在 dev extras）
- 被测服务必须以 compose 全栈运行（含 worker——STRESS-02 的 analyze 依赖 arq 消费；
  W2/W3 依赖 worker-report / worker-index）

## 3. 指标口径（基线优先）

### 3.1 基线对照制与归档

绝对阈值只有极少数几条有明确业务依据（§3.3）；其余指标走基线对照：

- **首轮**：产出基线，`run_stress.py baseline` 将 locust CSV、观测项与 `summary.json` 一并归档至
  `output/stress/<YYYYMMDD>-<tag>/`（`<tag>` 如 `dev-L1-read`），作为后续回归的对照物
- **回归轮**：`run_stress.py compare --baseline <dir>` 与同硬件档位基线对比，**p95 劣化 > 30%** 输出劣化清单并非 0 退出
  （劣化可能来自代码变更或环境噪声，先复跑一次排除噪声）
- 归档目录必须自描述：`summary.json` 记录硬件档位、profile、VU 阶梯、数据规模（judgments/cases 行数）、
  provider 配置与守卫结果——**没有这些元数据的 CSV 不可作为基线**
- `run_stress.py report` 把归档渲染成**自包含** HTML（内联样式、无 JS、无外部资源）：达标判定、
  端点明细（按归一化路径聚合）、稳态期趋势、归档元数据与服务端观测项；`output/stress/index.html`
  汇总全部归档。报告是给人看的视图，**判定仍以 CSV 与 summary.json 为准**

### 3.2 成功与失败的判定（不可只看 5xx）

- **计入失败**：5xx、连接超时、读取超时、**非预期 4xx**（如读接口收到 401/404/422）、
  **2xx 但响应体缺少关键字段**（如 judgments 缺 `status`、subgraph 缺 `nodes`）
- **不计入失败**（预期拒绝，仅 STRESS-03/07 场景）：429（限流/配额）、403（demo 地址限制）、
  mock 注入的失败路径（预期 `failed` 终态）
- 判定线：主轮各 VU 档位错误率 < 1%

### 3.3 绝对阈值（有业务依据的白名单）

| 阈值 | 依据 | 状态 |
|---|---|---|
| `GET /judgments/{id}` p95 ≤ 300ms（热档；冷档单独记录） | 前端 analyze 后以 ~200ms 间隔轮询，该接口延迟直接决定用户体感 | 保留；首轮校准确认阈值合理性，不合理则修订本 spec 而非硬凑 |
| 各档错误率 < 1% | §3.2 判定口径 | 保留 |
| **停止压测后 60s 内队列深度回落至 0**（`q_analysis` / `q_report` / `q_index`） | 堆积不回落说明消费能力不足或有任务卡死；fixture+mock 下任务本应秒级~分钟级完成 | 新增（W2/W3 中报告/索引任务耗时更长，回落窗口按档位标注） |

其余一律基线对照，**不新增硬阈值**（避免为凑数而设阈值）。

### 3.4 服务端观测项（人工/脚本记录，非达标项）

压测工具只给客户端视角，瓶颈必须在服务端定位。每档 VU 稳态期记录：

| 观测点 | 命令 | 关注 |
|---|---|---|
| 容器资源 | `docker stats --no-stream` | backend/worker/worker-report/worker-index/db/redis CPU 与 RSS |
| PG 连接 | `psql -c "SELECT count(*), wait_event FROM pg_stat_activity GROUP BY 2"` | 连接数逼近 pool 上限、等待事件 |
| Redis | `redis-cli info stats` | ops/sec、rejected connections |
| 队列深度 | `redis-cli llen q_analysis` / `q_report` / `q_index` | 堆积是否持续增长不回落（**旧命令 `llen q_graph` 已废弃**） |
| 队列状态端点 | `GET /api/v1/admin/queues` | pending/retry/最老等待与 `llen` 口径是否一致 |
| 写入放大 | `SELECT count(*), count(DISTINCT trace_id) FROM analysis_spans` | 每次分析固定写多行 span（实测 7 行/trace）；表增长与 trace 查询成本 |

### 3.5 拐点判定（阶梯档位如何判「打满」）

「打满」不是把 VU 调到很大，而是找到**饱和点**：任一条件成立即该档触拐点。

| 条件 | 阈值 | 含义 |
|---|---|---|
| 尾延迟非线性抬升 | 校正 p95 ≥ 首档校正 p95 的 **3×** | 排队开始主导（利用率约 70–80%） |
| 错误率抬头 | ≥ 1%（§3.2 口径） | 服务端已无法稳定处理 |
| 报价跟不上 | 实测 RPS < 目标 RPS 的 **90%** | 生成器或被测系统任一方已饱和（必须同时看生成器余量，§3.6） |
| 队列堆积不回落 | 停止后 60s 仍未回落至 0 | 消费能力不足 |

**拐点必须能被服务端资源解释（否则不是容量结论）**：延迟抬升若伴随**服务端 CPU 仍很低**
（如 < 10%），瓶颈更可能在压测环境——同机竞争、容器网络栈、或压测机自身排队。此时结论是
**`environment_limited`（拐点不可信）**，而不是「容量上限 X RPS」。判定依据必须写进归档：
该档服务端 CPU、各端点延迟是否**同步抬升**（同步抬升 = 与端点自身逻辑无关 = 环境因素）。

- 判定用**校正延迟**（§3.6），不用工具口径延迟——闭环/浅队列下后者会系统性低估
- 生成器 CPU 超限的档位**排除**在拐点之外（那是压测机饱和，不是服务端饱和）
- 触拐点后默认停止升压（`--continue-past-knee` 可继续，用于观察崩溃形态）
- 结论四态：`meets`（达到目标）/ `below`（容量上限低于目标）/ `environment_limited`
  （抬升不可信，未验证）/ `indeterminate`（有效档位没打到目标，通常压测机先饱和）

### 3.6 延迟口径：校正延迟（coordinated omission）

固定到达率生成器记录两种延迟：

- **工具口径** `latency_ms`：从实际发出到响应——服务端卡顿时已计划的请求被延后发出，
  这段等待**不计入**，尾延迟被系统性低估
- **校正口径** `corrected_ms`：从**计划发出时刻**到响应，含排队等待——生产级判定用这一口径

两者差值即 coordinated omission 的幅度；报告中并列展示，差值大说明服务端在稳态期内已经排队。
生成器侧同时记录自身 CPU 与系统负载：**若生成器成为瓶颈，该档数据无效**，必须换压测机重跑。

生成器实现：`tests/performance/loadgen.py`（固定到达率、多进程可并行、无外部二进制依赖）。
单进程 asyncio 在 dev 档约 **250 RPS 触顶**（CPU ~70%），因此高于该量级必须
`--generators N` 多进程，或把生成器放到独立机器（首选，见 §2.1 环境隔离）。


## 4. 场景用例

沿用 `docs/test-cases/performance-test-cases.md` 的用例格式。

---

### STRESS-01 纯读负载基线

- **优先级**: P1 · **状态**: P0 已实现

**前置条件**
- §2.1 环境就绪（守卫通过）+ §2.2 数据已灌入 + §2.3 池文件已生成
- 压测账号存在（locustfile `on_start` 幂等 seed）

**操作步骤**
1. `uv run python tests/performance/run_stress.py baseline --profile read --vu 1,10,50,100,200 --tag dev-read`
   （P1 起由脚本跑阶梯；P0 可手工执行 `STRESS_PROFILE=read locust ... --csv` 后由脚本归档）
2. 每档独立进程运行，稳态 2–3 分钟
3. 每档记录 §3.4 观测项
4. 找拐点：p95 陡增或错误率抬头的位置即当前硬件档位的承载上限

**任务权重**（模拟真实读流量）
| 任务 | 权重 | 说明 |
|---|---|---|
| `GET /judgments/{id}` | 60% | 轮询热路径；id 取自 §2.3 池文件（热/冷两档分别跑） |
| `GET /addresses/{addr}/subgraph` | 25% | 大 JSONB 反序列化路径 |
| `GET /cases` | 10% | 分页列表 |
| `GET /patterns` | 5% | 知识库列表 |

**预期结果**
- 各档错误率（§3.2 口径）< 1%
- 热档轮询 p95 ≤ 300ms；冷档单独记录并标注
- 首轮产出 CSV + `summary.json` 基线归档

---

### STRESS-02 读写混合

- **优先级**: P1 · **状态**: P0 已实现

**前置条件**
- 同 STRESS-01；**worker 容器运行中**（analyze 走 arq）

**操作步骤**
1. 同 STRESS-01 命令，改用 `--profile mixed`（叠加 analyze 5%）
2. analyze 任务：`POST /addresses/analyze`（种子地址 + Bearer）→ 202 → `GET /judgments/{id}` 轮询至终态
3. 观测 `q_analysis` 队列长度曲线

**预期结果**
- **幂等复用正确**：并发撞同一 (address, hops, time_window) 只产生一个进行中任务，响应为 202（新建）或 200（复用），无 5xx
- `reclaim_zombies`（每次 analyze 触发）不成为 p95 主导项——若 analyze 202 响应 p95 随 VU 线性恶化且 PG 等待事件集中在该查询，即命中瓶颈
- 停止压测后 `q_analysis` 在 60s 内回落至 0（§3.3）
- `/admin/queues` 的 pending 计数与 `llen q_analysis` 一致（#74 后台治理的口径校验）

---

### STRESS-03 限流边界

- **优先级**: P2 · **状态**: P0 已实现

**前置条件**
- 同 STRESS-01；**不携带 Bearer token** 的独立场景（`--profile anon`）

**操作步骤**
1. 匿名 analyze 打非白名单地址 → 预期 403
2. 匿名 analyze 打白名单地址直至超配额 → 预期 429 + `Retry-After`
3. 高频失败登录 → 预期 429（SEC-05 爆破防护在压力下生效）
4. 登录用户 analyze 高活跃地址（live 模式，`tx_count` 超过 `ADDRESS_TX_COUNT_LIMIT`）→ 预期 422 `ADDRESS_TOO_ACTIVE`（issue #79 预检在压测下不成为新增瓶颈——stats 响应经 Redis L2 缓存）
5. 登录用户 + `mock_scenario=timeout` → 预期任务 `failed` 终态（非 5xx）

**预期结果**
- 各类预期拒绝状态码与语义正确，**无 5xx 泄漏**（限流实现本身在高并发下不崩）
- 匿名限流不误伤已登录用户（带 token 的 STRESS-01 类同机运行无 429）

---

### STRESS-04 Redis 降级 smoke

- **优先级**: P3 · **状态**: P0 已实现（冒烟验证，不设吞吐指标）

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

---

### STRESS-05 报告队列并发（W2）

- **优先级**: P1 · **状态**: P1 待实现

**前置条件**：§2 环境 + worker-report 运行中（`ARQ_REPORT_MAX_JOBS=1`）

**操作步骤**
1. `--profile queue-report`：并发 K 个已关联 ≥50 地址的案件同时 `POST /cases/{id}/reports`（pdf + html）
2. 记录 `q_report` 深度曲线与单份报告端到端耗时随 K 的变化
3. 校验产出：HTTP 200 + `%PDF` 魔数 + `%%EOF`；HTML 含证据链字段（模型/提示词/构造器）

**预期结果**
- 所有报告任务最终 `completed`，无 5xx、无重复渲染
- 单份耗时随 K 的劣化曲线被记录（单并发下排队时间 = 总量 / 消费速率的线性关系成立）
- 停止后 `q_report` 回落至 0（回落窗口按任务量标注，可放宽于 60s）

---

### STRESS-06 索引重算并发与资源争用（W3）

- **优先级**: P1 · **状态**: P1 待实现

**前置条件**：worker-index 运行中；两个子档：`EMBEDDING_PROVIDER=stub`（测队列/并发）与真实 provider（测 429 语义）

**操作步骤**
1. 并发 K 个模式 `POST /patterns/{id}/edit` 触发索引重算（K ≤ 4：单次 73–180s，`ARQ_INDEX_MAX_JOBS=2`）
2. 同时运行 STRESS-01 读负载，记录读侧 p95 劣化幅度与容器 CPU/RSS
3. 观测 `q_index` 深度、`/admin/queues` 的 index 队列计数
4. 真实 provider 档下确认失败留痕：revision `index_status=failed` + 错误码，且该模式**上一 active 版本仍可被检索**

**预期结果**
- 读侧劣化幅度被记录在案（作为「索引重算与在线读共存的容量代价」结论）
- 失败的重算不阻塞其他模式、不污染召回、可重试
- 停止后 `q_index` 回落至 0

---

### STRESS-07 失败与重试风暴（覆盖 #74 治理路径）

- **优先级**: P2 · **状态**: P2 待实现

**前置条件**：mock provider；worker 运行中

**操作步骤**
1. `--profile failure`：高并发 analyze 携带 `mock_scenario` ∈ {`timeout`, `rate_limited`, `invalid_json_then_valid`}
2. 观测重试曲线、`q_analysis` 深度、`task_dead_letters` 行数与失败分类
3. 并发轮询 `/admin/queues`，验证治理端点自身不被压垮

**预期结果**
- 客户端**零 5xx 泄漏**（失败以任务 `failed` 终态 + 错误码表达，而不是 HTTP 5xx）
- 死信表只收纳超过重试上限的任务，分类与 `PATTERN_INDEX_FAILED` / `LLM_PROVIDER_*` 语义一致
- 队列状态端点在压力下 p95 稳定（不成为新的瓶颈）

---

### STRESS-08 可观测与管理端点成本

- **优先级**: P3 · **状态**: P2 待实现

**操作步骤**
1. 以监控轮询频率（如 1s）请求 `/admin/queues`、`GET /judgments/{id}/trace`、`GET /cases/{id}`
2. 在 `analysis_spans` 增长到较多行后重复测量（写入放大对查询的影响）

**预期结果**
- 各端点 p95 被记录；`/admin/queues` 的聚合扫描不随队列长度线性恶化
- trace 查询在 span 表增长后仍可用（若无索引支撑，则记录为待优化项）

## 5. 交付物

| 文件 | 职责 |
|---|---|
| `tests/performance/run_stress.py` | **单一入口**：`guard`（环境守卫）/ `pool`（分层抽样池）/ `baseline`（跑档 + 采集观测 + 归档）/ `report`（归档渲染为自包含 HTML）/ `ladder`（P1）/ `compare`（P1）/ `cleanup` |
| `tests/performance/pool.py` | 池文件的读写与抽样的纯函数（不依赖 locust，便于单测） |
| `tests/performance/seed_volume.py` | 幂等灌数：万级 judgments（合成地址 + 真实体积快照）+ 百级 cases；`--cleanup` 清理 |
| `tests/performance/locustfile.py` | `read`/`mixed`/`anon`（P0）+ `queue-analysis`/`queue-report`/`queue-index`/`failure`（P1/P2），`STRESS_ID_POOL_FILE` 读池，按 §3.2 判定成功/失败 |
| `tests/performance/perf_phase6.py` | 进程内基准 PERF-01/02（与 HTTP 压测互补，口径不可混用） |
| `output/stress/<YYYYMMDD>-<tag>/` | 归档：locust CSV + `observations.json` + `summary.json` + `report.html`（**归档物必须自描述**，§3.1） |

## 6. 验收标准

### P0（本次落地）

- [x] 环境守卫：非 `fixture`+`mock` 下 `run_stress.py` 拒绝运行（dev 档实测拒绝一次）
- [x] `seed_volume.py` 执行后 judgments ≥ 10 000 行、cases ≥ 100；重复执行不产生重复数据
      （实测 10 000 bulk + 500 热档 = 10 501；重跑 `inserting=0`、热档重建 500）
- [x] `seed_volume.py --cleanup` 后 STRESS 命名空间残留为 0（实测 removed 500 case_addresses /
      100 cases / 10 500 judgments，复查 `residue: judgments=0 cases=0`）
- [x] id 池分层抽样产出热/冷两档，locust 经 `STRESS_ID_POOL_FILE` 消费；无池文件时标注
      `derived(hot-only)` 且拒绝作为基线（实测池 hot=500/cold=500/addresses=200；
      冷档为空时 `baseline` 退出码 1 并提示）
- [x] 判定口径（§3.2）落地：非预期 4xx 与缺失关键字段计失败（用全零 judgment id 构造 404，
      实测 26/26 全部计入失败——旧口径只判 5xx 会全部漏掉）
- [x] `perf_phase6.py` RSS 口径修正后自述清晰（实测输出 `container:worker-report, 68.3MB`，
      旧实现测的是压测进程自身），PERF-02 HTTP 口径打通（实测 http p95 10ms/6ms
      vs db p95 4ms/4ms，差值即端点与序列化开销）
- [x] `run_stress.py baseline` 产出 dev 档首份归档（含热/冷两档 p95、观测项、summary.json）
- [x] STRESS-01 首份基线：热档轮询 p95 ≤ 300ms

**dev 档首基线（2026-09-23，归档于 `output/stress/20260923-dev-read-{hot,cold}/`）**

| 档位 | 请求数 | 失败 | 轮询 p95 | 备注 |
|---|---|---|---|---|
| hot（500 热档 id） | 749 | 0 | 12ms | 阈值 300ms，余量充足 |
| cold（500 冷档 id） | 751 | 0 | 12ms | 与热档无差异——1 万行全在 PG 缓存内，冷热口径要等数据量/并发上去才见区分 |

观测项同档采集：三队列深度均 0、`analysis_spans` 7 行/1 trace、PG 连接 10。
基线为 1 万行 / 10 VU / 60s 档位，**不可外推到更高 VU 或更大数据量**（阶梯与拐点属 P1）。

### P1

- [x] `run_stress.py ladder` 自动跑阶梯并归档每档观测项（开放模型固定到达率 + 逐档服务端观测）
- [x] 延迟口径双轨（工具 / 校正）落地，报告并列展示 coordinated-omission 幅度
- [x] 拐点判定收敛到单一决策点，四态结论（meets / below / environment_limited / indeterminate）
- [x] 压测机余量进判定：生成器 CPU ≥ 60% 的档位判为数据无效并排除在拐点之外
- [ ] `run_stress.py compare` 与基线对比输出劣化清单（自校验：同一份 CSV 自比应报 0%）
- [ ] STRESS-01/02 达标项（错误率、热档 p95、队列 60s 回落）全绿
- [ ] STRESS-03 预期拒绝不计入错误率 + mock_scenario 失败路径无 5xx
- [ ] W1 分析队列专线（`LLM_MOCK_DELAY_MS`）产出消费速率/堆积曲线
- [ ] STRESS-05 报告并发：任务全 completed、PDF/HTML 完整性、`q_report` 回落
- [ ] STRESS-06 索引并发：读侧劣化记录、失败留痕且旧版本仍可检索、`q_index` 回落

**P1 首轮实测结论（2026-09-23，dev 档，归档 `output/stress/20260923-dev-ladder/`）**

| 档位 | 实测 RPS | 校正 p95 | 轮询 p95 | 错误率 | 生成器 CPU | 服务端 CPU | 判定 |
|---|---|---|---|---|---|---|---|
| 200（4 进程） | 200 | 644ms | — | 0.00% | 11.0% | 0.31% | 有效 |
| 250（4 进程） | 250 | 8629ms | — | 0.05% | 13.6% | 0.24% | 抬升但疑环境限制 |

- **未验证到目标 250 RPS**：250 档出现 3× 尾延迟抬升，但服务端 CPU 仅 0.24%，
  且四个端点（含最轻的 `/patterns`）延迟**同步**抬升 → 瓶颈在压测环境而非服务端，
  结论只能是 `environment_limited`
- 单进程生成器在 ~250 RPS 触顶（CPU 70%，校正 p95 150s vs 工具 6.8s，CO 幅度 22×），
  故本轮改用 4 进程（`--generators 4`）
- 已知偏差：压测机与 SUT **同机**（macOS Docker Desktop 网络栈），
  资源观测不可作为容量依据；下一步必须独立压测机 + 服务端侧延迟分解（如 pprof/event loop lag）

### P2

- [ ] STRESS-07 失败/重试风暴：零 5xx 泄漏、死信分类正确
- [ ] STRESS-08 可观测端点成本：`/admin/queues`、`/trace`、`/cases/{id}` p95 记录
- [ ] 冷档抽样纳入常态化（每轮都跑）而非仅首轮

## 7. 修订记录

- **v2（本次）**：与 #73/#74/#75 后的系统现状对齐——队列专线（q_analysis/q_report/q_index 三进程）；
  id 池改分层抽样（修"热集代替全表"的系统性高估）；成功判定不再只看 5xx；
  队列回落提升为达标项；观测命令修正（`q_graph` 废弃）；新增 STRESS-05~08；
  新增 `run_stress.py` 单一入口与归档自描述要求；补 `analysis_spans` 写入放大观测；
  `LLM_MOCK_DELAY_MS` 通道（W1 依赖）与 `mock_scenario` 注入点显式记录
- **v1**：初版（读/写/限流/降级四场景 + 基线与阈值口径）
