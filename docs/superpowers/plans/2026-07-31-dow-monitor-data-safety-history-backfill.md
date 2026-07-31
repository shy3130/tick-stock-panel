# Dow Monitor Data Safety and History Backfill Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Apply `spec-guard` before every production edit and stop if repository specifications conflict.

**Goal:** Eliminate invalid Dow-engine bar payloads and automatically warm newly monitored symbols with missing minute and daily structure data without creating another Longbridge quote connection or blocking the WebSocket path.

**Architecture:** The existing Longbridge subscription runner detects newly subscribed Dow-monitor symbols and places them in a bounded in-process warmup queue serviced by one dedicated background worker that reuses the runner's single `QuoteContext`. The subscription loop and WebSocket callbacks only perform constant-time enqueue/status work. Missing historical rows are written asynchronously at lower source priority, while TickFlow reads the shared status snapshot and rebuilds its normal multi-timeframe state. Dow-engine requests pass through a finite-OHLCV sanitizer before network I/O.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, Polars, Longbridge OpenAPI SDK, ClickHouse, pytest, systemd, Docker Compose.

## Global Constraints

- Requirements: `REQ-DOW-MONITOR-DAILY-PAYLOAD-SAFETY-001` and `REQ-DOW-MONITOR-NEW-SYMBOL-HISTORY-BACKFILL-001`.
- `POST /api/dow-monitor/symbols` must return without waiting for history.
- The production collector must retain exactly one Longbridge `QuoteContext`.
- Historical work must never execute in a WebSocket callback.
- Historical SDK calls and ClickHouse writes must never block the subscription/control loop; one bounded background worker owns them.
- Existing WebSocket candlesticks have higher read priority than backfilled minute rows and must not be overwritten.
- Missing/non-finite data stays missing; never replace it with zero.
- Lower-layer ClickHouse row semantics must pass before multi-timeframe or UI acceptance.
- Production target is `192.168.10.28`; TickFlow remains on `3018`, the Dow engine remains on `19912`.
- Update `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md` in the implementation task.

---

## File Structure

### `longbridge-stock` repository

- Create `docs/specs/dow-monitor-history-warmup-provider.md`: authoritative provider requirement.
- Create `src/longbridge_stock/monitor_history_warmup.py`: bounded queue, shared-context fetcher, ClickHouse gap repository, status snapshot.
- Create `tests/test_monitor_history_warmup.py`: provider-level behavior and semantic data tests.
- Modify `scripts/run_longbridge_quote_subscription.py`: enqueue only newly subscribed Dow-monitor symbols and process one bounded task outside callbacks.
- Modify `tests/test_run_longbridge_quote_subscription.py`: prove one context, non-blocking callbacks, deduplication, and status metrics.
- Modify `docs/spec-index.yaml` and `docs/traceability.yaml`: provider traceability.
- Create `docs/acceptance/dow-monitor-history-warmup-provider.md`.
- Create `docs/reviews/dow-monitor-history-warmup-provider.md`.

### `tickflow-monitor-list-v2` repository

- Create `docs/specs/dow-monitor-data-safety-history-backfill.md`: authoritative consumer specification.
- Create `backend/app/services/dow_monitor_bar_safety.py`: pure bar normalization and validation.
- Create `backend/app/services/dow_monitor_history_status.py`: read-only parser for the collector status file.
- Modify `backend/app/services/dow_monitor_client.py`: call the sanitizer before HTTP.
- Modify `backend/app/services/dow_monitor_service.py`: isolate insufficient timeframe history and expose history status.
- Modify `backend/app/main.py`: inject the history-status reader.
- Modify `backend/app/services/dow_monitor_models.py`: response models for backfill state.
- Modify `backend/tests/test_dow_monitor_api.py`: API, lifecycle, RNG payload, and immediate-add tests.
- Create `backend/tests/test_dow_monitor_bar_safety.py`.
- Create `backend/tests/test_dow_monitor_history_status.py`.
- Modify `frontend/src/components/dow-monitor/types.ts`: backfill response type only; rendering belongs to the mobile plan.
- Modify `docs/spec-index.yaml` and `docs/traceability.yaml`.
- Create `docs/acceptance/dow-monitor-data-safety-history-backfill.md`.
- Create `docs/reviews/dow-monitor-data-safety-history-backfill.md`.

