status: passed

# Dow Monitor Offline AI Bootstrap Independent Review

Review date: 2026-07-31

Scope: requirements-to-evidence review. The original sections preserve the
local review, failed first production attempt, and successful historical
`d35a39d` retry. The final section independently reviews the `6530979`
boundary, gate, and exact-cutoff reacceptance on 10.28.

## Authority

The review began from
`SPEC-DOW-MONITOR-OFFLINE-AI-BOOTSTRAP-001`, not the implementation reports.
`docs/spec-index.yaml` registers its two exact requirement IDs as authoritative.
The conflict with
`SPEC-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001` is resolved by
`DEC-20260731-DOW-MONITOR-OFFLINE-AI-BOOTSTRAP-001`: exactly one latest
completed startup checkpoint may satisfy `window_end < created_at`, and only
when `calendar.is_regular_session_time(market, created_at)` is true. Older
startup checkpoints remain prohibited; every normal checkpoint satisfying
`window_end >= created_at` may use bounded offline recovery when canonical rows
are missing. No unresolved conflict or exception applies.

## Requirements-to-evidence matrix

| Requirement / mandatory behavior | Implementation evidence | Executable and semantic evidence | Review conclusion |
| --- | --- | --- | --- |
| `REQ-DOW-MONITOR-HALF-HOUR-AI-OFFLINE-BOOTSTRAP-001`: select exactly the latest completed startup checkpoint and never replay an older one | `select_due_windows()` takes the maximum completed window strictly before `created_at`; normal windows start at equality; the worker gates only startup with `calendar.is_regular_session_time(market, created_at)`; `_run_checkpoint()` checks the logical key before lower-layer work | Equality, lunch, after-close, holiday, and startup-only suppression tests plus existing latest-only/terminal tests; the same scheduler probe passed inside the deployed candidate | Passed locally and live |
| Same requirement: skip duplicate terminal logical keys and continue later normal checkpoints | `DowMonitorHalfHourAiRepository.exists_completed()` treats `completed`, `insufficient_data`, and `failed` as terminal for `(market, symbol, trade_date, window_end)`; the worker checks it first | `test_existing_terminal_key_skips_bootstrap_and_model`, `test_terminal_latest_startup_checkpoint_does_not_fall_back_older`, `test_next_normal_checkpoint_runs_after_startup_checkpoint`, and `test_normal_checkpoint_can_bootstrap_missing_canonical_rows` | Passed |
| Same requirement: canonical evidence must be reloaded before the model, and insufficient evidence must not call the model | The worker loads `longbridge.lb_dow_monitor_minute_results`, invokes bootstrap only for an insufficient snapshot, reloads on `completed`/`not_needed`, and saves `insufficient_data` before returning on persistent insufficiency | Worker reload/sufficient/insufficient tests plus both Task 6 integration scenarios; canonical rows are asserted before the AI row, with one model call for 30 rows and zero calls for two rows | Passed |
| `REQ-DOW-MONITOR-HALF-HOUR-AI-BOOTSTRAP-ISOLATION-001`: use persisted ClickHouse raw evidence and the existing canonical calculation path | `DowMonitorMinuteResultSource` reads the five existing raw tables; `DowMonitorMinuteResultHistoryBuilder` builds contexts; `DowMonitorMinuteResultMaterializer.materialize_checkpoint()` calls the existing `calculate_minute_result()` and writes through `DowMonitorMinuteResultRepository` to `longbridge.lb_dow_monitor_minute_results` | Source/history/calculator/repository/materializer tests and the Task 6 real internal-chain integration test; 30/2 canonical rows are independently validated before the analysis | Passed |
| Same requirement: one symbol/checkpoint, cutoff bounded, backfill marked, maximum 500 rows | The checkpoint materializer loads one requested symbol, bounds decision minutes to `(session_open, window_end]`, queries an exclusive `window_end + 1ms` upper bound compatible with `DateTime64(3)`, passes `backfill=True`, caps any caller budget at 500, and rejects an over-budget set before calculation/insertion | `test_checkpoint_exact_cutoff_dedup_respects_datetime64_milliseconds` uses the real repository SQL boundary and proves zero calculation/insert; production `DateTime64(3)` replay found the exact cutoff and performed zero second-pass calculation/insert | Passed locally and live |
| Same requirement: at most 15 seconds of worker waiting, physical single-flight retained, and failure isolated | `DowMonitorOfflineBootstrap` uses `asyncio.to_thread`, `wait_for(shield(task))`, a 15-second hard cap, and one retained in-flight task; worker terminal outcomes are per checkpoint and the outer symbol loop catches exceptions | Coordinator off-loop, concurrent `busy`, timeout-while-physically-running, late-result, late-exception, and diagnostic tests; worker error and subsequent-symbol tests | Passed |
| Same requirement: `busy`, timeout, budget, and insufficient outcomes do not invent model evidence | `busy` returns without persistence; timeout/budget/failure persist explicit `insufficient_data` diagnostics; persistent insufficiency uses `INSUFFICIENT_DATA`; all return before prompt invocation | `test_busy_bootstrap_saves_no_terminal_row_and_next_poll_can_retry`, parameterized terminal-error tests, persistent-insufficiency test, coordinator outcome tests, and insufficient integration scenario | Passed |
| Same requirement: model concurrency remains one and bootstrap stays outside 3018/WebSocket/realtime/formal-signal paths | The dedicated `TickFlow_Dow_AI_Worker` owns the sequential loop and coordinator; Compose keeps concurrency default 1 and exposes no worker port; 15m/30m stable-state evaluation uses the independent 19912 client. No 3018/realtime module imports the coordinator | Worker factory/Compose/lifecycle tests and AST isolation contract; final worker-only deployment preserved one process tree, the exact 3018 panel identity/start/restart state, healthy queues, WebSocket behavior, and the production monitor-symbol hash | Passed locally and live |

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

