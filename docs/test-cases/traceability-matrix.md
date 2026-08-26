# 需求追踪矩阵与语义覆盖率

> 方法：将 9 份 specs 中每条可验证需求（含验收标准 checkbox、性能目标、数据模型约束、安全规则、评审修订决策）编码为 R-xx 需求项，映射到测试用例。
> 覆盖率 = 已覆盖需求数 / 总需求 × 100%
> 审计日期：2026-08-23

## 1. frontend-spec.md

| 需求 ID | 需求描述（语义） | 来源章节 | 覆盖用例 |
|---|---|---|---|
| FR-01 | 输入地址→loading→展示图谱+verdict | §6-1 | FE-01 |
| FR-02 | 刷新 /analyze/[id] 恢复不重分析 | §5/§2 | FE-02 |
| FR-03 | evidence 点击高亮对应节点/边 | §6-2,§3 | FE-05 |
| FR-04 | 四档色卡颜色+action 一致 | §6-3 | FE-06 |
| FR-05 | 登录后可创建案件并关联地址 | §6-4 | FE-07 |
| FR-06 | 未登录写操作路由重定向 login | §6-5,§2 | FE-08 |
| FR-07 | GraphCanvas 自定义节点类型（地址/交易/mixer/摘要） | §3 | FE-13 |
| FR-08 | stopped_expansion 虚线边 | §3 | FE-14 |
| FR-09 | crosschain 桥协议标签边 | §3 | FE-15 |
| FR-10 | highlightIds Set 高亮青色发光 | §3 | FE-05,FE-26 |
| FR-11 | 缩放拖拽节点详情侧栏跳数过滤交互 | §3 | FE-16,FE-17 |
| FR-12 | VerdictCard 四态矩阵 loading/failed/no_match/success | §4 | FE-03,FE-04,FE-28,FE-32 |
| FR-13 | a11y 图标双编码色卡 | §4 | FE-06,FE-31 |
| FR-14 | recommended_action 标签展示 | §4 | FE-06 |
| FR-15 | PatternCompare 双栏差异标注 | §3 | FE-34（P2 冒烟级）|
| FR-16 | CaseList 状态徽章三色 | §3 | FE-19 |
| FR-17 | AnalysisStore 接口定义完整 | §5 | FE-01,02,03（行为级） |
| FR-18 | startAnalysis 与 pollJudgment 分离 | §5 | FE-02 |
| FR-19 | poll_url 使用 + 退避策略 + 90s 超时 + AbortController | §5 | FE-28,29,30 |
| FR-20 | access token 内存持有禁 localStorage | §3 | FE-10,11,27 |
| FR-21 | CSRF header X-Requested-With | §3 | FE-21 |
| FR-22 | middleware 保护 /cases/:path* 豁免 analyze | §2 | FE-08,09 |
| FR-23 | API client GET 幂等重试+15s超时+取消 pending | §6 | FE-23,24 |
| FR-24 | OpenAPI codegen openapi-typescript CI 校验 | §6 | FE-25,IF-07 |
| FR-25 | 大图性能配置 onlyRenderVisibleElements/memo/dagre/fitView | §4 | FE-12,26 |
| FR-26 | patterns/cases SWR 分页 URL searchParams | §5 | FE-18 |
| FR-27 | 移动端只读降级非目标声明 | §1 | FE-22 |
| FR-28 | 报告导出异步轮询下载 | §7-6 | FE-20 |

**小计**：28 条需求，28 条全部覆盖（FR-15 由 FE-34 冒烟覆盖）→ **100%**

## 2. backend-api-spec.md

