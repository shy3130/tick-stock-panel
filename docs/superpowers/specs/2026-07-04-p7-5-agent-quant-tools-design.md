# P7.5 Agent 量化工具组 — 设计文档

> 状态：panel 3 二次复核 **Approve**（2026-07-04），可转 writing-plans。

## 背景

`docs/vibe-agent-page-gap-assessment.md` 评估了 `../Vibe-Trading` `/agent` 页面相对本项目当前 P7 Agent Chat MVP 的功能缺口，并按依赖顺序拆成 P7.1~P7.7 若干子项目。用户随后给出该页面的实际截图（`Vibe-Trading` 欢迎屏五大类示例：多市场回测/研究与分析/群体智能团队/文档与网络研究/影子账户），要求尽可能多支持。

经与用户逐项对齐范围：
- **排除**（不纳入任何轮次）：期权希腊字母分析（A股场内期权覆盖窄，价值存疑）、交易连接器（含只读查询，实盘券商对接，敏感边界）、投委会评审（多头vs空头多 persona 辩论，机制全新且成本高）。
- **不重复规划**（已有归属）：影子账户 = C1 Trade Journal Phase 2+3（`docs/superpowers/specs/2026-07-04-vibe-frontend-porting-design.md` 子项目3）；WelcomeScreen/Markdown渲染/导出/重试/Session地基/长任务进度/附件上传 = 已有 gap-assessment 文档里的 P7.1~P7.4。
- **本轮（P7.5）**：截图"多市场回测"+"群体智能团队→量化策略工作台"两类背后的量化能力——回测、组合优化、因子分析——包装成 agent tool。不做真正的 Swarm 框架；"工作台"编排效果由现有 `agent_loop.py` 的多轮工具循环自然实现。

P7.5 不依赖 P7.1~P7.4 中任何一个（不需要 session、不需要附件、不需要新流式事件类型），可最先独立交付。

**并行状态说明**：P7.1（WelcomeScreen/Markdown/导出/重试）、P7.2（session 持久化 `agent_sessions.py`）、P7.3（最小 attempt 取消闭环）目前正由另一条工作线直接推进实现，尚未经过本文档这一套 spec review 和独立验收流程。P7.5 的设计和范围裁剪不依赖其结果是否最终定型，但 `compose_factor_score`/`run_backtest` 等新工具落地时，应确认届时 `agent_loop.py`/`agent.py` 的接口形状（如 `session_id` 是否已成为必填参数）未发生本文档未预期的破坏性变化。

## 现状盘点

| 能力 | 现状 | 证据 |
|---|---|---|
| 回测执行 | `agent_tools.py` 已有 `run_backtest` 实现，但被 `agent_loop.py` 的 `_EXCLUDED_TOOLS` 排除（无成本闸门） | `backend/app/services/agent_tools.py:109-126`、`backend/app/services/agent_loop.py:12` |
| 组合优化 | P3 已交付 `POST /api/backtest/optimize`，未接入 agent tool | `backend/app/api/backtest.py`（`OptimizeRequest`/`/optimize`）、`backend/app/backtest/portfolio.py` |
| 单因子 IC/IR 分析 | `FactorBacktestService.run(FactorConfig)` 已完整实现（IC分析+分层回测+多空组合），有 API `POST /factor/run`，未接入 agent tool | `backend/app/backtest/factor.py:70-116`、`backend/app/api/backtest.py:241-260` |
| 多因子逐个对比 | `POST /factors/compare`（`factor_ids: list, max_length=20`），逐个跑 `FactorBacktestService`，不合成，未接入 agent tool | `backend/app/api/backtest.py:220-320` |
| 多因子 IC 加权合成打分 | **不存在**。截图"多因子 Alpha 模型…IC加权因子合成"对应的是一个新算法 | 全仓库搜索无 composite/combine 相关逻辑 |
| Screener pool 参数 | `pool: list[str] \| None`，**不是命名预设**，`None` 即全市场扫描、无内建上限 | `backend/app/services/screener.py:390-482` |
| Agent 工具循环 | `agent_loop.py` 已支持每次请求最多 `MAX_TOOL_ROUNDS=5` 轮工具调用（同步执行、无并发） | `backend/app/services/agent_loop.py:10, 38-85` |
| Agent 会话状态 | `backend/app/services/agent_sessions.py`（panel 3 另一线并行交付，本文档撰写时才发现，尚未经我独立验收）已提供文件级 session 持久化（`sessions.json` 索引 + 按 session 存消息），`POST /api/agent/stream` 可选传 `session_id` 接入。但消息记录只有 `{role, content}`，**没有工具调用计数字段**，不能开箱即用地做"某工具本会话已调用几次" | `backend/app/services/agent_sessions.py:45-125` |

