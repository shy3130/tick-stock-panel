status: passed

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

The final evidence must prove the exact boundary: the one startup checkpoint
requires `window_end < created_at`, while normal scheduling uses
`window_end >= created_at`. A pre-created startup is eligible only when
`calendar.is_regular_session_time(market, created_at)` is true, so lunch,
after-close, and holiday creation must not trigger it. The same evidence must
prove exact-cutoff deduplication against ClickHouse `DateTime64(3)` without a
duplicate calculation or insert.

The successful `d35a39d` evidence below remains the end-to-end model-boundary
evidence. The final-fix reacceptance at the end of this document separately
accepts the stricter equality boundary, non-regular-session gate, and
millisecond-safe exact-cutoff behavior introduced by `6530979`.

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

## Task 7 local regression and non-impact evidence (2026-07-31)

Status: passed locally; production observations remain pending for Task 8.

Fresh verification from repository HEAD ran the required focused lower-layer,
worker, and integration slice:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_half_hour_ai.py tests/backend/test_dow_monitor_offline_bootstrap.py tests/backend/test_dow_monitor_offline_ai_bootstrap_integration.py tests/backend/test_dow_monitor_minute_result_materializer.py tests/backend/test_dow_monitor_minute_result_history.py tests/backend/test_dow_monitor_minute_result_calculator.py tests/backend/test_dow_monitor_minute_result_source.py tests/backend/test_dow_monitor_minute_result_repository.py -q
```

Result: `82 passed in 8.53s`.

The authority/isolation contracts and repository specification contract were
then run together:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/spec_contracts/test_dow_monitor_offline_ai_bootstrap_contract.py tests/spec_contracts/test_spec_guard_contract.py -q
```

Result: `5 passed in 0.54s`.

```powershell
python scripts/check_spec_compliance.py
```

Result: `Specification compliance passed.`

