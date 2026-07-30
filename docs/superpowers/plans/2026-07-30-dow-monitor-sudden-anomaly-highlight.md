# Trend Monitor Sudden Anomaly Highlight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Highlight sudden changes in six high-sensitivity Dow monitor values for 10 seconds without changing formal trading signals.

**Architecture:** Add a pure anomaly state machine that owns thresholds, baselines, expiry times, and page cleanup. A small React Hook drives the state machine and one nearest-expiry timer; `DowMonitorList` supplies already-derived display values and applies a reusable red metric wrapper only to active values.

**Tech Stack:** React 18, TypeScript, Vitest, Testing Library, Tailwind CSS, Python spec-contract tests.

## Global Constraints

- Authoritative requirement ID: `REQ-DOW-MONITOR-SUDDEN-ANOMALY-HIGHLIGHT-001`.
- Detect only change percentage, 1m momentum, 1m volume speed, five-level depth pressure, distance to day high, and distance from day low.
- Thresholds are respectively `0.50` percentage points, `0.40` percentage points, `1.00×`, `40` percentage points, `0.50` percentage points, and `0.50` percentage points.
- First valid values, delayed values, missing values, and first recovered values must not trigger.
- A trigger lasts exactly 10 seconds; another threshold-reaching change resets that metric's expiry to 10 seconds from the new trigger.
- Highlight only the changed value with red text, a pale red background, a red border, and visible `异动` text.
- Red color must not be the only cue; the active value needs an accessible name containing `突发异动`.
- Leaving the current page removes that symbol's baselines and active highlights.
- Do not modify backend APIs, port 3018/19912 responsibilities, WebSocket subscriptions, persisted decisions, notifications, or formal signal selection.

---

### Task 1: Register Authority and Traceability

**Files:**
- Modify: `docs/spec-index.yaml`
- Modify: `docs/traceability.yaml`
- Create: `docs/acceptance/dow-monitor-sudden-anomaly-highlight.md`
- Create: `docs/reviews/dow-monitor-sudden-anomaly-highlight.md`
- Create: `tests/spec_contracts/test_dow_monitor_sudden_anomaly_highlight_contract.py`

**Interfaces:**
- Consumes: approved design `docs/superpowers/specs/2026-07-30-dow-monitor-sudden-anomaly-highlight-design.md`.
- Produces: authoritative specification ID `USER-20260730-DOW-MONITOR-SUDDEN-ANOMALY-HIGHLIGHT` and requirement ID `REQ-DOW-MONITOR-SUDDEN-ANOMALY-HIGHLIGHT-001`.

- [ ] **Step 1: Write the failing specification contract**

Create a Python contract that loads the index, traceability file, approved design, list component, detector, Hook, help page, acceptance, and review files. Assert:

```python
SPEC_ID = "USER-20260730-DOW-MONITOR-SUDDEN-ANOMALY-HIGHLIGHT"
REQ_ID = "REQ-DOW-MONITOR-SUDDEN-ANOMALY-HIGHLIGHT-001"

assert SPEC_ID in spec_index
assert REQ_ID in spec_index
assert REQ_ID in traceability
for token in ("0.50", "0.40", "1.00", "40", "10 秒", "突发异动"):
    assert token in approved_design
```

Also assert that traceability points to:

```text
frontend/src/components/dow-monitor/suddenAnomalyHighlights.ts
frontend/src/components/dow-monitor/useSuddenAnomalyHighlights.ts
frontend/src/components/dow-monitor/DowMonitorList.tsx
frontend/src/pages/DowMonitorHelp.tsx
```

- [ ] **Step 2: Run the contract and verify RED**

Run:

```powershell
python -m pytest tests/spec_contracts/test_dow_monitor_sudden_anomaly_highlight_contract.py -q
```

Expected: FAIL because the new specification and traceability entries do not exist.

- [ ] **Step 3: Register the approved design as authoritative**

