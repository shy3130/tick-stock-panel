# Vibe-Trading 前端移植专项 — 设计文档

> 状态：已通过设计评审（2026-07-04）。本文档是**程序级分解**，拆成 3 个互相独立的子项目，各自走 spec→plan→实现 循环。样式风格一律对齐现有 panel（卡片/表格/图表/空错载状态复用现有组件与 Tailwind token）。

## 背景

从 `../Vibe-Trading` 移植过来的能力大多落在**后端层**，用户可见入口缺失或不完整。经排查，真正需要"前端移植 + 少量后端补齐"的是三块：

- **P3 组合优化器**：`backend/app/backtest/optimizers.py::portfolio_weights` 已就绪，但**只在回测引擎内部调用**（`engine.py:1120`），无独立 API、无 UI。
- **P7 Agent 对话**：`backend/app/api/agent.py` 已有 `/chat`，但**单轮/无状态/单工具/非流式**；前端零消费。
- **C1 Trade Journal 完备**：后端 `app/services/trade_journal/`（fifo/diagnose/benchmark/parser/store）**相当完整**，前端 `pages/TradeJournal.tsx` 已展示大部分；缺"剩余诊断维度露出"和"影子账户/抽规则"（后端也无）。

用户已选定三者均做**完备版**（P7、C1 含后端增强）。

## 构建顺序

**P3（最小、独立、最快见效）→ P7 完备版 → C1 完备版。** 三者无代码耦合，可并行分派，但优先级如上。

---

## 子项目 1 — P3 组合优化器

**目标：** 独立"组合优化器"工具页：选一组标的 + 优化方法 → 算并展示权重与组合统计。

### 后端（薄端点，新增）
- `POST /api/backtest/optimize`（加入 `app/api/backtest.py`）。
- 入参：`{ symbols: string[], method: str, lookback_days: int = 120, strategy_id?: str }`；`method ∈ {equal, equal_vol, risk_parity, mean_variance, max_diversification, score_weight}`。
- 逻辑（**codex review M3 修订**）：新增一个 **backtest 层小 helper**，复用 `BacktestEngine.load_panel(symbols, start, end, [symbol,date,close])`（`engine.py:166-230`）取面板 → 生成 returns matrix → `portfolio_weights(returns, method, scores?)`。**不复用** `simulate_portfolio` 内部的 `_candidate_returns` 闭包（`engine.py:853-863`，不可直接调用），**不另写 parquet scan**。
- `score_weight` 的 scores（**codex review M4 修订**）：无策略上下文（独立优化器）→ 用"近 lookback 日累计动量"作默认打分；**当传入 `strategy_id`（从策略池导入 symbols）→ 可选复用该策略的 `StrategyBacktestService._apply_score()`**（`strategy.py:668-723`，支持 `meta.scoring` 多字段横截面归一，内置策略常用 momentum_60d/momentum_20d/change_pct/vol_ratio_5d/turnover_rate）。
- 出参：`{ weights: [{symbol, name, weight}], stats: { annualized_vol, diversification_ratio, n }, method, lookback_days }`。stats 由 weights+cov 现算。
- 边界：标的 < 2、数据不足、全 NaN → 回退等权并在 `meta.note` 标注。

### 前端（新页）
- 新路由 `/optimizer`（`router.tsx`）+ 菜单项"组合优化"（`Layout.tsx` nav，icon 用 `PieChart`/`Scale`）。
- 组件 `pages/Optimizer.tsx`：
  - 标的选择：复用现有标的搜索组件；提供"从策略池导入"按钮拉某策略选中标的。
  - 方法下拉（6 项，带 tooltip 说明）+ 回看天数输入。
  - "计算权重"按钮 → `api.optimize(...)`。
  - 结果：权重表（symbol/name/weight%）+ ECharts 环形图 + 组合统计卡。
  - 空/错/载状态套现有页面范式。
- `api.ts` 加 `optimize()` 类型与调用。