| 需求 ID | 描述 | 来源 | 用例 |
|---|---|---|---|
| BR-01 | RFC9457 Problem Details 全局错误格式 | §2 | BE-41,BE-02 |
| BR-02 | 统一分页包 {items,total,...} page_size≤100 | §2 | BE-21,BE-04 |
| BR-03 | 权限矩阵 anonymous/investigator/admin | §2 | BE-05,06,36 |
| BR-04 | login 返回 access_token+user info | §3 | BE-01 |
| BR-05 | refresh token HTTPOnly Cookie SameSite | §3 | BE-16 |
| BR-06 | refresh rotation 旧 token revoked | §3 | BE-16 |
| BR-07 | reuse detection 撤销整个 family | §3 | BE-17 |
| BR-08 | logout 清除 cookie 撤销 family | §3 | BE-18 |
| BR-09 | analyze 白名单+配额成本控制 | §3 | BE-08,09,29 |
| BR-10 | hops 1–3 校验（D6） | §3 | BE-10,GB-14 |
| BR-11 | address checksum 级校验 | §3 | BE-11 |
| BR-12 | rate limit IP 维度+Retry-After | §3/§5 | BE-28,44 |
| BR-13 | anon daily quota | §3 | BE-29 |
| BR-14 | 同参数幂等提交 | §3 | BE-12,CM-12 |
| BR-15 | Arq async task 处理流程 | §3/D2 | IF-02,CM-01 |
| BR-16 | 僵尸任务 120s 回收 | §3 | BE-40,CM-11 |
| BR-17 | 队列分离 q_graph/q_llm | §3 | IF-02 |
| BR-18 | subgraph 返回最新 completed 快照 | §3 | BE-33 |
| BR-19 | ?judgment_id= 历史查询 | §3 | BE-34 |
| BR-20 | judgment completed 字段完整性 | §3 | BE-13 |
| BR-21 | judgment failed 含 error_code/retry_count | §3 | BE-14 |
| BR-22 | 状态机终态不可变 | §3 | BE-15,CM-03 |
| BR-23 | cases CRUD 完整契约 | §3 | BE-23,24,25 |
| BR-24 | Idempotency-Key 防重复创建 | §3 | BE-23 |
| BR-25 | case status 单向流转 | §3 | BE-25 |
| BR-26 | reports 异步 202+轮询+下载 | §3 | BE-26,FE-20 |
| BR-27 | 报告并发限制 ≤2 | §3 | BE-27 |
| BR-28 | users 管理 admin only | §3 | BE-36 |
| BR-29 | bootstrap admin env 注入 | §3 | BE-35 |
| BR-30 | healthz/readyz | §3 | BE-19,20 |
| BR-31 | 数据模型 users bcrypt(12)+72byte | §4 | BE-37 |
| BR-32 | judgments snapshot LZ4 TOAST | §4 | BE-45 |
| BR-33 | case_addresses PK (case_id,address) | §4 | BE-24 |
| BR-34 | audit_logs 字段完备性 | §4 | BE-32,CM-10 |
| BR-35 | CORS allow_credentials 显式 origin | §5 | BE-30,31 |
| BR-36 | SQL 注入防护权重服务端读取 | §5 | BE-43,RT-05 |

**小计**：36 条全部覆盖 → **100%**

## 3. graph-builder-spec.md

| 需求 ID | 描述 | 来源 | 用例 |
|---|---|---|---|
| GR-01 | 五类终止条件 unspent | §3 | GB-01 |
| GR-02 | out_of_range block_time 口径 | §3 | GB-02,03,04 |
| GR-03 | early_stop coinjoin+crosschain | §3/§4 | GB-05,06 |
| GR-04 | tx4_new_dst depth=3 硬停 | §3 | GB-08 |
| GR-05 | queue_empty 正常结束 | §3 | GB-12 |
| GR-06 | seen_utxos 完整三元组去重 | §5 | GB-07 |
| GR-07 | 快照语义逐层展开 | §2 | GB-08 |
| GR-08 | max_total_nodes=200 硬上限 | §8 | GB-09 |
| GR-09 | max_nodes_per_layer=50 | §8 | GB-10 |
| GR-10 | fanout_truncate_threshold=20 摘要节点 | §8 | GB-11 |
| GR-11 | D3 ID 规范生成 | D3 | GB-13,CM-02 |
| GR-12 | async-lru 缓存 key 含 base URL | §7 | GB-15 |
| GR-13 | Redis 二级缓存 TTL24h | §7 | GB-21 |
| GR-14 | 重试指数退避+jitter | §7.1 | GB-16 |
| GR-15 | circuit breaker 5次失败打开30s | §7.1 | GB-17 |
| GR-16 | 备用 provider mempool.space | §7.1 | GB-18 |
| GR-17 | 部分失败 degraded 标志 | §7.1 | GB-19 |
| GR-18 | Semaphore 并发预算公共5自托管10 | §7.1 | GB-20 |
| GR-19 | 热路径 BFS≤500ms | §9/D7 | GB-23 |
| GR-20 | 内存泄漏 RSS≤10%增长 | §9 | GB-22 |
| GR-21 | Rust 定位批量离线 ≥100K 边基准 | §9 | GB-24 |
| GR-22 | hops 参数校验 1–3 | validate_hops | GB-14 |