The complete backend suite passed independently:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend -q
```

Result: `139 passed in 12.05s`.

No unrelated or pre-existing backend failure was observed. A path-level diff
from the completed authority slice (`cfac9d8`) through the verified
implementation changes contains only the canonical materializer, the offline
bootstrap coordinator, the dedicated AI worker, and its Compose service
configuration. It contains no frontend/static bundle, API route, API model,
overview/detail payload, 3018 startup, WebSocket/realtime, monitor-service, or
formal-signal file change. Therefore this stored-data-availability change
requires no frontend bundle or API payload change.

### Deferred-minor triage

The six deferred test-quality observations in the SDD ledger were reviewed
against the authoritative requirements, current implementation, and layered
executable evidence:

- The row-budget unit test proves zero insertion but does not spy on the pure
  calculator. This is accepted as non-blocking: the budget branch is visibly
  before the calculator call, the 501-row hard-ceiling test proves the branch,
  and the authoritative acceptance is zero rows beyond the 500-row ceiling.
- The Task 2 history-builder fake hardcodes `backfill=True`. This observation
  is closed by the Task 6 integration test, which uses the real history builder
  and asserts every persisted canonical row has `backfill=true`.
- `select_due_windows()` accepts terminal windows while production supplies an
  empty set. This is accepted as non-blocking: `_run_checkpoint()` performs
  the repository terminal-key check before session, canonical-row, bootstrap,
  or model work, and the executable terminal-startup and duplicate-key tests
  prove no older fallback or repeated call.
- The worker `busy` test does not literally execute a second poll. This is
  accepted as non-blocking: it proves no terminal row is saved, every poll
  recomputes the same eligible window, and coordinator tests separately prove
  `busy` is retryable and the slot is released only after physical completion.
- The integration ClickHouse fake dispatches by table and has no post-cutoff
  poison row. This is accepted as layered evidence: the integration verifies
  every persisted source timestamp and 19912 `as_of` is at or before the
  checkpoint, while source, history, repository, and worker tests independently
  exercise strict cutoff filtering and future-row exclusion.
- The integration assertion `len(rows) <= 500` follows from its exact 30/2 row
  counts. This observation is closed by the independent 501-row materializer
  test, which proves the absolute 500-row ceiling and zero partial insertion.

None of these observations is a requirement, safety, or production-code gap;
no test waiver is being used to hide a failing command.

## Production observations originally pending for Task 8

The local executable evidence does not substitute for production acceptance.
The 10.28 worker image/SHA, live raw and canonical ClickHouse queries, worker
restart/model-concurrency observations, next-normal-checkpoint behavior, and
3018/WebSocket restart, backlog, latency, and formal-signal non-regression
evidence remained pending until the successful Task 8 retry recorded below.

## Task 8 failed production boundary and Task 8A local correction (2026-07-31)

Status: the repository boundary is corrected and accepted locally; production
acceptance remains pending a new Task 8 deployment.

The first controlled Task 8 attempt deployed candidate
`f4413441afb910f505c170d81dfceb8aa4f53d1e` to the dedicated worker only.
Production proved the lower layer by persisting 98 canonical `2526.HK` rows,
all `backfill=1`, no later than the 15:00 Beijing cutoff, and below the 500-row
limit. The higher-layer snapshot nevertheless contained zero observations and
saved `INSUFFICIENT_DATA` without calling the model.

The failed handoff was traced to the real storage representation:
`lb_dow_monitor_minute_results` declares its datetime columns as
`DateTime64(3, 'Asia/Shanghai')`, while ClickHouse `FORMAT JSONEachRow`
serialized values such as `2026-07-31 09:31:00.000` without an offset.
`DowMonitorMinuteResultRepository.load_cumulative_rows()` passed that naive
local value through, and `HalfHourAiSnapshotBuilder._time()` correctly rejected
it as ambiguous. The production worker was rolled back and fixture data was
removed; no production acceptance was promoted.

Task 8A adds deserialization at the owning repository boundary. Naive values
from the table's declared DateTime64 columns receive `Asia/Shanghai` without
changing their wall-clock value; already-aware values retain their original
offset and instant. The executable repository/snapshot test uses the exact
production string shape, proves the 14:59 row becomes an aware Beijing instant
and is counted, and proves a 15:01 poison row remains excluded by the 15:00
cutoff. The existing-key query is covered with the same naive DateTime64 shape,
so bounded materialization deduplication also receives aware logical keys.

The requested focused repository, snapshot/worker, and offline integration
slice passed locally (`43 passed`). This is local semantic evidence for the
corrected storage-to-snapshot boundary only. Task 8 must be rerun from a new
commit-addressed candidate before claiming a successful model call, the next
normal checkpoint, or production acceptance.

Fresh broader verification also passed: the complete backend suite reported
`141 passed`; the offline-bootstrap and specification-guard contracts reported
`5 passed`; repository compliance reported `Specification compliance passed`;
Ruff reported `All checks passed`; and targeted mypy reported no issues for the
changed production module. No production endpoint, host, container, or data was
accessed during Task 8A.

## Task 8 production retry acceptance (2026-07-31)

Status: passed on the established 10.28 production host.

The retry used a new clean Git archive from
`d35a39d6284a0ac5e4c4663e743fc5bd15fe35fe`; it did not reuse the failed
`f441344` image. The archive SHA-256 was
`d0c58525a9fb79f9693fdc52b21d87e7869bb198908b5b2173c6996e633b6bf4`.
The worker-only candidate was:

```text
tag: tickflow-stock-panel-app:dow-offline-bootstrap-d35a39d6284a
image ID: sha256:c608dd809a49228874c6e6ab03b905ef4594e0a22883252e3a5910e290556a3a
revision: d35a39d6284a0ac5e4c4663e743fc5bd15fe35fe
```

The image was layered from the exact rollback image
`sha256:908b0722e187d0b582cae65bb3207456418345d148259634cd22c1d1d4a04aa7`.
Its four worker/repository file hashes matched the extracted Git archive.
An in-image probe normalized the real ClickHouse shape
`2026-07-31 14:59:00.000` to
`2026-07-31T14:59:00+08:00[Asia/Shanghai]`.

### Lower-layer production gate

The disposable `2526.HK` fixture used production ClickHouse raw inputs while
remaining outside the 3018 data directory and formal-signal store. Before the
poll, canonical and AI counts were both zero. Raw evidence through the actual
16:00 Hong Kong close contained 484 quote rows, 4,455 depth rows, 724 trade
rows, 1,081 candlestick rows including warmup, and 49 capital rows.

The canonical query was evaluated before the AI query and proved:

- 110 rows for exactly `hk/2526.HK`;
- 110 of 110 rows had `backfill=1`;
- first decision minute 09:31 BJT and last decision minute 15:30 BJT;
- maximum source timestamp `2026-07-31T15:29:22.039+08:00`;
- zero row later than the 16:00 data cutoff;
- 110 rows is below the absolute 500-row budget.

#### Sanitized exact production query record

These are the exact SQL statements executed through
`app.plugins.clickhouse.bridge.query_json_each_row()` inside the deployed
worker. The bridge supplied the configured ClickHouse endpoint/credentials and
appended `FORMAT JSONEachRow`; no credential value is retained here.

Raw preconditions:

```sql
SELECT count() n, max(updated_at) latest
FROM longbridge.lb_realtime_quotes
WHERE symbol='2526.HK'
  AND updated_at>=parseDateTime64BestEffort('2026-07-31T09:30:00+08:00')
  AND updated_at<parseDateTime64BestEffort('2026-07-31T16:00:00+08:00');

