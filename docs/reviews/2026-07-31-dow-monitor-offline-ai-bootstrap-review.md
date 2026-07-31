status: passed

# Dow Monitor Offline AI Bootstrap Independent Review

Review date: 2026-07-31

Scope: local requirements-to-evidence review. Production acceptance remains
pending for Task 8 and is not implied by this passed local review.

## Authority

The review began from
`SPEC-DOW-MONITOR-OFFLINE-AI-BOOTSTRAP-001`, not the implementation reports.
`docs/spec-index.yaml` registers its two exact requirement IDs as authoritative.
The conflict with
`SPEC-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001` is resolved by
`DEC-20260731-DOW-MONITOR-OFFLINE-AI-BOOTSTRAP-001`: exactly one latest
completed startup checkpoint may be at or before `created_at`; older startup
checkpoints remain prohibited; every later normal checkpoint may use bounded
offline recovery when canonical rows are missing. No unresolved conflict or
exception applies.

## Requirements-to-evidence matrix

| Requirement / mandatory behavior | Implementation evidence | Executable and semantic evidence | Review conclusion |
| --- | --- | --- | --- |
| `REQ-DOW-MONITOR-HALF-HOUR-AI-OFFLINE-BOOTSTRAP-001`: select exactly the latest completed startup checkpoint and never replay an older one | `select_due_windows()` in `backend/app/workers/dow_monitor_half_hour_ai.py` takes the maximum completed window at or before `created_at`; `_run_checkpoint()` checks the logical key before any lower-layer work | `test_select_due_windows_never_falls_back_from_terminal_latest_startup`, `test_new_symbol_analyzes_only_latest_completed_startup_checkpoint`, and `test_terminal_latest_startup_checkpoint_does_not_fall_back_older`; the integration test produces 22:00 and proves no 21:30 analysis | Passed |
| Same requirement: skip duplicate terminal logical keys and continue later normal checkpoints | `DowMonitorHalfHourAiRepository.exists_completed()` treats `completed`, `insufficient_data`, and `failed` as terminal for `(market, symbol, trade_date, window_end)`; the worker checks it first | `test_existing_terminal_key_skips_bootstrap_and_model`, `test_terminal_latest_startup_checkpoint_does_not_fall_back_older`, `test_next_normal_checkpoint_runs_after_startup_checkpoint`, and `test_normal_checkpoint_can_bootstrap_missing_canonical_rows` | Passed |
| Same requirement: canonical evidence must be reloaded before the model, and insufficient evidence must not call the model | The worker loads `longbridge.lb_dow_monitor_minute_results`, invokes bootstrap only for an insufficient snapshot, reloads on `completed`/`not_needed`, and saves `insufficient_data` before returning on persistent insufficiency | Worker reload/sufficient/insufficient tests plus both Task 6 integration scenarios; canonical rows are asserted before the AI row, with one model call for 30 rows and zero calls for two rows | Passed |
| `REQ-DOW-MONITOR-HALF-HOUR-AI-BOOTSTRAP-ISOLATION-001`: use persisted ClickHouse raw evidence and the existing canonical calculation path | `DowMonitorMinuteResultSource` reads the five existing raw tables; `DowMonitorMinuteResultHistoryBuilder` builds contexts; `DowMonitorMinuteResultMaterializer.materialize_checkpoint()` calls the existing `calculate_minute_result()` and writes through `DowMonitorMinuteResultRepository` to `longbridge.lb_dow_monitor_minute_results` | Source/history/calculator/repository/materializer tests and the Task 6 real internal-chain integration test; 30/2 canonical rows are independently validated before the analysis | Passed |
| Same requirement: one symbol/checkpoint, cutoff bounded, backfill marked, maximum 500 rows | The checkpoint materializer loads one requested symbol, bounds decision minutes to `(session_open, window_end]`, passes `backfill=True`, caps any caller budget at 500, and rejects an over-budget set before calculation/insertion | `test_materialize_checkpoint_writes_only_missing_rows_through_cutoff`, deduplication tests, the 501-row ceiling test, source/history future-data tests, and integration assertions for symbol, cutoff, source timestamps, and backfill flags | Passed |
| Same requirement: at most 15 seconds of worker waiting, physical single-flight retained, and failure isolated | `DowMonitorOfflineBootstrap` uses `asyncio.to_thread`, `wait_for(shield(task))`, a 15-second hard cap, and one retained in-flight task; worker terminal outcomes are per checkpoint and the outer symbol loop catches exceptions | Coordinator off-loop, concurrent `busy`, timeout-while-physically-running, late-result, late-exception, and diagnostic tests; worker error and subsequent-symbol tests | Passed |
| Same requirement: `busy`, timeout, budget, and insufficient outcomes do not invent model evidence | `busy` returns without persistence; timeout/budget/failure persist explicit `insufficient_data` diagnostics; persistent insufficiency uses `INSUFFICIENT_DATA`; all return before prompt invocation | `test_busy_bootstrap_saves_no_terminal_row_and_next_poll_can_retry`, parameterized terminal-error tests, persistent-insufficiency test, coordinator outcome tests, and insufficient integration scenario | Passed |
| Same requirement: model concurrency remains one and bootstrap stays outside 3018/WebSocket/realtime/formal-signal paths | The dedicated `TickFlow_Dow_AI_Worker` owns the sequential loop and coordinator; Compose keeps concurrency default 1 and exposes no worker port; 15m/30m stable-state evaluation uses the independent 19912 client. No 3018/realtime module imports the coordinator | Worker factory/Compose/lifecycle tests and AST isolation contract; the implementation diff from `cfac9d8` has no frontend, API, 3018 startup, realtime, WebSocket, monitor-service, or formal-signal file change | Passed locally; live restart/backlog/latency evidence remains Task 8 |

