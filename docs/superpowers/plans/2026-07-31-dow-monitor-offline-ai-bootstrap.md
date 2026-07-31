# Dow Monitor Offline AI Bootstrap Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Use `spec-guard` before every production edit, use `superpowers:test-driven-development` for each behavior change, and use `superpowers:verification-before-completion` before claiming completion or publishing.

**Goal:** Make a newly added, in-session trend-monitor stock immediately produce the independent half-hour AI analysis from already persisted offline market data, without waiting for another 30 minutes of live WebSocket accumulation.

**Architecture:** Keep the existing half-hour AI worker as the only orchestration owner. When the latest completed checkpoint lacks enough canonical minute-result rows, the worker asks a bounded bootstrap coordinator to materialize one symbol from regular-session open through that checkpoint using the existing ClickHouse raw-data source, history builder, and canonical minute-result calculator. The worker then reloads the canonical rows and either invokes the LLM or stores an explicit `insufficient_data` result. Bootstrap work runs outside the 3018/WebSocket path, remains single-flight, and never changes real-time interpretation or formal buy/sell signal semantics.

**Tech Stack:** Python 3.11, asyncio, FastAPI service modules, ClickHouse, Pydantic, pytest, Docker Compose, existing Longbridge/Dow 19912 client.

**Active requirements:**

- `REQ-DOW-MONITOR-HALF-HOUR-AI-OFFLINE-BOOTSTRAP-001`
- `REQ-DOW-MONITOR-HALF-HOUR-AI-BOOTSTRAP-ISOLATION-001`

**Approved authority:** `docs/superpowers/specs/2026-07-31-dow-monitor-offline-ai-bootstrap-design.md`

---

## Non-negotiable semantic constraints

- A stock added at 10:17 may analyze only the latest completed checkpoint, 10:00. It must not replay 09:30 or any earlier checkpoint.
- This is the sole exception to the old `window_end <= created_at` skip rule. Every later checkpoint continues under normal scheduling.
- The AI snapshot must read canonical rows from `longbridge.lb_dow_monitor_minute_results`; the AI worker must not independently calculate a second version of the indicators from raw tables.
- Offline materialization may read only data whose event time is at or before the selected checkpoint. `data_cutoff` must equal that checkpoint.
- A logical checkpoint with an existing terminal result (`completed` or `insufficient_data`) must not be materialized or sent to the LLM again.
- A single bootstrap may generate at most 500 canonical minute rows. The worker wait budget is 15 seconds.
- Model concurrency remains one. Only one offline materialization may be in flight.
- A failure or timeout for one symbol must not stop subsequent symbols or terminate the worker loop.
- Bootstrap runs only in `TickFlow_Dow_AI_Worker`. It must not enter the 3018 request/render path or any WebSocket callback.
- When evidence remains insufficient, persist `insufficient_data` with a precise error/reason and do not invoke the model.
- Do not alter formal buy/sell signals, real-time key interpretation, WebSocket subscriptions, or the ordinary minute persistence cadence.

## Required state model

The coordinator returns a typed outcome so the worker can distinguish a terminal data failure from a temporary single-flight contention:

```python
BootstrapStatus = Literal[
    "not_needed",
    "completed",
    "budget_exceeded",
    "timed_out",
    "busy",
    "failed",
]


class OfflineBootstrapOutcome(BaseModel):
    status: BootstrapStatus
    attempted: bool
    written_rows: int = 0
    error_code: str | None = None
    error_message: str | None = None

    @property
    def retryable(self) -> bool:
        return self.status == "busy"
```

`busy` is not a terminal checkpoint result: the worker skips that symbol for the current poll and retries on a later poll. `budget_exceeded`, `timed_out`, and `failed` are terminal for that logical checkpoint and are stored as `insufficient_data` or the existing failure state according to the repository contract.

---

## Task 1: Register the approved authority, resolved precedence, and traceability

**Files:**

- Modify: `docs/superpowers/specs/2026-07-31-dow-monitor-offline-ai-bootstrap-design.md`
- Create: `docs/decisions/2026-07-31-dow-monitor-offline-ai-bootstrap-precedence.md`
- Create: `docs/acceptance/dow-monitor-offline-ai-bootstrap.md`
- Create: `docs/reviews/2026-07-31-dow-monitor-offline-ai-bootstrap-review.md`
- Modify: `docs/spec-index.yaml`
- Modify: `docs/traceability.yaml`
- Test: `tests/spec_contracts/test_dow_monitor_offline_ai_bootstrap_contract.py`

