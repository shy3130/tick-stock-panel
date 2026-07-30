# Trend Monitor Key Interpretation Column Implementation Plan

> **Status:** Superseded by the approved opportunity/anomaly interpreter redesign.
> Do not execute this plan. A replacement plan must be written only after the user
> reviews the revised authoritative design specification.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic three-line “重点解读” column that keeps every raw indicator visible while making each of the 20 monitored stocks fast to scan.

**Architecture:** A pure TypeScript interpreter converts the existing `MonitorRowPresentation` plus active anomaly metrics into three structured lines. A focused presentational cell renders those lines; `DowMonitorList` only supplies row data and places the new column after the intraday chart. No new backend, WebSocket, timer, persistence, model call, or formal signal is introduced.

**Tech Stack:** React 18, TypeScript, Vitest, Testing Library, Tailwind CSS, static HTML prototype, Python specification-contract tests.

## Global Constraints

- Authoritative requirement ID: `REQ-DOW-MONITOR-KEY-INTERPRETATION-COLUMN-001`.
- Keep every current raw indicator column visible.
- Place `重点解读` after `日内走势` and before `趋势 / 位置`.
- Render exactly three semantic lines: state, evidence, and risk.
- Each line contains at most two short phrases.
- The column has a stable minimum width of about 260px and must not collapse into per-character wrapping.
- Use only values already produced by `deriveMonitorRow` and the existing 10-second anomaly state.
- Do not call an LLM and do not add API requests, WebSocket subscriptions, polling, timers, persistence, sorting, notifications, or automatic scrolling.
- Missing or delayed evidence must be explicit; never substitute zero or infer a value.
- Only anomaly and explicit risk phrases may use red; do not color the whole cell or row.
- Red cannot be the only anomaly cue; visible `异动` copy and a complete accessible label are required.
- The interpreter must never output `建议买入`, `建议卖出`, `立即操作`, `止盈`, or `止损`.
- Do not change the formal signal label, time, source, selection, persistence, notification, backend decision, 3018/19912 responsibilities, or existing WebSocket behavior.
- A static prototype must be shown to the user and explicitly approved before production component work begins.
- Do not deploy until the user separately requests deployment.

---

### Task 1: Register Authority, Traceability, and Acceptance

**Files:**
- Modify: `docs/spec-index.yaml`
- Modify: `docs/traceability.yaml`
- Create: `docs/acceptance/dow-monitor-key-interpretation-column.md`
- Create: `docs/reviews/dow-monitor-key-interpretation-column.md`
- Create: `tests/spec_contracts/test_dow_monitor_key_interpretation_column_contract.py`

**Interfaces:**
- Consumes: approved design `docs/superpowers/specs/2026-07-30-dow-monitor-key-interpretation-column-design.md`.
- Produces: specification ID `USER-20260730-DOW-MONITOR-KEY-INTERPRETATION-COLUMN` and requirement ID `REQ-DOW-MONITOR-KEY-INTERPRETATION-COLUMN-001`.

- [ ] **Step 1: Write the failing specification contract**

Create a Python contract that loads the specification index and traceability YAML, then asserts:

```python
SPEC_ID = "USER-20260730-DOW-MONITOR-KEY-INTERPRETATION-COLUMN"
REQ_ID = "REQ-DOW-MONITOR-KEY-INTERPRETATION-COLUMN-001"

assert specification["status"] == "authoritative"
assert specification["requirements"] == [REQ_ID]
assert entry["specification"] == SPEC_ID
```

Require these implementation paths:

```python
required_implementation = {
    "frontend/src/components/dow-monitor/keyInterpretation.ts",
    "frontend/src/components/dow-monitor/KeyInterpretationCell.tsx",
    "frontend/src/components/dow-monitor/DowMonitorList.tsx",
    "frontend/src/pages/DowMonitorHelp.tsx",
}
assert required_implementation <= set(entry["implementation"])
```

Add a second contract test that runs this behavioral suite:

```text
src/components/dow-monitor/keyInterpretation.test.ts
src/components/dow-monitor/DowMonitorList.test.tsx
src/pages/DowMonitorHelp.test.tsx
```

- [ ] **Step 2: Run the contract and verify RED**

Run:

```powershell
python -m pytest tests/spec_contracts/test_dow_monitor_key_interpretation_column_contract.py -q
```

Expected: FAIL because the new specification and traceability entries do not exist.

- [ ] **Step 3: Register the approved design**

Add this exact specification entry:

```yaml
  - id: USER-20260730-DOW-MONITOR-KEY-INTERPRETATION-COLUMN
    path: docs/superpowers/specs/2026-07-30-dow-monitor-key-interpretation-column-design.md
    status: authoritative
    requirements:
      - REQ-DOW-MONITOR-KEY-INTERPRETATION-COLUMN-001
```

Add traceability with the four implementation paths, the executable contract, and:

```yaml
    acceptance:
      - {path: docs/acceptance/dow-monitor-key-interpretation-column.md, type: semantic-acceptance}
      - {path: docs/reviews/dow-monitor-key-interpretation-column.md, type: independent-review}
```

The acceptance document must copy the exact placement, three-line structure, two-phrase limit, missing-data behavior, anomaly/risk styling, and formal-signal boundary from the approved design. Set its status to `用户已批准语义；执行证据待补`.

- [ ] **Step 4: Run the specification contract**

Run:

```powershell
python -m pytest tests/spec_contracts/test_dow_monitor_key_interpretation_column_contract.py -q
python scripts/check_spec_compliance.py
```

Expected: the dedicated traceability test passes. The repository checker may still report only the two existing baselines: expired `EXC-COLLECTION-MONITOR-PREACCEPTANCE-DEPLOY-001` and the historical detail-toggle test path outside `tests/`.

- [ ] **Step 5: Commit authority registration**

```powershell
git add docs/spec-index.yaml docs/traceability.yaml `
  docs/acceptance/dow-monitor-key-interpretation-column.md `
  docs/reviews/dow-monitor-key-interpretation-column.md `
  tests/spec_contracts/test_dow_monitor_key_interpretation_column_contract.py
git commit -m "docs(dow-monitor): register key interpretation column"
```

### Task 2: Build and Review the Static Prototype

**Files:**
- Create: `output/playwright/dow-monitor-key-interpretation-prototype.html`

**Interfaces:**
- Consumes: the approved three-line design and the current dark table layout.
- Produces: a self-contained visual prototype; it does not become a production dependency.

- [ ] **Step 1: Build a self-contained table prototype**

Create a static HTML page showing at least five rows:

```text
上升通道 · 动量同向上
量速 2.4× · 资金流入 61%
异动：盘口 +72% · 距日高 0.3%
```

```text
下降通道 · 动量同向下
卖盘占优 -58% · 量能 1.8×
接近日低 0.4% · 振幅 1.2 ATR
```

```text
震荡/过渡 · 周期分歧
量能一般 · 资金待确认
等待稳定周期确认
```

Also include delayed-data and no-prominent-risk rows. Keep all existing columns visible in the mock table and place `重点解读` between `日内走势` and `趋势 / 位置`.

- [ ] **Step 2: Validate the prototype in a real browser**

Open the file with Playwright, then assert:

```ts
await expect(page.getByRole('columnheader', { name: '重点解读' })).toBeVisible()
await expect(page.getByText('上升通道 · 动量同向上')).toBeVisible()
await expect(page.getByText('异动：盘口 +72% · 距日高 0.3%')).toBeVisible()
```

Inspect at desktop width and a narrower viewport. Confirm the column remains about 260px, three lines stay readable, and the red treatment applies only to the anomaly/risk phrase.

- [ ] **Step 3: Present the prototype and stop for approval**

Open `output/playwright/dow-monitor-key-interpretation-prototype.html` for the user. Do not start Task 3 until the user explicitly approves the prototype.

The prototype is an untracked review artifact and is not committed.

### Task 3: Implement the Pure Interpretation Model with TDD

**Files:**
- Create: `frontend/src/components/dow-monitor/keyInterpretation.ts`
- Create: `frontend/src/components/dow-monitor/keyInterpretation.test.ts`
- Modify: `frontend/src/components/dow-monitor/suddenAnomalyHighlights.ts`

**Interfaces:**
- Consumes:

```ts
MonitorRowPresentation
ReadonlySet<SuddenAnomalyMetric>
```

- Produces:

```ts
export type KeyInterpretationTone = 'default' | 'muted' | 'anomaly' | 'risk'
export type KeyInterpretationLineKind = 'state' | 'evidence' | 'risk'