**小计**：22 条全部覆盖 → **100%**

## 4. retrieval-spec.md

| 需求 ID | 描述 | 来源 | 用例 |
|---|---|---|---|
| RR-01 | 特征向量核心维度正确 | §3 | RT-01 |
| RR-02 | 归一化存入 pgvector | §3 | RT-02 |
| RR-03 | 语义 embedding 自然语言序列化 | §4 | RT-03 |
| RR-04 | 混合召回权重服务端配置 | §5 | RT-05 |
| RR-05 | 数千条精确扫描≤50ms | §5 | RT-06 |
| RR-06 | 规模化两路 ANN+RRF 方案 | §5 | RT-07 |
| RR-07 | HNSW 双列独立索引 | §5 | RT-08 |
| RR-08 | embedding 版本锁定校验 | §5 | RT-04,CM-08 |
| RR-09 | WL 同构高分 | §6 | RT-09 |
| RR-10 | WL 异构低分 | §6 | RT-10 |
| RR-11 | WL 带属性区分类型 | §6 | RT-11 |
| RR-12 | WL 空图边界 | §6/§10 | RT-12 |
| RR-13 | 差异说明文本生成 | §6 | RT-14 |
| RR-14 | Top-K 输出结构完整 3–5 | §7 | RT-15 |
| RR-15 | recall@10≥80% | §9 | RT-16 |
| RR-16 | Top-3 命中率追踪 | §9 | RT-17 |
| RR-17 | 空子图/单节点不崩溃 | §10 | RT-18 |
| RR-18 | cosine similarity 边界 | §5 语义 | RT-13 |

**小计**：18 条全部覆盖 → **100%**

## 5. llm-judge-spec.md

| 需求 ID | 描述 | 来源 | 用例 |
|---|---|---|---|
| LR-01 | Provider 抽象四实现切换 | §2 | LJ-01 |
| LR-02 | structured-output 能力降级契约 | §2 修正 | LJ-17 |
| LR-03 | System Prompt 五条防幻觉规则 | §3 | LJ-19 |
| LR-04 | JSON Schema 枚举校验 | §4 | LJ-02,12 |
| LR-05 | evidence 引用校验拒绝越界 | §5.2 | LJ-05 |
| LR-06 | 重试 MAX_RETRIES=3 + 错误提示追加 | §5.2 | LJ-03,04,06 |
| LR-07 | 仅校验通过才写缓存 | §5.3 修正 | LJ-06,CM-04 |
| LR-08 | 缓存命中跳过 LLM | §5.3 | LJ-07,BE-38 |
| LR-09 | prompt_version 缓存隔离 | §5.4 | LJ-08 |
| LR-10 | builder_version 缓存隔离 | build_cache_key | LJ-09,CM-07 |
| LR-11 | no_match→review 强制约束 | §3 rule3 | LJ-10,11 |
| LR-12 | thinking mode 提取 <think> | §7 | LJ-13,14 |
| LR-13 | canonical_subgraph_hash 规范化稳定 | P2-25 | LJ-15,16 |
| LR-14 | TTL 7 天 | §5.3 | LJ-18 |
| LR-15 | p95≤10s 热路径 | §8/D7 | LJ-20,CM-06 |
| LR-16 | evidence 有效率=100% 硬校验 | §8 | LJ-05,06 |
| LR-17 | 准确率≥85% 留出集 | §8 | RT-17 关联 |