- [ ] **Step 1: Write the failing specification-contract test**

Create a test that loads `docs/spec-index.yaml` and `docs/traceability.yaml` and asserts:

```python
REQUIREMENT_IDS = {
    "REQ-DOW-MONITOR-HALF-HOUR-AI-OFFLINE-BOOTSTRAP-001",
    "REQ-DOW-MONITOR-HALF-HOUR-AI-BOOTSTRAP-ISOLATION-001",
}


def test_offline_bootstrap_authority_and_traceability_are_registered():
    index = load_yaml("docs/spec-index.yaml")
    traceability = load_yaml("docs/traceability.yaml")

    assert approved_spec_is_authoritative(index)
    assert old_created_at_conflict_is_resolved(index)
    assert REQUIREMENT_IDS <= traced_requirement_ids(traceability)


def test_bootstrap_contract_keeps_websocket_and_realtime_paths_out_of_scope():
    text = Path(
        "docs/superpowers/specs/"
        "2026-07-31-dow-monitor-offline-ai-bootstrap-design.md"
    ).read_text(encoding="utf-8")
    assert "WebSocket" in text
    assert "不改变正式买卖信号" in text
```

Run:

```powershell
Set-Location E:\my_project\.worktrees\tickflow-monitor-list-v2
.\backend\.venv\Scripts\python.exe -m pytest tests/spec_contracts/test_dow_monitor_offline_ai_bootstrap_contract.py -q
```

Expected: FAIL because the approved spec, decision, and traceability entries are not registered.

- [ ] **Step 2: Mark the design approved and record the narrow precedence decision**

Change the design status to approved and create the decision document. The decision must state:

- The existing half-hour specification remains authoritative for normal checkpoints.
- The new specification supersedes only the clause that forbids a model call for the latest completed checkpoint before a newly monitored symbol's `created_at`.
- Exactly one such startup checkpoint is eligible.
- Older checkpoints remain prohibited.
- The user approved this rule on 2026-07-31.

- [ ] **Step 3: Register both stable requirement IDs**

Add the approved design to `docs/spec-index.yaml`, add a resolved conflict referencing the precedence decision, and add both requirements to `docs/traceability.yaml`.

The initial traceability paths must all exist before production edits:

```yaml
implementation:
  - backend/app/services/dow_monitor_minute_result_materializer.py
  - backend/app/workers/dow_monitor_half_hour_ai.py
tests:
  - tests/backend/test_dow_monitor_minute_result_materializer.py
  - tests/backend/test_dow_monitor_half_hour_ai.py
acceptance:
  - docs/acceptance/dow-monitor-offline-ai-bootstrap.md
review:
  - docs/reviews/2026-07-31-dow-monitor-offline-ai-bootstrap-review.md
```

The acceptance and review documents begin with `status: pending`; they must describe the exact evidence that will close them, rather than claiming work has passed.

- [ ] **Step 4: Run the contract and repository compliance checks**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/spec_contracts/test_dow_monitor_offline_ai_bootstrap_contract.py -q
python scripts/check_spec_compliance.py
```

Expected: PASS. If an unresolved conflict is reported, stop before production code and request an authoritative decision.

- [ ] **Step 5: Commit the authority slice**

```powershell
git add docs/superpowers/specs/2026-07-31-dow-monitor-offline-ai-bootstrap-design.md docs/decisions/2026-07-31-dow-monitor-offline-ai-bootstrap-precedence.md docs/acceptance/dow-monitor-offline-ai-bootstrap.md docs/reviews/2026-07-31-dow-monitor-offline-ai-bootstrap-review.md docs/spec-index.yaml docs/traceability.yaml tests/spec_contracts/test_dow_monitor_offline_ai_bootstrap_contract.py
git commit -m "docs: authorize offline bootstrap for monitor AI"
```

---

## Task 2: Add a bounded single-symbol checkpoint entry to the canonical materializer

**Files:**

- Modify: `backend/app/services/dow_monitor_minute_result_materializer.py`
- Modify: `tests/backend/test_dow_monitor_minute_result_materializer.py`

- [ ] **Step 1: Write failing materializer behavior tests**

Add focused tests using the existing source/repository/history-builder fakes:

```python
def test_materialize_checkpoint_writes_only_missing_rows_through_cutoff():
    run = materializer.materialize_checkpoint(
        symbol=monitored_symbol("RNG.US"),
        session_open=beijing("2026-07-31T21:30:00"),
        window_end=beijing("2026-07-31T22:00:00"),
        max_rows=500,
    )

    assert run.error is None
    assert all(
        beijing("2026-07-31T21:30:00") < row.observed_at
        <= beijing("2026-07-31T22:00:00")
        for row in repository.inserted_rows
    )
    assert all(row.is_backfill for row in repository.inserted_rows)
    assert run.written_rows == len(repository.inserted_rows)