## Interfaces

```python
# longbridge-stock/src/longbridge_stock/monitor_history_warmup.py
class MonitorHistoryWarmupService:
    def start(self) -> None: ...
    def stop(self, timeout_seconds: float = 10) -> None: ...
    def enqueue(self, symbol: str) -> bool: ...
    def snapshot(self) -> dict[str, object]: ...

class MonitorHistoryRepository:
    def existing_minute_points(
        self, symbol: str, start: datetime, end: datetime
    ) -> set[datetime]: ...
    def existing_daily_dates(
        self, symbol: str, start: date, end: date
    ) -> set[date]: ...
    def write_minute_rows(self, rows: list[dict[str, object]]) -> int: ...
    def write_daily_rows(self, rows: list[dict[str, object]]) -> int: ...
```

```python
# tickflow/backend/app/services/dow_monitor_bar_safety.py
class InsufficientDowBars(ValueError):
    timeframe: str
    valid_bars: int
    required_bars: int

def sanitize_engine_bars(
    timeframe: str,
    bars: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]: ...
```

```python
# tickflow/backend/app/services/dow_monitor_history_status.py
class DowMonitorHistoryStatusReader:
    def for_symbols(self, symbols: Sequence[str]) -> dict[str, HistoryBackfillStatus]: ...
```

### Task 1: Register Narrow Authoritative Specifications

**Files:**
- Create: `[longbridge-stock] docs/specs/dow-monitor-history-warmup-provider.md`
- Modify: `[longbridge-stock] docs/spec-index.yaml`
- Create: `[tickflow] docs/specs/dow-monitor-data-safety-history-backfill.md`
- Modify: `[tickflow] docs/spec-index.yaml`

**Interfaces:**
- Consumes: approved design `docs/superpowers/specs/2026-07-31-dow-monitor-backfill-mobile-half-hour-ai-design.md`.
- Produces: two local authoritative specifications with repository-owned requirement IDs.

- [ ] **Step 1: Add the provider specification**

Use the provider ID `SPEC-DOW-MONITOR-HISTORY-WARMUP-PROVIDER-001` and local requirement `REQ-LONGBRIDGE-DOW-MONITOR-HISTORY-WARMUP-PROVIDER-001`. State that the collector:

```markdown
- MUST reuse the runner's existing QuoteContext.
- MUST enqueue only enabled monitor symbols newly added to the subscription set.
- MUST query existing minute/daily coverage before writing.
- MUST publish per-symbol status to the shared status directory.
- MUST keep WebSocket callbacks limited to enqueue/publish work already present.
```

- [ ] **Step 2: Add the TickFlow consumer specification**

Use `SPEC-DOW-MONITOR-DATA-SAFETY-HISTORY-BACKFILL-001` with:

```yaml
requirements:
  - REQ-DOW-MONITOR-DAILY-PAYLOAD-SAFETY-001
  - REQ-DOW-MONITOR-NEW-SYMBOL-HISTORY-BACKFILL-001
```

Record the provider requirement as a lower-layer dependency, not as TickFlow implementation evidence.

- [ ] **Step 3: Run authority checks**

Run in each repository:

```powershell
python scripts/check_spec_compliance.py
```

Expected: the only allowed temporary failures are missing traceability entries for the newly registered requirements; there must be no unresolved conflict or duplicate ID.

- [ ] **Step 4: Commit specifications in each repository**

```powershell
git add docs/specs/dow-monitor-history-warmup-provider.md docs/spec-index.yaml
git commit -m "docs: specify monitor history warmup provider"
```

```powershell
git add docs/specs/dow-monitor-data-safety-history-backfill.md docs/spec-index.yaml
git commit -m "docs: specify monitor data safety and backfill"
```