Add this exact index entry:

```yaml
  - id: USER-20260730-DOW-MONITOR-SUDDEN-ANOMALY-HIGHLIGHT
    path: docs/superpowers/specs/2026-07-30-dow-monitor-sudden-anomaly-highlight-design.md
    status: authoritative
    requirements:
      - REQ-DOW-MONITOR-SUDDEN-ANOMALY-HIGHLIGHT-001
```

Add traceability with the four implementation paths above, the new executable contract path, and these acceptance paths:

```yaml
    acceptance:
      - {path: docs/acceptance/dow-monitor-sudden-anomaly-highlight.md, type: semantic-acceptance}
      - {path: docs/reviews/dow-monitor-sudden-anomaly-highlight.md, type: independent-review}
```

Create acceptance and review documents with status `待实现`, the exact six thresholds, the 10-second lifetime, delayed/missing reset semantics, page cleanup, and the formal-signal non-interference boundary.

- [ ] **Step 4: Run the contract and repository checker**

Run:

```powershell
python -m pytest tests/spec_contracts/test_dow_monitor_sudden_anomaly_highlight_contract.py -q
python scripts/check_spec_compliance.py
```

Expected: new contract PASS. The checker may still report only the two recorded repository baselines: expired `EXC-COLLECTION-MONITOR-PREACCEPTANCE-DEPLOY-001` and the old detail-toggle test path outside `tests/`.

- [ ] **Step 5: Commit the contract registration**

```powershell
git add docs/spec-index.yaml docs/traceability.yaml `
  docs/acceptance/dow-monitor-sudden-anomaly-highlight.md `
  docs/reviews/dow-monitor-sudden-anomaly-highlight.md `
  tests/spec_contracts/test_dow_monitor_sudden_anomaly_highlight_contract.py
git commit -m "docs(dow-monitor): register sudden anomaly highlights"
```

### Task 2: Build the Pure Anomaly State Machine

**Files:**
- Create: `frontend/src/components/dow-monitor/suddenAnomalyHighlights.ts`
- Create: `frontend/src/components/dow-monitor/suddenAnomalyHighlights.test.ts`

**Interfaces:**
- Consumes: values already expressed in display units by `deriveMonitorRow`.
- Produces:

```ts
export type SuddenAnomalyMetric =
  | 'changePct'
  | 'momentum1m'
  | 'volumeSpeed'
  | 'depthPressurePct'
  | 'toDayHighPct'
  | 'fromDayLowPct'

export interface SuddenAnomalyMetricReading {
  value: number | null
  delayed: boolean
}

export interface SuddenAnomalySymbolReading {
  symbol: string
  metrics: Record<SuddenAnomalyMetric, SuddenAnomalyMetricReading>
}

export interface SuddenAnomalyTrackerState {
  baselines: Record<string, number>
  expiresAt: Record<string, number>
}

