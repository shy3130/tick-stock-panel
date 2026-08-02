# Dow Monitor Hourly AI Manual Rerun Acceptance

Status: local semantic acceptance passed; production release verification pending

Requirement: `REQ-DOW-MONITOR-HOURLY-AI-MANUAL-RERUN-001`

Accepted on: 2026-08-02

## Observed behavior

- The action targets the selected stored hourly analysis, asks for confirmation,
  and submits one durable queued request. A second sequential or concurrent
  submission reuses the active request instead of creating another task.
- The API only validates and persists the request. Model execution remains in
  the independent hourly AI worker and stays serial with scheduled model work.
- A manual request can execute outside market hours. Its regenerated snapshot
  keeps the stored trade date, window end, and data cutoff; a test observation
  later than the cutoff is excluded.
- A successful run writes the same `analysis_id` and logical checkpoint with an
  incremented attempt. A failed run marks only the request failed and leaves the
  stored report unchanged and retryable.
- The frontend keeps the old report visible while a request is queued or
  running, disables duplicate submission, refreshes detail/history only after
  completion, and renders a retryable failure message. Legacy 30-minute reports
  do not expose the action.
- The request table has no TTL. The path does not call the model from 3018 and
  does not write realtime interpretation, formal signals, minute results, or
  WebSocket ingestion state.

## Executable evidence

- RED evidence: before the service critical section, the concurrent-request
  regression test produced two requests and failed `len(repository.requests) ==
  1`; after the fix it passed.
- Backend/spec/integration suite:
  `uv run --frozen pytest ../tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py ../tests/backend/test_dow_monitor_half_hour_ai.py ../tests/backend/test_dow_monitor_offline_ai_bootstrap_integration.py -q`
  -> `70 passed`.
- Frontend suite: `pnpm exec vitest run` -> `47` files, `218 passed`,
  `2 skipped`, `0 failed`.
- Frontend contract: `python -m pytest
  tests/frontend/test_dow_monitor_half_hour_ai_frontend.py -q` -> `1 passed`.
- Production build: `pnpm build` -> TypeScript and Vite succeeded; generated
  trend-monitor chunk `assets/DowMonitor-GKgYU7qp.js`. The only build notice was
  the repository's existing large-chunk warning.
- Backend static check: `uv run --frozen ruff check ... --ignore B008` ->
  `All checks passed!` (`B008` is an existing FastAPI `Query(...)` convention).
- Specification check: `python scripts/check_spec_compliance.py` ->
  `Specification compliance passed.`

## Release boundary

No deployment, container restart, production request, or GitHub push was
performed in this acceptance pass. Live 10.28 evidence for the 3018 bundle,
ClickHouse table creation, worker consumption, and process non-interference
remains mandatory when the user authorizes release.
