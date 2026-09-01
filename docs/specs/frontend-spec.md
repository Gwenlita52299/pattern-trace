# Spec · frontend/ — Next.js SPA 前端

> 模块路径：`pattern_trace/frontend/`
> 技术栈：Next.js 14+ / TypeScript / Tailwind CSS / React Flow (xyflow) / Zustand
> 依赖：后端 REST API `/api/v1`（契约以 backend-api-spec.md 为准）
> 状态：设计定稿，待开发

## 1. 职责边界

前端只负责交互与可视化。不持有任何业务判断逻辑、不做检索打分、不调用 LLM。
所有图谱数据由后端 `/subgraph` 接口返回标准化 JSON。
MVP **桌面优先**；移动端仅保证只读浏览（画布禁拖拽、详情栏改全屏抽屉），完整移动适配列为非目标。

## 2. 页面路由

| 路由 | 组件文件 | 核心功能 |
|---|---|---|
| `/` | `app/page.tsx` | 地址查询输入 + 演示地址 chips |
| `/analyze/[id]` | `app/analyze/[id]/page.tsx` | 图谱画布 + verdict 面板 + 证据面板（id=judgment_id，刷新页面直接恢复轮询，不重新分析） |
| `/patterns` | `app/patterns/page.tsx` | pattern 列表（分页/筛选同步 URL searchParams） |
| `/cases` | `app/cases/page.tsx` | 案件列表表格（登录后可访问） |
| `/cases/[id]` | `app/cases/[id]/page.tsx` | 案件详情 + 关联地址 + 报告导出按钮 |
| `/login` | `app/login/page.tsx` | 登录表单 |

**路由保护**：`middleware.ts` 匹配 `/cases/:path*`，仅检查 access token 存在性（内存丢失则尝试一次 refresh）；验签由后端 401 兜底。`/analyze/*` 明确豁免（免登录演示）。

## 3. 认证方案（D4 定稿）

- login 成功：access_token 存 **Zustand 内存 store**；refresh_token 由后端 Set-Cookie（HTTPOnly）
- 所有请求：`Authorization: Bearer <access>` + `X-Requested-With: XMLHttpRequest`（CSRF）
- 401 → 自动调用 refresh → 重放原请求；refresh 失败 → 清空内存态 + 跳转 /login
- **禁止使用 localStorage 存任何 token**

## 4. 核心组件

### GraphCanvas (`components/GraphCanvas.tsx`)

- 基于 React Flow，**地址流式展示模型**（address-as-node / tx-as-edge）：画布**不直接渲染**
  后端返回的 canonical 子图（其中 tx 也是节点），而是经 `lib/address-flow.ts::toAddressFlow()`
  变换为「仅 address 节点 + 交易作为有向边」的流式图，每条边标注入/出边交易金额：
  - 地址节点：显示标签 / BTC 余额摘要 / script_type
  - 交易 → 有向边（每个输入地址 → 每个输出地址），边上标注
    **出边金额（源地址流入交易的金额）→ 入边金额（目标地址从交易收到的金额）**
  - 混币器分支：特殊样式（红色边框 + ⚠ 图标）；摘要节点在流式视图中不渲染（非地址）
  - 边样式：`is_stopped_expansion` → 虚线 + 停止图标；`is_crosschain` → 桥协议标签
- **证据高亮**：接收 `highlightIds: Set<string>`（canonical D3 ID），经
  `lib/address-flow.ts::resolveHighlightIds()` 映射到本视图节点/边 ID（`addr:`→节点、
  `tx:`→该交易所有边与端点、`edge:`→尽力匹配），用 `useMemo` 派生样式，不重建 nodes 数组
- 交互：缩放、拖拽、节点点击弹出详情侧栏、按跳数过滤
- **大图性能配置（200 节点）**：
  - `onlyRenderVisibleElements: true`
  - 地址节点 `React.memo` 包裹
  - 布局算法 dagre（LR 方向）自动排列，禁用 `nodesConnectable`（只读图谱）
  - 初始 `fitView`