### 验证
`pnpm tsc` 全绿；node 脚本打 `/api/backtest/optimize` 验 5 种方法权重和≈1、非负；手测页面。

---

## 子项目 2 — P7 Agent 对话（完备版）

**目标：** 全新 AI 助手对话页，支持多轮、流式、多轮工具循环，复用多 AI 配置。

### 后端（增强 `app/api/agent.py` + agent 循环）
- **多轮**：前端每轮发完整 `messages: [{role, content}]` 历史；后端无状态，不建 server session。
- **流式（codex review H2 修订 — 关键）**：用 **`POST /api/agent/stream` + NDJSON body**，**不用 GET SSE**。理由：现有 GET SSE（`/api/backtest/strategy/stream`，`backtest.py:478-624`）是 query 参数驱动，把 `messages` 历史塞 query 有长度/编码/隐私问题；而现有 AI 流式端点本就是 **POST + NDJSON**（`stock_analysis.py:155-176`、`market_recap.py:61-68`、`financials.py:175-199`），天然适合带 `messages/profile_id`。**对齐这套范式**。底层复用已有 `stream_ai_text`（OpenAI 真流式，Codex/ACP 退化为整块）。NDJSON 行类型：`{type:'text', delta}` / `{type:'tool_call', name, args}` / `{type:'tool_result', name, result}` / `{type:'done'}` / `{type:'error', msg}`。
- **多轮工具循环**：模型可反复请求工具 → 执行（`agent_tools.call_tool`）→ 回喂结果 → 继续，直到出答案或达上限 **5 次**（防跑飞）。现有 7 个工具**全只读**，循环安全，v1 无需权限门。**注意（codex review L6）**：`run_backtest` 虽 read_only 但是重计算，agent 循环里给它保留参数/耗时上限，避免 5 轮里反复触发重回测。
- 保留 `profile_id`（多 AI 分派）。
- 旧 `/chat` 保留兼容或标记 deprecated。

### 前端（新页）
- 新路由 `/agent` + 菜单项"AI 助手"（icon `Bot`/`Sparkles`）。
- 组件 `pages/Agent.tsx` + 子组件（消息列表、工具调用卡、输入栏）：
  - 消息气泡（user/assistant），assistant 流式打字。
  - 工具调用渲染为可折叠卡片：工具名 + 参数 + 结果 JSON，套现有代码/数据块样式。
  - 输入框 + 发送；复用 `AiProviderSelector`（`entry="agent"`）。
  - 会话本地持久化（复用 `lib/aiReportStore.ts`/`stockAnalysisStore.ts` 的 localStorage 模式）；"清空会话"。
  - **流式消费用 `fetch` + `ReadableStream` 读 NDJSON**（复用个股分析/复盘那套 NDJSON stream helper），**不用 `EventSource`**（因后端改为 POST NDJSON）。每轮把完整 `messages[]` 历史 POST 过去。

### 验证
`pnpm tsc` 全绿；curl `POST /api/agent/stream` 验 NDJSON 行序列（text/tool_call/tool_result/done）；手测多轮 + 工具循环 + 多 AI 切换。

---

## 子项目 3 — C1 Trade Journal 完备（含影子账户 / 抽规则）

**目标：** 补全行为诊断展示；新增"从盈利回合抽个人规则 + 影子回测 + 今日信号"闭环。

### ⚠️ 隐私红线（必须遵守 — codex 已核实现有代码如何保证）
- **行为诊断纯统计，不经 LLM。**（`diagnose()` 纯统计，`upload_journal` 直接调，`diagnose.py:9-60`、`trade_journal.py:107-110`。）
- **`methodology_context` 不得持久化进 ledger。**（上传响应可带，落盘剔除，`trade_journal.py:119-124`；`GET /ledger` 挂载但不回写，`:128-138`；测试 `test_trade_journal.py:61-67` 已断言。）
- **混合 LLM 只发送聚合数字，不发原始成交明细。**
- **新 `shadow.py` 约束：原始 fills 持久化在 `source.json`（`store.py:10-47`），不在 ledger；shadow.py 读 `source.json`，绝不把 fills 塞回 ledger。**

