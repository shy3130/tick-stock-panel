# Dow Monitor Hourly AI Decision-First View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the hourly intraday AI dialog lead with a concise decision summary and three readable next-stage conditions while keeping complete evidence closed by default and available with one disclosure control.

**Architecture:** Keep the existing structured `DowMonitorAiStageReport` payload and legacy renderer unchanged. Refactor only the hourly React presenter into a decision-summary region plus one native, accessible `<details>` disclosure containing three grouped evidence sections. Register the approved presentation rule as a new requirement and prove it with component behavior tests, the existing frontend contract suite, semantic acceptance, and a fresh requirements-to-evidence review.

**Tech Stack:** React 18, TypeScript 5.5, Tailwind CSS, Vitest 2, Testing Library, pytest specification contracts, Markdown/YAML specification records.

## Global Constraints

- Authoritative parent specification: `SPEC-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001`.
- New stable requirement: `REQ-DOW-MONITOR-HOURLY-AI-DECISION-FIRST-VIEW-001`.
- The default first screen shows the conclusion, opportunity/risk direction, one holder sentence, one watcher sentence, and the available strengthening/risk/invalidation groups.
- Complete evidence is closed by default behind one accessible control and can be closed again.
- Next-stage groups are vertical and text-labelled; colour is supplementary only.
- Do not add Markdown parsing or alter report generation, schema, ClickHouse, formal signals, real-time interpretation, WebSocket ingestion, or the legacy 30-minute renderer.
- Desktop and mobile use the same information order; mobile remains one column without horizontal scrolling.
- Empty optional values do not create empty blocks.
- Update `E:/Obsidian-alwin/alwin/longbridge-stock/dow-monitor-system-api-runbook.md` in the same implementation task.
- Production deployment is not authorized by this plan and remains a separate user decision.

---

## File structure

- `frontend/src/components/dow-monitor/DowMonitorAiStageReport.tsx`: owns the structured hourly report presentation, decision summary, condition cards, and full-analysis disclosure.
- `frontend/src/components/dow-monitor/DowMonitorAiStageReport.test.tsx`: proves default priority, disclosure toggling, vertical condition semantics, and empty-value omission.
- `tests/frontend/test_dow_monitor_half_hour_ai_frontend.py`: retains the repository-level executable frontend contract; no code change is expected unless the targeted test list changes.
- `tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py`: proves the new requirement is authoritative and traced.
- `docs/specs/dow-monitor-half-hour-ai-analysis.md`: authoritative normative behavior.
- `docs/spec-index.yaml`: authoritative requirement registration.
- `docs/traceability.yaml`: requirement-to-implementation/test/acceptance mapping.
- `docs/acceptance/dow-monitor-hourly-ai-decision-first-view.md`: semantic acceptance evidence.
- `docs/reviews/2026-08-02-dow-monitor-hourly-ai-decision-first-view-review.md`: independent requirements-to-evidence review.
- `docs/superpowers/specs/2026-08-02-dow-monitor-hourly-ai-decision-first-view-design.md`: approved design status and rationale.
- `E:/Obsidian-alwin/alwin/longbridge-stock/dow-monitor-system-api-runbook.md`: current 3018 UI and verification behavior.

---

### Task 1: Activate the approved decision-first requirement

**Files:**
- Modify: `docs/superpowers/specs/2026-08-02-dow-monitor-hourly-ai-decision-first-view-design.md:1-10`
- Modify: `docs/specs/dow-monitor-half-hour-ai-analysis.md:1-15,103-120`
- Modify: `docs/spec-index.yaml:117-127`
- Modify: `docs/traceability.yaml:542-556`
- Modify: `tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py:8-60`

**Interfaces:**
- Consumes: explicit user approval of design option A and parent specification `SPEC-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001`.
- Produces: authoritative requirement `REQ-DOW-MONITOR-HOURLY-AI-DECISION-FIRST-VIEW-001` with planned implementation, executable test, and acceptance paths.

- [ ] **Step 1: Extend the specification contract with the new requirement**

Add the requirement to `REQUIREMENT_IDS`, add the design path constant, and add this executable assertion:

```python
DECISION_FIRST_DESIGN_PATH = (
    "docs/superpowers/specs/"
    "2026-08-02-dow-monitor-hourly-ai-decision-first-view-design.md"
)


def test_hourly_ai_view_is_decision_first_and_complete_evidence_is_disclosed() -> None:
    spec = (ROOT / SPEC_PATH).read_text(encoding="utf-8")
    design = (ROOT / DECISION_FIRST_DESIGN_PATH).read_text(encoding="utf-8")

    assert "REQ-DOW-MONITOR-HOURLY-AI-DECISION-FIRST-VIEW-001" in spec
    assert "decision summary" in spec
    assert "closed by default" in spec
    assert "strengthening, risk and invalidation" in spec
    assert "Status: approved" in design
```