### VerdictCard (`components/VerdictCard.tsx`)

四态矩阵：

| 态 | 展示 |
|---|---|
| loading | skeleton 卡片 + 进度文案（"正在构建子图…" / "AI 分析中…"） |
| failed | 错误提示卡 + error_code + **重试按钮**（重新发起轮询或重新 analyze） |
| no_match | 灰色 NO_MATCH 卡 + recommended_action=review 说明 |
| success | 四档色卡 |

四档色卡颜色 + 图标双编码（a11y）：
- HIGH = red ⚠ / MEDIUM = orange ● / LOW = green ✓ / NO_MATCH = gray ○
- 置信度进度条（0–1）
- recommended_action 标签（freeze / monitor / review / none）
- reasoning 全文展示
- evidence 列表：每条 ID 可点击 → `setHighlight(id)` 高亮对应节点/边

### PatternCompare (`components/PatternCompare.tsx`)

- 左右双栏：左侧输入子图缩略渲染，右侧命中 pattern 的规范子图
- 差异点标注（LLM reasoning 中提到的结构差异用橙色高亮）

### CaseList (`components/CaseList.tsx`)

- 表格列：标题 / 状态徽章（含文字标签）/ 创建时间 / 关联地址数 / 操作
- 状态：open（蓝）/ investigating（橙）/ closed（灰）

## 5. 状态管理

**核心分析页用 Zustand；列表页统一用 SWR**（避免每页重复手写 loading/error 缓存逻辑）。

```typescript
interface AnalysisStore {
  accessToken: string | null;          // 仅内存
  currentJudgment: Judgment | null;
  subgraph: { nodes: GraphNode[]; edges: GraphEdge[] } | null;
  status: 'idle' | 'queued' | 'processing' | 'completed' | 'failed';
  error: string | null;
  highlightIds: Set<string>;
  startAnalysis: (params: { address: string; hops: number; time_window_days?: number }) => Promise<string>; // 返回 judgment_id
  pollJudgment: (id: string) => Promise<void>;   // 可独立重试
  setHighlight: (ids: string[]) => void;
}
```

**轮询协议**：
- 使用 202 响应返回的 `poll_url`
- 间隔策略：2s 固定起步，连续失败退避至 5s 上限
- 终止条件：`status ∈ {completed, failed}` 或总时长 > 90s（超时转 failed UI）
- 组件卸载时 AbortController 取消轮询
- `/analyze/[id]` 页面挂载时直接以 id 恢复轮询

SWR 用于 `/patterns`、`/cases` 列表页：分页与筛选条件写入 URL searchParams（刷新/分享保留状态）。

## 6. API Client (`lib/api.ts`)

- fetch 封装：自动携带 Bearer + CSRF header，15s 请求超时
- 幂等 GET 自动重试最多 2 次（指数退避）
- 按 URL+params 取消同类 pending 请求（防快速切换地址的竞态覆盖）
- 401 处理见 §3
- **TypeScript 类型生成**：`openapi-typescript` 从 backend `/openapi.json` 生成 `types/api.d.ts` 并提交 git；CI 中 backend job 导出 schema artifact，frontend job 校验类型一致性

## 7. 验收标准

- [ ] 输入地址 → 显示 loading → 展示图谱 + verdict；刷新 /analyze/[id] 不重新发起分析
- [ ] judgment failed 时显示错误卡 + 重试按钮
- [ ] 点击 evidence ID → 图谱中对应节点高亮（ID 匹配 D3 规范）
- [ ] 四档色卡颜色正确且带图标双编码
- [ ] 登录后可创建案件、关联地址、导出报告（异步轮询下载）
- [ ] 未登录访问 /cases 重定向到 /login；token 过期自动 refresh
- [ ] 200 节点图谱首次交互渲染无肉眼可见卡顿（>30fps）
