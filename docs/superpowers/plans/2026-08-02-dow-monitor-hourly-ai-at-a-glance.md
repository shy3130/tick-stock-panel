# Dow Monitor Hourly AI At-a-Glance Conclusions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put a labelled, no-more-than-six-item `一眼结论` list before the existing hourly AI evidence without changing prompts, report JSON, APIs, persistence, or model execution.

**Architecture:** The existing `DowMonitorAiStageReport` component derives every list item from the already-loaded structured hourly report. A small local presentation helper omits empty items and keeps the approved order; existing advice, condition cards, disclosure, legacy reports, and backend behavior remain unchanged.

**Tech Stack:** React 18, TypeScript, Testing Library, Vitest, Tailwind CSS.

## Global Constraints

- Reuse the current `DowMonitorHalfHourAiAnalysis.report` contract; do not add fields.
- Do not modify the hourly worker, model prompt, API, ClickHouse schema, formal signals, or WebSocket ingestion.
- Do not make a model call or regenerate historical reports.
- Render structured strings as text; do not parse Markdown.
- Keep desktop and mobile single-column and free of horizontal overflow.
- Preserve the existing untracked `.playwright-cli/`, `.superpowers/brainstorm/`, and `output/` directories.

---

### Task 1: Render the six labelled conclusions from existing report fields

**Files:**
- Modify: `frontend/src/components/dow-monitor/DowMonitorAiStageReport.test.tsx`
- Modify: `frontend/src/components/dow-monitor/DowMonitorAiStageReport.tsx`

**Interfaces:**
- Consumes: `DowMonitorHalfHourAiAnalysis.report` and its existing `headline`, `stage_path`, `comparison_with_previous`, `volume_capital_interpretation`, `holding_advice`, `watching_advice`, and `next_stage_conditions` fields.
- Produces: a first-visible `<section aria-label="一眼结论">` containing zero to six ordered list items with the labels `当前状态`, `这一小时`, `资金含义`, `转强条件`, `风险条件`, and `阶段建议`.

- [ ] **Step 1: Write the failing component test**

Add `within` to the Testing Library import and assert observable order and content against the real component:

```tsx
const quickView = screen.getByRole('region', { name: '一眼结论' })
const quickItems = within(quickView).getAllByRole('listitem')
expect(quickItems).toHaveLength(6)
expect(quickItems.map((item) => item.textContent)).toEqual([
  expect.stringContaining('当前状态'),
  expect.stringContaining('这一小时'),
  expect.stringContaining('资金含义'),
  expect.stringContaining('转强条件'),
  expect.stringContaining('风险条件'),
  expect.stringContaining('阶段建议'),
])
expect(quickItems[0]).toHaveTextContent('尾盘V形修复，但突破未确认')
expect(quickItems[2]).toHaveTextContent('尾段放量推动修复')
expect(quickItems[3]).toHaveTextContent('放量站稳阶段前高')
expect(quickItems[4]).toHaveTextContent('量价背离或重新跌回VWAP下方')
expect(quickItems[5]).toHaveTextContent('持仓者可继续观察前高确认')
expect(quickItems[5]).toHaveTextContent('未参与者等待放量站稳')
```

Extend the sparse-report test so empty source groups do not leave empty list items and the list never exceeds six items.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
cd frontend
pnpm exec vitest run src/components/dow-monitor/DowMonitorAiStageReport.test.tsx
```

Expected: FAIL because no region named `一眼结论` exists.

- [ ] **Step 3: Implement the minimal presentation helper and list**

In `DowMonitorAiStageReport.tsx`, add local types/helpers that only compose existing text:

```tsx
type QuickConclusion = { label: string; value: string }

function joinText(values: Array<string | null | undefined>) {
  return values.filter(hasText).join('；')
}