SELECT count() n, max(updated_at) latest
FROM longbridge.lb_realtime_depth
WHERE symbol='2526.HK'
  AND updated_at>=parseDateTime64BestEffort('2026-07-31T09:30:00+08:00')
  AND updated_at<parseDateTime64BestEffort('2026-07-31T16:00:00+08:00');

SELECT count() n, max(trade_time) latest
FROM longbridge.lb_realtime_trades
WHERE symbol='2526.HK'
  AND trade_time>=parseDateTime64BestEffort('2026-07-31T09:30:00+08:00')
  AND trade_time<parseDateTime64BestEffort('2026-07-31T16:00:00+08:00');

SELECT count() n, max(bar_time) latest
FROM longbridge.lb_realtime_candlesticks FINAL
WHERE symbol='2526.HK'
  AND bar_time>=parseDateTime64BestEffort('2026-07-21T09:30:00+08:00')
  AND bar_time<parseDateTime64BestEffort('2026-07-31T16:00:00+08:00');

SELECT count() n, max(updated_at) latest
FROM longbridge.lb_realtime_capital
WHERE symbol='2526.HK'
  AND updated_at>=parseDateTime64BestEffort('2026-07-31T09:30:00+08:00')
  AND updated_at<parseDateTime64BestEffort('2026-07-31T16:00:00+08:00');
```

Zero-row preconditions:

```sql
SELECT count() n, max(decision_minute) latest
FROM longbridge.lb_dow_monitor_minute_results FINAL
WHERE symbol='2526.HK';

SELECT count() n, max(window_end) latest
FROM longbridge.lb_dow_monitor_half_hour_ai_analyses FINAL
WHERE symbol='2526.HK';
```

Canonical-first inspection:

```sql
SELECT decision_minute, source_bar_time, backfill, source_timestamps, updated_at
FROM longbridge.lb_dow_monitor_minute_results FINAL
WHERE market='hk' AND symbol='2526.HK'
ORDER BY decision_minute;
```

The retained acceptance script counted these returned rows, summed
`backfill`, partitioned them at 15:30/16:00, and parsed
`source_timestamps` to obtain the maximum source instant before running the AI
query.

AI logical-row and snapshot inspection:

```sql
SELECT analysis_id, market, symbol, trade_date, window_end, data_cutoff,
       status, model_name, error_code, input_snapshot_json, updated_at
FROM longbridge.lb_dow_monitor_half_hour_ai_analyses FINAL
WHERE market='hk' AND symbol='2526.HK'
ORDER BY window_end;
```

Generated-content nonempty check:

```sql
SELECT window_end, status,
       length(ifNull(title,'')) title_len,
       length(ifNull(summary,'')) summary_len,
       length(ifNull(conclusion,'')) conclusion_len,
       length(evidence_json) evidence_len,
       length(risks_json) risks_len,
       length(scenarios_json) scenarios_len