## Task 8 production retry independent review (2026-07-31)

This final review started again from
`REQ-DOW-MONITOR-HALF-HOUR-AI-OFFLINE-BOOTSTRAP-001` and
`REQ-DOW-MONITOR-HALF-HOUR-AI-BOOTSTRAP-ISOLATION-001`, not from the candidate
status or passing tests. The indexed precedence decision remains resolved and
no exception applies.

| Mandatory behavior | Independent production evidence | Conclusion |
| --- | --- | --- |
| A clean reviewed candidate must be traceable and rollback-safe | The source archive is tied to full commit `d35a39d6284a0ac5e4c4663e743fc5bd15fe35fe`; local/remote archive SHA-256 matched; the image revision label and four in-image source hashes matched the archive; the old image tag/ID and both old container identities were captured before deployment | Passed |
| Lower-layer canonical semantics must pass before evaluating AI | Production raw inputs existed through the actual HK close; the independent canonical query found 110 exact-symbol rows, all `backfill=1`, at most 500, maximum source time 15:29:22 BJT, and no row after cutoff | Passed |
| The real ClickHouse `DateTime64` boundary must reload nonzero observations | An in-image naive-string probe produced the declared Shanghai instant; the deployed repository reloaded 110 production canonical observations for both AI snapshots | Passed |
| Startup eligibility must select only the latest completed checkpoint before `created_at` | With `created_at=15:47`, the actual 16:04:59 poll selected 15:30 as startup and did not produce 15:00 or any older AI row | Passed |
| Sufficient evidence must reach the model and persist a completed result | The 15:30 row was `completed`, had 110 observations and nonempty validated generated fields; the worker can save that state only after the prompt/model boundary returns | Passed |
| The next normal checkpoint must still execute | The same honest wall-clock poll selected actual 16:00 and saved a second `completed` row with `window_end=data_cutoff=16:00`; this was not labeled as replay | Passed |
| Duplicate logical keys must not invoke the model again | A second isolated poll selected the same due windows, returned `completed_count=0`, and left exactly two final logical rows | Passed |
| Model concurrency and component isolation must remain one/worker-only | The normal worker was stopped during each disposable poll; only one candidate process tree ran. The deployed worker has read-only production data mounts, while the fixture used a separate directory. The 3018 container never changed | Passed |
| Realtime, WebSocket, 3018/19912, and formal-signal scope must not regress | Panel ID/start/restart remained unchanged; both health endpoints passed; realtime queue stayed zero; existing accumulated reject/failure counters did not increase; Redis failures stayed zero; p95 improved relative to the freshly captured baseline; WebSocket delivered hello/snapshot/unsubscribe; monitor-symbol hash and formal-signal paths were untouched | Passed |
| Acceptance state must be cleaned without using downstream success as proof | The AI result was inspected only after canonical acceptance. Cleanup then verified zero exact fixture rows in both tables and removed the disposable container/data/secret mount | Passed |

