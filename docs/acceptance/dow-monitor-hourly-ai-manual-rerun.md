# Dow Monitor Hourly AI Manual Rerun Acceptance

Status: local and 10.28 production semantic acceptance passed

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
- A completed request no longer replaces the action label with `已更新`.
  The selected hourly report keeps an enabled `重跑AI分析` action and shows
  `上次重跑已完成` separately; a selected legacy report shows `旧版报告只读`.
- The request table has no TTL. The path does not call the model from 3018 and
  does not write realtime interpretation, formal signals, minute results, or
  WebSocket ingestion state.
- If the provider returns invalid structured output, the worker performs one
  bounded schema-repair call. A second invalid output stops the request as
  failed; it never loops indefinitely and still preserves the prior report.

## Executable evidence

- RED evidence: before the service critical section, the concurrent-request
  regression test produced two requests and failed `len(repository.requests) ==
  1`; after the fix it passed.
- Backend/spec/integration suite:
  `uv run --frozen pytest ../tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py ../tests/backend/test_dow_monitor_half_hour_ai.py ../tests/backend/test_dow_monitor_offline_ai_bootstrap_integration.py -q`
  -> `72 passed` after adding the bounded JSON-repair regression cases.
- Frontend suite: `pnpm exec vitest run` -> `47` files, `219 passed`,
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

## 10.28 production release evidence

- GitHub branch `userzhangtao2023/tickflow-stock-panel` /
  `codex/monitor-list-websocket` was pushed through repair commit
  `d897a60c32b353e012553bd21f94c016b92e012f`.
- The full release source archive for `91f61f4d4d34` matched SHA-256
  `72f785d8ead6b6f2b7260453f9b38e74e84ba59dbf4dcacddb565ee3fad9d130`.
  The worker-repair archive for `d897a60c32b3` matched SHA-256
  `8cd587b7b10928cca9429bbec75fea8677d57dd722f41c9ece9ad4133dc59eee`.
  Both builds and the pre-deploy backup live under `/data/apps`, where 346 GB
  was available; no build was placed on the nearly full root filesystem.
- 3018 runs immutable image
  `tickflow-stock-panel-app:dow-hourly-ai-rerun-91f61f4d4d34-20260802-115002`
  (`sha256:c5361d3839d...`), container `a7f02954fb7c...`, restart count 0.
  `/health` returns exact build `91f61f4d4d348a3552172639516b1be2c40a6c72`.
- The independent worker runs
  `tickflow-stock-panel-app:dow-hourly-ai-rerun-repair-d897a60c32b3-20260802-120547`
  (`sha256:27e7a3443d51...`), container `cd8e67229d9b...`, restart count 0.
  The worker-only repair cutover left the 3018 container ID, start time, and
  restart count byte-for-byte unchanged.
- The served trend-monitor chunk is `assets/DowMonitor-3LXLC7Qz.js`, SHA-256
  `57177a2931e88ca6f87574812e336aaf9fc3c297dc702bf35e27a426fff15344`,
  and contains the rerun/queue presentation.
- ClickHouse created permanent `ReplacingMergeTree` table
  `longbridge.lb_dow_monitor_hourly_ai_rerun_requests`. Two pre-repair live
  requests failed with `InvalidAiAnalysis`; both left the NBIS report at
  attempt 1 with identical report hash. The repaired request
  `42a0ae3cd4b1422e9287502190949bfc` completed through
  `queued -> running -> completed`.
- The selected NBIS report retained analysis ID
  `7b26526afc5e622bc66f8f5baa20454d742ebbb860d8f164aeca5d71cc08834a`,
  advanced from attempt 1 to 2, and received a new validated report hash and
  title. This is direct success-only replacement evidence, not a screenshot.
- 19912 remained healthy at PID `3511290`; priority market-data ingestion
  remained PID `461251`. Production WebSocket verification completed
  `hello -> snapshot -> unsubscribed`. App and worker scans contained no
  `ERROR`, `CRITICAL`, `Traceback`, or `Exception` entries.

Rollback is image-only and data preserving. Restore the app from
`/data/apps/tickflow-builds/fast-bootstrap-eccbd6a` with image
`tickflow-stock-panel-app:dow-fast-bootstrap-eccbd6a-20260802-095510`; restore
the worker from
`/data/apps/tickflow-builds/hourly-ai-rerun-91f61f4d4d34-20260802-115002`
with its `91f61f4d4d34` image. Do not delete the request table or reports and do
not restart 19912 or the market-data WebSocket process.