function buildQuickConclusions(
  report: NonNullable<DowMonitorHalfHourAiAnalysis['report']>,
): QuickConclusion[] {
  const stagePath = report.stage_path.map((item) => item.description).filter(hasText)
  const stageSummary = stagePath.length > 0
    ? joinText(stagePath.length <= 2 ? stagePath : [stagePath[0], stagePath.at(-1)])
    : report.comparison_with_previous

  return [
    { label: '当前状态', value: joinText([report.headline.title, report.headline.summary]) },
    { label: '这一小时', value: stageSummary },
    { label: '资金含义', value: report.volume_capital_interpretation },
    { label: '转强条件', value: report.next_stage_conditions.strengthen[0] ?? '' },
    {
      label: '风险条件',
      value: joinText([
        report.next_stage_conditions.risk[0],
        report.next_stage_conditions.invalidation[0],
      ]),
    },
    {
      label: '阶段建议',
      value: joinText([
        report.holding_advice.advice,
        report.watching_advice.advice,
      ]),
    },
  ].filter((item) => hasText(item.value)).slice(0, 6)
}
```

Render the result as the first section inside `hourly-ai-stage-report`, before the existing conclusion card:

```tsx
<section aria-label="一眼结论" className="rounded-card border border-border bg-elevated p-4">
  <h3 className="font-semibold">一眼结论</h3>
  <ul className="mt-3 space-y-2">
    {quickConclusions.map((item) => (
      <li key={item.label} className="grid min-w-0 grid-cols-1 gap-1 sm:grid-cols-[5rem_minmax(0,1fr)]">
        <strong>{item.label}</strong>
        <span className="break-words text-secondary">{item.value}</span>
      </li>
    ))}
  </ul>
</section>
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the same Vitest command. Expected: all tests in the file PASS.

- [ ] **Step 5: Run the affected frontend contracts and production build**

```powershell
cd frontend
pnpm exec vitest run src/components/dow-monitor/DowMonitorAiStageReport.test.tsx src/components/dow-monitor/DowMonitorHalfHourAiButton.test.tsx
pnpm build
```

Expected: all tests PASS and the TypeScript/Vite production build succeeds.

- [ ] **Step 6: Commit the UI change**

```powershell
git add frontend/src/components/dow-monitor/DowMonitorAiStageReport.tsx frontend/src/components/dow-monitor/DowMonitorAiStageReport.test.tsx
git commit -m "feat(dow-monitor): add hourly AI at-a-glance conclusions"
```

### Task 2: Close traceability, acceptance, and operational evidence

**Files:**
- Modify: `docs/acceptance/dow-monitor-hourly-ai-decision-first-view.md`
- Modify: `docs/reviews/2026-08-02-dow-monitor-hourly-ai-decision-first-view-review.md`
- Modify: `E:/Obsidian-alwin/alwin/longbridge-stock/dow-monitor-system-api-runbook.md`

**Interfaces:**
- Consumes: the already-indexed `REQ-DOW-MONITOR-HOURLY-AI-DECISION-FIRST-VIEW-001`, the focused component tests, the production build, and the final source diff.
- Produces: semantic acceptance and an independent requirements-to-evidence review showing that the change is presentation-only and uses existing reports.

- [ ] **Step 1: Record semantic acceptance**

Append the exact six labels, omission behavior, no-more-than-six boundary, existing-report compatibility, focused test results, and build result to `docs/acceptance/dow-monitor-hourly-ai-decision-first-view.md`.

- [ ] **Step 2: Perform and record the independent review**

Update `docs/reviews/2026-08-02-dow-monitor-hourly-ai-decision-first-view-review.md` by checking the requirement against implementation, executable tests, acceptance evidence, and the unchanged prompt/API/schema boundaries. Do not use a snapshot as semantic proof.

- [ ] **Step 3: Update the runbook**

Append a concise section documenting that `一眼结论` is frontend-derived from existing `report_json`, applies to historical hourly reports, makes no model call, and changes neither 3018 API paths nor 19912/WebSocket behavior.

- [ ] **Step 4: Run final compliance and diff checks**

```powershell
python scripts/check_spec_compliance.py
python -m pytest tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py tests/frontend/test_dow_monitor_half_hour_ai_frontend.py -q
git diff --check
git status --short
```

Expected: compliance passes, contract tests pass, no whitespace errors appear, and only intended files plus the pre-existing untracked directories are present.

- [ ] **Step 5: Commit the evidence**

```powershell
git add docs/acceptance/dow-monitor-hourly-ai-decision-first-view.md docs/reviews/2026-08-02-dow-monitor-hourly-ai-decision-first-view-review.md
git commit -m "docs(dow-monitor): verify hourly AI at-a-glance view"
```