def test_materialize_checkpoint_does_not_insert_when_row_budget_is_exceeded():
    run = materializer.materialize_checkpoint(
        symbol=monitored_symbol("RNG.US"),
        session_open=beijing("2026-07-31T09:30:00"),
        window_end=beijing("2026-07-31T22:00:00"),
        max_rows=2,
    )

    assert run.error.code == "BACKFILL_BUDGET_EXCEEDED"
    assert repository.inserted_rows == []


def test_materialize_checkpoint_never_reads_after_window_end():
    materializer.materialize_checkpoint(
        symbol=monitored_symbol("RNG.US"),
        session_open=beijing("2026-07-31T21:30:00"),
        window_end=beijing("2026-07-31T22:00:00"),
        max_rows=500,
    )

    assert source.calls[0].end == beijing("2026-07-31T22:00:00")
```

Also cover:

- already materialized logical minute keys are skipped;
- only the requested symbol is loaded;
- history candles may start at `session_open - WARMUP_DAYS`, but quote/depth/trade/capital evidence starts at the requested session;
- an upstream source/history/repository exception becomes a typed `MaterializeRun.error` and does not partially insert after a row-budget rejection.

Run:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_minute_result_materializer.py -q
```

Expected: FAIL because `materialize_checkpoint` does not exist.

- [ ] **Step 2: Implement the bounded checkpoint method**

Add this public entry:

```python
def materialize_checkpoint(
    self,
    *,
    symbol: MonitoredSymbol,
    session_open: datetime,
    window_end: datetime,
    max_rows: int = 500,
) -> MaterializeRun:
    ...
```

Implementation rules:

1. Reject non-positive `max_rows`, naive datetimes, or `window_end <= session_open`.
2. Call `load_raw_history` for one symbol only.
3. Use `candle_start=session_open - timedelta(days=WARMUP_DAYS)` for stable 5m/15m/30m structure, but keep the target market day and decision-minute range limited to `(session_open, window_end]`.
4. Ask the existing repository for existing logical minute keys in that same range.
5. Use `DowMonitorMinuteResultHistoryBuilder.build_contexts(..., backfill=True, decision_minutes=missing_keys)`.
6. Check the number of candidate contexts before calculating or inserting. If it exceeds `max_rows`, return `BACKFILL_BUDGET_EXCEEDED` and write zero rows.
7. Calculate every row through the existing canonical `calculate_minute_result`; do not duplicate formulas.
8. Insert once through the existing repository and return the existing `MaterializeRun`.
9. Preserve the current `materialize()` method and its behavior.

If shared private logic is extracted, first run all existing materializer tests to prove no semantic drift.

- [ ] **Step 3: Run focused and lower-layer regression tests**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_minute_result_materializer.py tests/backend/test_dow_monitor_minute_result_history.py tests/backend/test_dow_monitor_minute_result_calculator.py tests/backend/test_dow_monitor_minute_result_source.py tests/backend/test_dow_monitor_minute_result_repository.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit the canonical materialization slice**

```powershell
git add backend/app/services/dow_monitor_minute_result_materializer.py tests/backend/test_dow_monitor_minute_result_materializer.py
git commit -m "feat: materialize monitor results for one AI checkpoint"
```

---

## Task 3: Add a 15-second single-flight bootstrap coordinator

**Files:**

- Create: `backend/app/services/dow_monitor_offline_bootstrap.py`
- Create: `tests/backend/test_dow_monitor_offline_bootstrap.py`
- Modify: `docs/traceability.yaml`

- [ ] **Step 1: Write failing coordinator tests**

Use an injected materialization callable so tests do not sleep or use ClickHouse:

```python
@pytest.mark.asyncio
async def test_success_runs_materializer_off_event_loop():
    outcome = await coordinator.ensure_checkpoint(
        symbol=monitored_symbol("RNG.US"),
        session_open=beijing("2026-07-31T21:30:00"),
        window_end=beijing("2026-07-31T22:00:00"),
    )

    assert outcome.status == "completed"
    assert outcome.written_rows == 30


@pytest.mark.asyncio
async def test_wait_budget_returns_terminal_timeout():
    outcome = await coordinator_with_controlled_future.ensure_checkpoint(...)

    assert outcome.status == "timed_out"
    assert outcome.error_code == "BACKFILL_TIMEOUT"


@pytest.mark.asyncio
async def test_second_bootstrap_is_busy_while_single_flight_is_running():
    first = asyncio.create_task(coordinator.ensure_checkpoint(...))
    await materializer_started.wait()

    second = await coordinator.ensure_checkpoint(...)

    assert second.status == "busy"
    assert second.retryable is True
    release_materializer.set()
    await first
```

Also cover materializer row-budget errors, arbitrary exceptions, and cleanup after a completed in-flight future.

Run:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_offline_bootstrap.py -q
```

Expected: FAIL because the coordinator does not exist.

- [ ] **Step 2: Implement typed outcomes and the coordinator**

Implement:

```python
class DowMonitorOfflineBootstrap:
    def __init__(
        self,
        materializer: DowMonitorMinuteResultMaterializer,
        *,
        timeout_seconds: float = 15.0,
        max_rows: int = 500,
    ) -> None:
        ...

    async def ensure_checkpoint(
        self,
        *,
        symbol: MonitoredSymbol,
        session_open: datetime,
        window_end: datetime,
    ) -> OfflineBootstrapOutcome:
        ...
```

Required execution semantics:

- Start the synchronous ClickHouse/materializer call with `asyncio.to_thread`.
- Keep exactly one in-flight task on the coordinator.
- Await it with `asyncio.wait_for(asyncio.shield(task), timeout_seconds)`.
- On timeout, return `BACKFILL_TIMEOUT` immediately and retain a done callback that consumes the late result/exception and clears the single-flight slot. Document that Python cannot forcibly cancel an already-running blocking DB call; the 15-second rule is the worker's maximum wait and decision budget.
- While a timed-out task is still physically running, return `busy` for other bootstrap attempts without starting more threads.
- Never invoke the LLM in this service.
- Convert materializer error codes without discarding the original diagnostic message.

- [ ] **Step 3: Register the new implementation and test paths**

Append these paths to both requirement entries in `docs/traceability.yaml`:

```yaml
implementation:
  - backend/app/services/dow_monitor_offline_bootstrap.py
tests:
  - tests/backend/test_dow_monitor_offline_bootstrap.py
```

- [ ] **Step 4: Run coordinator and compliance tests**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_offline_bootstrap.py -q
python scripts/check_spec_compliance.py
```

Expected: PASS.

- [ ] **Step 5: Commit the isolation slice**

```powershell
git add backend/app/services/dow_monitor_offline_bootstrap.py tests/backend/test_dow_monitor_offline_bootstrap.py docs/traceability.yaml
git commit -m "feat: isolate offline monitor bootstrap"
```

---

## Task 4: Select exactly one startup checkpoint and re-evaluate after bootstrap

**Files:**

- Modify: `backend/app/workers/dow_monitor_half_hour_ai.py`
- Modify: `tests/backend/test_dow_monitor_half_hour_ai.py`

- [ ] **Step 1: Replace the conflicting old scheduling test with failing approved behavior tests**

Replace the old assertion that always skips pre-`created_at` windows with explicit startup and normal-schedule cases:

```python
@pytest.mark.asyncio
async def test_new_symbol_analyzes_only_latest_completed_startup_checkpoint():
    symbol = monitored_symbol(created_at=beijing("2026-07-31T22:17:00"))
    calendar.completed = [
        beijing("2026-07-31T21:30:00"),
        beijing("2026-07-31T22:00:00"),
    ]

    await worker.run_due_jobs(now=beijing("2026-07-31T22:17:05"))

    assert bootstrap.window_ends == [beijing("2026-07-31T22:00:00")]
    assert prompt.window_ends == [beijing("2026-07-31T22:00:00")]


@pytest.mark.asyncio
async def test_next_normal_checkpoint_runs_after_startup_checkpoint():
    repository.completed_keys.add(startup_key("RNG.US", "22:00"))
    calendar.completed.append(beijing("2026-07-31T22:30:00"))

    await worker.run_due_jobs(now=beijing("2026-07-31T22:30:05"))

    assert prompt.window_ends == [beijing("2026-07-31T22:30:00")]
```

