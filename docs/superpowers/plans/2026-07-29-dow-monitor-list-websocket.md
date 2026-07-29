# Dow Monitor List WebSocket Implementation Plan
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Dow monitor card grid with a 20-row-per-page market list, WebSocket-backed real-time quote and intraday sparkline, stable backend-owned decision indicators, and an inline detail panel.

**Architecture:** Keep the existing 15-second HTTP overview/notification queries as the authoritative decision plane. Reuse the existing batched `/ws/realtime` client as a current-page market-data fast path. Put all display derivations in pure presentation helpers, render them through a focused list component, and reuse the existing detail query/chart mapping in an embedded detail panel.

**Tech Stack:** React 18, TypeScript, TanStack Query, Vitest, Testing Library, existing ECharts candlestick component, FastAPI WebSocket gateway.

## Global Constraints

- Preserve the authoritative backend signal and notification semantics.
- Never derive a formal buy/sell signal from quote, depth, or a forming candle.
- Use only completed bars for channel and momentum presentation.
- Subscribe only current-page enabled symbols; keep existing one-second visible-state batching.
- Run executable semantic tests before UI integration tests.
- Update `docs/traceability.yaml`, acceptance evidence, independent review, and the Obsidian runbook.

---

## Task 1: Register requirements and executable contracts

- [x] Add the authoritative specification to `docs/spec-index.yaml`.
- [x] Add all five requirement IDs to `docs/traceability.yaml`.
- [x] Add `tests/spec_contracts/test_dow_monitor_list_websocket_contract.py`.
- [x] Run the contract wrapper; after Vitest ignored nonexistent filtered paths, confirm the red state with the focused missing-module tests.
- [x] Add the minimal production files/components needed to satisfy the contract.
- [x] Re-run the contract test and confirm it passes.

## Task 2: Build completed-bar presentation helpers

**Files:**

- Create `frontend/src/components/dow-monitor/monitorListPresentation.ts`
- Create `frontend/src/components/dow-monitor/monitorListPresentation.test.ts`

- [x] Write failing table-driven tests for channel, control-line fallback, completed-bar momentum, relative volume, active-funds quality, formal signal persistence, false-break exclusion, stale-state handling, current-day sparkline points, and 20-row pagination.
- [x] Run the focused test and record the expected missing-module failure.
- [x] Implement pure helpers using literal thresholds and backend fields from the specification.
- [x] Re-run the focused test and refactor only while green.

## Task 3: Build the dense list and sparkline

**Files:**

- Create `frontend/src/components/dow-monitor/DowMonitorSparkline.tsx`
- Create `frontend/src/components/dow-monitor/DowMonitorList.tsx`
- Create `frontend/src/components/dow-monitor/DowMonitorList.test.tsx`

- [x] Write failing component tests for the required columns, single-line sparkline, selected row, “查看详情”, signal timestamp, delayed label, and pagination callback.
- [x] Implement the accessible table, horizontal inner scroll, compact mobile behavior, row selection, and pagination.
- [x] Re-run the component tests.

## Task 4: Embed the existing detail experience

**Files:**

- Create `frontend/src/components/dow-monitor/DowMonitorDetailPanel.tsx`
- Create `frontend/src/components/dow-monitor/DowMonitorDetailPanel.test.tsx`

- [x] Write a failing test proving the detail is a normal page region rather than a dialog and retains timeframe/overlay controls.
- [x] Implement the embedded panel using the existing detail query, chart mappings, and ECharts candlestick component.
- [x] Re-run the detail-panel test.

## Task 5: Integrate the page and current-page WebSocket subscription

**Files:**

- Modify `frontend/src/pages/DowMonitor.tsx`
- Modify `frontend/src/pages/DowMonitor.test.tsx`

- [x] Add failing integration tests for three market tabs without an all-market view, 20-row pagination, current-page subscription, real-time-only field updates, decision-field stability, and inline detail selection.
- [x] Replace the card grid/modal path with the list/detail path.
- [x] Reset page on market/signal changes and keep selection valid for the visible page.
- [x] Re-run focused page and real-time client tests.

## Task 6: Verification, build, runbook, and independent review

- [x] Run frontend focused tests and the complete frontend suite.
- [x] Run backend WebSocket tests and specification contract tests.
- [x] Run `pnpm build`.
- [x] Record exact commands and results in `docs/acceptance/dow-monitor-list-websocket.md`.
- [x] Update `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md` with the new UI/data-boundary/build verification.
- [x] Complete `docs/reviews/dow-monitor-list-websocket.md` by independently mapping every requirement to implementation, executable test, and semantic evidence.
- [x] Inspect `git diff --check`, `git status`, and the final diff before claiming completion.