## 范围：4 个 agent tool

全部只读计算，不持久化任何策略/规则定义，不新增流式事件类型（沿用现有 `tool_call`/`tool_result`/`delta`/`done`/`error`）。**若请求带 `session_id`（P7.3 并行工作线新增），流的第一条事件可能是 `attempt_start`，用户主动停止时可能出现 `cancelled`；本文档描述的 4 个新工具不需要感知或携带 attempt/session 相关字段，但实现/测试时不能假设流的第一条事件必然是 `tool_call`。**

### 1. `run_backtest`（重新开放 + 加安全闸门）

**现状**：`agent_tools.py:109-126` 已实现，接受 `strategy_id`（必填）、可选 `symbols`/`start`/`end`。当前 `symbols` 可省略（跑该策略默认股票池，可能是全市场），故被排除在 agent 白名单外。

**改动**：
- 从 `agent_loop.py:12` 的 `_EXCLUDED_TOOLS` 移除 `run_backtest`。
- 新增闸门（在 `call_tool` 的 `run_backtest` 分支内校验，通不过则 `raise ValueError`，与现有错误处理路径一致——`agent_loop.py` 会捕获并转成 `tool_result.result.error`，不会中断整个对话）：
  - `symbols` 必须非空列表（不再允许省略）。
  - `len(symbols) <= 20`。
  - `(end - start).days <= 365`（默认区间仍是 180 天不变）。
- **不做跨请求调用次数限制**（已与用户确认此为有意选择：即便 `agent_sessions.py` 已提供 session 持久化，其消息记录只有 `{role, content}`，没有工具调用计数字段，新增该计数是额外范围；为保持 P7.5 最小化，闸门只依赖单次请求内 `MAX_TOOL_ROUNDS=5` 的自然限流，以及上述 symbols/日期上限。若后续需要跨请求限流，应作为独立改动基于 `agent_sessions.py` 扩展，不在本次范围内）。

### 2. `optimize_portfolio`（新工具，零新后端逻辑）

直接复用 P3 已交付的 `portfolio.py::load_price_matrix` / `returns_from_prices` / `portfolio_weights` 与 `app/api/backtest.py` 里 `POST /optimize` 端点的处理逻辑。

**⚠️ 修正（panel 3 评审 Medium-1，已verify属实）**：`OptimizeRequest.symbols` 只有 `Field(..., min_length=1)`，**没有 max_length**；`load_price_matrix` 是逐 symbol 循环调用 `repo.get_daily_asset`（`backend/app/backtest/portfolio.py:9-20`），symbols 越多、I/O 次数越多，不是"协方差计算所以天然便宜"。agent tool 层必须**自己加上限**，不能假设 API 层已经约束住。

- 入参：`symbols: list[str]`（**非空，且 `len(symbols) <= 50`，agent tool 层新增此约束**）、`method: Literal[...6种...]`、`lookback_days: int = 120`（复用 `OptimizeRequest` 已有的 20-1000 校验范围，这部分不变）。
- 出参：`{weights, stats, method, lookback_days, meta}`，与 `/optimize` 端点返回体一致。
- 复用现有 `len(kept)<2 → 错误`、`common trading days 不足 → 错误` 的边界保护（P3 已实现并测试过）。

### 3. `analyze_factor` + `compare_factors`（新工具，零新后端逻辑）

分别包装现有两个端点背后的逻辑：

