# Trend Monitor Opportunity / Anomaly Interpretation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic three-line “重点解读” column that identifies actionable market scenarios, explains the combined indicator meaning, and shows concrete confirmation and invalidation prices without changing formal buy/sell signals.

**Architecture:** A pure market-context layer derives trustworthy decision prices and five independent evidence dimensions from existing overview, completed bars, and WebSocket state. A second pure interpreter selects exactly one prioritized scenario and emits structured conclusion, behavior explanation, and named price levels. A focused React cell renders the output, while `DowMonitorList` only supplies inputs and keeps every existing raw indicator visible.

**Tech Stack:** React 18, TypeScript, Vite, Vitest, Testing Library, Tailwind CSS, Playwright CLI, Python pytest specification contracts.

## Global Constraints

- Authoritative requirement: `REQ-DOW-MONITOR-KEY-INTERPRETATION-COLUMN-001`.
- Authoritative design: `docs/superpowers/specs/2026-07-30-dow-monitor-key-interpretation-column-design.md`.
- Keep every existing raw indicator column and place `重点解读` after `日内走势` and before `趋势 / 位置`.
- Render exactly three semantic lines: conclusion, market-behavior explanation, and named key prices.
- Use a deterministic rule engine; do not call an LLM.
- An opportunity or explicit risk needs at least two independent evidence dimensions.
- A single 10-second anomaly can only produce `异动待确认`.
- Distinguish live attempts from completed-5m confirmations.
- Missing or delayed data must degrade explicitly; never infer, zero-fill, or display stale opportunities.
- Do not alter the backend, port 3018 service, port 19912 Dow engine, WebSocket subscriptions, ClickHouse persistence, notifications, sorting, or automatic trading.
- Do not alter formal signal labels, times, sources, or selection.
- Update `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md` in the same task as the production trend-monitor change.
- Do not deploy until the user separately requests deployment.

## File Structure

- Create `frontend/src/components/dow-monitor/interpretationMarketContext.ts`
  - Owns completed-5m selection, cross-day filtering, decision-price derivation, and evidence dimensions.
- Create `frontend/src/components/dow-monitor/interpretationMarketContext.test.ts`
  - Proves lower-layer price and evidence semantics independently of scenario output.
- Create `frontend/src/components/dow-monitor/keyInterpretation.ts`
  - Owns scenario thresholds, priority, templates, phase, and key-level selection.
- Create `frontend/src/components/dow-monitor/keyInterpretation.test.ts`
  - Proves every scenario’s minimum conditions, counterexamples, priority, and signal boundary.
- Create `frontend/src/components/dow-monitor/KeyInterpretationCell.tsx`
  - Renders the three structured lines and local tone spans.
- Create `frontend/src/components/dow-monitor/KeyInterpretationCell.test.tsx`
  - Proves accessibility, local highlighting, and no whole-cell risk fill.
- Modify `frontend/src/components/dow-monitor/DowMonitorList.tsx`
  - Places the cell and supplies existing item, row, realtime, and anomaly state.
- Modify `frontend/src/components/dow-monitor/DowMonitorList.test.tsx`
  - Proves placement, 20-row behavior, and regression boundaries.
- Modify `frontend/src/components/dow-monitor/suddenAnomalyHighlights.ts`
  - Exports the existing fixed anomaly metric list for interpreter wiring.
- Modify `frontend/src/components/dow-monitor/suddenAnomalyHighlights.test.ts`
  - Proves the exported list and thresholds remain aligned.
- Modify `frontend/src/pages/DowMonitorHelp.tsx`
  - Documents scenarios, reference prices, confirmation, invalidation, and signal boundary.
- Modify `frontend/src/pages/DowMonitorHelp.test.tsx`
  - Proves help navigation and definitions.
- Modify `tests/spec_contracts/test_dow_monitor_key_interpretation_column_contract.py`
  - Makes all implementation and behavioral evidence mandatory.
- Modify `docs/traceability.yaml`
  - Adds the created implementation and test paths.
- Modify `docs/acceptance/dow-monitor-key-interpretation-column.md`
  - Records RED/GREEN, prototype approval, verification, and no-deploy evidence.
- Modify `docs/reviews/dow-monitor-key-interpretation-column.md`
  - Performs the final independent requirements-to-evidence review.
- Modify `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md`
  - Records the final list semantics and verification commands.

---

### Task 1: Produce and validate the second static prototype

**Files:**
- Create, do not commit: `output/playwright/dow-monitor-opportunity-interpretation-prototype.html`
- Reference: `docs/superpowers/specs/2026-07-30-dow-monitor-key-interpretation-column-design.md`