export interface KeyInterpretationPhrase {
  text: string
  tone: KeyInterpretationTone
  metric?: SuddenAnomalyMetric
}

export interface KeyInterpretationLine {
  kind: KeyInterpretationLineKind
  phrases: KeyInterpretationPhrase[]
}

export interface KeyInterpretation {
  lines: [
    KeyInterpretationLine,
    KeyInterpretationLine,
    KeyInterpretationLine,
  ]
  accessibleText: string
}

export function deriveKeyInterpretation(
  row: MonitorRowPresentation,
  activeAnomalies: ReadonlySet<SuddenAnomalyMetric>,
): KeyInterpretation
```

Also export the existing metric order:

```ts
export const SUDDEN_ANOMALY_METRICS: SuddenAnomalyMetric[]
```

using the current order `changePct`, `momentum1m`, `volumeSpeed`, `depthPressurePct`, `toDayHighPct`, `fromDayLowPct`.

- [ ] **Step 1: Write failing state-line tests**

Create fixture builders for `MonitorRowPresentation`. Assert:

```ts
expect(deriveKeyInterpretation(upRow, new Set()).lines[0]).toEqual({
  kind: 'state',
  phrases: [
    { text: '上升通道', tone: 'default' },
    { text: '动量同向上', tone: 'default' },
  ],
})
```

Add cases for:

-下降通道 plus three downward momentum periods;
- stable 5m/15m down with 1m up → `短线转强`;
- stable 5m/15m up with 1m down → `短线转弱`;
- other mixed directions → `周期分歧`;
- insufficient valid periods → `动量待确认`;
- unknown channel → `通道数据不足`.

- [ ] **Step 2: Write failing evidence and risk tests**

Assert evidence selection is deterministic and limited:

```ts
expect(result.lines[1].phrases).toHaveLength(2)
expect(result.lines[1].phrases.map(item => item.text)).toEqual([
  '量速异动 3.20×',
  '盘口异动 -65.00%',
])
```

Add cases proving:

- active volume/depth anomalies replace their normal phrases;
- volume speed precedes relative volume, capital inflow, and depth pressure;
- missing or delayed values are skipped;
- no usable evidence produces `量价待确认`;
- the risk line follows anomaly order exactly;
- quote/depth/K-line delay appears before normal position evidence;
- the nearer day boundary is shown without red when it is only positional;
- `riskTitle` is red;
- `dayRangeAtrRatio` is formatted as `振幅 1.20 ATR` without inventing a direction;
- fewer than two confirmed stable periods produces `周期 1/2` or `周期 0/2`;
- no selected risk produces `暂无突出风险`.

- [ ] **Step 3: Write failing boundary tests**

For representative up, down, anomaly, delayed, missing, and risk rows:

```ts
const text = deriveKeyInterpretation(row, anomalies).accessibleText
expect(text).not.toMatch(/建议买入|建议卖出|立即操作|止盈|止损/)
expect(result.lines).toHaveLength(3)
for (const line of result.lines) expect(line.phrases.length).toBeLessThanOrEqual(2)
```

Also prove an active anomaly with a missing current value is not rendered as an anomaly phrase.

- [ ] **Step 4: Run tests and verify RED**

Run:

```powershell
pnpm exec vitest run src/components/dow-monitor/keyInterpretation.test.ts
```

Expected: FAIL because `keyInterpretation.ts` does not exist.

- [ ] **Step 5: Implement the minimal pure interpreter**

Use focused helpers:

```ts
function stateLine(row: MonitorRowPresentation): KeyInterpretationLine
function evidenceLine(
  row: MonitorRowPresentation,
  active: ReadonlySet<SuddenAnomalyMetric>,
): KeyInterpretationLine
function riskLine(
  row: MonitorRowPresentation,
  active: ReadonlySet<SuddenAnomalyMetric>,
): KeyInterpretationLine
```

Formatting must reuse the display units already present in the row:

```ts
const percent = (value: number) => `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`
const ratio = (value: number) => `${value.toFixed(2)}×`
```

Deduplicate by metric key before taking `slice(0, 2)`. Build `accessibleText` as:

```ts
lines
  .map(line => line.phrases.map(phrase => phrase.text).join('，'))
  .join('；')
