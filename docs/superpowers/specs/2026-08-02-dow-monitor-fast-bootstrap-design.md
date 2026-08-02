# 趋势监控快速首屏设计

状态：用户已于 2026-08-02 明确批准，可作为生产实现权威规格。

## 1. 目标与现状

生产页面 `/dow-monitor` 当前必须等待完整 overview 才能得到当前页股票，导致 WebSocket 订阅也被约 56 秒的 overview 阻塞。该 overview 对每只股票、每个周期重复读取约 13 MB 的状态文件，并返回约 6.8 MB 的完整图表状态。通知接口还会每 15 秒重读约 44 MB 的 JSONL，并把大体积 `snapshot_payload` 返回给列表。

本轮目标是让“股票行和实时价格”先出现，稳定指标随后异步补齐；同时保持所有稳定指标、正式信号、15 秒 HTTP 兜底、详情图表和实时数据语义不变。

## 2. 权威边界

本设计细化以下既有权威要求，不覆盖它们：

- `REQ-DOW-MONITOR-LIST-REALTIME-001`：当前页股票继续订阅 `/ws/realtime`；HTTP 稳定字段和通知仍以 15 秒为兜底刷新周期。
- `REQ-DOW-MONITOR-STABLE-DECISION-METRICS-001`、`REQ-DOW-MONITOR-INDICATOR-SIGNAL-BOUNDARY-001`：实时 quote/depth/形成中 1m K 不能改变正式信号或稳定指标。
- `REQ-DOW-MONITOR-LIST-SIGNAL-STABILITY-001`：正式信号只能来自持久化通知或符合要求的完成 K 线预警。
- `REQ-DOW-MONITOR-MOBILE-COMPACT-LIST-001`：移动端首四列和重点解读仍完整可用。
- `REQ-DOW-MONITOR-FRONTEND-VERSION-REFRESH-001`：版本更新提示与自动刷新行为不变。
- `REQ-DOW-MONITOR-INCREMENTAL-TIMEFRAME-EVALUATION-001`：本轮不修改 19912 调度、分钟决策和状态生成。

## 3. 采用方案

采用“轻量启动清单 + 轻量列表摘要 + 按需详情”的三层读取模型。

### 3.1 启动清单

页面立即请求现有 `/api/dow-monitor/symbols`。它只返回代码、市场、启用状态和更新时间，用来完成市场过滤、分页，并立即为当前页建立 WebSocket 订阅。

在列表摘要到达前：

- 行内先显示股票代码；名称暂以代码占位；
- 实时价格、涨跌幅、mini 趋势可由 WebSocket 到达后显示；
- 稳定指标和重点解读明确显示“指标加载中”，不得用零值或实时字段伪装；
- 信号筛选依赖稳定摘要，摘要未到时显示加载态，不能错误判为“无信号”。

### 3.2 轻量列表摘要

新增向后兼容接口：

`GET /api/dow-monitor/list-overview?market=cn|hk|us|all`

返回现有列表真正需要的元数据、分钟决策、资金、最新 AI 摘要、最新通知摘要，以及经过裁剪的周期状态。旧 `/api/dow-monitor/overview` 保留，避免破坏未知调用方；但其内部也改为一次批量读取状态后按 `(symbol, timeframe)` 建索引，禁止循环调用 `get_state()`。

周期状态裁剪规则固定为：

- `5m`：保留最新交易日全部 bars，以及 turning signals；用于日内 mini 趋势、5m 动量、60 分钟区间和重点解读；
- `15m`：保留末尾最多 16 根 bars，以及 snapshot、freshness 和 turning signals；用于完成 K 过滤、15m 动量、通道、ATR14、控制线和量比；
- `30m`：保留末尾最多 2 根 bars，以及 snapshot、freshness 和 turning signals；用于完成 K 过滤、通道、控制线和量比；
- `60m`、`day`：保留 snapshot、freshness 和 turning signals，不返回 bars；用于既有预警选择；
- 列表摘要不返回 lines、普通 signals、longTerm、headShoulders 等详情图表字段。

裁剪必须保持当前 `deriveMonitorRow()` 与 `deriveInterpretationMarketContext()` 对同一完整状态的列表语义等价。任何无法证明等价的字段必须保留，不能为了减包而改变指标结果。

### 3.3 通知摘要

新增向后兼容接口：

`GET /api/dow-monitor/notification-summaries?market=...&unreadOnly=...`

摘要仅返回：`notification_id`、`event_key`、`symbol`、`market`、`timeframe`、`side`、`action_name`、`shape_name`、`triggered_at`、`available_at`、`trigger_price`、`category`、`read_at`。列表不再获取 `snapshot_payload`、`prompt_text` 和 `evidence_text`。

前端仍每 15 秒刷新该轻量摘要，满足正式信号和断线兜底要求；不再轮询大体积完整通知列表。完整通知和单条读取能力保留给详情或兼容调用方。