**Interfaces:**
- Consumes: the approved three-line copy model and exact price definitions.
- Produces: a user-reviewed visual contract for the production React component.

- [ ] **Step 1: Replace metric-summary copy with scenario explanations**

Build a self-contained dark-table prototype with the existing columns and a 320px `重点解读`
column after `日内走势`. Include at least these seven rows:

```text
机会｜放量突破正在形成
买盘主动抬价，量能与短周期方向形成共振
确认 5m收>650.20｜失效 5m收<650.20｜日高 652.00

机会｜放量突破已确认
5分钟收盘站稳区间上沿，主动资金继续承接
维持 >118.60｜失效 <118.60｜压力 120.40

机会｜回踩承接正在形成
上升结构未破，回踩VWAP后卖压没有扩大
确认 >52.20｜失效 <52.20｜压力 53.10

风险｜下跌正在加速
价格逼近日低，主动卖盘与放量方向一致
确认 <114.30｜解除 >114.30｜日低 113.80

异动｜盘口突变，价格待确认
挂单快速偏多，但成交价格和主动资金尚未响应
站上 36.80确认｜跌破 35.90转弱

观察｜暂无清晰机会
周期方向冲突，量能和资金没有形成合力
等待上破 82.40｜或下破 79.60

数据｜关键数据延迟
行情或K线时效不足，暂停实时机会判断
关键价待确认
```

Keep the formal `买卖信号` column separate and unchanged. Do not repeat raw metric lists inside
the interpretation cell.

- [ ] **Step 2: Validate DOM semantics and exact column order**

Run the existing local static server or:

```powershell
python -m http.server 8765
```

Open:

```text
http://127.0.0.1:8765/output/playwright/dow-monitor-opportunity-interpretation-prototype.html
```

Use Playwright CLI to prove:

```text
股票 → 价格/涨跌 → 日内走势 → 重点解读 → 趋势/位置 → 动能/涨速
→ 量价/资金 → 突破/风险 → 买卖信号 → 操作
```

Expected: all seven scenario rows are present, every interpretation has exactly three semantic
lines, and `买卖信号` remains a separate header.

- [ ] **Step 3: Validate desktop and narrow widths**

At `1720×900`, evaluate all header widths and confirm `重点解读 >= 320`.

At `1100×800`, evaluate the table shell:

```js
({ clientWidth: shell.clientWidth, scrollWidth: shell.scrollWidth })
```

Expected: `scrollWidth > clientWidth`, the interpretation column remains at least 320px, and no
content forces other columns to collapse.

- [ ] **Step 4: Inspect the rendered screenshot**

Capture a full viewport screenshot and verify:

- the first line reads as a conclusion, not a metric list;
- the second line explains market behavior in plain Chinese;
- the third line shows named prices;
- only the category or specific risk phrase is colored;
- no whole row or whole cell is red;
- text remains legible without increasing row height beyond a compact list layout.

- [ ] **Step 5: Open the prototype for user review and stop**

Open the prototype in the in-app browser and keep the tab as a deliverable.

Expected: stop all production implementation until the user explicitly approves this second
prototype. Do not commit `output/` or `.playwright-cli/`.

---

### Task 2: Derive trustworthy decision-price context

**Files:**
- Create: `frontend/src/components/dow-monitor/interpretationMarketContext.ts`
- Create: `frontend/src/components/dow-monitor/interpretationMarketContext.test.ts`
- Reference: `frontend/src/components/dow-monitor/monitorListPresentation.ts`
- Reference: `frontend/src/components/dow-monitor/types.ts`
- Reference: `frontend/src/lib/realtimeMarketData.ts`

**Interfaces:**
- Consumes:

```ts
export interface InterpretationMarketContextInput {
  item: DowMonitorOverviewSymbol
  row: MonitorRowPresentation
  realtime?: RealtimeSymbolState
}
```

- Produces:

```ts
export type EvidenceDimension =
  | 'PRICE_STRUCTURE'
  | 'TREND_MOMENTUM'
  | 'VOLUME'
  | 'FUNDS'
  | 'DEPTH'

export interface PriceRange {
  low: number
  high: number
}

export interface InterpretationMarketContext {
  currentPrice: number | null
  liveDayHigh: number | null
  liveDayLow: number | null
  referenceDayHigh: number | null
  referenceDayLow: number | null
  confirmationReferenceDayHigh: number | null
  confirmationReferenceDayLow: number | null
  priorConfirmationReferenceDayHigh: number | null
  priorConfirmationReferenceDayLow: number | null
  attemptRange60m: PriceRange | null
  confirmationRange60m: PriceRange | null
  priorConfirmationRange60m: PriceRange | null
  latestCompleted5mClose: number | null
  previousCompleted5mClose: number | null
  vwap: number | null
  controlLine: {
    price: number
    role: string
    timeframe: '15m' | '30m'
  } | null
  evidence: Record<EvidenceDimension, {
    direction: 'UP' | 'DOWN' | 'NEUTRAL' | 'UNKNOWN'
    available: boolean
  }>
  delayed: boolean
}

export function deriveInterpretationMarketContext(
  input: InterpretationMarketContextInput,
): InterpretationMarketContext
```

- [ ] **Step 1: Write failing price-reference tests**

Cover these exact semantics:

```ts
expect(context.liveDayHigh).toBe(652)
expect(context.liveDayLow).toBe(638.4)
expect(context.referenceDayHigh).toBe(650.2)
expect(context.referenceDayLow).toBe(640.1)
expect(context.confirmationReferenceDayHigh).toBe(649.8)
expect(context.confirmationReferenceDayLow).toBe(640.1)
expect(context.attemptRange60m).toEqual({ low: 642.8, high: 650.2 })
expect(context.confirmationRange60m).toEqual({ low: 641.9, high: 649.8 })
expect(context.priorConfirmationRange60m).toEqual({ low: 641.6, high: 649.4 })
expect(context.latestCompleted5mClose).toBe(650.6)
```

Fixtures must include 14 completed same-day 5m bars plus one forming bar. Prove:

- the forming bar is excluded;
- attempt range uses the latest 12 completed bars;
- confirmation range uses the 12 bars preceding the latest completed bar;
- prior confirmation range uses the 12 bars preceding the previous completed bar;
- reference high/low only uses current-day completed bars;
- confirmation reference high/low excludes the latest completed confirmation bar;
- prior confirmation reference high/low excludes the latest two completed bars;
- previous-day bars never leak into the current reference;
- fewer than 12 valid same-day bars returns a null range;
- a missing or delayed quote returns null live prices and sets `delayed`.

- [ ] **Step 2: Run tests to verify RED**

Run:

```powershell
pnpm --dir frontend exec vitest run src/components/dow-monitor/interpretationMarketContext.test.ts
```

Expected: FAIL because the module and exported function do not exist.

- [ ] **Step 3: Implement completed-bar and price-level derivation**

Use finite-number guards and preserve the repository’s forming-bar rule:

```ts
function completedBarsForDate(
  state: DowMonitorTimeframeState | undefined,
  date: string,
): DowMonitorBar[] {
  const bars = state?.chart?.bars ?? []
  const completed = state?.snapshot.bar_completion === 'FORMING'
    || state?.snapshot.provisional
    ? bars.filter(bar => bar.timestamp !== state.snapshot.bar_time)
    : bars
  return completed.filter(bar => (
    bar.timestamp.slice(0, 10) === date
    && Number.isFinite(bar.high)
    && Number.isFinite(bar.low)
    && Number.isFinite(bar.close)
  ))
}
```

Derive `date` from the newest realtime candle or same-day 5m bar. Use:

```ts
const attemptBars = completed5m.slice(-12)
const confirmationBars = completed5m.slice(-13, -1)
const priorConfirmationBars = completed5m.slice(-14, -2)
```

Only emit each range when its array length is exactly 12.

Reference high / low arrays are not limited to 12 bars:

```ts
const attemptDayBars = completed5m
const confirmationDayBars = completed5m.slice(0, -1)
const priorConfirmationDayBars = completed5m.slice(0, -2)
```

Only emit a high / low pair when the corresponding array is non-empty.

The control line is valid only when the selected stable 15m or 30m snapshot is final,
non-provisional, has finite `line_value`, and has a non-empty `line_role`.

- [ ] **Step 4: Run price-reference tests to verify GREEN**

Run the Task 2 Vitest command.

Expected: PASS for cross-day, forming-bar, 12-bar, confirmation-bar, control-line, and delayed
cases.

- [ ] **Step 5: Write failing evidence-dimension tests**

Use the approved thresholds:

```ts
expect(context.evidence.VOLUME.direction).toBe('UP') // speed or RVOL >= 1.5
expect(context.evidence.FUNDS.direction).toBe('UP')  // confirmed inflow >= 55
expect(context.evidence.DEPTH.direction).toBe('DOWN') // pressure <= -20
```