- `analyze_factor`：对应 `POST /factor/run`。入参 `factor_name/symbols/start/end/n_groups/rebalance/weight`（`FactorConfig` 字段），出参 `FactorResult`（`ic_mean/ic_std/ir/ic_win_rate/group_stats/long_short_stats/...`）。因子范围：不限于 Alpha Zoo，任何 `FACTOR_COLUMNS` 里存在（或可由 `_compute_missing_factor` 从基础行情算出）的因子列都可以，包括 `momentum_20d`/`rsi_14`/`macd_hist` 等基础因子。
- `compare_factors`：对应 `POST /factors/compare`。入参 `factor_ids: list[str]`（复用现有 `max_length=20` 约束）+ 其余 `FactorConfig` 公共字段，逐个跑 `FactorBacktestService` 后汇总返回。**因子范围仅限 `factor_zoo.ALPHAS`**（`backend/app/api/backtest.py:277-282` 的现有校验 `if x not in ALPHAS`），**不认** `FACTOR_COLUMNS` 里的 `momentum_20d`/`rsi_14` 等基础因子——这是现有 `/factors/compare` 本身的既有限制，agent tool 直接透传即可，不是本次要修的 bug；若未来要支持基础因子对比，属于对 `/factors/compare` 本身的改造，不在 P7.5 范围内。

**⚠️ 修正（panel 3 评审 High-1，已verify属实）**：`FactorBacktestRequest.symbols`/`FactorCompareRequest.symbols` 都是 `list[str] | None = None`（`backend/app/api/backtest.py:220-223, 232-235`），**现有 API 并不要求 symbols 非空**——省略即视为全市场（仅在你显式传入且超过 `FACTOR_MAX_SYMBOLS=1000` 时才报错，这个上限对 agent 工具毫无意义）。原文档"现有 API 已经这样要求，agent tool 层直接透传该约束"的说法是错的。

- 两个工具的 `symbols` 参数在 agent tool 层**必须自己要求非空**，且 `len(symbols) <= 50`（与 `optimize_portfolio` 保持一致的上限），不能依赖/透传现有 API 的（不存在的）约束。

### 4. `compose_factor_score`（新算法）

截图"多因子Alpha模型…对300只股票进行IC加权因子合成"对应的功能。**这是 P7.5 唯一需要新写的后端逻辑**，落在 `backend/app/backtest/factor.py`（与 `FactorBacktestService`/`FACTOR_COLUMNS`/`ALPHAS` 同一模块，复用其 IC 计算能力）。

**入参**：
- `factor_ids: list[str]`（1~20个，因子 id 需在 `FACTOR_COLUMNS` 或 `factor_zoo.ALPHAS` 中存在——注意这比 `compare_factors` 的范围更宽，因为 `compose_factor_score` 是新写的算法，不受 `/factors/compare` 现有"仅 Alpha Zoo"限制约束）
- `pool: list[str]`（**必填**，`1 <= len(pool) <= 300`；为空/省略直接 `ValueError`——已与用户确认 `pool` 在本代码库里只是普通 symbol 列表，不是命名预设，`None` 等于无限制全市场扫描，`compose_factor_score` 比 `run_screener` 单纯列筛选重得多，必须显式加上限）
- `as_of: date`（打分日，默认今天）
- `lookback_days: int = 120`（用于计算各因子历史 IC 的回看窗口，`start = as_of - lookback_days`）
- `top_n: int = 50`（返回排序后的前 N 只；夹到 `1 <= top_n <= len(pool)`，超出直接钳到边界值而非报错——避免负数/超大值这类无意义入参，panel 3 提醒）

**⚠️ 修正（panel 3 评审 Medium-3，已verify属实）**：`FactorBacktestService.run()` 不只算 IC——固定还会跑分层回测（`_add_groups`/`_calc_group_nav`/`_calc_group_stats`）和多空组合（`_calc_long_short`）（`backend/app/backtest/factor.py:194-207`），每个 `factor_id` 跑一次这个完整流程再丢弃分层/多空结果、只取 `ic_mean`/`ic_std`，对 `factor_ids<=20` 的 agent 工具偏重。**已决定：在 `FactorBacktestService` 里新增一个轻量 `compute_ic_only(config) -> {ic_mean, ic_std, ir, ic_win_rate, error}` 方法**，只跑到"1. IC 分析"这一步，跳过分层和多空。