```

Do not read `row.signal`; formal signal content must not influence interpretation.

- [ ] **Step 6: Run tests and verify GREEN**

Run:

```powershell
pnpm exec vitest run `
  src/components/dow-monitor/keyInterpretation.test.ts `
  src/components/dow-monitor/suddenAnomalyHighlights.test.ts
```

Expected: both files pass.

- [ ] **Step 7: Commit the pure model**

```powershell
git add frontend/src/components/dow-monitor/keyInterpretation.ts `
  frontend/src/components/dow-monitor/keyInterpretation.test.ts `
  frontend/src/components/dow-monitor/suddenAnomalyHighlights.ts
git commit -m "feat(dow-monitor): derive key interpretations"
```

### Task 4: Render the New Column without Affecting Signals

**Files:**
- Create: `frontend/src/components/dow-monitor/KeyInterpretationCell.tsx`
- Modify: `frontend/src/components/dow-monitor/DowMonitorList.tsx`
- Modify: `frontend/src/components/dow-monitor/DowMonitorList.test.tsx`

**Interfaces:**
- Consumes: `KeyInterpretation` from Task 3.
- Produces:

```ts
export function KeyInterpretationCell({
  symbol,
  interpretation,
}: {
  symbol: string
  interpretation: KeyInterpretation
}): JSX.Element
```

- [ ] **Step 1: Write the failing placement and structure test**

Render one complete row and assert header order:

```ts
const headers = screen.getAllByRole('columnheader').map(node => node.textContent)
expect(headers.indexOf('日内走势')).toBeLessThan(headers.indexOf('重点解读'))
expect(headers.indexOf('重点解读')).toBeLessThan(headers.indexOf('趋势 / 位置'))
```

Assert:

```ts
const cell = screen.getByTestId('key-interpretation-700.HK')
expect(cell).toHaveClass('min-w-[260px]')
expect(within(cell).getAllByTestId(/interpretation-line-/)).toHaveLength(3)
expect(cell).toHaveAccessibleName(/上升通道.*动量同向上/)
```

- [ ] **Step 2: Write the failing anomaly styling test**

Reuse the existing anomaly rerender fixture. After a threshold-reaching update, assert:

```ts
const cell = screen.getByTestId('key-interpretation-700.HK')
const anomaly = within(cell).getByText(/异动/)
expect(anomaly).toHaveClass('text-danger')
expect(cell).not.toHaveClass('bg-danger/10', 'text-danger')
expect(screen.getByRole('row', { name: /腾讯控股/ })).not.toHaveClass('bg-danger/10')
```

Assert the existing exact numeric anomaly wrappers still show all six `异动` labels.

- [ ] **Step 3: Write the failing formal-signal regression**

Capture the formal signal before and after the same real-time rerender:

```ts
expect(screen.getByText('买入确认')).toBeInTheDocument()
expect(screen.getByText('北京时间 09:34')).toBeInTheDocument()
```

Also assert the summary never contains those signal strings and the action button remains `查看详情`.

- [ ] **Step 4: Run list tests and verify RED**

Run:

```powershell
pnpm exec vitest run src/components/dow-monitor/DowMonitorList.test.tsx
```

Expected: FAIL because the `重点解读` header and cell do not exist.

- [ ] **Step 5: Implement the focused presentational cell**

Map tones exactly:

```ts
const toneClass = {
  default: 'text-foreground',
  muted: 'text-muted',
  anomaly: 'font-semibold text-danger',
  risk: 'font-medium text-danger',
} satisfies Record<KeyInterpretationTone, string>
```

Render an `<td>` with:

```tsx
<td
  data-testid={`key-interpretation-${symbol}`}
  aria-label={`重点解读，${interpretation.accessibleText}`}
  title={interpretation.accessibleText}
  className="min-w-[260px] border-b border-border px-3 py-2 align-top"