### Task 2: Reject Invalid Dow-Engine Bars Before HTTP

**Files:**
- Create: `[tickflow] backend/app/services/dow_monitor_bar_safety.py`
- Create: `[tickflow] backend/tests/test_dow_monitor_bar_safety.py`
- Modify: `[tickflow] backend/app/services/dow_monitor_client.py`
- Modify: `[tickflow] backend/tests/test_dow_monitor_api.py`

**Interfaces:**
- Consumes: raw `Sequence[Mapping[str, object]]` from `DowMonitorService._merge_evaluation_bars`.
- Produces: `sanitize_engine_bars(timeframe, bars)` and `InsufficientDowBars`.

- [ ] **Step 1: Write failing finite-value tests**

```python
def test_sanitize_engine_bars_drops_nonfinite_and_invalid_ohlc() -> None:
    bars = [
        {"timestamp": "2026-07-28", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100},
        {"timestamp": "2026-07-29", "open": float("nan"), "high": 11, "low": 9, "close": 10, "volume": 100},
        {"timestamp": "2026-07-30", "open": 10, "high": 9, "low": 11, "close": 10, "volume": 100},
        {"timestamp": "2026-07-31", "open": 11, "high": 12, "low": 10, "close": 11.5, "volume": 120},
    ]

    assert sanitize_engine_bars("day", bars) == [
        {"timestamp": "2026-07-28", "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 100.0},
        {"timestamp": "2026-07-31", "open": 11.0, "high": 12.0, "low": 10.0, "close": 11.5, "volume": 120.0},
    ]
```

Add tests for duplicate timestamps, negative volume, zero/negative price, and fewer than two valid bars raising:

```python
InsufficientDowBars(timeframe="day", valid_bars=1, required_bars=2)
```

- [ ] **Step 2: Run the tests and observe failure**

Run:

```powershell
cd backend
uv run pytest tests/test_dow_monitor_bar_safety.py -v
```

Expected: FAIL because the module/function does not exist.

- [ ] **Step 3: Implement the pure sanitizer**

Use this exact validation order:

```python
MIN_ENGINE_BARS = {"5m": 2, "15m": 2, "30m": 2, "60m": 2, "day": 2}

def _finite_number(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None

def sanitize_engine_bars(timeframe, bars):
    by_timestamp: dict[str, dict[str, object]] = {}
    for raw in bars:
        timestamp = str(raw.get("timestamp") or "").strip()
        values = {name: _finite_number(raw.get(name)) for name in ("open", "high", "low", "close", "volume")}
        if not timestamp or any(value is None for value in values.values()):
            continue
        if min(values["open"], values["high"], values["low"], values["close"]) <= 0:
            continue
        if values["volume"] < 0 or values["low"] > min(values["open"], values["close"]) or values["high"] < max(values["open"], values["close"]):
            continue
        by_timestamp[timestamp] = {"timestamp": timestamp, **values}
    result = [by_timestamp[key] for key in sorted(by_timestamp)]
    required = MIN_ENGINE_BARS[timeframe]
    if len(result) < required:
        raise InsufficientDowBars(timeframe, len(result), required)
    return result
```

- [ ] **Step 4: Add the HTTP boundary test**

Use `httpx.MockTransport` and capture the request body:

```python
def test_longbridge_client_never_serializes_nan_to_engine() -> None:
    captured = {}
    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_engine_payload())

    client = LongbridgeDowClient("http://engine", transport=httpx.MockTransport(handler))
    client.evaluate("RNG.US", "day", rng_rows_with_nan(), "FINAL", aware_now())
    assert all(math.isfinite(bar[field]) for bar in captured["bars"] for field in ("open", "high", "low", "close", "volume"))
```

- [ ] **Step 5: Call the sanitizer in `LongbridgeDowClient.evaluate`**

```python
safe_bars = sanitize_engine_bars(timeframe, bars)
payload = {
    "symbol": symbol,
    "timeframe": timeframe,
    "completion": completion,
    "asOf": as_of.isoformat(),
    "bars": safe_bars,
}
```