export function suddenAnomalyKey(symbol: string, metric: SuddenAnomalyMetric): string
export function advanceSuddenAnomalyState(
  previous: SuddenAnomalyTrackerState,
  readings: SuddenAnomalySymbolReading[],
  nowMs: number,
): SuddenAnomalyTrackerState
export function activeSuddenAnomalyKeys(
  state: SuddenAnomalyTrackerState,
  nowMs: number,
): Set<string>
```

- [ ] **Step 1: Write failing threshold and lifecycle tests**

Use table-driven cases:

```ts
const cases = [
  ['changePct', 1, 1.50],
  ['momentum1m', 0.1, 0.50],
  ['volumeSpeed', 1.2, 2.20],
  ['depthPressurePct', -10, 30],
  ['toDayHighPct', 2, 1.50],
  ['fromDayLowPct', 1, 1.50],
] as const
```

For each case, establish the first baseline, advance at the exact threshold, and expect
`activeSuddenAnomalyKeys` to contain `suddenAnomalyKey('700.HK', metric)`.

Add separate tests that prove:

- a delta `0.01` below each threshold does not trigger;
- first load does not trigger;
- delayed or missing input removes the baseline and recovery first value does not trigger;
- the key is active at `trigger + 9_999ms` and inactive at `trigger + 10_000ms`;
- another qualifying change at `trigger + 5_000ms` moves expiry to `trigger + 15_000ms`;
- omitting a symbol from the next readings removes its baselines and expiry.

- [ ] **Step 2: Run the state-machine tests and verify RED**

Run:

```powershell
pnpm exec vitest run src/components/dow-monitor/suddenAnomalyHighlights.test.ts
```

Expected: FAIL because the state machine module does not exist.

- [ ] **Step 3: Implement the minimal state machine**

Define exact thresholds:

```ts
export const SUDDEN_ANOMALY_THRESHOLDS: Record<SuddenAnomalyMetric, number> = {
  changePct: 0.50,
  momentum1m: 0.40,
  volumeSpeed: 1.00,
  depthPressurePct: 40,
  toDayHighPct: 0.50,
  fromDayLowPct: 0.50,
}

export const SUDDEN_ANOMALY_DURATION_MS = 10_000
```

Normalize symbols to uppercase in keys. On every advance:

1. retain entries only for symbols present in `readings`;
2. prune expiries `<= nowMs`;
3. delete a metric baseline and expiry when its value is non-finite or delayed;
4. compare a valid value with the prior baseline;
5. set expiry to `nowMs + 10_000` when `Math.abs(next - previous) >= threshold`;
6. always replace the valid baseline with the latest value.

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```powershell
pnpm exec vitest run src/components/dow-monitor/suddenAnomalyHighlights.test.ts
```

Expected: all new state-machine tests PASS.

- [ ] **Step 5: Commit the state machine**

```powershell
git add frontend/src/components/dow-monitor/suddenAnomalyHighlights.ts `
  frontend/src/components/dow-monitor/suddenAnomalyHighlights.test.ts
git commit -m "feat(dow-monitor): detect sudden metric anomalies"
```

### Task 3: Add the Hook and Render Exact-Value Highlights

**Files:**
- Create: `frontend/src/components/dow-monitor/useSuddenAnomalyHighlights.ts`
- Create: `frontend/src/components/dow-monitor/useSuddenAnomalyHighlights.test.tsx`
- Modify: `frontend/src/components/dow-monitor/DowMonitorList.tsx`
- Modify: `frontend/src/components/dow-monitor/DowMonitorList.test.tsx`

**Interfaces:**
- Consumes: `SuddenAnomalySymbolReading[]`.
- Produces:

```ts
export function useSuddenAnomalyHighlights(
  readings: SuddenAnomalySymbolReading[],
): ReadonlySet<string>
```

- [ ] **Step 1: Write failing Hook timer tests**

Render a harness with one symbol. Rerender from `changePct=1.00` to `1.50`, use
`vi.useFakeTimers()` and `vi.setSystemTime(...)`, then assert:

```ts
expect(result.current.has(suddenAnomalyKey('700.HK', 'changePct'))).toBe(true)
await vi.advanceTimersByTimeAsync(9_999)
expect(result.current.has(key)).toBe(true)
await vi.advanceTimersByTimeAsync(1)
expect(result.current.has(key)).toBe(false)
```

Add a second trigger at five seconds and prove the key remains active until ten seconds after the second trigger. Assert unmount clears the nearest-expiry timer.

- [ ] **Step 2: Run the Hook test and verify RED**

Run:

```powershell
pnpm exec vitest run src/components/dow-monitor/useSuddenAnomalyHighlights.test.tsx
```

Expected: FAIL because the Hook does not exist.

- [ ] **Step 3: Implement the minimal Hook**