**实现要点（panel 3 二次确认，2026-07-04）**：IC 分析之前的准备步骤——加载 panel、补算缺失因子列、过滤有效行、计算 `_next_return`——已经被封装在私有方法 `_load_factor_panel()`（`backend/app/backtest/factor.py:248-270`）里，分层/多空是在这之后才发生（`:194-207`）。所以 `compute_ic_only()` 最省事的实现就是直接复用 `_load_factor_panel()` 拿到处理好的 panel，再调 `_calc_ic()`，不需要重复造轮子，也不存在"跳过分层步骤会漏掉某个隐藏前置依赖"的风险（分层/多空在数据管道上完全是 IC 之后的下游）。`compose_factor_score` 改为调用这个新方法而不是完整 `run()`。这是本 spec 除 `compose_factor_score` 主算法外唯一的第二处新增后端代码。

**算法**：
1. 对每个 `factor_id`，构造 `FactorConfig(factor_name=factor_id, symbols=pool, start=as_of-lookback_days, end=as_of, rebalance="daily")`，跑 `FactorBacktestService(engine).compute_ic_only(config)`（新方法，见上）得到 `{ic_mean, ic_std, ir, ic_win_rate, error}`。
   - 若 `error` 非空，或 `ic_mean is None`，或 `ic_std` 为 `None`/`0`：该因子记为"数据不足，已剔除"，跳过，不参与合成（不是整体报错）。
2. 对剩余每个因子，计算 `ir = ic_mean / ic_std`；`raw_weight = abs(ir)`；若该因子 `ic_mean < 0`，记 `sign = -1`（用于第4步翻转因子原始值方向），否则 `sign = 1`。
3. 归一化权重：`weight_i = raw_weight_i / sum(raw_weight)`；若 `sum(raw_weight) == 0`（所有因子 IR 都是0，理论边界情况），退化为等权 `1/n`，并在返回体的 `meta.note` 里标注"IC全为0，已退化为等权"。
4. 确定实际打分日：若 `as_of` 在 pool 的行情数据里没有对应行（非交易日，或当日数据尚未同步），**回退到 `<= as_of` 的最新可用交易日**（在已加载的 panel 内直接按日期列取 `max(date) where date <= as_of`，不新增额外数据查询）；若 pool 在整个 `[as_of-lookback_days, as_of]` 区间内都没有任何可用交易日，返回 `error: "所选股票池在该日期范围内无可用行情数据"`。
5. 用 `factor_zoo.compute_factor(panel, factor_id)`（或 `FACTOR_COLUMNS` 里的现成列）取第4步确定的实际打分日每支股票的因子原始值 → 按 `sign_i` 翻转 → 做**截面 rank 归一化**：用 `factor.py::_rank_average`（返回 `1..n`）算出 `rank` 后，归一化为 `(rank - 1) / (n - 1)`（映射到 `[0,1]`；**注意不是 `rank/(n-1)`——那样最大值会是 `n/(n-1) > 1`，是 panel 3 评审 High-2 指出的公式错误，已修正**）；若当天存活股票数 `n==1`，该因子在该截面下所有股票 rank 值固定取 `0.5`（无法排序，视为中性）→ 乘以 `weight_i` → 对所有存活因子求和，得到每支股票的 composite score。
6. 按 composite score 降序排序，返回前 `top_n` 只：`{symbol, name, composite_score, per_factor: {factor_id: {weight, ic_mean, rank_normalized_value}}}`；以及 `meta: {used_factors, excluded_factors: [{factor_id, reason}], pool_size, as_of, scored_date}`（`scored_date` 是第4步实际使用的打分日，可能不等于请求的 `as_of`）。

**边界**：
- `factor_ids` 中含未知 id（不在 `FACTOR_COLUMNS` 或 `factor_zoo.ALPHAS` 中）→ 直接 `ValueError: unknown factor: <id>`，与 `/factors/compare`（`backend/app/api/backtest.py:280-282`）现有校验行为对齐，不静默跳过。
- `factor_ids` 全部因数据不足被剔除 → 返回 `error: "所有因子均无法计算，无法合成"`（不返回空 composite 列表冒充结果）。
- `pool` 中部分股票在实际打分日缺数据 → 该股票直接从结果里剔除，不参与排名，计入 `meta.uncovered_symbols`。
- `pool` 整体在请求的日期范围内无可用数据 → 见上面算法第4步的 error。

