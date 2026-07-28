# A股可信数据与小白模拟盘实施计划

## Context

在现有 TickFlow Stock Panel 上完成两个已确认阶段：

- P0：把“数据是否可信”变成任何研究候选进入清单前的硬门禁，并隔离复权日/异常涨跌幅。
- P1：提供手机和平板也能看懂的“今日行动”页面，以及只在本机保存、绝不连接券商的模拟账户与复盘日志。

现有 FastAPI + Polars + Parquet/JSON 与 React + TypeScript + TanStack Query 架构继续使用，不重写框架。

## Global Constraints

- 仅个人研究与教学；页面不得输出“买入、卖出、目标价、保证收益”等实盘指令。
- 不接券商、不读取券商凭据、不自动下单；所有模拟成交必须由用户手动提交。
- AI 不参与数据门禁、候选评分、交易规则或模拟账户记账；`ai_can_change_score` 始终为 `false`。
- 缺数据、数据过期、回执异常、未经授权回退、合成数据或关键字段异常时必须 fail closed；不得静默补值或切换数据源。
- 所有新业务行为测试先行：先看到针对缺失行为的失败，再做最小实现并看到测试通过。
- 保留现有 `GO / WAIT / NO-GO` 兼容字段；`GO` 只表示“可进入研究清单”，绝不表示买入。
- 今日行动最多展示 3 个研究候选；数据门禁失败时行动状态必须是 `OBSERVE_ONLY`，且候选全部不可进入模拟操作。
- 模拟账户初始资金只能选 5000 或 10000 元；默认 10000 元。
- 模拟买入按 A 股整手约束：普通标的数量为 100 股整数倍；`688`/`689` 科创板标的至少 200 股且仍按 100 股步进。模拟账户不支持 ETF、可转债、港股或美股。
- 模拟卖出执行 T+1：同一交易日买入的数量不可卖出；不得透支现金或卖空。
- 模拟费用假设固定、清楚展示且可在以后版本调整：双边佣金 0.03%，单笔最低 5 元；卖出印花税 0.05%；用户输入的成交价视为已包含自行判断的滑点。费用只是模拟假设，实际以券商为准。
- 模拟数据仅写入 `data/user_data/paper_account.json`，使用进程锁与临时文件原子替换；重置必须带显式确认值 `RESET`。
- 所有面向用户的新文案使用简体中文；错误必须告诉用户“为什么被拦住”和“下一步该做什么”。
- 手机布局不得依赖固定 900px 宽表格；360px 宽度下核心状态、3 个候选、模拟账户与日志必须无需横向滚动即可使用。
- 不修改与本计划无关的现有脏文件；每个任务只提交自己触及的文件。

## Task 1: Complete the trusted-data gate contract

### Files

- Modify: `backend/app/data_providers/trust.py`
- Modify: `backend/app/services/advisor.py`
- Modify only where the derived dataset is successfully persisted: `backend/app/jobs/daily_pipeline.py` and/or the existing enriched pipeline call site
- Test: `backend/tests/test_data_trust.py`
- Test: `backend/tests/test_advisor_recommendations.py`

### Requirements

1. Add an auditable `daily_enriched` receipt for the successfully persisted derived daily partition. Its provider is `derived`, its observed end is the actual latest date, and its coverage is calculated from the requested/eligible stock universe rather than hard-coded.
2. The advisor global gate requires four current receipts: `instruments`, `daily`, `adj_factor`, and `daily_enriched`.
3. `daily`, `adj_factor`, and `daily_enriched` must not be `error`, `invalid`, or `empty`, must not be synthetic, and must not report an unauthorized fallback.
4. `daily` and `daily_enriched` require at least 95% coverage and `observed_end == strategy as_of`.
5. `adj_factor` is an event stream: its coverage receipt is authoritative, but its observed start/end need not equal the strategy date. A partial receipt below 95% blocks the gate.
6. The response exposes a `datasets` map with each required dataset's status, provider, coverage, observed range, and human-readable reasons. Missing receipts must appear explicitly rather than disappear.
7. Existing overall gate compatibility fields remain: `decision`, `provider`, `coverage_ratio`, `observed_end`, `reasons`.

### Verification

- RED: focused tests fail because `adj_factor`/`daily_enriched` are currently not required and no derived receipt exists.
- GREEN: `uv.exe run pytest -q tests/test_data_trust.py tests/test_advisor_recommendations.py`.

## Task 2: Quarantine adjustment-day and abnormal-return candidates

### Files

- Modify: `backend/app/services/advisor.py`
- Modify: `backend/app/api/advisor.py`
- Test: `backend/tests/test_advisor_recommendations.py`

### Requirements

1. The API loads `data/adj_factor/all.parquet` read-only and passes the set of symbols with an adjustment event on `as_of` into the pure advisor builder. A missing/unreadable factor file must not crash the endpoint; the global receipt gate already decides whether factor data is trustworthy.
2. Add structured `risk_flags` to every candidate while preserving the existing `risk_reasons` strings.
3. Hard-block a candidate with code `ADJUSTMENT_EVENT_ON_AS_OF` when its symbol has an adjustment event on the strategy date.
4. Hard-block a candidate with code `ABNORMAL_DAILY_RETURN` when finite `abs(change_pct) > 0.30`. This is a quarantine for manual review, not a claim that the move is necessarily wrong.
5. Hard-block missing/non-finite/non-positive `close` with code `INVALID_PRICE`.
6. Existing limit-up/down blocks remain and also receive stable codes.
7. Tests reproduce a 50% strategy-row move with a same-day factor event and prove it cannot be `GO`.

### Verification