The 16:00 result used the sufficient 110-row cumulative snapshot ending at
15:30, so this review does not claim new 15:31–15:59 canonical rows. The
authoritative acceptance requires the next normal checkpoint to execute; that
checkpoint did execute at real exchange time with the correct 16:00 cutoff.

Fresh verification after the acceptance/review edits passed: focused
repository/worker/integration tests `43 passed`, the complete backend
`141 passed`, specification contracts `5 passed`, specification compliance,
Ruff, targeted mypy, and `git diff --check`. Both stable requirements now have
complete production semantic evidence and the acceptance document is correctly
promoted to `status: passed`.

## Final-fix independent requirements-to-evidence review (2026-07-31)

This review restarted from the two indexed requirement IDs and the precedence
decision. The user-ratified authority is unambiguous: startup requires
`window_end < created_at`, normal scheduling requires
`window_end >= created_at`, and only startup requires the creation instant to
be in a regular exchange session. No unresolved specification conflict or
exception remains.

The reviewed source is commit
`6530979992a085f0e09df002ec134d9c0aa6b047`, after merge commit `62c3112`
integrated current `origin/main`. The source archive SHA-256 matched locally
and remotely. The candidate was built from the already accepted `d35a39d`
image and replaced only four source files; the repository and coordinator files
were byte-identical, while the worker and materializer hashes matched the
reviewed commit. The new runtime imports only the standard library, so the
worker-only candidate did not depend on the unrelated merged dependency-lock
changes.

| Mandatory behavior | Independent final evidence | Conclusion |
| --- | --- | --- |
| Equality is normal, not startup | The deployed-image scheduler probe selected 15:00 startup plus 15:30 normal for `created_at=15:30`; with startup terminal, it invoked the prompt only for 15:30 | Passed |
| Startup must be a pre-created regular-session checkpoint | Real exchange-calendar probes returned false for HK lunch, HK after-close, and the 2026-07-03 US holiday, with zero lower-layer or prompt calls | Passed |
| Exact-cutoff dedup must survive real `DateTime64(3)` | Production materialization created 110 canonical rows; the real repository found the exact 15:30 key; the second pass kept 110 keys and performed zero calculation and zero insertion | Passed |
| Lower-layer semantics must precede higher-layer claims | The canonical query independently proved 110 exact-symbol backfill rows, one exact-cutoff row, no future row, and bounded source time before any conclusion was drawn | Passed |
| End-to-end model semantics must remain traceable | The successful `d35a39d` production run remains the real model-call proof; byte-level candidate comparison identifies the changed scheduler/materializer delta, and the deployed scheduler probe exercises the changed prompt-routing path without claiming a new external model run | Passed as composite evidence |
| Deployment must be worker-only and rollback-safe | Only the worker was recreated; candidate revision/image labels and source hashes matched; exact `d35a39d` rollback tag/image remained available and was not needed | Passed |
| 3018, 19912, realtime, WebSocket, and formal-signal scope must not regress | Panel identity/start/restart stayed exact; both health endpoints passed; queue/failure counters stayed healthy; latency did not regress; WebSocket hello/snapshot/unsubscribe passed; monitor-symbol hash stayed unchanged | Passed |
| Acceptance fixtures must be removed | The exact 110 canonical rows were synchronously deleted, canonical and AI final counts were zero, and disposable remote scripts/SQL were removed | Passed |

The final local review also found and closed a static typing issue in the
materializer (`date` key annotation); targeted mypy, Ruff, and executable tests
then passed. One reviewer noted that the merged kline tests do not directly
exercise a private `QuoteService` fallback success branch. That pre-existing,
non-runtime-path P3 observation is unrelated to either active offline-bootstrap
requirement and is recorded without expanding this fix wave.

Fresh final evidence from the exact reviewed worktree was: offline-bootstrap
semantic slice `90 passed`; specification contracts `5 passed`; specification
compliance passed; repository `tests/backend` `147 passed`; complete
`backend/tests` `680 passed` with only 13 deprecation warnings; targeted mypy
clean for all three production modules; scoped final-fix Ruff passed; and
`git diff --check` passed. The post-merge frontend suite had already passed
205 tests with 2 skipped and its production build. No failure was waived.

Disposition: **passed**. The strict scheduler boundary, regular-session gate,
and millisecond-safe exact-cutoff behavior have lower-layer executable and live
semantic proof. The accepted candidate remains deployed with the exact prior
worker image retained for rollback. No downstream signal or golden result was
used as a substitute for canonical storage acceptance.