## 工作台编排（不新建框架）

截图"量化策略工作台：筛选→因子研究→回测→风控审计流水线"不通过新建 Swarm/pipeline 框架实现，而是依赖：
- 现有 `run_screener` 工具（筛选）
- 本次新增的 `analyze_factor`/`compose_factor_score`（因子研究）
- `optimize_portfolio`（组合构建）
- 重新开放的 `run_backtest`（回测验证）

这 4+1 个工具都在 `agent_loop.py` 现有的 `ALLOWED_AGENT_TOOLS` 白名单和 `MAX_TOOL_ROUNDS=5` 循环内，由模型自主决定调用顺序和参数传递（比如把 screener 筛出的 symbols 列表喂给下一轮的 `compose_factor_score`）。不需要新的编排代码。

**已知限制**：`MAX_TOOL_ROUNDS=5` 意味着一次"筛选→因子→优化→回测"的完整链路就已经用满或逼近上限，模型没有余量再做纠错重试。这是现有架构的已知取舍，不在本次改动范围内（如需放宽，应作为独立决策，涉及成本重新评估）。

## 测试与验证

- `FactorBacktestService.compute_ic_only()` 新方法单测：与 `run()` 对同一 `FactorConfig` 算出的 `ic_mean`/`ic_std`/`ir`/`ic_win_rate` 一致（数值对拍），但不产出 `group_stats`/`long_short_stats`；覆盖数据不足时 `error` 非空的路径。**对拍 fixture 要避免因子值出现 ties**（`_calc_ic()` 底层用 `rank(method="random")`，`backend/app/backtest/factor.py:299-301`，有 ties 时不同调用可能得到不同排名，逐值精确对拍会脆——用无 ties 的构造数据，或对最终 `ic_mean`/`ic_std` 结果加合理容差，panel 3 提醒）。
- 后端单测：4 个工具的 `call_tool()` 分支各自的正常路径 + 边界路径：
  - `run_backtest`：symbols 为空/超20/日期超365 各自拒绝。
  - `optimize_portfolio`：symbols 为空/超50 拒绝；复用 P3 现有测试模式验证权重和≈1。
  - `analyze_factor`/`compare_factors`：symbols 为空/超50 拒绝（agent tool 层新增校验，不依赖 API 层）；`compare_factors` 传入非 Alpha-Zoo 因子 id（如 `momentum_20d`）应报 `unknown factor`。
  - `compose_factor_score`：覆盖"部分因子被剔除"、"全部剔除报错"、"IC全零退化等权"、"pool超限/为空拒绝"、"未知 factor_id 报错"、"rank 归一化边界（n=1 固定0.5，n>1 时最大值恰好为1.0 不超界）"、"as_of 非交易日回退到最新可用交易日"、"整体无可用数据报错" 八类边界。
- `agent_loop.py` 层：确认 `run_backtest` 从 `_EXCLUDED_TOOLS` 移除后仍受 `_ALLOWED_NAMES` 校验（防止误开放其它工具）。
- 手测：`curl -X POST /api/agent/stream` 分别验证 4 个工具被模型正确调用、参数正确传递、超限参数被正确拒绝且不中断对话（走 `tool_result.result.error` 而非整个请求 500）；额外验证带 `session_id` 时事件序列以 `attempt_start` 开头也不影响这 4 个工具的正常解析。

## 跨子项目约定（继承自 gap-assessment 文档）

- 不新建 Swarm/pipeline 框架。
- 不做跨请求的调用次数持久化计数（`agent_sessions.py` 已提供 session 持久化，但无计数字段；是否扩展留待独立评估，不在 P7.5 范围内）。
- 所有新工具遵循 `agent_tools.py` 现有的 `_require(app_state, attr)` 模式获取 `repo`/`strategy_engine` 等依赖，不引入新的依赖获取方式。
- commit 需用户授权；永不 push。