Do not wrap `InsufficientDowBars` as `DowEngineUnavailable`; callers need the semantic distinction.

- [ ] **Step 6: Isolate insufficient history per timeframe**

In `DowMonitorService._evaluate_symbol`, catch `InsufficientDowBars` before the generic exception and append:

```python
f"{timeframe}: HISTORY_INCOMPLETE:VALID_BARS_{exc.valid_bars}_OF_{exc.required_bars}"
```

Mark only that timeframe `ANALYSIS_PAUSED`. Continue evaluating remaining timeframes.

- [ ] **Step 7: Run focused and regression tests**

```powershell
cd backend
uv run pytest tests/test_dow_monitor_bar_safety.py tests/test_dow_monitor_api.py -v
```

Expected: PASS; the captured body contains no non-standard JSON number and a day failure does not suppress 5m/15m/30m/60m.

- [ ] **Step 8: Commit**

```powershell
git add backend/app/services/dow_monitor_bar_safety.py backend/app/services/dow_monitor_client.py backend/app/services/dow_monitor_service.py backend/tests/test_dow_monitor_bar_safety.py backend/tests/test_dow_monitor_api.py
git commit -m "fix: sanitize dow monitor engine bars"
```

### Task 3: Build the Shared-Context Warmup Queue

**Files:**
- Create: `[longbridge-stock] src/longbridge_stock/monitor_history_warmup.py`
- Create: `[longbridge-stock] tests/test_monitor_history_warmup.py`
- Modify: `[longbridge-stock] src/longbridge_stock/clickhouse_realtime.py`

**Interfaces:**
- Consumes: the already-created runner `QuoteContext`, `RealtimeSinkRouter`, newly monitored symbols.
- Produces: `MonitorHistoryWarmupService.start`, `enqueue`, `stop`, and `snapshot`.

- [ ] **Step 1: Write failing queue tests**

```python
def test_queue_is_bounded_and_deduplicated(tmp_path):
    service = warmup_service(tmp_path, max_queue=2)
    assert service.enqueue("RNG.US") is True
    assert service.enqueue("RNG.US") is False
    assert service.enqueue("GTLB.US") is True
    assert service.enqueue("NP.US") is False
    assert service.snapshot()["queue_depth"] == 2
```

Also assert:

- the background worker uses the exact injected `ctx` object;
- only missing minute timestamps and daily dates are written;
- current and previous regular sessions are requested;
- retries leave a persisted `failed` status;
- successful re-enqueue with complete coverage performs no writes;
- status JSON uses atomic temporary-file replacement.
- `enqueue()` returns promptly while a fake SDK history request and ClickHouse write are deliberately blocked;
- `stop()` drains or times out without creating another `QuoteContext`.

- [ ] **Step 2: Run and observe failure**

```powershell
uv run pytest tests/test_monitor_history_warmup.py -v
```

Expected: FAIL because `monitor_history_warmup` does not exist.

- [ ] **Step 3: Implement the queue state types**

```python
@dataclass(frozen=True)
class WarmupStatus:
    symbol: str
    state: Literal["queued", "running", "completed", "partial", "failed"]
    progress: int
    missing_timeframes: tuple[str, ...]
    last_error: str | None
    updated_at: datetime

class MonitorHistoryWarmupService:
    def __init__(self, *, ctx, sdk, repository, status_path, max_queue=64, now_fn=_now):
        self._ctx = ctx
        self._sdk = sdk
        self._repository = repository
        self._queue: queue.Queue[str] = queue.Queue(maxsize=max_queue)
        self._queued: set[str] = set()
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
```

`start()` creates exactly one daemon worker thread. That thread blocks on the bounded queue and performs `_process_symbol`; `enqueue()` only normalizes/deduplicates and calls `put_nowait`. SDK requests, gap queries, JSONEachRow writes, retry waits, and status-file writes all remain on the worker thread.

