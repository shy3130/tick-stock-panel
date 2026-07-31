# Dow Monitor New-Symbol History Backfill Acceptance

Requirement: `REQ-DOW-MONITOR-NEW-SYMBOL-HISTORY-BACKFILL-001`

Status: TickFlow consumer acceptance passed; lower-layer provider acceptance
pending.

Consumer evidence:

- `tests/backend/test_dow_monitor_history_status.py` proves Hong Kong alias
  matching, terminal-state preservation, missing-file pending state, malformed
  isolation, stale active-state isolation, one batch read per overview, and
  immediate symbol addition with a gateway that raises on every access.
- `backend/tests/test_dow_monitor_api.py` plus the consumer suite passed with 44
  tests on 2026-07-31.
- The production frontend build completed with the optional typed
  `history_backfill` projection.

Final production acceptance still requires provider status progression while
WebSocket events continue, and lower-layer ClickHouse row evidence.
