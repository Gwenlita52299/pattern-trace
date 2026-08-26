# 可靠性与运维测试用例 · 跨模块可靠性

> 覆盖 worker 崩溃恢复、外部依赖降级、部署回滚兼容性
> 执行环境：docker-compose 全栈 + 故障注入工具

---

## REL-01 Worker 崩溃后 queued 任务恢复执行

- **优先级**:P0
- **来源**：BR-16 补充——崩溃恢复路径（区别于 BE-40 定时回收路径）

**前置条件**
- docker-compose 全栈运行中
- 有若干 queued 任务在队列中等待

**操作步骤**
1. 向队列提交 ≥5 个 analyze 任务
2. `kill -9` worker 进程（模拟崩溃，非优雅关闭）
3. 等待 docker compose restart policy 重启 worker
4. 观察任务完成情况

**预期结果**
- queued 任务在 worker 重启后被继续消费执行
- processing 中被中断的任务要么重新入队执行，要么被僵尸回收机制标记 failed(TASK_TIMEOUT)
- 无任务永久悬挂或静默丢失
- 全部任务最终达到终态（completed 或 failed）

---

## REL-02 Redis 不可用时显式降级行为

- **优先级**:P0
- **来源**：BR-15 补充——缓存层故障降级契约

**前置条件**
- docker-compose 全栈运行中
- 可控制 redis 容器启停

**操作步骤**
1. `docker compose stop redis`
2. 尝试发起 analyze 提交
3. 尝试触发 rate limit 判断
4. 尝试读取 LLM 结果缓存
5. `GET /readyz` 检查健康状态
6. `docker compose start redis` 恢复

**预期结果**
- 各操作的降级行为符合 spec 显式定义（fail-open 接受请求 或 fail-closed 返回 503，不允许静默忽略）
- readyz 返回 503 并说明 Redis unreachable
- Redis 恢复后系统自动回归正常（无需重启 backend）
- rate limit 在 Redis 不可用期间的策略有明确文档定义（放行或拒绝均可，但必须有据可查）

---

## REL-03 LLM Provider 超时/限流与校验失败区分

- **优先级**:P0
- **来源**：LJ-06 补充——provider 层故障 vs 输出质量故障

**前置条件**
- mock LLM provider 支持模拟超时和 429 响应

**操作步骤**
1. 配置 mock provider 对所有请求返回超时（不响应）
2. 发起 analyze 并轮询至终态
3. 修改配置使 provider 返回 HTTP 429 Too Many Requests
4. 再次发起 analyze 并轮询至终态

**预期结果**
- 超时场景最终 status=failed，error_code = "LLM_PROVIDER_TIMEOUT"
- 429 场景最终 status=failed，error_code = "LLM_PROVIDER_RATE_LIMITED"
- 与 LLM_VALIDATION_FAILED（输出内容校验失败）三种 error_code 互不相同
- 超时和 429 均触发退避重试后再进入终态

---

## REL-04 部署回滚兼容性——expand 阶段新旧代码共存

- **优先级**:P1
- **来源**：IF-11 补充——回滚窗口读写兼容

**前置条件**
- Alembic expand 迁移已执行（新列已存在但旧代码不感知）
- 可同时部署两个版本的 backend 容器指向同一 DB

**操作步骤**
1. 版本 V_new（含新列写入逻辑）与版本 V_old（不含）同时运行
2. 通过 V_old 创建一条记录
3. 通过 V_new 读取该记录
4. 通过 V_new 创建一条记录
5. 通过 V_old 读取 V_new 创建的记录

**预期结果**
- V_old 写入的行在 V_new 读取时新列取默认值/null 不报错
- V_new 写入的行在 V_old 读取时忽略未知列正常工作
- 双版本并行期间无数据损坏或唯一约束冲突

---

## REL-05 Esplora 主备全失败终态明确

- **优先级**:P2
- **来源**：GB-17/18/19 补充——完全分区场景

**前置条件**
- 主备 Esplora mock 均持续返回 500
- circuit breaker 与 fallback 配置生效

**操作步骤**
1. 发起 analyze
2. 观察 graph-builder 阶段的重试→熔断→fallback 全过程
3. 轮询至终态

**预期结果**
- 最终 judgment.status = failed
- error_code 明确指示上游数据源不可用（如 "ESPLORA_UNAVAILABLE"）
- 前端展示的错误文案准确反映"数据源暂不可用"而非泛化的系统错误