- [ ] **Step 4: Implement exact coverage queries**

Add public helpers to `clickhouse_realtime.py`:

```python
def fetch_monitor_minute_points(symbol: str, start: datetime, end: datetime) -> set[datetime]:
    # UNION completed keys from lb_realtime_candlesticks FINAL and lb_intraday_lines.

def fetch_monitor_daily_dates(symbol: str, start: date, end: date) -> set[date]:
    # SELECT trade_date FROM lb_daily_bars FINAL for adjusted=1.
```

The minute query must normalize US rows in `America/New_York` and CN/HK rows in their exchange timezone before comparing session membership.

- [ ] **Step 5: Fetch through the injected QuoteContext**

For minute rows:

```python
rows = self._ctx.history_candlesticks_by_date(
    symbol,
    self._sdk.Period.Min_1,
    self._sdk.AdjustType.NoAdjust,
    start_date,
    end_date,
)
```

For daily rows use the same context:

```python
rows = self._ctx.candlesticks(
    symbol,
    self._sdk.Period.Day,
    120,
    self._sdk.AdjustType.ForwardAdjust,
)
```

Normalize finite OHLCV, retain only required dates/timestamps, and calculate missing keys before any write.

- [ ] **Step 6: Write lower-priority history**

Minute rows go to `lb_intraday_lines` through the injected realtime sink router with:

```python
payload={
    "backfill_source": "monitor_shared_quote_context",
    "trade_date": exchange_date.isoformat(),
    "open": open_price,
    "high": high_price,
    "low": low_price,
    "close": close_price,
}
```

Daily rows go to `lb_daily_bars` with `adjusted=1` and `payload.source="monitor_shared_quote_context"`. Add a public ClickHouse JSONEachRow writer rather than importing a script helper.

- [ ] **Step 7: Persist status atomically**

Write:

```json
{
  "updated_at": "2026-07-31T10:02:00+08:00",
  "queue_depth": 0,
  "symbols": {
    "RNG.US": {
      "state": "completed",
      "progress": 100,
      "missing_timeframes": [],
      "last_error": null
    }
  }
}
```

Default path:

```text
/data/longbridge/status/monitor-history-warmup.json
```

- [ ] **Step 8: Run tests**

```powershell
uv run pytest tests/test_monitor_history_warmup.py tests/test_clickhouse_realtime.py -v
```

Expected: PASS, including exact missing-point assertions.

- [ ] **Step 9: Commit**

```powershell
git add src/longbridge_stock/monitor_history_warmup.py src/longbridge_stock/clickhouse_realtime.py tests/test_monitor_history_warmup.py tests/test_clickhouse_realtime.py
git commit -m "feat: add shared-context monitor history warmup"
```

### Task 4: Integrate Warmup Without Blocking WebSocket Callbacks or the Subscription Loop

**Files:**
- Modify: `[longbridge-stock] scripts/run_longbridge_quote_subscription.py`
- Modify: `[longbridge-stock] tests/test_run_longbridge_quote_subscription.py`
- Modify: `[longbridge-stock] scripts/systemd/longbridge-quote-subscription.service`

**Interfaces:**
- Consumes: `MonitorHistoryWarmupService`.
- Produces: automatic enqueue on newly subscribed Dow-monitor symbols and one lifecycle-managed low-priority worker.

- [ ] **Step 1: Write failing integration tests**

```python
def test_new_monitor_symbol_is_enqueued_after_subscription(monkeypatch):
    runner = _runner()
    runner.last_monitor_symbols = ["RNG.US"]
    runner.history_warmup = FakeWarmup()
    runner._sync_subscriptions(["RNG.US"], first_push=False)
    assert runner.history_warmup.enqueued == ["RNG.US"]

def test_non_monitor_candidate_is_not_enqueued(monkeypatch):
    runner = _runner()
    runner.last_monitor_symbols = []
    runner.history_warmup = FakeWarmup()
    runner._sync_subscriptions(["RNG.US"], first_push=False)
    assert runner.history_warmup.enqueued == []
```

