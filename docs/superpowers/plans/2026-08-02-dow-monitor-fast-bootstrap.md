# Dow Monitor Fast Bootstrap Implementation Plan

> Approved design: `docs/superpowers/specs/2026-08-02-dow-monitor-fast-bootstrap-design.md`

**Goal:** Make the monitored-symbol list and WebSocket quotes usable before stable analysis payloads finish loading, while preserving every existing stable-indicator and formal-signal semantic.

**Architecture:** Keep existing full overview/detail APIs for compatibility. Add compact list-overview and notification-summary APIs, project the persisted states in one bulk read, and let the frontend build the current page from the symbols feed before merging stable summaries. Full charts remain detail-only.

**Stack:** FastAPI/Pydantic, file-backed Dow monitor store, React/TypeScript, TanStack Query, Vitest/Testing Library, pytest.

## Task 1: Lock contracts and traceability

- Register `SPEC-DOW-MONITOR-FAST-BOOTSTRAP-001` in `docs/spec-index.yaml`.
- Map all four requirement IDs in `docs/traceability.yaml`.
- Add a spec-contract test proving the registry, routes, query hooks, and evidence files remain present.
- Run: `python -m pytest tests/spec_contracts/test_dow_monitor_fast_bootstrap_contract.py -q`.

## Task 2: Backend red tests

- Extend `backend/tests/test_dow_monitor_api.py` with tests for:
  - `/list-overview` excludes full chart payload but retains exact list inputs;
  - overview/list-overview call `list_states()` once and do not call `get_state()`;
  - compact 5m/15m/30m/60m/day projection boundaries;
  - `/notification-summaries` excludes snapshot and long text;
  - unchanged notification file does not reload JSONL;
  - 20-symbol and 100-notification serialized byte budgets.
- Run focused tests and confirm the new tests fail for missing behavior.

## Task 3: Backend implementation

- Add state indexing and compact projection helpers in `backend/app/services/dow_monitor_service.py`.
- Refactor legacy overview to use the same one-read index.
- Add list overview and notification summary routes in `backend/app/api/dow_monitor.py`.
- Add stat-signature notification refresh in `backend/app/services/dow_monitor_store.py` without changing append/read semantics.
- Re-run focused tests until green, then run the existing Dow monitor API suite.

## Task 4: Frontend red tests

- Add `useDowMonitorSymbols`, list-overview and notification-summary hook expectations in `useDowMonitor.test.tsx`.
- Add page tests showing:
  - symbols can render before list overview;
  - WebSocket receives current-page symbols before list overview resolves;
  - realtime price appears while stable indicators show loading;
  - previous stable summary survives a refresh failure;
  - details still request the full timeframe endpoint only after selection.
- Add compact-state semantic-equivalence coverage in `monitorListPresentation.test.ts`.
- Run focused Vitest tests and confirm the new assertions fail first.

## Task 5: Frontend implementation

- Add compact response/notification summary types and API methods.
- Add query hooks with the existing 15-second stable fallback.
- Refactor `DowMonitor.tsx` to page from symbols first, subscribe immediately, and merge compact summaries by canonical symbol.
- Add explicit stable-summary loading state to `DowMonitorList` and mobile rows; never turn missing stable data into zero or a formal signal.
- Invalidate both symbols and compact summaries after add/remove/toggle; invalidate notification summaries after mark-read.
- Re-run focused tests and the complete frontend test/build set.

## Task 6: Acceptance and release evidence

- Run backend API, frontend, spec-guard, typecheck, lint/build, and relevant semantic-equivalence suites.
- Start a candidate service and capture browser request ordering and payload sizes.
- On 10.28 candidate port verify symbols TTFB, subscribe-to-first-quote, and list-overview TTFB thresholds before switching 3018.
- Update `docs/acceptance/dow-monitor-fast-bootstrap.md` with commands and measured evidence.
- Conduct an independent requirements-to-evidence review in `docs/reviews/2026-08-02-dow-monitor-fast-bootstrap-review.md`.
- Update `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md` before completion.