- [ ] **Step 2: Run the contract and observe the expected failure**

Run:

```powershell
python -m pytest tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py -q
```

Expected: FAIL because the new requirement and approved written status are not yet registered.

- [ ] **Step 3: Register the normative requirement and traceability**

Change the design status to `Status: approved`. Add the requirement ID to the specification header and `docs/spec-index.yaml`. Append this normative section to the authoritative specification:

```markdown
## REQ-DOW-MONITOR-HOURLY-AI-DECISION-FIRST-VIEW-001

The structured hourly detail view MUST place a decision summary before its
supporting evidence. The summary MUST include the concise conclusion,
opportunity/risk direction, separate concise holder and watcher guidance, and
available strengthening, risk and invalidation conditions. The three condition
groups MUST be separately labelled and stacked vertically.

Minute path, hidden changes, previous-stage comparison, cumulative structure,
channel/pattern, volume/capital interpretation, confidence and data quality
MUST remain available through one disclosure control that is closed by default
and can be closed again. Empty optional values MUST NOT create empty blocks.
The frontend MUST render structured text without Markdown parsing and MUST keep
the same single-column information order on mobile.
```

Add the traceability entry with these exact paths:

```yaml
  - id: REQ-DOW-MONITOR-HOURLY-AI-DECISION-FIRST-VIEW-001
    specification: SPEC-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001
    implementation:
      - frontend/src/components/dow-monitor/DowMonitorAiStageReport.tsx
    tests:
      - {path: frontend/src/components/dow-monitor/DowMonitorAiStageReport.test.tsx, type: executable-test}
      - {path: tests/frontend/test_dow_monitor_half_hour_ai_frontend.py, type: executable-test}
      - {path: tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py, type: executable-test}
    acceptance:
      - {path: docs/acceptance/dow-monitor-hourly-ai-decision-first-view.md, type: semantic-acceptance}
      - {path: docs/reviews/2026-08-02-dow-monitor-hourly-ai-decision-first-view-review.md, type: independent-review}
```

- [ ] **Step 4: Re-run the focused contract**

Run:

```powershell
python -m pytest tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the authority and traceability change**

```powershell
git add docs/superpowers/specs/2026-08-02-dow-monitor-hourly-ai-decision-first-view-design.md docs/specs/dow-monitor-half-hour-ai-analysis.md docs/spec-index.yaml docs/traceability.yaml tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py
git commit -m "docs: specify decision-first hourly AI view"
```

---

### Task 2: Implement the decision-first structured report with TDD

**Files:**
- Modify: `frontend/src/components/dow-monitor/DowMonitorAiStageReport.test.tsx:1-104`
- Modify: `frontend/src/components/dow-monitor/DowMonitorAiStageReport.tsx:1-115`
- Test: `tests/frontend/test_dow_monitor_half_hour_ai_frontend.py`

**Interfaces:**
- Consumes: `DowMonitorHalfHourAiAnalysis.report`, especially `headline`, `holding_advice`, `watching_advice`, `next_stage_conditions`, stage evidence, `confidence`, and `analysis.data_quality`.
- Produces: `DowMonitorAiStageReport({ analysis })` with a visible decision summary and a closed native `<details>` element labelled `展开完整分析（分钟路径、形态、量价、数据质量）`.

- [ ] **Step 1: Replace the old all-open component assertion with decision-first behavior tests**

Import user interaction support:

```tsx
import userEvent from '@testing-library/user-event'
```

Add a default-priority and disclosure test using the existing complete `analysis` fixture:

```tsx
it('shows the decision summary first and keeps complete evidence closed by default', async () => {
  const user = userEvent.setup()
  render(<DowMonitorAiStageReport analysis={analysis} />)

  expect(screen.getByRole('heading', { name: analysis.report!.headline.title })).toBeVisible()
  expect(screen.getByText(analysis.report!.holding_advice.advice)).toBeVisible()
  expect(screen.getByText(analysis.report!.watching_advice.advice)).toBeVisible()
  expect(screen.getByRole('heading', { name: '增强确认' })).toBeVisible()
  expect(screen.getByRole('heading', { name: '风险出现' })).toBeVisible()
  expect(screen.getByRole('heading', { name: '判断失效' })).toBeVisible()

  const disclosure = screen
    .getByText('展开完整分析（分钟路径、形态、量价、数据质量）')
    .closest('details')
  expect(disclosure).not.toHaveAttribute('open')

  await user.click(screen.getByText('展开完整分析（分钟路径、形态、量价、数据质量）'))
  expect(disclosure).toHaveAttribute('open')
  expect(screen.getByRole('heading', { name: '本小时发生了什么' })).toBeVisible()
  expect(screen.getByRole('heading', { name: '当日整体结构与量价资金' })).toBeVisible()
  expect(screen.getByRole('heading', { name: '分析依据与数据质量' })).toBeVisible()

  await user.click(screen.getByText('收起完整分析'))
  expect(disclosure).not.toHaveAttribute('open')
})
```

Add a layout/empty-value test:

```tsx
it('stacks available next-stage groups and omits empty optional blocks', () => {
  const sparse = {
    ...analysis,
    report: {
      ...analysis.report!,
      watching_advice: { state: 'WAIT_CONFIRMATION' as const, advice: '', conditions: [] },
      next_stage_conditions: {
        strengthen: ['站稳阶段高点'],
        risk: [],
        invalidation: ['跌破阶段低点'],
      },
    },
  }
  render(<DowMonitorAiStageReport analysis={sparse} />)

  const conditions = screen.getByTestId('next-stage-conditions')
  expect(conditions).toHaveClass('grid-cols-1')
  expect(screen.getByRole('heading', { name: '增强确认' })).toBeVisible()
  expect(screen.queryByRole('heading', { name: '风险出现' })).not.toBeInTheDocument()
  expect(screen.getByRole('heading', { name: '判断失效' })).toBeVisible()
  expect(screen.queryByRole('heading', { name: '未参与者建议' })).not.toBeInTheDocument()
})
```

- [ ] **Step 2: Run the component test and observe the expected failure**

Run:

```powershell
pnpm --dir frontend test --run src/components/dow-monitor/DowMonitorAiStageReport.test.tsx
```

Expected: FAIL because the old component renders every section open, has no disclosure, and uses a responsive three-column next-stage grid.

- [ ] **Step 3: Add focused presentation helpers**

Keep the implementation in the existing component file and add these focused interfaces:

```tsx
const CONFIDENCE_LABELS = {
  HIGH: '高',
  MEDIUM: '中',
  LOW: '低',
} as const

function hasText(value: string | null | undefined) {
  return Boolean(value?.trim())
}

function List({ items }: { items: string[] }) {
  const visibleItems = items.filter(hasText)
  if (visibleItems.length === 0) return null
  return (
    <ul className="space-y-1.5">
      {visibleItems.map((item, index) => (
        <li key={`${item}-${index}`} className="break-words">{item}</li>
      ))}
    </ul>
  )
}

function AdviceCard({ title, advice }: { title: string; advice: string }) {
  if (!hasText(advice)) return null
  return (
    <section className="rounded-card border border-border p-3">
      <h4 className="text-xs font-medium text-muted">{title}</h4>
      <p className="mt-1 break-words leading-6 text-secondary">{advice}</p>
    </section>
  )
}