- RED: the reproduced candidate remains `GO`.
- GREEN: `uv.exe run pytest -q tests/test_advisor_recommendations.py`.

## Task 3: Build the deterministic beginner daily brief

### Files

- Modify: `backend/app/services/advisor.py`
- Modify: `backend/app/api/advisor.py`
- Test: `backend/tests/test_beginner_daily_brief.py`

### Requirements

1. Add `GET /api/advisor/daily-brief`.
2. Return `action_state` as exactly one of:
   - `OBSERVE_ONLY`: data gate blocked.
   - `SIMULATE_ONLY`: gate passed but no `GO` candidate survived hard risk gates.
   - `RESEARCH_ONLY`: gate passed and at least one `GO` research candidate survived.
3. Return at most 3 candidates. Use deterministic advisor order and never call AI.
4. Each candidate contains: symbol/name, current research decision, deterministic reasons, conditions to keep observing, invalidation conditions, and all risk flags. It must not contain a target price or an imperative trade instruction.
5. Include one plain-language `today_message` and `next_step` suitable for a beginner.
6. Include the complete data gate and method safety metadata so the frontend never reconstructs business decisions.

### Verification

- RED: endpoint and action-state behavior are absent.
- GREEN: `uv.exe run pytest -q tests/test_beginner_daily_brief.py tests/test_advisor_recommendations.py`.

## Task 4: Implement the local paper account and journal API

### Files

- Create: `backend/app/services/paper_account.py`
- Create: `backend/app/api/paper.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_paper_account.py`
- Test: `backend/tests/test_paper_api.py`

### Requirements

1. Persist schema-versioned account state at `data/user_data/paper_account.json` using a process lock and atomic replace.
2. `GET /api/paper/account` lazily creates a 10000-yuan account and returns cash, cost basis, marked value, total equity, realized/unrealized P&L, positions, sellable quantities, fee assumptions, and journal entries.
3. `POST /api/paper/reset` accepts only initial cash 5000 or 10000 and exact confirmation `RESET`.
4. `POST /api/paper/trades` records a manual simulated fill with side, symbol, name, trade date, positive finite price, valid quantity, plan note, and invalidation note.
5. The trade API must independently reject a simulated fill while the current advisor data gate is `BLOCK`; frontend disabling alone is not a sufficient safety boundary.
6. Buy validation enforces supported stock symbols, lot rules, and sufficient cash including commission.
7. Sell validation enforces ownership, T+1 sellable quantity, no short selling, and sell-side commission plus stamp tax.
8. Lots are consumed FIFO. Every fill produces an immutable journal item with before/after cash, fees, realized P&L where applicable, and the user's notes.
9. Read responses may mark positions from the latest strategy-cache close; if no mark exists, use average cost and set `mark_source` to `COST_FALLBACK`.
10. Validation errors are HTTP 400 with beginner-readable Chinese messages. A blocked data gate uses HTTP 409. No route or service may call a broker or external trading API.

### Verification

- RED: service/API tests fail because the module and routes do not exist.
- GREEN: `uv.exe run pytest -q tests/test_paper_account.py tests/test_paper_api.py`.

## Task 5: Replace the advisor screen with a responsive beginner workflow

### Files

- Modify: `frontend/src/lib/advisor.ts`
- Create: `frontend/src/lib/paper-account.ts`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/lib/queryKeys.ts`
- Modify: `frontend/src/pages/Advisor.tsx`
- Test: `frontend/src/lib/advisor.test.ts`
- Create: `frontend/src/lib/paper-account.test.ts`

### Requirements

1. The first viewport answers three questions in plain Chinese: today observe/simulate/research, whether data passed, and what the next action is.
2. Show no more than 3 research cards, each with “为什么入选 / 继续观察条件 / 失效条件 / 风险拦截”. Never label a card as a buy recommendation.
3. Show all four dataset receipts in a compact trust panel and expose exact block reasons.
4. Add a clearly separated `模拟账户（不会下单）` section with:
   - 5000/10000 reset control requiring confirmation.
   - current cash/equity/P&L and positions.
   - manual buy/sell form.
   - recent journal with plan and invalidation notes.
5. Disable the simulated-trade form when the daily brief is `OBSERVE_ONLY`; explain that this is a safety guard, not a technical error.
6. Use cards and responsive grids at mobile widths; core content must not use a fixed minimum table width.
7. Keep the detailed 50-stock research list out of the default beginner flow.

### Verification

- RED: pure presentation helpers/types tests fail for new action states, dataset summaries, and paper validation messages.
- GREEN: local Vitest run for the touched tests.
- Type/build: `pnpm.cmd build`.

## Task 6: Integration, documentation, and whole-project verification

### Files

- Modify: `docs/a-share-advisor.md`
- Include this plan file in the final task commit if still untracked.
- Add only narrowly necessary integration tests or fixes discovered by verification.

### Requirements

1. Document the four hard data gates, abnormal-return quarantine, daily action states, paper-account rules, fee assumptions, storage path, reset behavior, and the explicit absence of broker integration.
2. Document that 0.03%/minimum-5-yuan commission is a simulation default that can differ by broker; sell stamp tax is modeled at 0.05% based on the currently verified official half-rate policy.
3. Run all verification fresh on the final tree:
   - `uv.exe run pytest -q`
   - local Vitest full run
   - `pnpm.cmd lint`
   - `pnpm.cmd build`
4. Inspect the final diff for secrets, broker endpoints, auto-order code, accidental data files, and unrelated edits.
5. Run a focused API smoke test proving `daily-brief` and paper-account read paths return schema-valid responses from a temporary data directory.

### Verification

- All commands exit 0; report exact counts and any warnings without hiding them.
- Final whole-branch review has no unresolved Critical or Important findings.
