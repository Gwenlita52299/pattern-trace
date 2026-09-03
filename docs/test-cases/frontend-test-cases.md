# 前端测试用例 · frontend-spec

> 模块路径：`frontend/`
> 执行环境：Chrome 最新版 + Next.js dev server (localhost:3000) + backend API (localhost:8000)
> 优先级定义：P0=阻塞上线 / P1=重要 / P2=体验优化

---

## FE-01 首页地址输入正常分析流程

- **优先级**：P0
- **来源**：§6 验收标准第 1 条

**前置条件**
- 后端 API 运行在 `http://localhost:8000`，`GET /readyz` 返回 200
- 前端 dev server 运行在 `http://localhost:3000`
- 演示地址已预取缓存（Esplora mock fixture）

**操作步骤**
1. 打开浏览器访问 `http://localhost:3000`
2. 在首页地址输入框中粘贴合法 BTC 地址 `bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97`
3. 点击"分析"按钮
4. 等待页面跳转到 `/analyze/{judgment_id}`

**预期结果**
- 步骤 3 点击后立即显示 loading skeleton 与进度文案"正在构建子图…"
- 页面 URL 变为 `/analyze/<uuid>` 格式
- 最终 GraphCanvas 渲染出节点数 > 0 的图谱
- VerdictCard 显示四档之一（HIGH/MEDIUM/LOW/NO_MATCH）

---

## FE-02 刷新 /analyze/[id] 不重新发起分析

- **优先级**：P0
- **来源**：§5 轮询协议、§2 路由表

**前置条件**
- FE-01 已完成，当前处于 `/analyze/<judgment_id>` 且状态 completed

**操作步骤**
1. 记录当前 VerdictCard 显示的风险等级
2. 使用 Playwright `page.reload()` 刷新页面
3. 断言网络层：拦截并计数 `POST /addresses/analyze` 请求（应为 0）
4. 断言 DOM：刷新后立即检查 VerdictCard 区域无 loading skeleton 元素

**预期结果**
- `POST /addresses/analyze` 请求数 = 0
- 仅发出 `GET /api/v1/judgments/<judgment_id>` 恢复数据
- VerdictCard 直接渲染与刷新前相同的风险等级，无 loading 态闪现

---

## FE-03 judgment failed 时显示错误卡与重试按钮

- **优先级**：P0
- **来源**：§4 VerdictCard 四态矩阵 failed 行

**前置条件**
- 已登录 investigator（非白名单地址需认证才可提交分析）
- 后端 mock LLM 配置为始终返回无效 evidence（触发 JudgmentValidationError）：`LLM_PROVIDER=mock` + mock 场景脚本设为 `invalid_evidence`

**操作步骤**
1. 在首页输入上述地址并点击分析
2. 等待轮询到终态
3. 观察 VerdictCard 区域

**预期结果**
- VerdictCard 渲染错误提示卡（非 loading/success 样式）
- 卡内显示 `error_code`（如 `LLM_VALIDATION_FAILED`）与人类可读 error_message
- 提供"重试"按钮；点击后以相同地址重新发起 `POST /addresses/analyze` 创建新 judgment（旧 judgment 保持 failed 不变），UI 回到 loading 态

---

## FE-04 no_match 场景正确展示灰色 NO_MATCH 卡

- **优先级**：P1
- **来源**：§4 VerdictCard 四态矩阵 no_match 行

**前置条件**
- 后端 mock LLM 配置为返回 `risk_level="no_match"` + `recommended_action="review"`

**操作步骤**
1. 输入一个普通钱包地址并分析
2. 等待轮询完成
3. 观察 VerdictCard

**预期结果**
- 显示灰色色卡 + ○ 图标
- 文案标注 NO_MATCH
- recommended_action 显示为 "review"
- 不显示 freeze/monitor 按钮

---

## FE-05 evidence 边 ID 点击高亮对应图谱边

- **优先级**：P0
- **来源**：§4 VerdictCard evidence 列表、D3 全局 ID 规范

**前置条件**
- FE-01 完成，VerdictCard 显示 HIGH
- reasoning/evidence 中引用了至少一条 edge ID（如 `edge:addr:...->tx:...`）

**操作步骤**
1. 在 evidence 列表中找到第一条 edge 类型 ID 并记录其完整字符串
2. 点击该 ID
3. 观察 GraphCanvas 画布

**预期结果**
- 对应边在画布上变为青色发光高亮样式
- 若对应元素不在视口内，画布自动平移使其可见
- 其他未匹配节点保持原样式不变

---