Also prove:

- 54.99% funds is neutral, 55% is up;
- 45.01% funds is neutral, 45% is down;
- +19.99% depth is neutral, +20% is up;
- -19.99% depth is neutral, -20% is down;
- unavailable or delayed inputs are `UNKNOWN`, never `NEUTRAL`;
- 1m direction alone does not make `TREND_MOMENTUM` available;
- valid same-direction 5m and 15m make trend momentum available.

- [ ] **Step 6: Implement centralized evidence thresholds**

Export immutable thresholds for tests and help documentation:

```ts
export const INTERPRETATION_THRESHOLDS = {
  volumeRatio: 1.5,
  fundsUpPct: 55,
  fundsDownPct: 45,
  depthUpPct: 20,
  depthDownPct: -20,
  nearAtrFraction: 0.25,
  nearFallbackPct: 0.5,
} as const
```

Count one dimension once even if both volume-speed and RVOL pass.

- [ ] **Step 7: Run Task 2 tests and commit**

Run:

```powershell
pnpm --dir frontend exec vitest run src/components/dow-monitor/interpretationMarketContext.test.ts src/components/dow-monitor/monitorListPresentation.test.ts
```

Expected: PASS.

Commit:

```powershell
git add -- frontend/src/components/dow-monitor/interpretationMarketContext.ts frontend/src/components/dow-monitor/interpretationMarketContext.test.ts
git commit -m "feat(dow-monitor): derive interpretation market context"
```

---

### Task 3: Implement deterministic scenario selection

**Files:**
- Create: `frontend/src/components/dow-monitor/keyInterpretation.ts`
- Create: `frontend/src/components/dow-monitor/keyInterpretation.test.ts`
- Reference: `frontend/src/components/dow-monitor/suddenAnomalyHighlights.ts`

**Interfaces:**
- Consumes:

```ts
export interface KeyInterpretationInput {
  context: InterpretationMarketContext
  anomalies: ReadonlySet<SuddenAnomalyMetric>
}
```

- Produces:

```ts
export type InterpretationCategory =
  | 'OPPORTUNITY'
  | 'RISK'
  | 'ANOMALY'
  | 'OBSERVE'
  | 'DATA'

export type InterpretationPhase =
  | 'ATTEMPT'
  | 'CONFIRMED'
  | 'INVALIDATED'
  | 'NONE'

export interface InterpretationLevel {
  label: string
  comparator?: '>' | '<'
  price: number
  basis:
    | 'RANGE_60M'
    | 'REFERENCE_DAY_HIGH'
    | 'REFERENCE_DAY_LOW'
    | 'LIVE_DAY_HIGH'
    | 'LIVE_DAY_LOW'
    | 'VWAP'
    | 'CONTROL_LINE'
}

export interface KeyInterpretation {
  scenarioId:
    | 'DATA_UNAVAILABLE'
    | 'BREAKOUT_INVALIDATED'
    | 'BREAKDOWN_CONFIRMED'
    | 'BREAKDOWN_INVALIDATED'
    | 'BREAKDOWN_ATTEMPT'
    | 'DOWNSIDE_ACCELERATION'
    | 'HIGH_PULLBACK'
    | 'HIGH_VOLUME_STALL'
    | 'BREAKOUT_CONFIRMED'
    | 'BREAKOUT_ATTEMPT'
    | 'RETEST_HOLD'
    | 'TREND_ACCELERATION'
    | 'ANOMALY_PENDING'
    | 'NO_CLEAR_OPPORTUNITY'
  category: InterpretationCategory
  phase: InterpretationPhase
  headline: string
  explanation: string
  levels: InterpretationLevel[]
  dimensions: EvidenceDimension[]
  accessibleText: string
}

export function deriveKeyInterpretation(
  input: KeyInterpretationInput,
): KeyInterpretation

export function formatInterpretationPrice(value: number): string {
  return value.toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
    useGrouping: false,
  })
}
```

- [ ] **Step 1: Write failing scenario minimum-condition tests**

Create focused builders such as:

```ts
const result = deriveKeyInterpretation({
  context: contextFixture({
    currentPrice: 650.6,
    attemptRange60m: { low: 642.8, high: 650.2 },
    latestCompleted5mClose: 649.9,
    evidence: evidenceFixture({
      PRICE_STRUCTURE: 'UP',
      VOLUME: 'UP',
      FUNDS: 'UP',
    }),
  }),
  anomalies: new Set(),
})

expect(result).toMatchObject({
  scenarioId: 'BREAKOUT_ATTEMPT',
  category: 'OPPORTUNITY',
  phase: 'ATTEMPT',
})
expect(result.dimensions).toEqual(['PRICE_STRUCTURE', 'VOLUME', 'FUNDS'])
```