>
```

Each semantic line is a compact block. Join phrases with a visible `·`, but keep phrase spans separate so only anomaly/risk phrases receive red.

- [ ] **Step 6: Integrate with the current anomaly set**

Inside each presented row, build:

```ts
const activeMetrics = new Set(
  SUDDEN_ANOMALY_METRICS.filter(metric =>
    anomalyHighlights.has(suddenAnomalyKey(item.symbol, metric))),
)
const interpretation = deriveKeyInterpretation(row, activeMetrics)
```

Insert the header and cell after `日内走势`. Do not change `deriveMonitorRow`, signal selection, pagination, selection handlers, detail controls, or the existing numeric anomaly wrappers.

- [ ] **Step 7: Run integration tests and verify GREEN**

Run:

```powershell
pnpm exec vitest run `
  src/components/dow-monitor/keyInterpretation.test.ts `
  src/components/dow-monitor/DowMonitorList.test.tsx `
  src/components/dow-monitor/monitorListPresentation.test.ts
```

Expected: all pass.

- [ ] **Step 8: Commit list integration**

```powershell
git add frontend/src/components/dow-monitor/KeyInterpretationCell.tsx `
  frontend/src/components/dow-monitor/DowMonitorList.tsx `
  frontend/src/components/dow-monitor/DowMonitorList.test.tsx
git commit -m "feat(dow-monitor): show key interpretation column"
```

### Task 5: Explain the Column and Complete Semantic Evidence

**Files:**
- Modify: `frontend/src/pages/DowMonitorHelp.tsx`
- Modify: `frontend/src/pages/DowMonitorHelp.test.tsx`
- Modify: `docs/acceptance/dow-monitor-key-interpretation-column.md`
- Modify: `docs/reviews/dow-monitor-key-interpretation-column.md`
- Modify outside repository: `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md`

**Interfaces:**
- Consumes: final interpretation semantics and passing behavior tests.
- Produces: user-facing explanation, semantic acceptance evidence, independent review, and current operations documentation.

- [ ] **Step 1: Write the failing help-page test**

Require a new navigation link and section:

```ts
expect(screen.getByRole('heading', { name: '重点解读' })).toBeInTheDocument()
expect(within(navigation).getByRole('link', { name: '重点解读' }))
  .toHaveAttribute('href', '#key-interpretation')
```

Require explanations containing:

```text
状态、证据、风险
每行最多两个重点
数据不足
周期分歧
不改变买卖信号
核对右侧原始指标
```

- [ ] **Step 2: Run the help test and verify RED**

Run:

```powershell
pnpm exec vitest run src/pages/DowMonitorHelp.test.tsx
```

Expected: FAIL because the section does not exist.

- [ ] **Step 3: Add the help section**

Place `重点解读` before the detailed indicator groups. Explain the fixed three-line reading order, anomaly/risk colors, missing-data language, raw-evidence verification, and formal-signal boundary. Do not add trading instructions.

- [ ] **Step 4: Run the help test and verify GREEN**