Add a construction test that patches `sdk.QuoteContext` and asserts it is called once even when warmup is enabled.
Add a slow-history test that blocks the fake SDK call and proves `_sync_subscriptions` and heartbeat publication still return immediately.

- [ ] **Step 2: Run and observe failure**

```powershell
uv run pytest tests/test_run_longbridge_quote_subscription.py -v
```

- [ ] **Step 3: Construct warmup with `self.ctx`**

```python
self.history_warmup = MonitorHistoryWarmupService(
    ctx=self.ctx,
    sdk=self.sdk,
    repository=MonitorHistoryRepository(router=self.store.realtime_sinks),
    status_path=Path(args.monitor_history_status_file),
    max_queue=args.monitor_history_queue_capacity,
)
```

No class in this path may construct `QuoteContext`.

Call `self.history_warmup.start()` after the runner is initialized, and call `stop()` from the existing shutdown/finally path.

- [ ] **Step 4: Enqueue only monitor additions**

After successful subscriptions:

```python
monitor_symbols = set(self.last_monitor_symbols)
for symbol in subscribed_added:
    if symbol in monitor_symbols:
        self.history_warmup.enqueue(symbol)
```

- [ ] **Step 5: Keep all synchronous loops and callbacks free of history I/O**

The runner loop must not call the SDK history methods or ClickHouse warmup writes. It may read `history_warmup.snapshot()` for heartbeat metadata. WebSocket `_on_quote`, `_on_depth`, `_on_trades`, `_on_brokers`, and `_on_candlestick` remain unchanged.

- [ ] **Step 6: Add bounded configuration**

```text
--monitor-history-queue-capacity (default 64)
--monitor-history-retry-base-seconds (default 2)
--monitor-history-status-file (default /data/longbridge/status/monitor-history-warmup.json)
```

Expose queue depth and last result in the existing heartbeat `runtime`.

- [ ] **Step 7: Run regression tests**

```powershell
uv run pytest tests/test_run_longbridge_quote_subscription.py tests/test_realtime_batch_writer.py tests/test_realtime_ui_publisher.py -v
```

Expected: all callbacks still enqueue durable events immediately; warmup uses the single context.

- [ ] **Step 8: Commit**

```powershell
git add scripts/run_longbridge_quote_subscription.py scripts/systemd/longbridge-quote-subscription.service tests/test_run_longbridge_quote_subscription.py
git commit -m "feat: warm new monitor symbols in quote collector"
```

### Task 5: Expose Backfill Progress in TickFlow

**Files:**
- Create: `[tickflow] backend/app/services/dow_monitor_history_status.py`
- Create: `[tickflow] backend/tests/test_dow_monitor_history_status.py`
- Modify: `[tickflow] backend/app/services/dow_monitor_models.py`
- Modify: `[tickflow] backend/app/services/dow_monitor_service.py`
- Modify: `[tickflow] backend/app/main.py`
- Modify: `[tickflow] backend/tests/test_dow_monitor_api.py`
- Modify: `[tickflow] frontend/src/components/dow-monitor/types.ts`

**Interfaces:**
- Consumes: read-only `/run/longbridge/monitor-history-warmup.json`.
- Produces: `history_backfill` on each overview symbol.

- [ ] **Step 1: Write failing parser tests**

Test valid, missing, stale, malformed, and symbol-alias cases:

```python
def test_status_reader_maps_hk_alias_and_never_raises(tmp_path):
    write_status(tmp_path, {"1347.HK": {"state": "running", "progress": 45}})
    reader = DowMonitorHistoryStatusReader(tmp_path / "monitor-history-warmup.json")
    assert reader.for_symbols(["01347.HK"])["01347.HK"].progress == 45
```

Malformed/missing files return `pending` or `unknown`, never crash `overview`.

- [ ] **Step 2: Run and observe failure**

```powershell
cd backend
uv run pytest tests/test_dow_monitor_history_status.py -v
```

- [ ] **Step 3: Implement typed models**

