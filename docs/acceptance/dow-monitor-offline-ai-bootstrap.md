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