## FE-06 四档色卡颜色与图标双编码正确

- **优先级**：P1
- **来源**：§4 VerdictCard a11y 双编码要求

**前置条件**
- 使用 MSW route handler 拦截 `GET /api/v1/judgments/:id`，准备 4 组响应分别返回 high/medium/low/no_match

**操作步骤**
1. 依次切换 MSW mock 场景加载页面
2. Playwright 断言每组的背景色 CSS class 与图标 SVG/DOM 元素同时存在

**预期结果**
- HIGH = 红色背景 + ⚠ 图标
- MEDIUM = 橙色背景 + ● 图标
- LOW = 绿色背景 + ✓ 图标
- NO_MATCH = 灰色背景 + ○ 图标
- 图标元素与颜色 token class 在 DOM 中并存（颜色不是唯一编码维度）

---

## FE-07 登录成功后可创建案件

- **优先级**：P0
- **来源**：§7 验收标准第 6 条

**前置条件**
- 已 seed 用户 inv@test.com / Passw0rd!123（investigator 角色）
- 用户未登录

**操作步骤**
1. 访问 `/login`
2. 输入 email 和密码，点击登录
3. 导航到 `/cases`
4. 点击"新建案件"，填写标题"Test Case #001"
5. 提交表单

**预期结果**
- 登录后跳转回之前页面（或 /cases）
- 新案件出现在列表中且状态为 open（蓝色徽章）
- 网络请求携带 `Authorization: Bearer <token>` header

---

## FE-08 未登录访问 /cases 重定向到 /login

- **优先级**：P0
- **来源**：§2 路由保护 middleware 规则

**前置条件**
- 清除所有 cookie 与内存 token（新隐身窗口）

**操作步骤**
1. 直接访问 `http://localhost:3000/cases`

**预期结果**
- 自动重定向到 `/login?next=/cases`
- 登录成功后跳回 `/cases`

---

## FE-09 /analyze/* 路由豁免登录保护

- **优先级**：P0
- **来源**：§2 "middleware 匹配 `/cases/:path*`"

**前置条件**
- 未登录（无任何认证态）

**操作步骤**
1. 通过首页发起一次匿名分析（演示地址）
2. 进入 `/analyze/<id>` 页面

**预期结果**
- 页面正常加载，不被重定向到 /login
- 图谱与 verdict 正常渲染

---

## FE-10 token 过期自动 refresh 并重放请求

- **优先级**：P1
- **来源**：§3 认证方案 401 处理逻辑

**前置条件**
- 已登录 investigator
- MSW 拦截业务 GET 请求首次返回 401、`POST /auth/refresh` 返回新 token

**操作步骤**
1. 导航到 `/cases`
2. MSW 使首个 `GET /api/v1/cases` 返回 401
3. MSW 使 `POST /auth/refresh` 返回新 access_token
4. 观察重放行为

**预期结果**
- 首个 GET 收到 401
- 自动发出 `POST /auth/refresh`（凭 Cookie）
- 收到新 access_token 后自动重放原 `GET /cases` 请求并成功返回
- 用户无感知，无需手动重新登录

---

## FE-11 refresh 失败清空态并跳转 /login

- **优先级**：P1
- **来源**：§3 认证方案

**前置条件**
- 已登录但 refresh_token Cookie 已被清除（Playwright `context.clearCookies()` 或 DevTools Application 面板删除）
- MSW 使 `POST /auth/refresh` 返回 401

**操作步骤**
1. MSW 使首个业务 GET 请求返回 401 触发 refresh 流程
2. refresh 也返回 401
3. 观察行为

**预期结果**
- refresh 请求失败（401）
- Zustand store 中 accessToken 清空
- 重定向至 `/login`
- localStorage 中不存在任何 token 键值

---

## FE-12 GraphCanvas 200 节点首次交互流畅

- **优先级**：P1
- **来源**：§4 大图性能配置、§7 验收标准第 7 条

**前置条件**
- 注入含 200 节点的子图 mock 数据
- Playwright + CDP tracing 脚本就绪

**操作步骤**
1. 加载 `/analyze/<id>` 使大图渲染完成
2. 启动 CDP trace 采集
3. 执行拖拽平移画布与滚轮缩放
4. 停止采集，解析 trace 数据

**预期结果**
- 拖拽与缩放期间帧率 ≥ 30fps（CDP tracing 解析）
- 交互阶段无长任务 > 100ms（dagre 初始布局计时窗口定义为"从 mount 到首次 fitView 完成"，不纳入交互判定）
- `document.querySelectorAll('.react-flow__node').length` < 总节点数（虚拟化生效）