Use one `useRef<SuddenAnomalyTrackerState>`, one `useState<ReadonlySet<string>>`, and one timeout for the nearest active expiry. Advance the pure state machine when `readings` changes, update the active key set, and reschedule only the nearest expiry. Clean up the timeout on dependency change and unmount.

- [ ] **Step 4: Run the Hook test and verify GREEN**

Run the command from Step 2. Expected: PASS with no leaked timer warning.

- [ ] **Step 5: Write the failing list integration test**

Rerender the same `DowMonitorList` instance with valid fresh realtime values:

1. first render establishes baselines and contains no `异动`;
2. second render changes all six metrics by exactly their thresholds;
3. assert six wrappers exist with test IDs:

```text
anomaly-changePct-700.HK
anomaly-momentum1m-700.HK
anomaly-volumeSpeed-700.HK
anomaly-depthPressurePct-700.HK
anomaly-toDayHighPct-700.HK
anomaly-fromDayLowPct-700.HK
```

Each active wrapper must have `border-danger`, `bg-danger/10`, `text-danger`, visible
`异动`, and an accessible name containing the metric label plus `突发异动`.

Assert the row and group cells do not receive `bg-danger/10`. Add a delayed rerender proving no marker appears after recovery first value. Assert the existing formal `买入确认` and its Beijing timestamp are unchanged.

- [ ] **Step 6: Run the list test and verify RED**

Run:

```powershell
pnpm exec vitest run src/components/dow-monitor/DowMonitorList.test.tsx
```

Expected: FAIL because no anomaly wrappers or markers are rendered.

- [ ] **Step 7: Integrate readings and exact-value wrappers**

Move per-item `deriveMonitorRow` calls before JSX mapping so the Hook receives one current-page reading array. Map freshness as:

```ts
changePct: { value: row.changePct, delayed: forceDelayed || row.freshness.quote.delayed }
momentum1m: { value: row.momentumSpeed.momentum1m.valuePct, delayed: forceDelayed || row.freshness.candlestick.delayed }
volumeSpeed: { value: row.volumeFunds.volumeSpeed, delayed: forceDelayed || row.freshness.candlestick.delayed }
depthPressurePct: { value: row.volumeFunds.depthPressurePct, delayed: forceDelayed || row.freshness.depth.delayed }
toDayHighPct: { value: row.breakoutRisk.toDayHighPct, delayed: forceDelayed || row.freshness.quote.delayed }
fromDayLowPct: { value: row.breakoutRisk.fromDayLowPct, delayed: forceDelayed || row.freshness.quote.delayed }
```

Add a focused `AnomalyMetric` renderer:

```tsx
<span
  data-testid={`anomaly-${metric}-${symbol}`}
  aria-label={active ? `${label}，突发异动` : label}
  className={cn(
    'inline-flex items-center gap-1',
    active && 'rounded border border-danger bg-danger/10 px-1 text-danger',
  )}
>
  <span>{children}</span>
  {active && <span className="text-[9px] font-semibold">异动</span>}
</span>
```

Wrap only the six target values. Do not wrap cells, rows, stable indicators, or signal badges.

- [ ] **Step 8: Run focused tests and verify GREEN**

Run:

```powershell
pnpm exec vitest run `
  src/components/dow-monitor/suddenAnomalyHighlights.test.ts `
  src/components/dow-monitor/useSuddenAnomalyHighlights.test.tsx `
  src/components/dow-monitor/DowMonitorList.test.tsx `
  src/components/dow-monitor/monitorListPresentation.test.ts
```

Expected: all focused tests PASS, including existing signal-stability assertions.

- [ ] **Step 9: Commit the Hook and UI**

```powershell
git add frontend/src/components/dow-monitor/useSuddenAnomalyHighlights.ts `
  frontend/src/components/dow-monitor/useSuddenAnomalyHighlights.test.tsx `
  frontend/src/components/dow-monitor/DowMonitorList.tsx `
  frontend/src/components/dow-monitor/DowMonitorList.test.tsx
