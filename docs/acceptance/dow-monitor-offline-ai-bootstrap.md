status: pending

# Dow Monitor Offline AI Bootstrap Acceptance

Applies to:

- `REQ-DOW-MONITOR-HALF-HOUR-AI-OFFLINE-BOOTSTRAP-001`
- `REQ-DOW-MONITOR-HALF-HOUR-AI-BOOTSTRAP-ISOLATION-001`

Semantic acceptance will close only after an independently observed run proves
that a symbol added at 10:17 selects 10:00 as its one eligible startup
checkpoint, materializes bounded offline minute results, and performs one
analysis using data no later than that checkpoint. The evidence must also show
that 09:30 and all older checkpoints remain uncalled; a duplicate logical key
does not call the model again; later normal checkpoints follow the existing
`created_at` rule; and insufficient offline data persists `insufficient_data`
without an invented result or model call.

The same evidence must prove that later normal checkpoints on or after
`created_at` may use the same bounded offline recovery only when their canonical
minute results are missing; this does not expand eligibility to older
checkpoints before the single startup checkpoint.

Isolation evidence must show that the bounded work uses the existing minute
result materializer and ClickHouse offline inputs, stays per-symbol and
per-checkpoint, preserves model concurrency one, enforces the 500-row and
15-second limits, and cannot execute in the 3018 WebSocket or realtime-render
paths. Formal buy/sell signals, realtime key interpretation, WebSocket
ingestion, and minute realtime append results must remain unchanged.

## Task 6 executable semantic acceptance (2026-07-31)

Status: passed locally; production observations remain pending for Task 8.

The integration acceptance starts with one real file-backed monitor-store
symbol, `RNG.US`, created at `2026-07-31 22:17:00 Asia/Shanghai`. Its external
ClickHouse boundary contains deterministic raw quote, depth, trade,
candlestick, and capital evidence through `22:00`, with no canonical minute
rows and no half-hour analysis rows. The test exercises the production source,
history builder, 19912 adapter, canonical calculator, minute repository,
bounded bootstrap coordinator, worker, snapshot/prompt service, and analysis
repository. Only the external ClickHouse query/execute boundary, 19912
evaluation boundary, and LLM generation boundary are replaced by deterministic
fakes.

The test validates the canonical rows before inspecting the AI row. For the
sufficient-evidence scenario it proves:

- the selected and only startup checkpoint is `22:00`;
- `21:30` has no analysis;
- 30 canonical rows are saved for exactly `("us", "RNG.US")`;
- every decision minute is in `(21:30, 22:00]`;
- the maximum canonical decision/event time is `22:00`, and every recorded
  source timestamp is at or before `22:00`;
- all 30 rows have `backfill=true`, and 30 is below the 500-row ceiling;
- the saved analysis is `completed`, with both `window_end` and `data_cutoff`
  equal to `22:00`;
- the validated snapshot contains 30 observations and the model boundary is
  called exactly once.

For the insufficient-evidence scenario it proves:

- two raw minute observations become two canonical rows before the higher-layer
  result is inspected;
- both rows are bounded to the same symbol/session/cutoff and have
  `backfill=true`;
- the saved analysis is `insufficient_data` with
  `error_code=INSUFFICIENT_DATA` and `data_cutoff=22:00`;
- the model boundary is called zero times;
- `21:30` still has no analysis.

### RED/GREEN evidence

Because Tasks 2-5 had already implemented the behavior, Task 6 used a temporary
mutation rather than claiming an initially missing production feature.
Suppressing the canonical checkpoint write produced the expected RED at the
lower-layer semantic gate:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests\backend\test_dow_monitor_offline_ai_bootstrap_integration.py -q
```

Result with the temporary mutation: `2 failed in 2.46s`; both failures reported
zero canonical rows instead of the expected 30 and 2. The original production
line was then restored, leaving no production diff. Fresh GREEN result:
`2 passed in 1.54s`.

### Verification commands

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests\backend\test_dow_monitor_offline_ai_bootstrap_integration.py tests\backend\test_dow_monitor_half_hour_ai.py tests\backend\test_dow_monitor_offline_bootstrap.py tests\backend\test_dow_monitor_minute_result_materializer.py tests\backend\test_dow_monitor_minute_result_history.py tests\backend\test_dow_monitor_minute_result_calculator.py tests\backend\test_dow_monitor_minute_result_source.py tests\backend\test_dow_monitor_minute_result_repository.py tests\spec_contracts\test_dow_monitor_offline_ai_bootstrap_contract.py -q
```

Result: `86 passed in 4.55s`.

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests\spec_contracts\test_dow_monitor_offline_ai_bootstrap_contract.py -q
```

Result: `4 passed in 0.16s`. The isolation contract parses the 3018 startup,
realtime API, realtime market-data, and Dow monitor service modules and proves
that none imports the offline bootstrap coordinator. Task 6 changes only this
acceptance document and the new backend integration test; it does not change a
3018, WebSocket, API, frontend, formal-signal, or minute-realtime module.

```powershell
python scripts\check_spec_compliance.py
```

Result: `Specification compliance passed.`

## Pending production observations (Task 8)

The local executable evidence does not substitute for production acceptance.
The 10.28 worker image/SHA, live raw and canonical ClickHouse queries, worker
restart/model-concurrency observations, next-normal-checkpoint behavior, and
3018/WebSocket restart, backlog, latency, and formal-signal non-regression
evidence remain pending until Task 8.