FROM longbridge.lb_dow_monitor_half_hour_ai_analyses FINAL
WHERE market='hk' AND symbol='2526.HK'
ORDER BY window_end;
```

The production repository then reloaded all 110 rows through the corrected
naive `DateTime64` boundary. The failed first attempt's zero-observation symptom
did not recur.

### Startup and next normal checkpoint

The fixture `created_at` was 15:47 BJT. A single actual-wall-clock poll at
16:04:59 BJT selected exactly:

```text
startup checkpoint: 15:30 BJT
next normal checkpoint: 16:00 BJT
```

It selected no 15:00 or older startup checkpoint and returned
`completed_count=2`. ClickHouse contained exactly two final AI logical rows:

- 15:30 startup: `status=completed`,
  `window_end=data_cutoff=15:30`, `observation_count=110`;
- 16:00 normal: `status=completed`,
  `window_end=data_cutoff=16:00`, `observation_count=110`.

Both rows had nonempty title, summary, conclusion, evidence, risks, and
scenarios. The worker schema does not currently populate `model_name`, but the
production code can save `completed` only after
`HalfHourAiPromptService.analyze()` returns validated generated content.
Therefore the two completed rows and `completed_count=2` are direct model
boundary evidence.

The 16:00 normal checkpoint honestly used the 110 cumulative observations
available through 15:30; because that snapshot was already sufficient, it did
not invoke another offline materialization. This evidence proves the next
normal checkpoint executed, but does not claim that wall-clock rows from
15:31–15:59 were newly materialized.

A second isolated poll at 16:06:56 selected the same two due windows but
returned `completed_count=0` and left exactly two logical rows, proving terminal
logical-key deduplication without another model call.

### Isolation and non-regression

The normal worker was stopped while each disposable poll ran, so physical model
concurrency remained one. After acceptance, only the normal candidate worker
was running, with one `uv` parent and one Python worker process.

The 3018 panel container remained
`183eef17e421ee4e055d020b458a2fa67cb96a3c4a0b3bb4ed27a63271ea92c5`,
on the old panel image with `RestartCount=0` and unchanged start time. Both
3018 `/health` and 19912 `/api/health` returned healthy responses.

Against the freshly recorded degraded realtime baseline:

- writer queue remained zero;
- accumulated `rejected=27595` and `flush_failures=11` did not increase;
- consecutive flush failures remained zero;
- Redis stayed connected with zero publish failures;
- callback-to-publish p95 moved from 168.98 ms to 128.73 ms;
- live WebSocket returned `hello`, an `1888.HK` snapshot in 224.2 ms, and a
  clean unsubscribe acknowledgement.

The deployed worker mounts production `/app/data` read-only. The acceptance
fixture mounted a separate data directory. The panel identity, formal-signal
code paths, and production monitor-symbol SHA-256
`9df392a342def1cf9f32b64ef92a1123dfae6cfa4d96b21de6d41463e04e69cd`
were unchanged. Worker and panel error-log probes were empty.

Finally, the 110 canonical rows and two AI rows were deleted for exactly
`hk/2526.HK`; both final counts were independently verified as zero. The
disposable container, fixture directory, and nested secret bind were removed.
The commit-addressed candidate remains deployed and healthy.

Exact cleanup statements executed through
`DowMonitorMinuteResultRepository._default_execute()`:

```sql
ALTER TABLE longbridge.lb_dow_monitor_minute_results
DELETE WHERE market='hk' AND symbol='2526.HK'
SETTINGS mutations_sync=2;

ALTER TABLE longbridge.lb_dow_monitor_half_hour_ai_analyses
DELETE WHERE market='hk' AND symbol='2526.HK'
SETTINGS mutations_sync=2;
```

Exact post-cleanup zero checks executed through the production bridge:

```sql
SELECT count() n
FROM longbridge.lb_dow_monitor_minute_results FINAL
WHERE market='hk' AND symbol='2526.HK';