Add tests for:

- canonical rows already sufficient: skip bootstrap, invoke LLM once;
- first snapshot insufficient: bootstrap once, reload rows, rebuild snapshot, invoke LLM only if now sufficient;
- materialization still insufficient: save `insufficient_data`, do not invoke LLM;
- `BACKFILL_BUDGET_EXCEEDED`: save `insufficient_data` with that code;
- `BACKFILL_TIMEOUT`: save terminal data result without invoking LLM;
- `busy`: do not save a terminal result, allowing the next poll to retry;
- one symbol failure does not prevent the next symbol;
- existing terminal logical result skips bootstrap and model;
- no startup checkpoint exists before the current time: do nothing;
- no future data is loaded beyond `window_end`.

Run:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_half_hour_ai.py -q
```

Expected: FAIL under the current unconditional `window_end <= symbol.created_at` skip rule.

- [ ] **Step 2: Isolate target-window selection in a pure helper**

Add a small pure function/method:

```python
def select_due_windows(
    *,
    completed_windows: Sequence[datetime],
    created_at: datetime,
    terminal_window_ends: set[datetime],
    startup_eligible: bool = True,
) -> list[datetime]:
    latest_before_created_at = max(
        (window for window in completed_windows if window < created_at),
        default=None,
    )
    startup = (
        latest_before_created_at
        if startup_eligible
        and latest_before_created_at not in terminal_window_ends
        else None
    )
    normal = [
        window
        for window in completed_windows
        if window >= created_at and window not in terminal_window_ends
    ]
    return ([startup] if startup is not None else []) + normal
```

Do not literally use this code if the repository can query only one logical key at a time; preserve the semantics while avoiding an unnecessary full-history query. The selected startup list must contain at most one checkpoint. Startup uses strict `window_end < created_at`; normal scheduling uses `window_end >= created_at`. Pass `startup_eligible=calendar.is_regular_session_time(market, created_at)` so lunch, after-close, and holiday creation suppress only the pre-created startup exception. If the latest pre-`created_at` checkpoint is already terminal, do nothing for startup; never fall back to an older unfinished checkpoint.

- [ ] **Step 3: Add the bootstrap/reload decision inside the worker**

For each selected logical checkpoint:

1. Check terminal-result existence first.
2. Compute `session_open`.
3. Load cumulative canonical minute rows through `window_end`.
4. Build the snapshot.
5. If sufficient, continue through the existing `running -> LLM -> completed` path without bootstrap.
6. If insufficient, invoke `offline_bootstrap.ensure_checkpoint(...)` once.
7. For `busy`, skip without writing a terminal result.
8. For a terminal bootstrap error, persist the explicit non-model result and continue to the next symbol.
9. For `completed`, reload cumulative rows and rebuild the snapshot from the repository.
10. If the rebuilt snapshot remains insufficient, persist `insufficient_data`; otherwise invoke the existing prompt service exactly once.

Keep all existing model schema validation, evidence rendering, repository logical keys, and sequential symbol loop unchanged.

- [ ] **Step 4: Run worker and lower-layer tests**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_half_hour_ai.py tests/backend/test_dow_monitor_offline_bootstrap.py tests/backend/test_dow_monitor_minute_result_materializer.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the orchestration slice**

```powershell
git add backend/app/workers/dow_monitor_half_hour_ai.py tests/backend/test_dow_monitor_half_hour_ai.py
git commit -m "feat: bootstrap latest monitor AI checkpoint"
```

---

## Task 5: Wire production dependencies only into the dedicated AI worker

**Files:**

- Modify: `backend/app/workers/dow_monitor_half_hour_ai.py`
- Modify: `tests/backend/test_dow_monitor_half_hour_ai.py`
- Test: `tests/spec_contracts/test_dow_monitor_offline_ai_bootstrap_contract.py`

- [ ] **Step 1: Write failing dependency-boundary tests**

Assert that `build_worker()` constructs the same canonical chain used by minute persistence:

```python
def test_build_worker_wires_canonical_materializer_into_offline_bootstrap(monkeypatch):
    worker = build_worker()

    assert isinstance(worker.offline_bootstrap, DowMonitorOfflineBootstrap)
    assert isinstance(
        worker.offline_bootstrap.materializer,
        DowMonitorMinuteResultMaterializer,
    )