Write a counterexample beside every positive case:

- price breakout with only one dimension returns `ANOMALY_PENDING` or `NO_CLEAR_OPPORTUNITY`;
- volume + funds without a crossed structure cannot be called `BREAKOUT`;
- a live cross with latest completed 5m below the level is `ATTEMPT`;
- a latest completed 5m close above `confirmationRange60m.high` is `CONFIRMED`;
- a previous completed close above `priorConfirmationRange60m.high` followed by a latest close
  below that upper level is `BREAKOUT_INVALIDATED`;
- a previous completed close below `priorConfirmationRange60m.low` followed by a latest close
  above that lower level is `BREAKDOWN_INVALIDATED`;
- a down-channel pullback near VWAP with no renewed 1m or funds support is not `RETEST_HOLD`;
- near day low + down momentum + volume/sell pressure yields downside risk;
- distance from day high alone cannot yield `HIGH_PULLBACK`;
- high volume with continuing 5m upside cannot yield `HIGH_VOLUME_STALL`;
- one active depth anomaly cannot yield opportunity or explicit risk;
- delayed input always yields `DATA_UNAVAILABLE`.

- [ ] **Step 2: Run scenario tests to verify RED**

Run:

```powershell
pnpm --dir frontend exec vitest run src/components/dow-monitor/keyInterpretation.test.ts
```

Expected: FAIL because `deriveKeyInterpretation` does not exist.

- [ ] **Step 3: Implement fixed candidate priority**

Create pure candidate evaluators and select in this order:

```ts
const candidates = [
  dataUnavailable(input),
  breakoutInvalidated(input),
  breakdownConfirmed(input),
  downsideAcceleration(input),
  highPullback(input),
  highVolumeStall(input),
  breakoutConfirmed(input),
  retestHold(input),
  trendAcceleration(input),
  breakdownAttempt(input),
  breakoutAttempt(input),
  breakdownInvalidated(input),
  anomalyPending(input),
  noClearOpportunity(input),
]
return candidates.find(Boolean)!
```

Each opportunity and explicit risk candidate must call one shared guard:

```ts
function hasIndependentSupport(
  dimensions: EvidenceDimension[],
): boolean {
  return new Set(dimensions).size >= 2
}
```

Do not let the scenario array’s traversal order leak into displayed dimension order; use the
stable dimension order from the specification.

- [ ] **Step 4: Implement scenario-specific named levels**

Examples:

```ts
levels: [
  { label: '确认 5m收', comparator: '>', price: upper, basis: 'RANGE_60M' },
  { label: '失效 5m收', comparator: '<', price: upper, basis: 'RANGE_60M' },
  { label: '日高', price: liveDayHigh, basis: 'LIVE_DAY_HIGH' },
]
```

For `NO_CLEAR_OPPORTUNITY`, show range upper and lower as “等待上破” and “或下破”. If the
range is absent, return an empty level array and let the renderer show `关键价待确认`.

Limit `levels` to three. Never use current price as a level unless it equals a separately derived
structure value.

- [ ] **Step 5: Implement fixed Chinese templates**

Templates must explain behavior, for example:

```ts
headline: '放量突破正在形成'
explanation: '买盘主动抬价，量能与短周期方向形成共振'
```

Forbidden substrings in every generated field:

```ts
['建议买入', '建议卖出', '立即操作', '止盈', '止损']
```

Do not include raw formal signal label or time in the input or output.

- [ ] **Step 6: Add priority, boundary, and invariant tests**

Prove:

- confirmed downside risk wins over confirmed upside opportunity;
- confirmed opportunity wins over an anomaly;
- fixed scenario ID resolves equal-priority ties;
- all opportunities and explicit risks have at least two unique dimensions;
- level arrays never exceed three;
- missing prices are omitted, never rendered as zero;
- all fields reject the forbidden substrings;
- the same input always returns deeply equal output.

- [ ] **Step 7: Run Tasks 2–3 tests and commit**

Run:

```powershell
pnpm --dir frontend exec vitest run src/components/dow-monitor/interpretationMarketContext.test.ts src/components/dow-monitor/keyInterpretation.test.ts
```

Expected: PASS.

Commit:

```powershell
git add -- frontend/src/components/dow-monitor/keyInterpretation.ts frontend/src/components/dow-monitor/keyInterpretation.test.ts
git commit -m "feat(dow-monitor): interpret opportunity and anomaly scenarios"
```

---

### Task 4: Render and integrate the interpretation column

**Files:**
- Create: `frontend/src/components/dow-monitor/KeyInterpretationCell.tsx`
- Create: `frontend/src/components/dow-monitor/KeyInterpretationCell.test.tsx`
- Modify: `frontend/src/components/dow-monitor/DowMonitorList.tsx`
- Modify: `frontend/src/components/dow-monitor/DowMonitorList.test.tsx`
- Modify: `frontend/src/components/dow-monitor/suddenAnomalyHighlights.ts`
- Modify: `frontend/src/components/dow-monitor/suddenAnomalyHighlights.test.ts`

**Interfaces:**
- Consumes:

```ts
export interface KeyInterpretationCellProps {
  interpretation: KeyInterpretation
}
```

- Produces: one accessible `td` content block with exactly three semantic lines.

- [ ] **Step 1: Write the failing cell-rendering tests**

Render a confirmed breakout and assert:

```ts
expect(screen.getByText('放量突破已确认')).toBeInTheDocument()
expect(screen.getByText('买盘主动抬价，量能与短周期方向形成共振'))
  .toBeInTheDocument()
expect(screen.getByText(/确认 5m收>650\.20/)).toBeInTheDocument()
expect(screen.getByLabelText(/重点解读，机会，放量突破已确认/))
  .toBeInTheDocument()
```

Also assert:

- the component has exactly three `data-interpretation-line` elements;
- an anomaly/risk phrase may have `text-danger`;
- the root does not have `bg-danger`, `bg-red-*`, or `text-danger`;
- an empty `levels` array renders `关键价待确认`;
- prices use `formatInterpretationPrice`, preserving two to four decimal places without grouping.

- [ ] **Step 2: Run the cell test to verify RED**

Run:

```powershell
pnpm --dir frontend exec vitest run src/components/dow-monitor/KeyInterpretationCell.test.tsx
```

Expected: FAIL because the component does not exist.

- [ ] **Step 3: Implement the focused cell**

Use a category-tone map:

```ts
const categoryClass: Record<InterpretationCategory, string> = {
  OPPORTUNITY: 'text-rose-300',
  RISK: 'text-danger',
  ANOMALY: 'text-amber-300',
  OBSERVE: 'text-muted',
  DATA: 'text-muted',
}
```

Render:

```tsx
<div
  className="grid min-w-[320px] gap-1 leading-tight"
  aria-label={`重点解读，${interpretation.accessibleText}`}
  title={interpretation.accessibleText}
>
  <div data-interpretation-line="conclusion">...</div>
  <div data-interpretation-line="explanation">...</div>
  <div data-interpretation-line="levels">...</div>
</div>
```

Keep category color local to the visible category badge or risk phrase.

- [ ] **Step 4: Write failing list-integration tests**

In `DowMonitorList.test.tsx`, assert:

```ts
const headers = screen.getAllByRole('columnheader').map(node => node.textContent)
expect(headers.indexOf('日内走势')).toBeLessThan(headers.indexOf('重点解读'))
expect(headers.indexOf('重点解读')).toBeLessThan(headers.indexOf('趋势 / 位置'))
```

For representative fixtures, assert the list displays:

- `放量突破正在形成`;
- `盘口突变，价格待确认`;
- `暂无清晰机会`;
- `关键数据延迟`.

Retain existing assertions for all six raw anomaly markers, formal signals, Beijing signal times,
pagination, and detail toggle.

- [ ] **Step 5: Integrate using existing item and realtime state**

For each presented item:

```ts
const context = deriveInterpretationMarketContext({
  item,
  row,
  realtime,
})
const interpretation = deriveKeyInterpretation({
  context,
  anomalies: new Set(
    SUDDEN_ANOMALY_METRICS.filter(metric => (
      anomalyHighlights.has(suddenAnomalyKey(item.symbol, metric))
    )),
  ),
})
```

Export `SUDDEN_ANOMALY_METRICS` from `suddenAnomalyHighlights.ts` as the existing fixed metric
list rather than duplicating anomaly names in the list component.

Add this regression assertion:

```ts
expect(SUDDEN_ANOMALY_METRICS).toEqual([
  'changePct',
  'momentum1m',
  'volumeSpeed',
  'depthPressurePct',
  'toDayHighPct',
  'fromDayLowPct',
])
expect(Object.keys(SUDDEN_ANOMALY_THRESHOLDS)).toEqual(SUDDEN_ANOMALY_METRICS)
```