SELECT count() n
FROM longbridge.lb_dow_monitor_half_hour_ai_analyses FINAL
WHERE market='hk' AND symbol='2526.HK';
```

Both returned `n=0`. These commands are retained as reproducible evidence; they
were not rerun during documentation Fix Round 1.

## Final-fix production reacceptance (2026-07-31)

Status: passed. This section records the delta-specific semantic reacceptance
for commit `6530979992a085f0e09df002ec134d9c0aa6b047` after merge commit
`62c3112` integrated current `origin/main` (`a975e93`). It is read together
with the successful `d35a39d` end-to-end model evidence above; it does not
rewrite that historical run.

### Candidate identity and accepted delta

The source archive `source-6530979992a0.tar` had matching local and remote
SHA-256
`b4b584ccfaf47c43346ba8e732055de31b6d66870e96227c3ab62b3c2a53d4ee`.
The worker-only image was built without cache from the accepted `d35a39d`
worker image and copied only the four relevant source files. In-image hashes
matched the commit archive:

| Runtime file | `d35a39d` archive SHA-256 | `6530979` archive SHA-256 | Result |
| --- | --- | --- | --- |
| `dow_monitor_minute_result_materializer.py` | `c1d30e1...` | `599fbd6e5499709a61435db21bc4646ea4ed21e8a415450d13281e3de4321fc0` | Changed: millisecond-safe exclusive upper bound |
| `dow_monitor_minute_result_repository.py` | `517782ac...` | `517782ac...` | Identical |
| `dow_monitor_offline_bootstrap.py` | `20083270...` | `20083270...` | Identical |
| `dow_monitor_half_hour_ai.py` | `b9201f4f...` | `90f3cccb009a695c90753562fb2b95d98646516e44fce645e71da9a8256528c3` | Changed: strict startup boundary and regular-session gate |

The deployed tag is
`tickflow-stock-panel-app:dow-offline-bootstrap-6530979992a0`, image ID
`sha256:f5369507b798564e271c1f0a731faf3833ee2085928ff4a4380e4666ebd0d442`,
and worker container ID
`071f5b701307832c33710fadb369364a5402cb6062224a54148d9ae8d2ad464b`.
The exact rollback remains
`tickflow-stock-panel-app:dow-offline-bootstrap-d35a39d6284a` with image ID
`sha256:c608dd809a49228874c6e6ab03b905ef4594e0a22883252e3a5910e290556a3a`;
no rollback trigger fired, so the accepted candidate remains deployed.

### Scheduler boundary and gate proof

The same isolated executable probe passed locally and inside the deployed
candidate image using the real exchange calendar:

- for a symbol created exactly at 15:30, selection returned 15:00 as the one
  startup checkpoint and 15:30 as the normal checkpoint; after the startup
  logical key was pre-marked terminal, the worker called the prompt exactly
  once for the 15:30 normal checkpoint and never fell back to an older startup;
- HK creation at 12:30 during lunch and at 16:30 after close both returned
  `is_regular_session_time=false` and performed zero snapshot, bootstrap,
  persistence, or prompt calls;
- US creation on the 2026-07-03 exchange holiday also returned false.

This directly proves startup uses `window_end < created_at`, normal scheduling
uses `window_end >= created_at`, and only the startup exception is suppressed
outside a regular session.

### Real `DateTime64(3)` lower-layer proof

Production raw inputs for the isolated `hk/2526.HK` fixture were first checked
to exist. The deployed materializer then wrote 110 canonical backfill rows
through the 15:30 checkpoint. A real repository lookup found the exact 15:30
cutoff key through the exclusive `window_end + 1ms` boundary. Repeating the
same materialization returned:

```json
{"exact_cutoff_present":true,"first_written_rows":110,"logical_keys_after_second":110,"logical_keys_before_second":110,"second_calculate_calls":0,"second_written_rows":0}
```

Before cleanup, the independently queried canonical table contained exactly
110 logical rows, all `backfill=1`, spanning 09:31 through 15:30, with latest
source bar 15:29, one exact-cutoff row, and zero future rows. This establishes
storage/key semantics before any higher-layer conclusion. No AI row was
written during this delta-specific probe; the successful `d35a39d` production
run above remains the real model-call evidence for the unchanged prompt and
analysis path.

### Worker-only isolation and cleanup

Only `TickFlow_Dow_AI_Worker` was recreated. At the final 20:48 BJT check it
was running with `RestartCount=0`, one `uv` parent, and one Python worker. The
3018 panel remained the exact pre-deployment container
`183eef17e421ee4e055d020b458a2fa67cb96a3c4a0b3bb4ed27a63271ea92c5`,
image `sha256:908b0722e187d0b582cae65bb3207456418345d148259634cd22c1d1d4a04aa7`,
original start time, and `RestartCount=0`. Both 3018 `/health` and 19912
`/api/health` passed, and worker/panel error scans were empty.

The realtime writer queue remained zero; accumulated `rejected=27595` and
`flush_failures=11` did not increase; consecutive failures and Redis publish
failures remained zero; callback-to-publish p95 moved from 172.33 ms to
169.812 ms. A live 3018 WebSocket probe received `hello`, an `1888.HK`
snapshot in 429.1 ms, and a clean unsubscribe acknowledgement. The production
monitor-symbol SHA-256 remained
`9df392a342def1cf9f32b64ef92a1123dfae6cfa4d96b21de6d41463e04e69cd`.

The 110 fixture canonical rows were deleted with a synchronous exact-symbol
ClickHouse mutation; final canonical and AI counts were both zero. Those
materialized fixture rows are no longer recoverable as stored rows, but are
reconstructible from the retained raw inputs. No AI fixture row had been
created. Disposable remote scripts and SQL were removed; only the
commit-addressed Dockerfile and source archive remain in the build directory.

### Fresh final local gates

After the acceptance and review edits, the exact final worktree passed:

```text
offline-bootstrap semantic slice: 90 passed in 10.09s
specification contracts: 5 passed in 0.51s
repository specification compliance: passed
repository tests/backend: 147 passed in 13.55s
complete backend/tests: 680 passed in 51.42s (13 deprecation warnings)
targeted mypy: success, 3 production source files
scoped final-fix Ruff: all checks passed
git diff --check: passed
```

The earlier post-merge frontend gate also passed 205 tests with 2 skipped and
completed the production build. No test failure was waived.

The combined historical end-to-end evidence and this commit-addressed
delta-specific production evidence satisfy both active requirements without
using a downstream signal, snapshot, or golden as a substitute for lower-layer
semantic acceptance.