git commit -m "feat(dow-monitor): highlight sudden metric changes"
```

### Task 4: Explain the Feature and Complete Acceptance Evidence

**Files:**
- Modify: `frontend/src/pages/DowMonitorHelp.tsx`
- Modify: `frontend/src/pages/DowMonitorHelp.test.tsx`
- Modify: `docs/acceptance/dow-monitor-sudden-anomaly-highlight.md`
- Modify: `docs/reviews/dow-monitor-sudden-anomaly-highlight.md`
- Modify: `E:/Obsidian-alwin/alwin/longbridge-stock/dow-monitor-system-api-runbook.md`

**Interfaces:**
- Consumes: the final detector constants and behavior.
- Produces: user-facing explanation, semantic acceptance, independent review, and current runbook guidance.

- [ ] **Step 1: Write the failing help-page behavior test**

Require a structured “突发异动高亮” section containing:

```text
10 秒
涨跌幅 0.50 个百分点
1m 涨速 0.40 个百分点
量速 1.00 倍
五档盘口 40 个百分点
距日高/日低 0.50 个百分点
仅作观察
不改变买卖信号
```

- [ ] **Step 2: Run the help test and verify RED**

Run:

```powershell
pnpm exec vitest run src/pages/DowMonitorHelp.test.tsx
```

Expected: FAIL because the help section is absent.

- [ ] **Step 3: Add the help section**

Place it after the four-group indicator explanation and before common misconceptions. Explain first-load suppression, delayed/missing reset, recovery baseline, exact thresholds, 10-second lifetime, and the formal-signal boundary. Keep it static: no API call and no WebSocket.

- [ ] **Step 4: Run the help test and verify GREEN**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Complete acceptance and independent review**

Update semantic acceptance from `待实现` to `通过`, recording RED and GREEN outputs for:

- pure threshold/lifecycle tests;
- Hook timer tests;
- exact-value component rendering;
- help content;
- unchanged formal signal.

Independently walk from the authoritative requirement to detector constants, state transitions, freshness mapping, exact rendered wrappers, timers, tests, and help copy. Explicitly state that screenshot/build success is not used as threshold or signal-boundary proof.

Update the Obsidian runbook with the six thresholds, 10-second rule, delayed/missing reset, page cleanup, source freshness mapping, test commands, and the unchanged 3018/19912 and formal-signal boundaries. Do not record a production image until a separate deployment actually occurs.

- [ ] **Step 6: Run full verification**

Run:

```powershell
pnpm exec vitest run `
  src/components/dow-monitor/suddenAnomalyHighlights.test.ts `
  src/components/dow-monitor/useSuddenAnomalyHighlights.test.tsx `
  src/components/dow-monitor/monitorListPresentation.test.ts `
  src/components/dow-monitor/DowMonitorList.test.tsx `
  src/pages/DowMonitorHelp.test.tsx
pnpm build
python -m pytest `
  tests/spec_contracts/test_dow_monitor_sudden_anomaly_highlight_contract.py `
  tests/spec_contracts/test_dow_monitor_p0_clarity_contract.py `
  tests/spec_contracts/test_dow_monitor_list_websocket_contract.py -q
python scripts/check_spec_compliance.py
git diff --check
```

Expected: all feature tests, build, and three spec-contract files PASS. The spec checker may report only the two pre-existing baselines recorded in Task 1.

- [ ] **Step 7: Commit documentation and acceptance**

```powershell
git add frontend/src/pages/DowMonitorHelp.tsx `
  frontend/src/pages/DowMonitorHelp.test.tsx `
  docs/acceptance/dow-monitor-sudden-anomaly-highlight.md `
  docs/reviews/dow-monitor-sudden-anomaly-highlight.md
git commit -m "docs(dow-monitor): explain sudden anomaly highlights"
```

The external Obsidian runbook is updated in the same task but is not part of this repository commit.

