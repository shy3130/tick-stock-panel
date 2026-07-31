# Dow Monitor Half-Hour AI Analysis Acceptance

Requirements:

- `REQ-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001`
- `REQ-DOW-MONITOR-HALF-HOUR-AI-VIEW-001`

Status: local and production semantic acceptance passed.

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
- ClickHouse's timezone-less JSON datetime is normalized to aware UTC at the
  repository boundary. Frontend tests prove `2026-07-31 03:30:00.000` renders
  as `北京时间 11:30`, while US history queries retain the exchange date across
  Beijing midnight.
- Desktop renders an independent half-hour column; mobile reuses the separate
  third-row action slot below real-time interpretation.
- 21 focused frontend assertions passed and `pnpm build` completed on
  2026-07-31.
- Compose configuration validates with an unexposed, separate
  `TickFlow_Dow_AI_Worker` service.

Production evidence on 2026-07-31:

- `TickFlow_Stock_Panel` and `TickFlow_Dow_AI_Worker` run image
  `tickflow-stock-panel-app:dow-monitor-8a2c007931af`, each with restart count
  zero. `/health.build_id` is the matching full Git revision.
- ClickHouse `FINAL` contains 29 rows and 29 unique logical keys in
  `lb_dow_monitor_half_hour_ai_analyses`; the worker has no public port.
- Authenticated production requests returned HTTP 200 for overview, RNG.US day
  and 5m detail, 002714.SZ analysis history, and its lazily loaded detail.
- A 390px production browser rendered with `scrollWidth=390`. The 002714.SZ
  entry displayed `北京时间 11:30`, and its modal displayed
  `截止 2026-07-31 11:30（北京时间）`.
- Production accepted `/ws/realtime` connections while calls to the independent
  19912 Dow-state service continued returning HTTP 200. Post-release app and
  worker `ERROR|CRITICAL|Traceback` counts were both zero.
