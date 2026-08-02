# Dow Monitor Hourly AI Decision-First View Acceptance

Date: 2026-08-02

Requirement: `REQ-DOW-MONITOR-HOURLY-AI-DECISION-FIRST-VIEW-001`

Status: passed

## Semantic evidence

- The current frontend was run locally while its same-origin API proxy read the
  production 10.28 report data. Opening the real `NBIS.US` 04:00 report showed
  the conclusion `修复后再度放量回落，机会结构继续弱化` first, followed by
  `机会减弱`, Beijing stage time 03:00-04:00, 60 trading minutes, one holder
  guidance card, one watcher guidance card, and the three next-stage groups.
- The initial browser snapshot exposed `增强确认`, `风险出现`, `判断失效` as
  three separately labelled vertical groups. Every condition occupied its own
  list row; no three-column condition layout remained.
- The complete-analysis disclosure started closed. One click exposed exactly
  `本小时发生了什么`, `当日整体结构与量价资金`, and `分析依据与数据质量`.
  A second click removed those sections from the accessible view and restored
  `展开完整分析（分钟路径、形态、量价、数据质量）`.
- Direct desktop inspection initially detected inherited `white-space: nowrap`
  causing the two guidance cards to overlap. The regression was fixed with a
  component-level normal-whitespace boundary and `min-w-0` guidance cards. The
  retest measured the holder paragraph at `width=402`, `scrollWidth=402`, and
  its parent at `width=427.33`, `scrollWidth=426`; the two cards no longer mixed.
- At a 375 x 812 viewport, the report rendered in one column. The report root
  measured `clientWidth/scrollWidth=317/317`, the dialog `359/359`, and the page
  body `375/375`, proving no horizontal overflow.
- Opening the real `INTC.US` history entry labelled `历史半小时分析` continued
  to render the legacy conclusion, `关键证据`, `风险与不确定性`, scenario and
  `数据质量` presentation rather than the new hourly decision-first component.
- The report remains structured React text. No Markdown parser, report schema,
  backend service, persistence, WebSocket, formal-signal or model-prompt change
  is part of this implementation.

## Executable evidence

- Focused component behavior: `2 passed` in
  `DowMonitorAiStageReport.test.tsx`, including disclosure reversal, vertical
  conditions, empty-value omission, minimum width and whitespace normalization.
- Repository frontend contract and hourly specification contract: `5 passed`
  with
  `python -m pytest tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py tests/frontend/test_dow_monitor_half_hour_ai_frontend.py -q`.
- Full frontend suite: 95/95 test files passed; 211 tests passed and 2 skipped,
  with zero failed tests, using Vitest from the `frontend` working directory.
- Production TypeScript/Vite build passed. The generated trend-monitor chunk is
  `frontend/dist/assets/DowMonitor-DUqOjF9g.js`; UTF-8 inspection confirmed it
  contains both `下一阶段只盯三件事` and `展开完整分析`.
- `python scripts/check_spec_compliance.py` reported
  `Specification compliance passed.` after the acceptance and review paths were
  present. `git diff --check` produced no error.

Production deployment was not part of this acceptance and remains a separate
user-authorized action.

## At-a-glance conclusions amendment

- The first visible report section is now the labelled `一眼结论` list. A
  complete report renders, in order, `当前状态`, `这一小时`, `资金含义`,
  `转强条件`, `风险条件`, and `阶段建议`, and never renders more than six
  rows.
- `当前状态` uses the existing headline, `这一小时` uses the existing stage
  path, `资金含义` uses the existing volume/capital interpretation, and the
  three action rows use the existing next-stage conditions and holder/watcher
  advice. `风险/失效` and `持仓/未参与` retain visible sublabels when their
  values are combined.
- A sparse structured report with no path, flow interpretation, conditions, or
  advice renders only `当前状态`; empty source fields do not create blank rows.
- The component consumes the existing `report_json` response. No prompt,
  worker, model call, API payload, report schema, ClickHouse column, signal, or
  WebSocket path changed, so stored hourly reports receive the list without
  regeneration.
- TDD evidence: the new component expectations first failed because the
  accessible `一眼结论` region did not exist. After the presentation-only
  implementation, `DowMonitorAiStageReport.test.tsx` passed all 3 tests. The
  related report and AI-entry tests passed 4/4. The final full frontend run
  passed 47/47 test files and 215 tests, with 2 skipped and 0 failed. The
  production TypeScript/Vite build succeeded with trend-monitor chunk
  `DowMonitor-C35O_jR6.js`.

This amendment has local semantic and build acceptance. Production deployment
is not included and still requires an explicit release action.