Run:

```powershell
pnpm exec vitest run src/pages/DowMonitorHelp.test.tsx
```

Expected: PASS.

- [ ] **Step 5: Complete acceptance and independent review**

Record the observed RED/GREEN evidence for:

- specification contract;
- pure interpreter;
- list placement;
- anomaly styling;
- formal signal non-interference;
- help content.

Independently map each acceptance criterion to code and an executable assertion. Explicitly state that screenshots and the prototype were not used as semantic proof.

- [ ] **Step 6: Update the Obsidian runbook**

Add a dated section covering:

- 260px placement after the intraday chart;
- three-line state/evidence/risk semantics;
- anomaly priority and signal boundary;
- implementation and test paths;
- current 3018 production image remains unchanged until a separate deployment request;
- existing mini/detail chart and 19912 verification flow remains unchanged.

- [ ] **Step 7: Commit help and evidence**

```powershell
git add frontend/src/pages/DowMonitorHelp.tsx `
  frontend/src/pages/DowMonitorHelp.test.tsx `
  docs/acceptance/dow-monitor-key-interpretation-column.md `
  docs/reviews/dow-monitor-key-interpretation-column.md
git commit -m "docs(dow-monitor): explain key interpretations"
```

The Obsidian runbook is outside the repository and must not be added to this Git commit.

### Task 6: Run Final Verification and Prepare a Release Handoff

**Files:**
- Verify only; modify acceptance/review evidence if command results differ from their recorded values.

**Interfaces:**
- Consumes: all Tasks 1–5.
- Produces: a verified local branch ready for a separate deployment request.

- [ ] **Step 1: Run the focused semantic suite**

```powershell
pnpm exec vitest run `
  src/components/dow-monitor/keyInterpretation.test.ts `
  src/components/dow-monitor/suddenAnomalyHighlights.test.ts `
  src/components/dow-monitor/useSuddenAnomalyHighlights.test.tsx `
  src/components/dow-monitor/monitorListPresentation.test.ts `
  src/components/dow-monitor/DowMonitorList.test.tsx `
  src/pages/DowMonitorHelp.test.tsx
```

Expected: all tests pass.

- [ ] **Step 2: Build the production frontend**

```powershell
pnpm build
```

Expected: TypeScript and Vite build pass. Existing chunk-size warnings are non-blocking; compile or type errors are blocking.

- [ ] **Step 3: Run related specification contracts**

```powershell
python -m pytest `
  tests/spec_contracts/test_dow_monitor_key_interpretation_column_contract.py `
  tests/spec_contracts/test_dow_monitor_sudden_anomaly_highlight_contract.py `
  tests/spec_contracts/test_dow_monitor_p0_clarity_contract.py `
  tests/spec_contracts/test_dow_monitor_list_websocket_contract.py -q
python scripts/check_spec_compliance.py
```

Expected: related contracts pass. Record the repository checker exactly; do not claim full compliance if the two existing baselines remain.

- [ ] **Step 4: Run the full frontend suite**

```powershell
pnpm exec vitest run --reporter=dot
```

Expected: interpretation tests pass. If the existing Screener integration assertion still fails, record its exact output and prove this task did not modify Screener files; do not call the full suite green.

- [ ] **Step 5: Perform the independent requirement-to-evidence review**

Check:

```powershell
git diff --check
git status --short
git diff --name-only
```

Verify each design acceptance criterion has direct code and behavioral evidence. Preserve unrelated `.playwright-cli/` and `output/` content.

- [ ] **Step 6: Report release readiness without deploying**

Report:

- exact passing focused-test and contract counts;
- build result;
- full-suite result including unrelated baselines;
- branch and commit;
- prototype approval evidence;
- explicit statement that 10.28 still runs the prior production image.

Deployment is a separate user-authorized task and must use the existing 10.28 candidate-image, isolated-health-check, rollback-container, static-hash, symbol-preservation, and runbook-update flow.