**小计**：17 条全部覆盖 → **100%**

## 6. ingest-spec.md

| 需求 ID | 描述 | 来源 | 用例 |
|---|---|---|---|
| IR-01 | run_all 一键产出正负样本 | §7-1 | IG-01 |
| IR-02 | 按 seed_address 分组切图 | §2 | IG-02 |
| IR-03 | 过滤 ≥5节点+mixer 接触 | §2 | IG-03 |
| IR-04 | 负样本独立表隔离 | §3a | IG-04,CM-09 |
| IR-05 | 负正比 3:1 | §3 | IG-05 |
| IR-06 | 负样本无 mixer/黑名单 | §3 | IG-06 |
| IR-07 | 三张标签表入库可加载 | §4 | IG-07 |
| IR-08 | embedding_model/dim 元数据写入 | §5 | IG-08 |
| IR-09 | 结构特征向量维度一致 | §5 | IG-09 |
| IR-10 | 幂等重复运行不重复 | §6 | IG-10 |
| IR-11 | upsert key (seed,content_hash) | §6 | IG-11 |
| IR-12 | WL fingerprint 写入 | §2 | IG-12 |
| IR-13 | HNSW 索引创建 | §7-3 | IG-13 |
| IR-14 | embedding 本地 JSON 缓存 | §6 | IG-14 |

**小计**：14 条全部覆盖 → **100%**

## 7. infra-spec.md

| 需求 ID | 描述 | 来源 | 用例 |
|---|---|---|---|
| NR-01 | compose 一键启动全服务 healthy | §5-1 | IF-01,03 |
| NR-02 | worker Arq broker 显式配置 | §1/D2 | IF-02 |
| NR-03 | healthcheck+condition service_healthy | §1 | IF-03 |
| NR-04 | JWT_SECRET 强制注入弱默认拒绝 | §1 | IF-04 |
| NR-05 | Fly secrets 生产密钥管理 | §2 | IF-05 |
| NR-06 | CI PR 三 job 流水线 | §3 | IF-06 |
| NR-07 | main 合并自动部署 Fly+Vercel | §3/§5-3 | IF-08 |
| NR-08 | Alembic release phase 失败阻断 | §4 | IF-09 |
| NR-09 | advisory lock 多实例迁移保护 | §4 | IF-10 |
| NR-10 | expand-contract 回滚流程 | §4 | IF-11 |
| NR-11 | rediss:// TLS 连接 | §4 | IF-12 |
| NR-12 | PgBouncer prepared statements 兼容 | §4 | IF-13 |
| NR-13 | Ollama 服务健康模型加载 | §1 | IF-14 |
| NR-14 | pgdata volume 持久化 | §1 | IF-15 |
| NR-15 | E2E mock fixture 不依赖公网 | tests-spec 修订 | IF-16 |
| NR-16 | CI frontend OpenAPI 类型校验步骤 | §3 | IF-07 |

**小计**：16 条全部覆盖 → **100%**

## 8. 跨模块决策（D1–D7）

| 需求 ID | 描述 | 用例 |
|---|---|---|
| CR-01 | D1 进程内包编排端到端 | CM-01 |
| CR-02 | D3 ID 契约闭环 builder→judge→frontend | CM-02 |
| CR-03 | D5 状态机全生命周期合法跳变 | CM-03 |
| CR-04 | 失败路径全链路降级 | CM-04 |
| CR-05 | D7 冷路径≤20s | CM-05 |
| CR-06 | D7 热路径 p95≤10s | CM-06 |
| CR-07 | builder_version 升级缓存失效联动 | CM-07 |
| CR-08 | embedding 升级重建提示 | CM-08 |
| CR-09 | 负样本隔离不影响召回 | CM-09 |
| CR-10 | audit_logs 覆盖写操作链路 | CM-10 |
| CR-11 | 僵尸回收前端联动 | CM-11 |
| CR-12 | 幂等性端到端 | CM-12 |

**小计**：12 条全部覆盖 → **100%**

---

## 总覆盖率计算