## Lower-layer semantic gate

The Task 6 integration test uses real source, history builder, 19912 adapter,
canonical calculator, canonical repository, coordinator, worker, snapshot,
prompt, and analysis repository code. Only ClickHouse I/O, the external 19912
response, and the LLM response are deterministic fakes. It first asserts the
canonical rows themselves: exact symbol/session/cutoff, 30 or 2 rows, all
`backfill=true`, and every decision/source time no later than 22:00. Only then
does it inspect the saved analysis. Its recorded mutation run failed both tests
at the zero-canonical-row gate, so the downstream AI result is not being used
as a substitute for canonical semantic acceptance.

## Non-impact review

The production diff from the completed authority slice (`cfac9d8`) contains
only:

- `backend/app/services/dow_monitor_minute_result_materializer.py`
- `backend/app/services/dow_monitor_offline_bootstrap.py`
- `backend/app/workers/dow_monitor_half_hour_ai.py`
- `docker-compose.yml`

There is no frontend/static bundle, API route, API model, overview/detail
payload, 3018 startup, WebSocket/realtime, monitor-service, or formal-signal
file change. The existing half-hour dialog contract is therefore unchanged.
The AST contract additionally parses the 3018 startup, realtime API,
realtime-market-data, and Dow monitor service modules and rejects a coordinator
import.

## Deferred-minor rulings

All six ledger observations were re-evaluated. Two are closed by stronger
Task 6 or lower-layer evidence: the hardcoded backfill unit fake is superseded
by real-builder integration evidence, and the integration's implied 500-row
assertion is backed by the independent 501-row rejection test. The remaining
four are accepted as non-blocking test-quality observations: calculator-call
spying is not needed to establish the zero-write 500-row contract; production
terminal checking occurs before lower-layer work even though the pure selector
can receive a terminal set; `busy` is proven non-terminal and retryable without
literally running the worker twice; and future-data exclusion is established
across source, history, repository, worker, and integration layers without a
poison row in the integration fake. None leaves a requirement without concrete
code, executable-test, and semantic evidence.

## Fresh verification

```text
focused backend slice: 82 passed in 8.53s
specification contracts: 5 passed in 0.54s
repository compliance: Specification compliance passed.
complete backend suite: 139 passed in 12.05s
```

No failure was waived.

## Review disposition

The local requirements-to-code, requirements-to-executable-test, and
lower-layer semantic-evidence mappings are complete, so this independent local
review is `passed`. The acceptance document correctly remains `status:
pending`: Task 8 must still record the 10.28 worker image/SHA, live ClickHouse
queries, one-poll startup result, next normal checkpoint, model concurrency,
worker/3018 restart counts, WebSocket queue/latency, and formal-signal
non-regression before production acceptance can close.

## Task 8A independent requirements-to-evidence review (2026-07-31)

Review scope: the production-discovered ClickHouse DateTime64 handoff only.
This review does not reopen or promote production acceptance.

The review began from the two authoritative requirement IDs and the recorded
precedence decision. No unresolved conflict or exception applies. The failed
Task 8 evidence established the lower-layer canonical rows before the
higher-layer failure: 98 bounded `backfill=1` rows existed, but
`decision_minute` arrived from `FORMAT JSONEachRow` as a naive Shanghai
wall-clock string and the snapshot rejected all 98. Thus neither a passing
golden nor downstream AI state was used as a substitute for the failed
storage-to-snapshot semantic boundary.

| Requirement behavior | Implementation evidence | Executable/semantic evidence | Conclusion |
| --- | --- | --- | --- |
| Canonical rows reconstructed for an eligible checkpoint must be consumable by the cumulative half-hour snapshot | `DowMonitorMinuteResultRepository` now deserializes the minute-results table's DateTime64 fields at the repository boundary, attaching `Asia/Shanghai` only to naive values | The production-shaped `2026-07-31 14:59:00.000` repository/snapshot test failed with zero observations before the fix and now proves one aware Beijing observation | Passed locally |
| Cutoff and future-data exclusion must not move | The snapshot's existing aware-time comparisons and `data_cutoff` logic are unchanged | The same test includes a 15:01 poison row and proves it remains excluded from the 15:00 snapshot | Passed locally |
| Existing canonical keys must remain safe for bounded materialization deduplication | The same repository boundary normalizes `existing_keys().decision_minute` | The existing-key test now uses the real naive DateTime64 shape and proves the same Beijing wall-clock instant | Passed locally |
| The fix must not reinterpret already-aware timestamps or widen into UI/realtime/formal-signal paths | The helper returns aware values without timezone conversion; only the minute-result repository production module changed | The aware `+00:00` test proves the offset representation is unchanged; diff review finds no snapshot, worker, API, frontend, 3018, WebSocket, or formal-signal production edit | Passed locally |

Fresh evidence: requested focused slice `43 passed`; full backend `141 passed`;
offline-bootstrap/specification contracts `5 passed`; specification compliance,
Ruff, targeted production-module mypy, and `git diff --check` all passed.
Traceability maps the repository implementation and executable repository test
to both active requirements.

Disposition: the Task 8A local boundary correction passes the independent
requirements-to-evidence review. The acceptance document must remain
`status: pending` until a new commit-addressed worker candidate is redeployed
and the complete Task 8 production sequence succeeds, including a real model
call and the next normal checkpoint.