通知存储增加文件签名检查。JSONL 的大小和最后修改时间未变化时，列表调用复用内存模型；文件变化时才重载。追加通知或已读回执后必须同步更新内存和文件签名，不能造成正式信号延迟。

### 3.4 详情按需加载

点击股票后继续通过现有 `GET /api/dow-monitor/{symbol}?timeframe=...` 获取完整周期状态。列表摘要不得携带完整 K 线、趋势线、形态覆盖层或大通知快照。

## 4. 前端数据流

1. 路由加载后并行请求 symbols、list-overview、notification-summaries 和 status。
2. symbols 到达后立即构造当前市场、当前页的 20 只股票并建立 WebSocket 订阅，不等待其它请求。
3. WebSocket quote/depth/candlestick 按既有最大每秒一次 React 可见快照更新实时字段。
4. list-overview 到达后，以标准化 symbol 为键合并名称、稳定周期、资金、分钟决策、AI 摘要和状态时效。
5. notification-summaries 到达后更新正式信号和信号筛选。
6. list-overview 或 notification-summaries 失败时保留实时行，稳定区域显示失败/延迟，不清除上一次有效稳定结果。
7. 新增、删除、启停股票后同时失效 symbols 和 list-overview；通知已读后失效通知摘要和 list-overview。

## 5. 稳定要求

### REQ-DOW-MONITOR-FAST-BOOTSTRAP-001

当前页 WebSocket 订阅必须只依赖轻量 symbols 清单，不得等待 list-overview、通知、状态或 AI 请求。稳定摘要尚未到达时，UI 必须显示明确加载态且不得生成新信号。

### REQ-DOW-MONITOR-LIGHTWEIGHT-LIST-OVERVIEW-001

列表必须使用专用轻量摘要。服务端生成一次响应最多批量读取一次状态集合，并按键索引；不得按股票/周期重复解析状态文件。裁剪后的列表展示和重点解读必须与完整状态输入语义等价。

### REQ-DOW-MONITOR-NOTIFICATION-SUMMARY-001

列表通知轮询必须使用不含大快照和长文本的摘要 DTO，并保持 15 秒正式信号兜底。通知文件未变化时不得重新解析整个 JSONL；变化后的正式通知必须在下一次刷新中可见。

### REQ-DOW-MONITOR-STARTUP-PERFORMANCE-001

在 20 只股票的验收夹具中：

- list-overview JSON 响应不超过 1 MB；
- notification-summaries 的 100 条响应不超过 256 KB；
- list-overview 的单次请求中状态集合读取次数不超过 1；
- 自动化浏览器证据必须证明 WebSocket subscribe 在 list-overview 完成前发出；
- 10.28 候选端口验收时，symbols TTFB 不超过 2 秒，健康 WebSocket 的首个 quote 不超过订阅后 3 秒，list-overview TTFB 不超过 3 秒。

网络或上游不健康时，最后三项性能门槛可以阻止发布，但不得通过延长超时、伪造缓存数据或改变信号口径绕过。

## 6. 测试与证据

实现必须先添加失败测试，再写生产代码：

- 后端 API：轻量字段契约、状态只读一次、状态裁剪边界、通知摘要字段和未变化文件不重载；
- 后端语义：完整状态与裁剪状态经过列表派生后的通道、动量、ATR、量比、预警和重点解读一致；
- 前端 hooks：四个请求并行、15 秒轻量兜底、mutation 正确失效缓存；
- 页面：symbols 先到时立即订阅并渲染实时价格，overview 后到时稳定字段补齐，失败时不清除旧稳定数据；
- payload 合约：20 只股票和 100 条通知满足字节上限；
- 浏览器：记录请求时间线、首行、首个 quote、稳定指标补齐时间和详情按需请求。

语义验收写入 `docs/acceptance/dow-monitor-fast-bootstrap.md`，独立需求证据复核写入 `docs/reviews/2026-08-02-dow-monitor-fast-bootstrap-review.md`。所有新要求在实现前登记到 `docs/spec-index.yaml`，实现、测试和证据在 `docs/traceability.yaml` 中逐项映射。

## 7. 发布与回滚

发布仍使用 10.28 的不可变候选镜像和候选端口验收。不得重启 19912、行情 WebSocket 或独立 AI worker。候选验证通过后只切换 3018。

回滚恢复上一个 3018 镜像；新增只读接口和内存缓存不迁移持久数据，也不删除通知、状态、分钟结果或 AI 报告。

发布后同步更新 `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md`，记录新接口、首屏加载顺序、性能门槛和验证步骤。

## 8. 非目标

- 不修改道氏计算、分钟决策、正式买卖信号、异常高亮或重点解读规则；
- 不修改 AI 小时报告内容与调度；
- 不把稳定指标迁移到 WebSocket 计算；
- 不删除旧 overview、完整通知或详情接口；
- 本轮不引入 Redis、ClickHouse 查询替代状态文件，也不调整非交易时段调度。