```

Extend the specification contract to scan the 3018 startup and WebSocket modules and prove they do not import `DowMonitorOfflineBootstrap`.

Also assert that the worker still has model concurrency one and that no new task/queue is created in the real-time service.

Run:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_half_hour_ai.py tests/spec_contracts/test_dow_monitor_offline_ai_bootstrap_contract.py -q
```

Expected: FAIL because `build_worker()` does not yet wire the coordinator.

- [ ] **Step 2: Build the canonical dependency graph**

Inside `build_worker()`, construct:

```python
minute_repository = DowMonitorMinuteResultRepository()
dow_client = LongbridgeDowClient(
    os.getenv("LONGBRIDGE_API_URL", "http://host.docker.internal:19912")
)
materializer = DowMonitorMinuteResultMaterializer(
    source=DowMonitorMinuteResultSource(),
    repository=minute_repository,
    history_builder=DowMonitorMinuteResultHistoryBuilder(
        DowEngineStableStateBuilder(dow_client)
    ),
    notifications_fn=lambda: store.list_notifications(limit=1_000_000),
)
offline_bootstrap = DowMonitorOfflineBootstrap(
    materializer,
    timeout_seconds=float(
        os.getenv("DOW_MONITOR_AI_BOOTSTRAP_TIMEOUT_SECONDS", "15")
    ),
    max_rows=int(os.getenv("DOW_MONITOR_AI_BOOTSTRAP_MAX_ROWS", "500")),
)
```

Reuse the same `minute_repository` instance for cumulative AI reads and checkpoint materialization. Pass the coordinator into `DowMonitorHalfHourAiWorker`.

If the client exposes `close()`, add an explicit worker shutdown/finally hook and test it. Do not add this wiring to `backend/app/main.py` beyond any already-existing ordinary minute materializer.

- [ ] **Step 3: Verify configuration and service isolation**

Confirm the current worker container already has:

- ClickHouse connectivity;
- `LONGBRIDGE_API_URL`/19912 reachability;
- the same AI provider variables;
- no need to expose a new host port.

Add environment defaults to Compose only if they are absent. Do not change the WebSocket service command or its restart policy.

- [ ] **Step 4: Run focused tests and compliance**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_half_hour_ai.py tests/backend/test_dow_monitor_offline_bootstrap.py tests/spec_contracts/test_dow_monitor_offline_ai_bootstrap_contract.py -q
python scripts/check_spec_compliance.py
```

Expected: PASS.

- [ ] **Step 5: Commit the production wiring slice**

```powershell
git add backend/app/workers/dow_monitor_half_hour_ai.py tests/backend/test_dow_monitor_half_hour_ai.py tests/spec_contracts/test_dow_monitor_offline_ai_bootstrap_contract.py
git commit -m "feat: wire offline bootstrap into monitor AI worker"
```

---

## Task 6: Prove semantic behavior from raw offline evidence to saved AI result

**Files:**

- Create: `tests/backend/test_dow_monitor_offline_ai_bootstrap_integration.py`
- Modify: `docs/acceptance/dow-monitor-offline-ai-bootstrap.md`

- [ ] **Step 1: Write an integration test for the complete lower-to-higher-layer chain**

Use deterministic ClickHouse/repository fakes or the existing integration fixture to seed:

- a new monitored US symbol with `created_at=22:17`;
- raw offline quote/depth/trade/minute-candle/capital data through 22:00;
- no canonical minute results initially;
- no half-hour AI row initially.

Run one worker poll and assert:

```python
assert canonical_rows
assert max(row.observed_at for row in canonical_rows) <= beijing("22:00")
assert all(row.is_backfill for row in canonical_rows)

analysis = half_hour_repository.get("us", "RNG.US", trade_date, beijing("22:00"))
assert analysis.status == "completed"
assert analysis.data_cutoff == beijing("22:00")
assert model.calls == 1
assert no_analysis_exists_at("21:30")
```

Add a second scenario with insufficient raw evidence:

```python
assert analysis.status == "insufficient_data"
assert model.calls == 0
```

This test must validate the canonical minute rows themselves before asserting the higher-level AI row. A passing AI snapshot/golden alone is not semantic proof.

- [ ] **Step 2: Run the integration test**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_offline_ai_bootstrap_integration.py -q
```