function ConditionCard({
  title,
  items,
  tone,
}: {
  title: string
  items: string[]
  tone: 'accent' | 'warning' | 'neutral'
}) {
  if (!items.some(hasText)) return null
  const toneClass = tone === 'accent'
    ? 'border-accent/40 bg-accent/5'
    : tone === 'warning'
      ? 'border-warning/40 bg-warning/5'
      : 'border-border bg-elevated'
  return (
    <section className={`rounded-card border p-3 ${toneClass}`}>
      <h4 className="font-medium">{title}</h4>
      <div className="mt-2 text-secondary"><List items={items} /></div>
    </section>
  )
}
```

Use only the repository's existing `accent`, `warning`, `border`, and `elevated` Tailwind colour tokens; do not introduce a new dependency or hard-coded inline colour.

- [ ] **Step 4: Replace the all-open layout with the approved two-level hierarchy**

Render this structure in `DowMonitorAiStageReport`:

```tsx
<div className="space-y-5 text-sm">
  <section aria-label="阶段结论" className="rounded-card border border-border bg-elevated p-4">
    <div className="text-xs font-medium text-muted">结论</div>
    <h3 className="mt-1 text-lg font-semibold">{report.headline.title}</h3>
    <p className="mt-2 break-words leading-6 text-secondary">{report.headline.summary}</p>
    <div className="mt-3 flex flex-wrap gap-2 text-xs text-muted">
      <span>{CHANGE_LABELS[report.headline.opportunity_change]}</span>
      <span>北京时间 {start?.slice(11) ?? '--'} 至 {cutoff?.slice(11) ?? '--'}</span>
      {analysis.stage_trading_minutes != null && <span>{analysis.stage_trading_minutes} 个交易分钟</span>}
    </div>
  </section>

  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
    <AdviceCard title="持仓者建议" advice={report.holding_advice.advice} />
    <AdviceCard title="未参与者建议" advice={report.watching_advice.advice} />
  </div>

  <section aria-labelledby="next-stage-title">
    <h3 id="next-stage-title" className="font-medium">下一阶段只盯三件事</h3>
    <div data-testid="next-stage-conditions" className="mt-2 grid grid-cols-1 gap-2">
      <ConditionCard title="增强确认" items={report.next_stage_conditions.strengthen} tone="accent" />
      <ConditionCard title="风险出现" items={report.next_stage_conditions.risk} tone="warning" />
      <ConditionCard title="判断失效" items={report.next_stage_conditions.invalidation} tone="neutral" />
    </div>
  </section>

  <details className="group rounded-card border border-border">
    <summary className="cursor-pointer list-none p-3 font-medium">
      <span className="group-open:hidden">展开完整分析（分钟路径、形态、量价、数据质量）</span>
      <span className="hidden group-open:inline">收起完整分析</span>
    </summary>
    <div className="space-y-5 border-t border-border p-4">
      {/* three evidence groups below */}
    </div>
  </details>
</div>
```

Inside the disclosure, group the existing renderers without changing their data:

```tsx
<Section title="本小时发生了什么">
  {/* stage_path, hidden_changes, comparison_with_previous */}
</Section>
<Section title="当日整体结构与量价资金">
  {/* day_overview, channel, patterns, volume_capital_interpretation */}
</Section>
<Section title="分析依据与数据质量">
  <p>置信度：{CONFIDENCE_LABELS[report.confidence]}</p>
  <List items={analysis.data_quality} />
</Section>
```

Keep the existing non-investment-advice footer after the disclosure. Do not render `holding_advice.conditions` or `watching_advice.conditions` as additional open lists; the report's concise `advice` strings are the selected default guidance contract.

- [ ] **Step 5: Run the focused component and repository frontend contract tests**

Run:

```powershell
pnpm --dir frontend test --run src/components/dow-monitor/DowMonitorAiStageReport.test.tsx
python -m pytest tests/frontend/test_dow_monitor_half_hour_ai_frontend.py -q
```

Expected: both commands PASS.

- [ ] **Step 6: Run TypeScript production compilation**

Run:

```powershell
pnpm --dir frontend build
```

Expected: PASS with a generated Vite bundle and no TypeScript errors.

- [ ] **Step 7: Commit the tested UI behavior**

```powershell
git add frontend/src/components/dow-monitor/DowMonitorAiStageReport.tsx frontend/src/components/dow-monitor/DowMonitorAiStageReport.test.tsx
git commit -m "feat: focus hourly AI report on decisions"
```

---

### Task 3: Prove semantic acceptance and update the operating runbook

**Files:**
- Create: `docs/acceptance/dow-monitor-hourly-ai-decision-first-view.md`
- Create: `docs/reviews/2026-08-02-dow-monitor-hourly-ai-decision-first-view-review.md`
- Modify: `E:/Obsidian-alwin/alwin/longbridge-stock/dow-monitor-system-api-runbook.md`
- Verify: `docs/traceability.yaml`

**Interfaces:**
- Consumes: the authoritative decision-first requirement and passing component/contract/build evidence from Tasks 1 and 2.
- Produces: semantic acceptance, an independent requirements-to-evidence review, and current 3018 operator verification instructions.

- [ ] **Step 1: Perform desktop and mobile semantic verification**

Run the frontend locally and inspect a structured hourly report at desktop width and at 375 px width:

```powershell
pnpm --dir frontend dev --host 127.0.0.1
```

Record exact evidence that:

- the conclusion, direction, both available advice cards, and condition groups appear before evidence;
- complete analysis starts closed, opens with one click, and closes with one click;
- strengthening, risk, and invalidation are separate vertical groups with wrapped text;
- the expanded view exposes all three grouped evidence sections;
- 375 px has no horizontal overflow;
- a legacy 30-minute report still uses the legacy dialog renderer.

- [ ] **Step 2: Write semantic acceptance with actual command results**

Create `docs/acceptance/dow-monitor-hourly-ai-decision-first-view.md` only after Step 1. Use the headings below, then write concrete sentences naming the observed report title, visible labels, disclosure state transitions, viewport width, overflow result, legacy-record identity, exact test counts, and command exit results. Do not mark the status passed until every statement is backed by that execution.

```markdown
# Dow Monitor Hourly AI Decision-First View Acceptance