---

## FE-13 混币器节点红色边框样式

- **优先级**：P2
- **来源**：§4 GraphCanvas 自定义节点类型

**前置条件**
- 子图中包含已知混币器地址节点

**操作步骤**
1. 加载分析结果页
2. 定位混币器节点

**预期结果**
- 该节点有红色边框
- 节点右上角显示 ⚠ 图标

---

## FE-14 stopped_expansion 边虚线样式

- **优先级**：P2
- **来源**：§4 自定义边规则

**前置条件**
- 子图中存在 `is_stopped_expansion=true` 的边

**操作步骤**
1. 加载分析页
2. 找到该边

**预期结果**
- 边呈虚线样式
- 边上有停止图标标记

---

## FE-15 crosschain 边显示桥协议标签

- **优先级**：P2
- **来源**：§4 自定义边规则

**前置条件**
- 存在 `is_crosschain=true` 且 `op_return_protocol="thorchain"` 的边

**操作步骤**
1. 加载分析页
2. 找到跨链边

**预期结果**
- 边旁显示 "thorchain" 文字标签

---

## FE-16 节点点击弹出详情侧栏

- **优先级**：P1
- **来源**：§4 GraphCanvas 交互列表

**前置条件**
- 分析页已渲染完成

**操作步骤**
1. 点击任意地址节点
2. 观察右侧区域

**预期结果**
- 右侧滑出详情侧栏
- 显示节点标签、BTC 余额、script_type
- 再次点击空白处或关闭按钮可收起侧栏

---

## FE-17 按跳数过滤图谱

- **优先级**：P2
- **来源**：§4 GraphCanvas 交互列表

**前置条件**
- 当前子图 hops=3 构建完成

**操作步骤**
1. 在过滤控件中选择 "仅显示 depth ≤ 1"
2. 观察画布变化

**预期结果**
- depth ≥ 2 的节点从视图中消失
- 相关边同时隐藏
- 切回"全部"恢复完整视图

---

## FE-18 patterns 分页筛选同步 URL searchParams

- **优先级**:P2
- **来源**: §5 SWR 用于 /patterns、URL searchParams 同步

**前置条件**
- patterns 表中数据量 ≥ 2×page_size（默认 page_size=20 时即 > 40 条）

**操作步骤**
1. 访问 `/patterns`
2. 切换到第 2 页
3. 选择 evidence_grade=A 筛选
4. 复制当前 URL 到新标签页打开

**预期结果**
- URL 包含 `page=2&evidence_grade=A` 参数（键名精确匹配实现约定）
- 新标签页打开后直接呈现相同的分页与筛选状态

---

## FE-19 cases 状态徽章文字标签

- **优先级**：P2
- **来源**：§4 CaseList 组件

**前置条件**
- 存在 open/investigating/closed 三种状态的案件各一条

**操作步骤**
1. 访问 `/cases`
2. 检查三行的徽章

**预期结果**
- open = 蓝色 + 文字 "Open"
- investigating = 橙色 + 文字 "Investigating"
- closed = 灰色 + 文字 "Closed"
- 徽章不仅靠颜色区分（含文字）

---

## FE-20 报告导出异步轮询下载

- **优先级**：P1
- **来源**：§7 验收标准第 6 条、backend reports 异步契约

**前置条件**
- 已登录且有至少一个关联地址的案件
- worker 进程运行中

**操作步骤**
1. 进入 `/cases/<id>`
2. 选择 format=pdf，点击导出报告
3. 观察网络请求

**预期结果**
- 发出 `POST /cases/<id>/reports?format=pdf` → 收到 202 + report_id
- UI 显示"报告生成中…"进度提示
- 轮询 `GET /reports/<report_id>` 至 completed
- 完成后出现下载按钮，点击可获取 PDF 文件

---

## FE-21 CSRF header 自动附加

- **优先级**：P1
- **来源**：§3 认证方案 X-Requested-With 要求

**前置条件**
- 已登录

**操作步骤**
1. 打开 DevTools → Network
2. 执行任一写操作（创建案件）

**预期结果**
- 请求 headers 中包含 `X-Requested-With: XMLHttpRequest`
- 后端接受该请求（不返回 403 CSRF 错误）

---

## FE-22 移动端只读浏览降级

- **优先级**：P2
- **来源**：§1 MVP 桌面优先声明

**前置条件**
- Chrome DevTools 设备模拟 iPhone 14 Pro 尺寸