```python
class HistoryBackfillStatus(BaseModel):
    status: Literal["pending", "queued", "running", "rebuilding", "completed", "partial", "failed", "unknown"]
    progress: int = Field(ge=0, le=100)
    missing_timeframes: tuple[str, ...] = ()
    last_error: str | None = None
    updated_at: datetime | None = None
```

- [ ] **Step 4: Inject the reader**

Extend `DowMonitorService.__init__` with `history_status_reader=None`; in `overview`, batch-read once for all symbols and add:

```python
"history_backfill": history_statuses.get(item.symbol).model_dump(mode="json")
```

Do not perform one file read per symbol.

- [ ] **Step 5: Prove symbol addition is immediate**

Add an API test where the gateway raises if called:

```python
def test_add_symbol_returns_without_history_io(tmp_path):
    service = _service(tmp_path)
    service._data_gateway = _ExplodesOnAccess()
    response = _client(service).post("/api/dow-monitor/symbols", json={"symbol": "RNG.US", "enabled": True})
    assert response.status_code == 200
```

- [ ] **Step 6: Update TypeScript contracts**

```typescript
export interface DowMonitorHistoryBackfillStatus {
  status: 'pending' | 'queued' | 'running' | 'rebuilding' | 'completed' | 'partial' | 'failed' | 'unknown'
  progress: number
  missing_timeframes: string[]
  last_error: string | null
  updated_at: string | null
}
```

Add `history_backfill?: DowMonitorHistoryBackfillStatus` to `DowMonitorOverviewSymbol`.

- [ ] **Step 7: Run tests**

```powershell
cd backend
uv run pytest tests/test_dow_monitor_history_status.py tests/test_dow_monitor_api.py -v
cd ..\frontend
pnpm test -- --run src/components/dow-monitor/DowMonitorList.test.tsx src/components/dow-monitor/useDowMonitor.test.tsx
```

- [ ] **Step 8: Commit**

```powershell
git add backend/app/services/dow_monitor_history_status.py backend/app/services/dow_monitor_models.py backend/app/services/dow_monitor_service.py backend/app/main.py backend/tests/test_dow_monitor_history_status.py backend/tests/test_dow_monitor_api.py frontend/src/components/dow-monitor/types.ts
git commit -m "feat: expose monitor history warmup status"
```

### Task 6: Add Traceability and Lower-Layer Acceptance

**Files:**
- Modify: both repositories' `docs/traceability.yaml`
- Create: `[longbridge-stock] docs/acceptance/dow-monitor-history-warmup-provider.md`
- Create: `[longbridge-stock] docs/reviews/dow-monitor-history-warmup-provider.md`
- Create: `[tickflow] docs/acceptance/dow-monitor-data-safety-history-backfill.md`
- Create: `[tickflow] docs/reviews/dow-monitor-data-safety-history-backfill.md`
- Modify: `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md`

**Interfaces:**
- Consumes: passing provider and consumer tests plus real ClickHouse sample queries.
- Produces: complete requirement-to-evidence records.

- [ ] **Step 1: Run semantic ClickHouse acceptance on a non-production fixture**

Query the chosen symbol/date from all three sources:

```sql
SELECT symbol, bar_time, open, high, low, close, source
FROM longbridge.lb_realtime_candlesticks FINAL
WHERE symbol = 'RNG.US'
ORDER BY bar_time;

SELECT symbol, line_time, price, payload
FROM longbridge.lb_intraday_lines
WHERE symbol = 'RNG.US'
ORDER BY line_time;

SELECT symbol, trade_date, open, high, low, close, adjusted, payload
FROM longbridge.lb_daily_bars FINAL
WHERE symbol = 'RNG.US'
ORDER BY trade_date DESC
LIMIT 120;
```

Record row counts, timezones, regular-session membership, finite OHLC, and source priority. Do not cite the UI as evidence.

- [ ] **Step 2: Record traceability**

Map each requirement to exact implementation, executable tests, semantic acceptance, and independent review. The provider requirement must point only to `longbridge-stock` paths; TickFlow requirements must point only to TickFlow paths.

