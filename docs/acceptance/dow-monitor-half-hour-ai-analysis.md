# Dow Monitor Half-Hour AI Analysis Acceptance

Requirements:

- `REQ-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001`
- `REQ-DOW-MONITOR-HALF-HOUR-AI-VIEW-001`

Status: local semantic acceptance passed; production acceptance pending.

Lower-layer evidence:

- `tests/backend/test_dow_monitor_half_hour_ai.py` verifies XSHG first
  checkpoint, XHKG lunch segmentation, XNYS DST mapping, an XNYS holiday,
  stable logical IDs, no-TTL schema, JSONEachRow persistence, cumulative query
  bounds, exclusion of observations after cutoff, backend-owned evidence
  values, rejection of invented evidence, and a new symbol's first checkpoint.
- The same executable suite verifies the worker writes a running/completed
  analysis sequence through read-only monitor inputs and does not process a
  checkpoint before `created_at`.
- Backend/API regression completed with 60 passing focused tests on
  2026-07-31.

API and UI evidence:

- Overview returns only `analysis_id`, status, checkpoint, title, and summary.
  The backend test proves the conclusion is absent until the detail route.
- The frontend component test proves history/detail are not requested before
  the independent action is opened.
- Desktop renders an independent half-hour column; mobile reuses the separate
  third-row action slot below real-time interpretation.
- 21 focused frontend assertions passed and `pnpm build` completed on
  2026-07-31.
- Compose configuration validates with an unexposed, separate
  `TickFlow_Dow_AI_Worker` service.

Production evidence still required: real ClickHouse schema/rows, one worker
process, one due checkpoint, lazy detail request, real mobile/desktop rendering,
and confirmation that formal signals and WebSocket freshness do not change.