**操作步骤**
1. 以移动端视口访问 `/analyze/<id>`
2. 尝试拖拽节点

**预期结果**
- 画布可平移缩放查看
- 节点不可拖拽重排（禁用 draggable）
- 详情侧栏以全屏抽屉形式弹出

---

## FE-23 API client GET 幂等重试

- **优先级**：P2
- **来源**：§6 API Client 幂等 GET 重试

**前置条件**
- MSW route handler 对 `GET /api/v1/judgments/:id` 设置带计数器的响应：第 1 次 502，第 2 次 200

**操作步骤**
1. 访问 `/analyze/<id>` 页面
2. Playwright 监听网络请求

**预期结果**
- 第一次 502 后自动重试
- 第二次成功返回数据
- 用户看到正常渲染而非错误页

---

## FE-24 快速切换地址防竞态

- **优先级**：P1
- **来源**：§6 API Client 取消同类 pending 请求

**前置条件**
- 分析页已打开且正在展示地址 A 的结果（或 loading 中）
- MSW 对 analyze 请求注入延迟使响应可观测

**操作步骤**
1. 地址 A 分析进行中（status=processing）
2. 通过页面内"重新分析"入口修改为地址 B 并提交
3. 等待两个请求都结束

**预期结果**
- 地址 A 的 pending 请求被取消（network 面板显示 cancelled）
- 最终页面只显示地址 B 的分析结果
- 不会出现 A 结果覆盖 B 结果的情况

---

## FE-25 OpenAPI 类型一致性 CI 校验

- **优先级**：P1
- **来源**：§6 TypeScript 类型生成流程

**前置条件**
- backend 运行中暴露 `/openapi.json`
- frontend 项目安装 openapi-typescript

**操作步骤**
1. 运行 `npx openapi-typescript http://localhost:8000/openapi.json -o types/api.d.ts`
2. 运行 `npm run type-check`

**预期结果**
- types/api.d.ts 成功生成且包含 AnalyzeRequest/JudgmentResponse 等类型
- type-check 无类型错误（前端接口调用与后端 schema 对齐）

---

## FE-26 evidence 高亮 useMemo 派生不重建 nodes 数组

- **优先级**：P2
- **来源**：§4 大图性能配置最后一条

**前置条件**
- Jest/RTL 单元测试环境就绪

**操作步骤**
1. 渲染 GraphCanvas 组件并传入 200 节点数据
2. 记录传入 GraphCanvas 的 nodes 数组引用（`useRef` 保存）
3. 模拟点击一条 evidence ID 触发高亮
4. 比较高亮前后 nodes 数组引用

**预期结果**
- 高亮前后 nodes 数组引用相等（`expect(nodesRef.current).toBe(nodesAfter)`）
- 只有受影响的自定义节点组件 re-render（Profiler 作为辅助证据）

---

## FE-27 登出清除内存态

- **优先级**：P1
- **来源**：§3 logout 流程

**前置条件**
- 已登录 investigator

**操作步骤**
1. 点击导航栏登出按钮
2. 观察网络与页面跳转

**预期结果**
- 发出 `POST /auth/logout` 请求
- 内存 accessToken 清空
- 重定向到 `/login`
- Cookie 中 refresh_token 被 Set-Cookie 过期清除

---

## FE-28 轮询超时转 failed UI

- **优先级**：P1
- **来源**：§5 轮询协议终止条件（90s 上限）

**前置条件**
- Mock 后端让 judgment 永远停在 status=processing（不推进）
- 环境变量 `NEXT_PUBLIC_POLL_TIMEOUT_MS=3000`（测试环境缩短为 3s；生产默认 90000ms）

**操作步骤**
1. 发起分析
2. 等待 3 秒以上（测试环境超时阈值）

**预期结果**
- 轮询在第 90 秒停止
- UI 显示错误提示"分析超时，请重试"
- 出现重试按钮

---

## FE-29 轮询指数退避生效

- **优先级**：P2
- **来源**：§5 轮询协议间隔策略

**前置条件**
- Mock 后端连续 3 次 GET /judgments/{id} 返回 500

**操作步骤**
1. 发起分析
2. 观察网络面板轮询请求的时间间隔

**预期结果**
- 第 1 次失败后等待 2s ±500ms 重试
- 第 2 次失败后等待 3s ±500ms
- 第 3 次失败后等待 5s ±500ms（上限）
- 第 4 次成功后退避计数器重置，恢复正常 2s ±200ms 间隔

---

## FE-30 组件卸载取消轮询

- **优先级**：P1
- **来源**：§5 轮询协议组件卸载 AbortController