Expected: PASS.

- [ ] **Step 3: Record executable acceptance evidence**

Update `docs/acceptance/dow-monitor-offline-ai-bootstrap.md` with:

- exact commands;
- test counts;
- evidence for selected 22:00 checkpoint and absent 21:30 analysis;
- maximum canonical event time;
- backfill markers;
- model-call count for sufficient and insufficient scenarios;
- proof that 3018/WebSocket contracts did not change.

Keep production observations pending until Task 8.

- [ ] **Step 4: Commit the semantic acceptance slice**

```powershell
git add tests/backend/test_dow_monitor_offline_ai_bootstrap_integration.py docs/acceptance/dow-monitor-offline-ai-bootstrap.md
git commit -m "test: prove offline bootstrap AI semantics"
```

---

## Task 7: Run full regression, update the runbook, and conduct an independent review

**Files:**

- Modify: `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md`
- Modify: `docs/acceptance/dow-monitor-offline-ai-bootstrap.md`
- Modify: `docs/reviews/2026-07-31-dow-monitor-offline-ai-bootstrap-review.md`

- [ ] **Step 1: Run backend and contract regression**

At minimum:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_half_hour_ai.py tests/backend/test_dow_monitor_offline_bootstrap.py tests/backend/test_dow_monitor_offline_ai_bootstrap_integration.py tests/backend/test_dow_monitor_minute_result_materializer.py tests/backend/test_dow_monitor_minute_result_history.py tests/backend/test_dow_monitor_minute_result_calculator.py tests/backend/test_dow_monitor_minute_result_source.py tests/backend/test_dow_monitor_minute_result_repository.py -q
.\backend\.venv\Scripts\python.exe -m pytest tests/spec_contracts/test_dow_monitor_offline_ai_bootstrap_contract.py tests/spec_contracts/test_spec_guard_contract.py -q
python scripts/check_spec_compliance.py
```

Then run the repository's complete backend suite:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend -q
```

Expected: PASS. Record any pre-existing unrelated failure separately; do not silently waive a new failure.

- [ ] **Step 2: Verify no frontend bundle change is required**

This change affects stored data availability, not the half-hour dialog contract. Confirm API schemas and frontend files are unchanged. If implementation unexpectedly changes an API payload or UI, stop and add a separate requirement/test slice rather than folding it into this backend change.

- [ ] **Step 3: Update the required Obsidian runbook**

Document:

- the independent worker bootstrap flow;
- raw ClickHouse sources and canonical destination table;
- 19912's role in stable 15m/30m structure;
- 3018/WebSocket isolation;
- latest-completed-checkpoint selection;
- 500-row and 15-second budgets;
- `busy`, `BACKFILL_TIMEOUT`, and `BACKFILL_BUDGET_EXCEEDED` operations;
- production inspection queries and rollback steps.

- [ ] **Step 4: Perform an independent requirements-to-evidence review**

Read the approved specification without relying on the implementation narrative, then inspect:

- every requirement-to-code mapping;
- every requirement-to-executable-test mapping;
- lower-layer canonical-row evidence;
- timeout/single-flight isolation;
- startup-window precedence;
- duplicate/idempotency behavior;
- WebSocket/3018 non-impact.

Set the review document to `status: passed` only if all mappings have concrete evidence. Otherwise list the gap and return to the relevant task.

- [ ] **Step 5: Commit verification documentation**

```powershell
git add docs/acceptance/dow-monitor-offline-ai-bootstrap.md docs/reviews/2026-07-31-dow-monitor-offline-ai-bootstrap-review.md
git commit -m "docs: verify offline monitor AI bootstrap"
```

The Obsidian vault is outside this Git repository. Save the runbook in place, do not attempt to stage it in this repository, and mention the absolute path in the handoff.

---

## Task 8: Publish to 10.28, verify production behavior, and push GitHub

**Files:**

- Modify: `docs/acceptance/dow-monitor-offline-ai-bootstrap.md`
- Modify: `docs/reviews/2026-07-31-dow-monitor-offline-ai-bootstrap-review.md`

- [ ] **Step 1: Capture the current production rollback point**