| 模块 | 需求数 | 覆盖 | 未覆盖 | 覆盖率 |
|---|---|---|---|---|
| frontend | 28 | 28 | 0 | 100% |
| backend-api | 36 | 36 | 0 | 100% |
| graph-builder | 22 | 22 | 0 | 100% |
| retrieval | 18 | 18 | 0 | 100% |
| llm-judge | 17 | 17 | 0 | 100% |
| ingest | 14 | 14 | 0 | 100% |
| infra | 16 | 16 | 0 | 100% |
| cross-module | 12 | 12 | 0 | 100% |
| **总计** | **163** | **163** | **0** | **100%** |

### 结论

总语义覆盖率 **163/163 = 100%** ✅ 达标（FR-15 已由 FE-34 冒烟用例覆盖）。

---

## 新增安全/可靠性/契约/性能模块覆盖

### security-test-cases.md

| 需求 ID | 描述 | 用例 |
|---|---|---|
| SR-01 | 水平越权访问案件与报告拒绝+审计留痕 | SEC-01 |
| SR-02 | Refresh Cookie SameSite/Secure/Path 属性完整 | SEC-02 |
| SR-03 | 后端 CSRF 强制拒绝（服务端主动校验） | SEC-03 |
| SR-04 | JWT 篡改/alg:none/过期/错签均拒绝 | SEC-04 |
| SR-05 | 登录爆破限流且不泄露用户存在性 | SEC-05 |
| SR-06 | 敏感信息泄露扫描（响应体/日志/audit detail） | SEC-06 |
| SR-07 | 分页参数极端值统一 422 | SEC-07 |
| SR-08 | 地址规范化行为定义明确 | SEC-08 |
| SR-09 | 多标签页认证态一致性 | SEC-09 |

### reliability-test-cases.md

| 需求 ID | 描述 | 用例 |
|---|---|---|
| RR-01 | Worker 崩溃后 queued 任务恢复执行不丢失 | REL-01 |
| RR-02 | Redis 不可用时显式降级契约 | REL-02 |
| RR-03 | LLM provider 超时/429 与校验失败 error_code 区分 | REL-03 |
| RR-04 | expand 阶段新旧代码共存读写兼容 | REL-04 |
| RR-05 | Esplora 主备全失败终态明确 | REL-05 |

### contract-test-cases.md

| 需求 ID | 描述 | 用例 |
|---|---|---|
| TR-01 | OpenAPI 反向漂移检测（backend 新增字段 CI 失败） | CT-01 |
| TR-02 | 轮询不存在 ID 稳定 404 + poll_url 契约 | CT-02 |
| TR-03 | ID 契约反向校验畸形 subgraph 防御 | CT-03 |

### performance-test-cases.md

| 需求 ID | 描述 | 用例 |
|---|---|---|
| PR-01 | 大案件报告生成容量（50–100 地址） | PERF-01 |
| PR-02 | 万级 judgment 表查询索引命中 p95 达标 | PERF-02 |

### backend-api 补充用例

| 需求 ID | 描述 | 用例 |
|---|---|---|
| BR-36a | 同参数幂等提交依赖 DB partial unique index（机制验证） | BE-46 |
| BR-22a | judgment 终态非法跳变防御（worker 消息重放被守卫拒绝） | BE-47 |
| BR-24a | Idempotency-Key 同 key 不同 body → 409 | BE-48 |
| BR-33a | case_addresses 异常关联（404 case / 422 地址） | BE-49 |
| BR-26a | 报告下载 URL 过期与吊销 | BE-50 |

### ingest 补充用例

| 需求 ID | 描述 | 用例 |
|---|---|---|
| IR-15 | Embedding API 失败断点续跑仅补算缺失 | IG-15 |
| IR-16 | Parquet 数据损坏容错零写入 | IG-16 |
| IR-17 | ingest 双实例并发 upsert 安全 | IG-17 |

### frontend 补充用例

| 需求 ID | 描述 | 用例 |
|---|---|---|
| FR-29 | evidence 节点 ID 高亮对应图谱节点 | FE-33 |
| FR-30 | PatternCompare 双栏差异标注冒烟 | FE-34 |