### 后端
- **诊断展示丰富化（codex review H1 修订）**：`diagnose.py` 实际只算 **4 类** 诊断——处置效应 disposition / 过度交易 overtrading / 追涨买入 chasing / 浮亏加仓 anchoring（`diagnose.py:35-60`），且前端 **已全部露出**（`TradeJournal.tsx:207-216`）。所以第一阶段**不是"补露维度"**，而是**丰富现有 4 类诊断的展示细节/解释/触发明细**（如展开每类的判定依据、涉及的具体 roundtrip 列表、阈值说明）。若要新增诊断维度（如锚定卖出、赌徒谬误等）属于 diagnose.py 的**后端扩展**，单列。
- **`app/services/trade_journal/shadow.py`（新）**：从盈利 roundtrip **纯统计/启发式**抽 if-then 个人规则（如"持仓 ≤N 天止盈胜率高""某入场形态"），**读原始 fills 用 `store.py` 的 `source.json`**（`store.py:10-47`，fills 持久化在此，不在 ledger），本地独立 rule store。**不经 LLM。不把 fills 塞回 ledger。**
- **影子回测（codex review M5 修订）**：**不能**直接调 `StrategyBacktestService.run()`（它依赖 `strategy_id` + StrategyEngine 策略定义，`strategy.py:96-106`）。撮合层复用 `BacktestEngine.simulate_portfolio()`，但需**新写一层把个人规则编译成 entry/exit mask**（参考 `custom_signals.py:123-181` 的条件编译）。与真实交易做 delta-PnL 归因。
- **今日信号扫描**：规则 vs 今日市场 → 命中标的。
- 新端点：`POST /api/trade_journal/rules/extract`、`GET /api/trade_journal/rules`、`POST /api/trade_journal/shadow/backtest`、`GET /api/trade_journal/shadow/signals`。

### 前端（扩 `pages/TradeJournal.tsx`）
- 现有 4 类诊断卡（处置效应/过度交易/追涨买入/浮亏加仓，`TradeJournal.tsx:207-216`）**丰富展示**：展开判定依据、涉及 roundtrip 明细、阈值说明。
- 新增"我的规则"区：规则列表 + 影子 delta-PnL 对比图（ECharts）+ 今日信号命中列表。
- 排版/图表打磨，对齐现有卡片风格。

### 分阶段（本子项目内部）
1. 诊断展示丰富化（纯前端小改，4 类现有诊断加明细/解释）。
2. 规则提取（后端 shadow.py 读 source.json + 独立 rule store + 前端规则区）。
3. 影子回测（新规则→entry/exit mask 层 + simulate_portfolio）+ 今日信号（后端 + 前端）。

### 验证
`pnpm tsc` + 后端 pytest（规则提取/影子回测纯统计单测，含红线断言：ledger 不含 methodology_context）；手测上传→诊断→规则→影子闭环。

---

## 跨子项目约定
- 样式：一律复用现有组件（PageHeader、卡片、表格、DatePicker、ECharts 封装）与 Tailwind token，不引新 UI 风格。
- 后端改动走 `data_providers`/现有 service 边界，不破坏 capability gate。
- 前端服务路径注意：dev 用 `:3011`（Vite HMR），LAN 生产版 `:8000` 需 `pnpm build` 重建 dist（见 frontend-two-serving-paths 备忘）。
- commit 需用户授权；永不 push。

## 跨子项目坑（codex review 汇总）
1. **P7 先定接口形态**：POST NDJSON（本 spec 已定），不用 GET SSE，否则前端会返工。
2. **C1 store 隔离**：规则/影子账户用独立 store，不污染策略定义或 ledger；fills 一律读 `source.json`。
3. **P3 returns matrix helper 放 backtest 层**：复用 `BacktestEngine.load_panel`，不另扫 parquet，不碰 `simulate_portfolio` 内部闭包。
