# Independent Review: Dow Monitor Hourly AI Decision-First View

Date: 2026-08-02

Status: passed

## Authority

- `docs/specs/dow-monitor-half-hour-ai-analysis.md` is authoritative and now
  contains `REQ-DOW-MONITOR-HOURLY-AI-DECISION-FIRST-VIEW-001`.
- `docs/spec-index.yaml` registers the requirement under the existing hourly AI
  specification. `docs/traceability.yaml` maps it to the component,
  executable tests, semantic acceptance and this review.
- The prior view clause was clarified to retain every required analytical fact
  while allowing the new requirement to control default ordering and disclosure
  state. No unresolved specification conflict remains.

## Requirement-to-evidence findings

- **Conclusion first:** direct testing and the real `NBIS.US` browser run prove
  that the conclusion, opportunity change, concise holder/watcher guidance and
  next-stage conditions precede supporting evidence.
- **Complete evidence preserved:** the single native disclosure reversibly
  exposes stage path, hidden changes, prior comparison, cumulative structure,
  channel/pattern, volume/capital, confidence and data quality in three groups.
- **Conditions readable:** strengthening, risk and invalidation use labelled,
  vertical cards. Empty condition groups and empty guidance strings are omitted.
  Colour is supplemental to the visible labels.
- **No mixed text:** a direct browser measurement found the original inherited
  nowrap defect, and the added regression assertion plus real retest prove the
  final guidance width equals its scroll width.
- **Mobile:** the 375 px real-browser measurement proves single-column content
  and no report, dialog or body horizontal overflow.
- **Compatibility:** the real `INTC.US` legacy record continued through the old
  evidence/risk/scenario renderer. The overview remains lightweight and the
  long report remains lazy-loaded only after the AI action is opened.
- **Scope boundary:** the implementation diff contains frontend presentation,
  tests and specification evidence only. It contains no backend, ClickHouse,
  WebSocket, model prompt, report schema or formal-signal file.
- **No Markdown:** the component renders typed strings and arrays directly and
  imports no Markdown renderer or parser.

## Evidence independence

The presentation requirement is accepted from direct component interaction,
computed browser layout measurements, real structured report data and legacy
record behavior. Backend report success, a snapshot alone, or the build result
was not treated as proof of ordering, wrapping, disclosure reversibility or
mobile overflow semantics.

The lower structured-report and persistence layers were already accepted under
their own requirements and were not re-inferred from this UI result.

## Verification result

- Hourly specification plus repository frontend contract: 5 passed.
- Full frontend suite: 95/95 files, 211 passed, 2 skipped, 0 failed.
- TypeScript/Vite production build: passed.
- Built `DowMonitor-DUqOjF9g.js`: both decision-first labels present.
- Specification compliance: passed.
- Final whitespace/diff check: passed.

## Independent amendment review: at-a-glance conclusions

- **Requirement:** the approved design requires a first-visible list of no more
  than six business conclusions using existing structured fields. The
  component implements the exact labels and order, and its direct DOM test
  asserts the six real rendered rows rather than a snapshot.
- **Empty-data boundary:** a separate real-component case removes every
  optional source group and proves that only the non-empty `当前状态` row
  remains. The helper also applies the explicit six-row upper bound.
- **Semantic clarity:** combined risk text retains `风险/失效` sublabels and
  combined guidance retains `持仓/未参与` sublabels, preventing adjacent
  sentences from losing their audience or trigger meaning.
- **Historical compatibility:** the list is derived after the existing hourly
  `report_json` is loaded. There is no new field, migration, model request, or
  regeneration dependency; legacy 30-minute reports still bypass this hourly
  component.
- **Scope audit:** the production diff is limited to
  `DowMonitorAiStageReport.tsx`; backend prompt, worker, API, persistence,
  signal, WebSocket, and data-source files are unchanged. The test, build,
  traceability, acceptance, and runbook changes are supporting evidence.
- **Evidence:** the focused RED failure named the missing accessible region;
  the final component suite passed 3/3, the affected frontend tests passed 4/4,
  and the production build passed. These behavioral assertions, not the built
  bundle or a visual snapshot alone, provide semantic acceptance.

Conclusion: the amendment satisfies
`REQ-DOW-MONITOR-HOURLY-AI-DECISION-FIRST-VIEW-001` without changing lower-layer
analysis semantics. Production release remains outside this review.