Before deployment, record:

- current host and Compose project;
- worker and 3018 image tags;
- container IDs, restart counts, and health;
- current Git SHA/build version;
- latest successful half-hour analysis;
- a rollback command that restores the exact old worker image.

Do not publish to localhost. The target is the established 10.28 production host and existing service names.

- [ ] **Step 2: Build a commit-addressed candidate**

Build from a clean committed SHA. Tag the image with that SHA. Inspect the built container/bundle metadata and confirm it matches the source commit.

This is a backend-worker change. Rebuild/restart only the services required by the actual Compose dependency graph; avoid restarting 3018 or WebSocket ingestion if the image/service split allows the worker to be replaced independently.

- [ ] **Step 3: Deploy the dedicated worker with bounded rollback**

Deploy the new worker image, then immediately check:

```powershell
docker ps --format "{{.Names}} {{.Image}} {{.Status}}"
docker inspect TickFlow_Dow_AI_Worker --format "{{.RestartCount}}"
docker logs --since 10m TickFlow_Dow_AI_Worker
```

Rollback immediately on crash loop, configuration failure, repeated ClickHouse errors, 19912 unreachability, or unexpected model fan-out.

- [ ] **Step 4: Run production semantic acceptance**

Use a controlled monitored stock added during an active regular session, or an equivalent disposable fixture that does not affect formal signals:

1. Record `created_at` and latest completed checkpoint.
2. Confirm raw offline WebSocket rows already exist through that checkpoint.
3. Confirm no canonical/AI logical row exists for the checkpoint.
4. Wait one worker poll, not 30 minutes.
5. Query canonical minute results and verify:
   - only the selected symbol/session/cutoff;
   - no event time after `data_cutoff`;
   - `is_backfill` is set;
   - generated rows are at most 500.
6. Query the half-hour analysis and verify:
   - latest completed checkpoint only;
   - no older startup analyses;
   - correct Beijing `window_end` and `data_cutoff`;
   - completed content when sufficient, explicit insufficient state otherwise.
7. Confirm the next ordinary checkpoint still runs.
8. Confirm worker model concurrency one.
9. Confirm 3018 and WebSocket containers show no restart, queue backlog, or latency regression.

- [ ] **Step 5: Close acceptance and independent review**

Append sanitized production queries/results, image SHA, timestamps, restart counts, and rollback evidence to the acceptance document. Re-run the independent requirements-to-evidence review after production evidence is present.

- [ ] **Step 6: Run final verification after documentation edits**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_half_hour_ai.py tests/backend/test_dow_monitor_offline_bootstrap.py tests/backend/test_dow_monitor_offline_ai_bootstrap_integration.py tests/spec_contracts/test_dow_monitor_offline_ai_bootstrap_contract.py -q
python scripts/check_spec_compliance.py
git status --short
git log -5 --oneline
```

Expected:

- all selected tests pass;
- spec compliance passes;
- only intentionally untracked tool artifacts remain;
- acceptance and review statuses are final.

- [ ] **Step 7: Commit, push, and update the existing draft PR**

```powershell
git add docs/acceptance/dow-monitor-offline-ai-bootstrap.md docs/reviews/2026-07-31-dow-monitor-offline-ai-bootstrap-review.md
git commit -m "docs: record production offline AI bootstrap acceptance"
git push origin codex/monitor-list-websocket
```

Update the existing draft PR with:

- both requirement IDs;
- the latest-checkpoint-only precedence;
- test/acceptance commands;
- 10.28 deployed image SHA;
- rollback image;
- explicit statement that real-time interpretation and formal signals are unchanged.

---

## Completion criteria

The work is complete only when all of the following are true:

- A newly added in-session stock can produce the latest completed half-hour AI analysis on the next worker poll from persisted offline data.
- No earlier startup checkpoint is analyzed.
- Canonical minute results are semantically validated before the AI result.
- Insufficient, timeout, budget, and busy states follow the approved behavior.
- The model is never called on insufficient evidence.
- WebSocket/3018 paths have no bootstrap dependency and show no production regression.
- The next normal half-hour checkpoint still runs.
- Both stable requirements have implementation, executable-test, acceptance, and independent-review evidence.
- The Obsidian runbook reflects the deployed architecture and operating procedure.
- The 10.28 deployment, GitHub push, and existing draft PR update are complete.