Date: 2026-08-02
Requirement: `REQ-DOW-MONITOR-HOURLY-AI-DECISION-FIRST-VIEW-001`
Status: passed

## Semantic evidence

- Default decision-summary observations.
- Disclosure closed/open/closed observations.
- Next-stage vertical-label and wrapping observations.
- 375 px mobile overflow observation.
- Legacy-renderer compatibility observation.

## Executable evidence

- Focused Vitest exact command and pass count.
- Frontend contract pytest exact command and pass count.
- Frontend build: passed.
- Specification compliance exact command and result.
```

- [ ] **Step 3: Update the 3018 runbook**

Under the hourly AI frontend verification section, replace the former all-open order with:

```markdown
- 新版小时报告弹窗默认先显示结论、机会/风险方向、持仓者与未参与者各一句建议，以及纵向排列的增强确认、风险出现、判断失效条件。
- 分钟路径、终点隐藏变化、上一阶段对比、当日结构、通道/形态、量价资金、置信度和数据质量默认收拢在“展开完整分析”中，可再次点击收起。
- 页面使用结构化 React 字段渲染，不解析 Markdown。桌面和手机保持同一阅读顺序，手机为单列且不得横向溢出。
```

Add verification instructions to check both closed and expanded states in the built `DowMonitor-*.js` bundle and at 375 px width.

- [ ] **Step 4: Run the complete relevant verification set**

Run:

```powershell
python -m pytest tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py tests/frontend/test_dow_monitor_half_hour_ai_frontend.py -q
pnpm --dir frontend test --run
pnpm --dir frontend build
python scripts/check_spec_compliance.py
git diff --check
```

Expected: every command PASS. Record exact test counts in the acceptance file.

- [ ] **Step 5: Conduct a fresh requirements-to-evidence review**

Create `docs/reviews/2026-08-02-dow-monitor-hourly-ai-decision-first-view-review.md` and explicitly answer:

```markdown
# Independent Review: Dow Monitor Hourly AI Decision-First View

Date: 2026-08-02
Status: passed

## Authority

- Confirm the requirement is present in the authoritative specification, index, and traceability map.

## Requirement-to-evidence findings

- Confirm conclusion-first default visibility from direct component behavior.
- Confirm all complete evidence remains reachable and the disclosure is reversible.
- Confirm next-stage conditions are vertical, labelled, wrapped, and omit empty groups.
- Confirm mobile single-column behavior and legacy compatibility.
- Confirm no backend, persistence, WebSocket, formal-signal, or Markdown-parser change exists in the diff.

## Evidence independence

- Explain why component behavior and direct visual inspection prove presentation semantics; do not substitute a snapshot or backend report success for UI acceptance.
```

Re-open the authoritative requirement and compare every MUST to the component test, visual observation, and diff before marking the review passed.

- [ ] **Step 6: Commit acceptance, review, and runbook evidence**

```powershell
git add docs/acceptance/dow-monitor-hourly-ai-decision-first-view.md docs/reviews/2026-08-02-dow-monitor-hourly-ai-decision-first-view-review.md E:/Obsidian-alwin/alwin/longbridge-stock/dow-monitor-system-api-runbook.md
git commit -m "docs: accept decision-first hourly AI view"
```

Note: if Git rejects the Obsidian path because it is outside this repository, commit the repository acceptance and review files, then preserve the separately updated runbook as required operational evidence without attempting to stage it in this repository.

---

## Self-review completed

- Spec coverage: every default-content, disclosure, vertical-condition, empty-value, mobile, compatibility, and no-Markdown requirement maps to an implementation step and semantic evidence.
- Lower-layer boundary: the plan leaves the structured report payload, model output, persistence, and real-time paths unchanged; UI evidence is not used to re-prove those lower layers.
- Placeholder scan: the implementation tasks contain exact component APIs, labels, commands, and expected outcomes. Runtime evidence fields are intentionally populated only from observed results and are not acceptance until filled.
- Type consistency: all property names match `DowMonitorAiStageReport`; `ConditionCard` accepts `string[]` and the existing three condition arrays; `confidence` matches `HIGH | MEDIUM | LOW`.
- Scope: this is one frontend presentation sub-project. Production deployment is excluded until separately authorized.