Insert the header and cell after the intraday sparkline. Do not change signal derivation,
notification selection, pagination, or detail handlers.

- [ ] **Step 6: Run component and regression tests**

Run:

```powershell
pnpm --dir frontend exec vitest run src/components/dow-monitor/KeyInterpretationCell.test.tsx src/components/dow-monitor/DowMonitorList.test.tsx src/components/dow-monitor/suddenAnomalyHighlights.test.ts src/components/dow-monitor/monitorListPresentation.test.ts
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add -- frontend/src/components/dow-monitor/KeyInterpretationCell.tsx frontend/src/components/dow-monitor/KeyInterpretationCell.test.tsx frontend/src/components/dow-monitor/DowMonitorList.tsx frontend/src/components/dow-monitor/DowMonitorList.test.tsx frontend/src/components/dow-monitor/suddenAnomalyHighlights.ts frontend/src/components/dow-monitor/suddenAnomalyHighlights.test.ts
git commit -m "feat(dow-monitor): show opportunity interpretation column"
```

---

### Task 5: Document semantics and make traceability executable

**Files:**
- Modify: `frontend/src/pages/DowMonitorHelp.tsx`
- Modify: `frontend/src/pages/DowMonitorHelp.test.tsx`
- Modify: `tests/spec_contracts/test_dow_monitor_key_interpretation_column_contract.py`
- Modify: `docs/traceability.yaml`
- Modify: `docs/acceptance/dow-monitor-key-interpretation-column.md`
- Modify: `docs/reviews/dow-monitor-key-interpretation-column.md`
- Modify: `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md`

**Interfaces:**
- Consumes: exported thresholds, scenario IDs, and verified UI behavior.
- Produces: user-facing help and mandatory requirements-to-evidence links.

- [ ] **Step 1: Write failing help-page tests**

Assert the help page contains:

```ts
expect(screen.getByRole('heading', { name: '重点解读' })).toBeInTheDocument()
expect(screen.getByText(/正在尝试.*已确认/)).toBeInTheDocument()
expect(screen.getByText(/最近12根已完成5分钟K线/)).toBeInTheDocument()
expect(screen.getByText(/确认价.*失效价/)).toBeInTheDocument()
expect(screen.getByText(/盘口.*不能单独/)).toBeInTheDocument()
expect(screen.getByText(/不是买卖建议/)).toBeInTheDocument()
```

- [ ] **Step 2: Run the help test to verify RED**

Run:

```powershell
pnpm --dir frontend exec vitest run src/pages/DowMonitorHelp.test.tsx
```

Expected: FAIL because the revised opportunity / anomaly explanation is absent.

- [ ] **Step 3: Implement structured help content**

Add sections in this order:

1. 三行怎么看；
2. 机会、风险、异动、观察、数据五类结论；
3. 正在尝试与 5m 已确认；
4. 日高 / 日低、参考高低、近 60 分钟区间、VWAP 和趋势线；
5. 确认价与失效价；
6. 为什么盘口单项异动只能待确认；
7. 与正式买卖信号的边界。

Copy exact threshold values from `INTERPRETATION_THRESHOLDS`; do not introduce different prose
thresholds.

- [ ] **Step 4: Tighten the specification contract**

Extend `BEHAVIOR_TESTS`:

```py
BEHAVIOR_TESTS = (
    "src/components/dow-monitor/interpretationMarketContext.test.ts",
    "src/components/dow-monitor/keyInterpretation.test.ts",
    "src/components/dow-monitor/KeyInterpretationCell.test.tsx",
    "src/components/dow-monitor/DowMonitorList.test.tsx",
    "src/pages/DowMonitorHelp.test.tsx",
)
```

Replace the temporary skip with:

```py
assert not missing, f"missing behavioral evidence: {missing}"
```

Update traceability implementation paths to include:

```yaml
- frontend/src/components/dow-monitor/interpretationMarketContext.ts
- frontend/src/components/dow-monitor/keyInterpretation.ts
- frontend/src/components/dow-monitor/KeyInterpretationCell.tsx
- frontend/src/components/dow-monitor/DowMonitorList.tsx
- frontend/src/pages/DowMonitorHelp.tsx
```

- [ ] **Step 5: Update acceptance evidence and runbook**

Record:

- approved second prototype URL and screenshot path;
- each RED command and expected failure;
- each GREEN command and pass result;
- exact no-backend/no-WebSocket/no-signal-change boundary;
- static bundle check commands;
- mini chart and detail chart regression verification;
- no-deployment status.