- [ ] **Step 3: Update the runbook**

Document:

- the single-`QuoteContext` invariant;
- queue defaults and status path;
- ClickHouse source priority;
- RNG payload inspection;
- 3018/19912 responsibilities;
- deployment and rollback commands.

- [ ] **Step 4: Run both compliance checkers**

```powershell
python E:\my_project\longbridge-stock\scripts\check_spec_compliance.py
python E:\my_project\.worktrees\tickflow-monitor-list-v2\scripts\check_spec_compliance.py
```

Expected: PASS.

- [ ] **Step 5: Commit evidence separately in each repository**

```powershell
git add docs/traceability.yaml docs/acceptance/dow-monitor-history-warmup-provider.md docs/reviews/dow-monitor-history-warmup-provider.md
git commit -m "docs: accept monitor history warmup provider"
```

```powershell
git add docs/traceability.yaml docs/acceptance/dow-monitor-data-safety-history-backfill.md docs/reviews/dow-monitor-data-safety-history-backfill.md
git commit -m "docs: accept monitor data safety and backfill"
```

### Task 7: Candidate Deployment and Production Verification

**Files:**
- Modify only if required by verified runtime: `[longbridge-stock] scripts/systemd/longbridge-quote-subscription.service`
- Modify only if required by verified runtime: `[tickflow] docker-compose.yml`
- Update: both acceptance documents and the Obsidian runbook with production evidence.

**Interfaces:**
- Consumes: committed provider and consumer implementations.
- Produces: production collector and TickFlow candidate with rollback evidence.

- [ ] **Step 1: Capture production baseline**

On `192.168.10.28`, record:

```text
collector service status and PID
TickFlow image ID and restart count
3018 and 19912 health
dow_monitor_symbols.json SHA-256
RNG.US current status error
ClickHouse latest timestamps for RNG.US
```

- [ ] **Step 2: Deploy the collector candidate**

Install code/unit changes without stopping ClickHouse or Redis. Restart only the quote-subscription service and verify:

```text
one collector PID
one QuoteContext construction in startup log/characterization evidence
WebSocket event counters continue increasing
history queue visible in heartbeat/status JSON
```

- [ ] **Step 3: Deploy TickFlow to an isolated candidate port**

Build a unique image tag and run on `13018` with the same read-only status mount. Verify `/health`, `/api/dow-monitor/status`, overview contracts, and no mutation of the monitored-symbol file.

- [ ] **Step 4: Exercise a new symbol**

Add one agreed test symbol through the candidate:

```text
POST returns immediately
collector sees it on the next symbol-feed reload
status moves queued -> running -> completed/partial
WebSocket quote/candlestick counters continue during warmup
ClickHouse gains only missing history rows
5m/15m/30m/60m/day states rebuild
```

- [ ] **Step 5: Verify RNG.US**

Capture the sanitized outbound day payload or deterministic replay, then assert:

```text
no NaN/Infinity
no HTTP 400
insufficient history is explicit if fewer than two valid bars remain
other timeframes still update
```

- [ ] **Step 6: Promote to 3018**

Back up the current image and symbol file, set the unique `TICKFLOW_IMAGE`, and run:

```powershell
ssh alwin@192.168.10.28 "cd /home/alwin/apps/tickflow-stock-panel && TICKFLOW_IMAGE=<candidate> docker compose up -d --no-build"
```

- [ ] **Step 7: Monitor and record acceptance**

For at least ten minutes verify:

- container restart count stays zero;
- a single process listens on 3018;
- 19912 remains independently healthy;
- WebSocket event and ClickHouse timestamps advance;
- no new 400, traceback, or queue overflow appears;
- the symbol-file hash is unchanged except for the intentionally added test symbol.

- [ ] **Step 8: Complete independent review**

Review from each authoritative requirement to code, test, lower-layer evidence, and production evidence. Explicitly reject any argument that “the overview looks correct” proves the underlying bars are correct.
