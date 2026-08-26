# 性能与容量测试用例 · 跨模块性能

> 覆盖大容量报告生成、大规模数据查询
> 执行档位：nightly / 发布前基准（非常规 CI 回归）

---

## PERF-01 大案件报告生成容量

- **优先级**:P1
- **来源**：BE-26 补充——容量上界验证

**前置条件**
- 存在一个关联了 50–100 个地址的案件
- worker 运行中，mock 渲染器就绪
- 内存监控工具可用

**操作步骤**
1. `POST /cases/<id>/reports?format=pdf`
2. 记录生成耗时
3. 监控 worker 内存峰值
4. 下载生成的 PDF 并检查文件完整性

**预期结果**
- 生成耗时 ≤ 60s
- worker RSS 峰值增长不超过基线 ×3
- PDF 文件完整可打开，页数合理（>10 页），包含所有关联地址的分析结果

---

## PERF-02 万级 judgment 表查询性能

- **优先级**:P2
- **来源**：BE-45 补充——规模增长下的索引有效性

**前置条件**
- judgments 表填充 ≥10000 行测试数据
- subgraph_snapshot 列已启用 LZ4 TOAST 压缩
- 相关索引已建立

**操作步骤**
1. 执行 `GET /addresses/<address>/subgraph` 采样 ≥30 次
2. 执行分页列表查询采样 ≥30 次
3. EXPLAIN ANALYZE 验证索引命中
4. 计算 p95

**预期结果**
- subgraph 查询 p95 ≤ 300ms
- 分页列表查询 p95 ≤ 200ms
- EXPLAIN 显示 Index Scan（非 Seq Scan）
- TOAST 解压开销在可接受范围内