In the Obsidian runbook, document the new column after `日内走势`, the 320px scroll behavior,
the three-line reading model, the completed-5m confirmation rule, and production verification on
port 3018. Preserve the 19912 engine distinction.

- [ ] **Step 6: Run help and contract tests**

Run:

```powershell
pnpm --dir frontend exec vitest run src/pages/DowMonitorHelp.test.tsx
python -m pytest tests/spec_contracts/test_dow_monitor_key_interpretation_column_contract.py -q
```

Expected: PASS with no skip.

- [ ] **Step 7: Commit**

```powershell
git add -- frontend/src/pages/DowMonitorHelp.tsx frontend/src/pages/DowMonitorHelp.test.tsx tests/spec_contracts/test_dow_monitor_key_interpretation_column_contract.py docs/traceability.yaml docs/acceptance/dow-monitor-key-interpretation-column.md docs/reviews/dow-monitor-key-interpretation-column.md
git commit -m "docs(dow-monitor): explain opportunity interpretation semantics"
```

The Obsidian runbook is outside this Git worktree. Verify its saved contents separately and do
not try to stage it in this repository.

---

### Task 6: Verify requirements independently and stop before deployment

**Files:**
- Modify: `docs/acceptance/dow-monitor-key-interpretation-column.md`
- Modify: `docs/reviews/dow-monitor-key-interpretation-column.md`
- Modify if evidence changed: `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md`

**Interfaces:**
- Consumes: all implementation, tests, authoritative specification, and built frontend.
- Produces: independent requirements-to-evidence review and a deployable but not deployed result.

- [ ] **Step 1: Run focused semantic suites**

```powershell
pnpm --dir frontend exec vitest run src/components/dow-monitor/interpretationMarketContext.test.ts src/components/dow-monitor/keyInterpretation.test.ts src/components/dow-monitor/KeyInterpretationCell.test.tsx src/components/dow-monitor/DowMonitorList.test.tsx src/pages/DowMonitorHelp.test.tsx
```

Expected: PASS.

- [ ] **Step 2: Run frontend regression and production build**

```powershell
pnpm --dir frontend test
pnpm --dir frontend build
```

Expected: PASS with a production bundle containing the `重点解读` help and list strings.

- [ ] **Step 3: Run backend-adjacent regression and specification checks**

```powershell
python -m pytest tests/spec_contracts/test_dow_monitor_key_interpretation_column_contract.py tests/spec_contracts/test_dow_monitor_sudden_anomaly_highlight_contract.py tests/spec_contracts/test_dow_monitor_list_websocket_contract.py -q
python scripts/check_spec_compliance.py
```

Expected:

- all feature contracts pass with no skip;
- specification checker has no new failure compared with the recorded repository baseline;
- the existing expired exception and existing outside-`tests/` path are reported separately if
  still present and are not misrepresented as feature acceptance.

- [ ] **Step 4: Browser-verify the built UI**

Start the local built service using the repository runbook. At `/dow-monitor`, verify:

- column order and 320px minimum width;
- all existing raw columns remain visible via horizontal scroll;
- scenario text is interpretation, not a metric dump;
- exact price labels match fixture/API values;
- one anomaly cannot present as opportunity;
- formal signal and Beijing time are unchanged;
- detail expands and collapses;
- mini trend line and detail chart still render;
- help link reaches the revised definitions;
- console has zero feature-related errors.

- [ ] **Step 5: Conduct the independent requirements-to-evidence review**

Read the authoritative design from top to bottom and map each acceptance item to:

- lower-layer executable price-context tests;
- scenario positive and counterexample tests;
- component behavior tests;
- help test;
- built-browser evidence.

Reject any acceptance item supported only by a screenshot, snapshot, build pass, or downstream
formal signal.

- [ ] **Step 6: Update evidence and commit**

Mark the acceptance and independent review complete only when every item has executable evidence.

```powershell
git add -- docs/acceptance/dow-monitor-key-interpretation-column.md docs/reviews/dow-monitor-key-interpretation-column.md
git commit -m "test(dow-monitor): verify opportunity interpretation column"
```

Verify the external Obsidian runbook is saved, but do not stage it in this repository.

- [ ] **Step 7: Stop before deployment**

Report:

- completed commits;
- focused and full test results;
- build result;
- specification baseline;
- browser verification;
- exact production deployment command from the runbook.

Do not publish to 10.28 or local production unless the user explicitly requests deployment in a
new instruction.