**前置条件**
- 分析进行中（status=processing）

**操作步骤**
1. 在 `/analyze/<id>` 页面等轮询开始
2. 点击导航栏切到其他路由
3. 观察网络面板

**预期结果**
- 进行中的 fetch 被 abort
- 不再发起新的轮询请求
- 控制台无 unmounted setState 警告

---

## FE-31 a11y aria-label 完整性

- **优先级**：P2
- **来源**：§4 a11y 双编码要求

**前置条件**
- axe-core + Playwright 就绪

**操作步骤**
1. 在首页和分析页运行 axe-core 扫描
2. 对每个 evidence ID 元素执行 `getAttribute('aria-label')` 显式断言非空且含 txid/address 描述
3. 对色卡元素断言 `role="status"` 与 aria-label 属性值

**预期结果**
- 所有可点击 evidence ID 的 aria-label 非空且含对应 txid/address 子串
- 色卡有 role="status" + aria-label（如"风险等级：高"）
- axe-core 无 critical/serious violation

---

## FE-32 错误边界防止整页崩溃

- **优先级**：P2
- **来源**：§4 VerdictCard 四态设计隐含要求

**前置条件**
- MSW route handler 使 `GET /api/v1/judgments/:id` 返回畸形 subgraph JSON（缺 nodes 字段）

**操作步骤**
1. 加载分析页
2. Playwright 断言三要素：占位符元素存在 + 页面根节点仍渲染 + console.error 有 ErrorBoundary 标识

**预期结果**
- GraphCanvas 区域显示"数据格式异常"占位符
- 页面其他区域（导航、VerdictCard）不受影响
- 控制台捕获错误但不白屏


## FE-33 evidence 节点 ID 点击高亮对应图谱节点

- **优先级**:P0
- **来源**：§4 VerdictCard evidence 列表、D3 全局 ID 规范

**前置条件**
- FE-01 完成，VerdictCard 显示 HIGH
- reasoning/evidence 中引用了至少一条 addr 类型节点 ID（如 `addr:bc1q...`）
- fixture 中固定放置一个目标节点使其初始位于视口外

**操作步骤**
1. 在 evidence 列表中找到第一条 addr 类型 ID 并记录其完整字符串
2. 点击该 ID
3. Playwright 截图对比 GraphCanvas 画布

**预期结果**
- 对应节点在画布上变为青色发光高亮样式
- 若节点不在视口内，画布自动 fitView 平移使其可见
- 其他未匹配节点保持原样式不变

---

## FE-34 PatternCompare 双栏差异标注冒烟

- **优先级**:P2
- **来源**：FR-15 PatternCompare 组件

**前置条件**
- patterns 表有 ≥ 1 条 pattern 数据
- 用户已登录 investigator

**操作步骤**
1. 进入 `/patterns/<pattern_id>/compare` 页面
2. 选择一个已分析 judgment 的 subgraph 与该 pattern 对比
3. 观察双栏视图

**预期结果**
- 左栏显示当前分析子图，右栏显示 pattern canonical_subgraph
- 差异节点/边以不同颜色或虚线标注
- 页面不崩溃且有基本布局

---

## FE-35 折叠自环/环路地址流不隐藏折叠节点自身（issue #6）

- **优先级**：P1
- **来源**：GitHub issue #6 —— GraphCanvas 折叠/展开在自环（A→A）与环路（A→B→A）下把折叠节点自身错误隐藏

**前置条件**
- 分析页加载了一个含自环/回流地址的子图（`A → A` 或 `A → B → A`，由自转账/回流地址形成）
- 该子图已渲染 GraphCanvas 地址流式视图

**操作步骤**
1. 找出带自环的出边节点 A（或环路成员 A），点击其右上角 "−" 收起下游
2. 观察画布：确认 A 节点仍可见，且其右上角仍显示 "＋"（展开下游）圆钮
3. 点击 A 的 "＋" 展开下游
4. 对环路中另一成员 B 重复收起，再同时收起 A 与 B

**预期结果**
- 收起 A 后 A 自身不被隐藏，仅严格下游节点（A→B 环中的 B、A→B→A 环中的 B）被隐藏
- 折叠节点仍保留其 "＋/−" 圆钮，可再次展开恢复下游，无需离开当前视图
- 多个节点分别折叠时，隐藏集合取各下游集合并集，且不包含任何折叠节点自身（折叠节点保持可见）
- 现有地址流变换、跳数（depth）过滤、节点拖动和 evidence 高亮行为不变
