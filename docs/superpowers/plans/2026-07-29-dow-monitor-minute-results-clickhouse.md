# Trend Monitor Minute Results ClickHouse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist one causally correct trend-monitor result per enabled symbol and completed minute in ClickHouse, permanently retain it, and backfill each market's current trading day.

**Architecture:** Add a pure minute-result calculator, a ClickHouse repository, a batched causal raw-data source, and a gap-driven materializer. The existing 3018 monitor loop invokes the materializer after its normal work; materialization failures are isolated from formal signals and are retried from ClickHouse gaps.

**Tech Stack:** Python 3.11, FastAPI lifespan, Pydantic, Polars, ClickHouse HTTP `JSONEachRow`, pytest, Docker Compose.

## Global Constraints

- Table name is exactly `longbridge.lb_dow_monitor_minute_results`.
- Logical key is `(market, symbol, decision_minute)`.
- Engine is `ReplacingMergeTree(updated_at)`, monthly partitioned, with no TTL.
- Results are permanent; automatic backfill covers only each market's local current trading day.
- A result requires a completed `min_1` bar and may use only data visible at or before `decision_minute`.
- Missing values remain `NULL`; `missing_fields` records them. Never backfill from a current value or zero.
- Percentage columns store percentage points: `1.25` means `1.25%`.
- `volume_ratio` and `volume_speed` store multiples: `1.25` means `1.25×`.
- Realtime inputs may explain a formal signal but may not create, clear, reverse, or upgrade it.
- A ClickHouse read/write failure must not interrupt monitor states, notifications, or `/ws/realtime`.
- Queries are batched by symbol set and time range; no per-symbol-per-minute ClickHouse query loops.
- Production changes require TDD RED evidence, stable requirement IDs, traceability, semantic acceptance, independent review, and an updated Obsidian runbook.

---

## File Structure

**Create**

- `docs/specs/dow-monitor-minute-results-clickhouse.md` — authoritative MUST requirements.
- `tests/spec_contracts/test_dow_monitor_minute_results_clickhouse_contract.py` — spec/index/traceability contract.
- `backend/app/services/dow_monitor_minute_result_models.py` — Pydantic result row and raw slice types.
- `backend/app/services/dow_monitor_minute_result_calculator.py` — pure 14-indicator and quality calculation.
- `backend/app/services/dow_monitor_minute_result_source.py` — batched ClickHouse raw-history reads.
- `backend/app/services/dow_monitor_minute_result_repository.py` — ClickHouse result DDL, existing-key reads, and inserts.
- `backend/app/services/dow_monitor_minute_result_history.py` — causal as-of joins, completed-minute selection, and cached stable-timeframe replay.
- `backend/app/services/dow_monitor_minute_result_materializer.py` — market-day gap planning and orchestration.
- `tests/backend/test_dow_monitor_minute_result_calculator.py` — formula, unit, causality, and missing-data tests.
- `tests/backend/test_dow_monitor_minute_result_source.py` — batched raw-query and source-shape tests.
- `tests/backend/test_dow_monitor_minute_result_repository.py` — SQL/DDL/serialization tests.
- `tests/backend/test_dow_monitor_minute_result_history.py` — as-of causality, market-clock, staleness, and replay-cache tests.
- `tests/backend/test_dow_monitor_minute_result_materializer.py` — backfill, idempotency, and isolation tests.
- `tests/backend/test_dow_monitor_minute_result_integration.py` — lifecycle and fail-open integration tests.
- `docs/acceptance/dow-monitor-minute-results-clickhouse.md` — production semantic acceptance.
- `docs/reviews/dow-monitor-minute-results-clickhouse.md` — independent requirement-to-evidence review.

**Modify**

- `docs/spec-index.yaml` — register the authoritative specification and three requirements.
- `docs/traceability.yaml` — map requirements to implementation, executable tests, acceptance, and review.
- `backend/app/services/dow_monitor_service.py` — invoke an optional materializer after the normal cycle and expose status.
- `backend/app/main.py` — build the repository/materializer and pass it to the service.
- `backend/tests/test_dow_monitor_api.py` — retain existing monitor API regression coverage.
- `tests/spec_contracts/test_dow_monitor_list_websocket_contract.py` — retain existing signal-boundary coverage; do not move minute-result tests here.
- `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md` — table, backfill, queries, deployment, and rollback.

---

### Task 1: Register authoritative requirements and contract

**Files:**

- Create: `docs/specs/dow-monitor-minute-results-clickhouse.md`
- Create: `tests/spec_contracts/test_dow_monitor_minute_results_clickhouse_contract.py`
- Modify: `docs/spec-index.yaml`
- Modify: `docs/traceability.yaml`
- Create: `docs/acceptance/dow-monitor-minute-results-clickhouse.md`
- Create: `docs/reviews/dow-monitor-minute-results-clickhouse.md`

**Interfaces:**

- Consumes: approved design `docs/superpowers/specs/2026-07-29-dow-monitor-minute-results-clickhouse-design.md`.
- Produces: stable requirements `REQ-DOW-MONITOR-MINUTE-RESULTS-SCHEMA-001`, `REQ-DOW-MONITOR-MINUTE-RESULTS-MATERIALIZATION-001`, and `REQ-DOW-MONITOR-MINUTE-RESULTS-BACKFILL-001`.

- [ ] **Step 1: Write the failing contract test**

```python
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "docs/specs/dow-monitor-minute-results-clickhouse.md"
REQUIREMENTS = {
    "REQ-DOW-MONITOR-MINUTE-RESULTS-SCHEMA-001",
    "REQ-DOW-MONITOR-MINUTE-RESULTS-MATERIALIZATION-001",
    "REQ-DOW-MONITOR-MINUTE-RESULTS-BACKFILL-001",
}


def test_minute_result_requirements_are_authoritative_and_traceable() -> None:
    index = yaml.safe_load((ROOT / "docs/spec-index.yaml").read_text(encoding="utf-8"))
    trace = yaml.safe_load((ROOT / "docs/traceability.yaml").read_text(encoding="utf-8"))
    indexed = {
        req
        for spec in index["specifications"]
        if spec["path"] == "docs/specs/dow-monitor-minute-results-clickhouse.md"
        for req in spec["requirements"]
    }
    traced = {
        item["id"]: item
        for item in trace["requirements"]
        if item["id"] in REQUIREMENTS
    }
    assert SPEC.is_file()
    assert indexed == REQUIREMENTS
    assert set(traced) == REQUIREMENTS
    text = SPEC.read_text(encoding="utf-8")
    for requirement_id, item in traced.items():
        assert f"## {requirement_id}" in text
        assert item["implementation"]
        assert all(
            test["type"] == "executable-test"
            and test["path"].startswith(("tests/", "backend/tests/"))
            for test in item["tests"]
        )
        assert {entry["type"] for entry in item["acceptance"]} == {
            "semantic-acceptance",
            "independent-review",
        }
```

- [ ] **Step 2: Run the contract and verify RED**

Run:

```powershell
python -m pytest tests/spec_contracts/test_dow_monitor_minute_results_clickhouse_contract.py -q
```

Expected: FAIL because the specification and index entry do not exist.

- [ ] **Step 3: Write the authoritative specification**

The specification must contain three exact `## REQ-...` headings and the following MUST rules:

```markdown
## REQ-DOW-MONITOR-MINUTE-RESULTS-SCHEMA-001

The service MUST create `longbridge.lb_dow_monitor_minute_results` with
`ReplacingMergeTree(updated_at)`, monthly partitions, logical order key
`(market, symbol, decision_minute)`, queryable columns for all 14 monitor
indicators, quality/provenance fields, and no TTL.

## REQ-DOW-MONITOR-MINUTE-RESULTS-MATERIALIZATION-001

For every enabled monitored symbol and newly completed one-minute bar, the
service MUST materialize at most one logical result using the same units,
stable/live boundaries, missing-value semantics, and formal-signal boundary
as the trend-monitor list. ClickHouse failure MUST NOT interrupt monitor or
signal processing and MUST remain retryable from a persistent gap.

## REQ-DOW-MONITOR-MINUTE-RESULTS-BACKFILL-001

At startup and after a gap, the service MUST batch-backfill missing completed
minutes for each market's current local trading day only. Every joined value
MUST have a source timestamp at or before the decision minute; missing values
MUST remain NULL and be listed in `missing_fields`.
```

- [ ] **Step 4: Register index, traceability, pending acceptance, and pending review**

Traceability must point only to executable test paths under `tests/`. Register all three entries exactly:

```yaml
  - id: REQ-DOW-MONITOR-MINUTE-RESULTS-SCHEMA-001
    specification: USER-20260729-DOW-MONITOR-MINUTE-RESULTS-CLICKHOUSE
    implementation:
      - backend/app/services/dow_monitor_minute_result_models.py
      - backend/app/services/dow_monitor_minute_result_repository.py
    tests:
      - {path: tests/spec_contracts/test_dow_monitor_minute_results_clickhouse_contract.py, type: executable-test}
      - {path: tests/backend/test_dow_monitor_minute_result_repository.py, type: executable-test}
    acceptance:
      - {path: docs/acceptance/dow-monitor-minute-results-clickhouse.md, type: semantic-acceptance}
      - {path: docs/reviews/dow-monitor-minute-results-clickhouse.md, type: independent-review}

  - id: REQ-DOW-MONITOR-MINUTE-RESULTS-MATERIALIZATION-001
    specification: USER-20260729-DOW-MONITOR-MINUTE-RESULTS-CLICKHOUSE
    implementation:
      - backend/app/services/dow_monitor_minute_result_models.py
      - backend/app/services/dow_monitor_minute_result_calculator.py
      - backend/app/services/dow_monitor_minute_result_history.py
      - backend/app/services/dow_monitor_minute_result_materializer.py
      - backend/app/services/dow_monitor_service.py
      - backend/app/main.py
    tests:
      - {path: tests/spec_contracts/test_dow_monitor_minute_results_clickhouse_contract.py, type: executable-test}
      - {path: tests/backend/test_dow_monitor_minute_result_calculator.py, type: executable-test}
      - {path: tests/backend/test_dow_monitor_minute_result_history.py, type: executable-test}
      - {path: tests/backend/test_dow_monitor_minute_result_materializer.py, type: executable-test}
      - {path: tests/backend/test_dow_monitor_minute_result_integration.py, type: executable-test}
    acceptance:
      - {path: docs/acceptance/dow-monitor-minute-results-clickhouse.md, type: semantic-acceptance}
      - {path: docs/reviews/dow-monitor-minute-results-clickhouse.md, type: independent-review}

  - id: REQ-DOW-MONITOR-MINUTE-RESULTS-BACKFILL-001
    specification: USER-20260729-DOW-MONITOR-MINUTE-RESULTS-CLICKHOUSE
    implementation:
      - backend/app/services/dow_monitor_minute_result_source.py
      - backend/app/services/dow_monitor_minute_result_history.py
      - backend/app/services/dow_monitor_minute_result_materializer.py
      - backend/app/services/dow_monitor_minute_result_repository.py
    tests:
      - {path: tests/spec_contracts/test_dow_monitor_minute_results_clickhouse_contract.py, type: executable-test}
      - {path: tests/backend/test_dow_monitor_minute_result_source.py, type: executable-test}
      - {path: tests/backend/test_dow_monitor_minute_result_history.py, type: executable-test}
      - {path: tests/backend/test_dow_monitor_minute_result_materializer.py, type: executable-test}
    acceptance:
      - {path: docs/acceptance/dow-monitor-minute-results-clickhouse.md, type: semantic-acceptance}
      - {path: docs/reviews/dow-monitor-minute-results-clickhouse.md, type: independent-review}
```

- [ ] **Step 5: Run the contract and checker**

Run:

```powershell
python -m pytest tests/spec_contracts/test_dow_monitor_minute_results_clickhouse_contract.py -q
python scripts/check_spec_compliance.py
```

Expected: contract PASS. Until Tasks 2–6 create the registered implementation and executable-test paths, the checker also reports those planned paths as invalid; this is an explicit in-progress state, not acceptance. Final verification may report only the two recorded pre-existing baseline findings and must introduce no new finding.

- [ ] **Step 6: Commit**

```powershell
git add docs/specs/dow-monitor-minute-results-clickhouse.md docs/spec-index.yaml docs/traceability.yaml tests/spec_contracts/test_dow_monitor_minute_results_clickhouse_contract.py docs/acceptance/dow-monitor-minute-results-clickhouse.md docs/reviews/dow-monitor-minute-results-clickhouse.md
git commit -m "docs(dow-monitor): specify minute result persistence"
```

---

### Task 2: Define the result model and pure display-equivalent formulas

**Files:**

- Create: `backend/app/services/dow_monitor_minute_result_models.py`
- Create: `backend/app/services/dow_monitor_minute_result_calculator.py`
- Create: `tests/backend/test_dow_monitor_minute_result_calculator.py`

**Interfaces:**

- Produces:
  - `DowMonitorMinuteResult(BaseModel)`
  - `MinuteResultContext(BaseModel)`
  - `calculate_minute_result(context: MinuteResultContext) -> DowMonitorMinuteResult`
- Consumes: causal raw values and stable 5m/15m/30m state snapshots supplied by Task 4.

- [ ] **Step 1: Write failing model and formula tests**

```python
def test_calculates_percent_units_and_depth_pressure() -> None:
    result = calculate_minute_result(
        context(
            minute_open=100.0,
            minute_close=101.0,
            last_price=101.0,
            day_high=102.0,
            day_low=98.0,
            bid_volumes=[300.0, 200.0],
            ask_volumes=[100.0, 100.0],
        )
    )
    assert result.momentum_1m_pct == pytest.approx(1.0)
    assert result.depth_imbalance_pct == pytest.approx(42.8571428571)
    assert result.distance_to_day_high_pct == pytest.approx((102 - 101) / 101 * 100)
    assert result.distance_to_day_low_pct == pytest.approx((101 - 98) / 101 * 100)


def test_missing_inputs_are_null_and_listed() -> None:
    result = calculate_minute_result(context(depth=None, capital=None))
    assert result.depth_imbalance_pct is None
    assert result.active_buy_ratio is None
    assert result.data_quality == "PARTIAL"
    assert {"depth_imbalance_pct", "active_buy_ratio"} <= set(result.missing_fields)


def test_realtime_values_cannot_change_formal_signal() -> None:
    bullish_book = calculate_minute_result(context(formal_signal_side="SELL", bid_volumes=[900], ask_volumes=[1]))
    bearish_book = calculate_minute_result(context(formal_signal_side="SELL", bid_volumes=[1], ask_volumes=[900]))
    assert bullish_book.formal_signal_side == bearish_book.formal_signal_side == "SELL"


def test_matches_frontend_authoritative_indicator_fixture() -> None:
    result = calculate_minute_result(frontend_authoritative_fixture())
    assert indicator_projection(result) == pytest.approx(
        frontend_authoritative_expected_values(),
    )
```

- [ ] **Step 2: Run and verify RED**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_minute_result_calculator.py -q
```

Expected: collection FAIL because the model/calculator modules are absent.

- [ ] **Step 3: Implement exact typed models**

The result model must declare all columns from the approved design, including:

```python
class DowMonitorMinuteResult(BaseModel):
    market: Literal["cn", "hk", "us"]
    symbol: str
    display_symbol: str
    decision_minute: datetime
    source_bar_time: datetime
    calculation_version: str = "v1"
    backfill: bool
    last_price: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None
    channel: str | None = None
    control_distance_pct: float | None = None
    vwap_distance_pct: float | None = None
    momentum_1m_pct: float | None = None
    momentum_5m_pct: float | None = None
    momentum_15m_pct: float | None = None
    volume_ratio: float | None = None
    volume_speed: float | None = None
    active_buy_ratio: float | None = None
    depth_imbalance_pct: float | None = None
    distance_to_day_high_pct: float | None = None
    distance_to_day_low_pct: float | None = None
    atr14_pct: float | None = None
    confirmation_count: int | None = Field(default=None, ge=0, le=2)
    formal_signal_side: str | None = None
    formal_signal_stage: str | None = None
    formal_signal_label: str | None = None
    formal_signal_time: datetime | None = None
    formal_signal_event_key: str | None = None
    data_quality: Literal["COMPLETE", "PARTIAL"]
    missing_fields: tuple[str, ...]
    source_timestamps: dict[str, datetime]
    result_payload: dict
    updated_at: datetime
```

Also include all approved minute OHLCV/turnover columns.

- [ ] **Step 4: Port formulas without approximations**

Implement named pure helpers matching `monitorListPresentation.ts`:

```python
def percent_change(current: float | None, base: float | None) -> float | None:
    return None if current is None or base in (None, 0) else (current - base) / base * 100.0


def depth_imbalance_pct(bids: Sequence[float], asks: Sequence[float]) -> float | None:
    bid = sum(bids[:5])
    ask = sum(asks[:5])
    total = bid + ask
    return (bid - ask) / total * 100.0 if bids and asks and total > 0 else None
```

Port completed-bar momentum, 15m ATR14, 15m→30m control/volume-ratio fallback, channel agreement, VWAP distance, active-buy ratio, day-high/day-low distance, and fixed 15m/30m confirmation count. The parity fixture must copy the same explicit inputs and expected outputs used by the authoritative frontend semantic test; it must not be a snapshot or golden file. Do not read a forming or provisional stable state.

- [ ] **Step 5: Run calculator tests and existing frontend semantic tests**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_minute_result_calculator.py -q
pnpm --dir frontend exec vitest run src/components/dow-monitor/monitorListPresentation.test.ts
```

Expected: both PASS; the explicit shared semantic fixture proves unit and formula parity while retaining the existing frontend authority.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/services/dow_monitor_minute_result_models.py backend/app/services/dow_monitor_minute_result_calculator.py tests/backend/test_dow_monitor_minute_result_calculator.py
git commit -m "feat(dow-monitor): calculate minute result snapshots"
```

---

### Task 3: Implement the ClickHouse raw source and minute-result repository

**Files:**

- Create: `backend/app/services/dow_monitor_minute_result_source.py`
- Create: `backend/app/services/dow_monitor_minute_result_repository.py`
- Create: `tests/backend/test_dow_monitor_minute_result_source.py`
- Create: `tests/backend/test_dow_monitor_minute_result_repository.py`

**Interfaces:**

- Produces:
  - `DowMonitorMinuteResultSource.load_raw_history(symbols, start, end) -> RawMinuteHistory`
  - `ensure_schema() -> None`
  - `existing_keys(symbols, start, end) -> set[MinuteResultKey]`
  - `insert_results(rows: Sequence[DowMonitorMinuteResult]) -> int`
  - `status() -> dict[str, object]`
- Consumes: `app.plugins.clickhouse.bridge` environment and Task 2 models.

- [ ] **Step 1: Write failing DDL and serialization tests**

```python
def test_schema_is_permanent_and_idempotent() -> None:
    repo = repository(capture_execute)
    repo.ensure_schema()
    ddl = capture_execute.sql[0]
    assert "ReplacingMergeTree(updated_at)" in ddl
    assert "PARTITION BY toYYYYMM(decision_minute)" in ddl
    assert "ORDER BY (market, symbol, decision_minute)" in ddl
    assert "TTL" not in ddl.upper()


def test_insert_serializes_nulls_arrays_and_percent_points() -> None:
    repo = repository(capture_execute)
    repo.insert_results([result(change_pct=1.25, active_buy_ratio=None, missing_fields=("active_buy_ratio",))])
    document = json.loads(capture_execute.payload.decode())
    assert document["change_pct"] == 1.25
    assert document["active_buy_ratio"] is None
    assert document["missing_fields"] == ["active_buy_ratio"]


def test_source_queries_each_raw_table_once_for_the_whole_symbol_batch() -> None:
    source = raw_source(capture_query)
    source.load_raw_history([hk_symbol(), us_symbol()], START, END)
    assert capture_query.count_for("lb_realtime_quotes") == 1
    assert capture_query.count_for("lb_realtime_depth") == 1
    assert capture_query.count_for("lb_realtime_trades") == 1
    assert capture_query.count_for("lb_realtime_candlesticks") == 1
    assert capture_query.count_for("lb_realtime_capital") == 1
```

- [ ] **Step 2: Run and verify RED**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_minute_result_source.py tests/backend/test_dow_monitor_minute_result_repository.py -q
```

Expected: FAIL because the source and repository do not exist.

- [ ] **Step 3: Implement safe ClickHouse execution**

Use validated database/table identifiers and the existing credential environment. The private executor signature is:

```python
ExecuteFn = Callable[[str, bytes | None], bytes]
QueryFn = Callable[[str], list[dict]]


class DowMonitorMinuteResultRepository:
    def __init__(self, query_fn: QueryFn = bridge.query_json_each_row, execute_fn: ExecuteFn | None = None):
        ...
```

`execute_fn` must use the same `CLICKHOUSE_URL`, database, user, password, and timeout semantics as `bridge.query_json_each_row`, without logging credentials.

- [ ] **Step 4: Implement the batched raw source**

`DowMonitorMinuteResultSource.load_raw_history` must issue one query per source table for the whole symbol batch/time range. Depth and quote SQL must select the last record at or before each snapshot minute using `argMax(..., updated_at)` or return rows that can be grouped in process. Candlestick SQL must read `FINAL` because it is a `ReplacingMergeTree`; do not use `FINAL` on plain `MergeTree` sources.

- [ ] **Step 5: Implement result DDL, existing-key query, and JSONEachRow insert**

The result repository owns only the derived-result table. It must not query raw source tables.

Serialize Pydantic rows with:

```python
documents = [
    row.model_dump(mode="json") | {
        "backfill": int(row.backfill),
        "source_timestamps": json.dumps(row.source_timestamps, ensure_ascii=False, default=str),
        "result_payload": json.dumps(row.result_payload, ensure_ascii=False, default=str),
    }
    for row in rows
]
payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in documents).encode("utf-8")
```

Insert exactly once per batch using `FORMAT JSONEachRow`.

- [ ] **Step 6: Run repository tests**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_minute_result_source.py tests/backend/test_dow_monitor_minute_result_repository.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add backend/app/services/dow_monitor_minute_result_source.py backend/app/services/dow_monitor_minute_result_repository.py tests/backend/test_dow_monitor_minute_result_source.py tests/backend/test_dow_monitor_minute_result_repository.py
git commit -m "feat(dow-monitor): add minute result ClickHouse storage"
```

---

### Task 4: Build causal historical contexts

**Files:**

- Create: `backend/app/services/dow_monitor_minute_result_history.py`
- Create: `tests/backend/test_dow_monitor_minute_result_history.py`

**Interfaces:**

- Produces:
  - `DowMonitorMinuteResultHistoryBuilder(stable_state_builder: StableStateBuilder)`.
  - `build_contexts(history: RawMinuteHistory, symbol: MonitoredSymbol, market_day: date, backfill: bool, notifications: Sequence[DowNotification]) -> list[MinuteResultContext]`.
- Consumes: Task 3 batched raw history from `DowMonitorMinuteResultSource`, an injected stable-timeframe replay dependency backed by the existing Dow engine, stable snapshot rules, and notification history.

- [ ] **Step 1: Write causality and market-clock RED tests**

```python
def test_context_never_uses_a_quote_from_the_future() -> None:
    contexts = history_builder().build_contexts(
        history(
            bars=[bar("2026-07-29T09:30:00+08:00")],
            quotes=[
                quote("2026-07-29T09:30:59+08:00", 100.0),
                quote("2026-07-29T09:31:01+08:00", 999.0),
            ],
        ),
        hk_symbol(),
        date(2026, 7, 29),
        backfill=True,
        notifications=[],
    )
    assert contexts[0].last_price == 100.0


def test_us_market_day_uses_new_york_date() -> None:
    contexts = history_builder().build_contexts(
        us_history_crossing_shanghai_midnight(),
        us_symbol(),
        date(2026, 7, 29),
        True,
        notifications=[],
    )
    assert {item.market_day for item in contexts} == {date(2026, 7, 29)}


def test_stable_state_replay_is_cached_per_completed_bucket() -> None:
    stable_state_builder = counting_stable_state_builder()
    history_builder(stable_state_builder).build_contexts(
        five_minutes_in_one_15m_bucket(),
        hk_symbol(),
        date(2026, 7, 29),
        True,
        notifications=[],
    )
    assert stable_state_builder.calls_for("min_15") == 1
```

- [ ] **Step 2: Run and verify RED**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_minute_result_history.py -q
```

Expected: FAIL because `DowMonitorMinuteResultHistoryBuilder` is absent.

- [ ] **Step 3: Implement as-of joins and complete-minute selection**

For each completed `min_1` bar, define:

```python
source_bar_time = market_local_bar_start
decision_minute = source_bar_time + timedelta(minutes=1)
```

Sort every source once, advance monotonic pointers, and reject source timestamps later than `decision_minute`. Apply existing quote/candle/capital age rules. For depth, use the final valid snapshot within that minute. Do not carry a stale snapshot across its threshold.

- [ ] **Step 4: Build stable timeframes and formal-signal references**

Use only bars complete at the target decision minute. Cache replay results by `(symbol, timeframe, bucket_end)` so 15m/30m engine work runs once per completed bucket, not once per minute. Formal signal lookup selects the latest persisted notification whose `triggered_at <= decision_minute`; absence produces `None`.

- [ ] **Step 5: Run causal-history and calculator tests**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_minute_result_history.py tests/backend/test_dow_monitor_minute_result_calculator.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/services/dow_monitor_minute_result_history.py tests/backend/test_dow_monitor_minute_result_history.py
git commit -m "feat(dow-monitor): build causal minute result history"
```

---

### Task 5: Implement gap-driven materialization and today's backfill

**Files:**

- Create: `backend/app/services/dow_monitor_minute_result_materializer.py`
- Create: `tests/backend/test_dow_monitor_minute_result_materializer.py`

**Interfaces:**

- Produces:
  - `materialize(symbols: Sequence[MonitoredSymbol], now: datetime) -> MaterializeRun`
  - `status() -> MaterializerStatus`
- Consumes: `DowMonitorMinuteResultSource`, repository, `DowMonitorMinuteResultHistoryBuilder`, and `calculate_minute_result`.

- [ ] **Step 1: Write failing gap/idempotency/isolation tests**

```python
def test_backfills_only_missing_minutes_for_each_market_today() -> None:
    run = materializer(existing={key("1347.HK", "09:31")}).materialize(
        [hk_symbol(), us_symbol()],
        now=datetime(2026, 7, 29, 13, 0, tzinfo=UTC),
    )
    assert key("1347.HK", "09:31") not in run.inserted_keys
    assert all(item.market_day == date(2026, 7, 29) for item in run.rows)


def test_clickhouse_failure_is_reported_without_raising() -> None:
    run = failing_materializer().materialize([hk_symbol()], NOW)
    assert run.written_rows == 0
    assert run.error is not None
    assert failing_materializer().status().pending_minutes >= 1
```

- [ ] **Step 2: Run and verify RED**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_minute_result_materializer.py -q
```

Expected: FAIL because the materializer does not exist.

- [ ] **Step 3: Implement market-local day planning**

Map markets exactly:

```python
MARKET_ZONES = {
    "cn": ZoneInfo("Asia/Shanghai"),
    "hk": ZoneInfo("Asia/Hong_Kong"),
    "us": ZoneInfo("America/New_York"),
}
```

Group enabled symbols by `(market, local_date)`, batch-load raw history with enough prior bars for MA/ATR/volume baselines, and compute only logical keys absent from `existing_keys`.

Candidate logical keys come only from completed raw `min_1` bars for that symbol and market-local day. A scheduled market minute without a completed raw bar is not a gap and must not produce a synthetic row.

- [ ] **Step 4: Implement fail-open status**

`MaterializerStatus` must expose:

```python
last_started_at: datetime | None
last_success_at: datetime | None
last_error: str | None
pending_minutes: int
last_written_rows: int
```

Catch repository/calculation failures at the materializer boundary, retain the pending count, and return a run record. Do not mutate monitor states or notifications.

- [ ] **Step 5: Run materializer tests**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_minute_result_materializer.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/services/dow_monitor_minute_result_materializer.py tests/backend/test_dow_monitor_minute_result_materializer.py
git commit -m "feat(dow-monitor): materialize missing minute results"
```

---

### Task 6: Integrate with the 3018 monitor lifecycle

**Files:**

- Modify: `backend/app/services/dow_monitor_service.py`
- Modify: `backend/app/main.py`
- Create: `tests/backend/test_dow_monitor_minute_result_integration.py`
- Modify: `backend/tests/test_dow_monitor_api.py`

**Interfaces:**

- Consumes: optional `DowMonitorMinuteResultMaterializer`.
- Produces: `status()["minute_results"]` without changing existing status fields.

- [ ] **Step 1: Write failing lifecycle tests**

```python
def test_monitor_cycle_materializes_after_signal_processing(tmp_path) -> None:
    events: list[str] = []
    service = service_with_materializer(tmp_path, events)
    asyncio.run(service.run_once())
    assert events.index("formal-signals-finished") < events.index("minute-results")


def test_materializer_failure_does_not_fail_monitor_cycle(tmp_path) -> None:
    service = service_with_failing_materializer(tmp_path)
    asyncio.run(service.run_once())
    assert service.status()["last_success_at"] is not None
    assert service.status()["minute_results"]["last_error"] == "clickhouse unavailable"
```

- [ ] **Step 2: Run and verify RED**

```powershell
Push-Location backend
python -m pytest tests/test_dow_monitor_api.py -k "materializ or minute_result" -q
Pop-Location
```

Expected: FAIL because the constructor/status do not accept the materializer.

- [ ] **Step 3: Add an optional constructor dependency**

```python
def __init__(..., minute_result_materializer=None) -> None:
    ...
    self._minute_result_materializer = minute_result_materializer
```

After normal per-symbol evaluation and before completing the cycle:

```python
if self._minute_result_materializer is not None:
    await asyncio.to_thread(
        self._minute_result_materializer.materialize,
        enabled,
        now,
    )
```

The materializer itself absorbs ClickHouse failures.

- [ ] **Step 4: Wire startup**

In `_start_dow_monitor`, construct the repository, call `ensure_schema`, construct the materializer, and pass it to `DowMonitorService`. Schema failure must leave a disabled/failing materializer status rather than preventing 3018 startup.

- [ ] **Step 5: Expose nested status**

Add:

```python
"minute_results": (
    self._minute_result_materializer.status().model_dump(mode="json")
    if self._minute_result_materializer is not None
    else {"enabled": False}
)
```

- [ ] **Step 6: Run backend integration and regression tests**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_minute_result_integration.py tests/backend/test_dow_monitor_minute_result_calculator.py tests/backend/test_dow_monitor_minute_result_source.py tests/backend/test_dow_monitor_minute_result_repository.py tests/backend/test_dow_monitor_minute_result_history.py tests/backend/test_dow_monitor_minute_result_materializer.py -q
Push-Location backend
.\.venv\Scripts\python.exe -m pytest tests/test_dow_monitor_api.py tests/test_realtime_websocket.py -q
Pop-Location
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add backend/app/services/dow_monitor_service.py backend/app/main.py backend/tests/test_dow_monitor_api.py tests/backend/test_dow_monitor_minute_result_integration.py
git commit -m "feat(dow-monitor): persist minute results from monitor loop"
```

---

### Task 7: Verify contracts, document evidence, and prepare production release

**Files:**

- Modify: `docs/acceptance/dow-monitor-minute-results-clickhouse.md`
- Modify: `docs/reviews/dow-monitor-minute-results-clickhouse.md`
- Modify: `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md`

**Interfaces:**

- Consumes: all three requirements and Tasks 1–6.
- Produces: exact acceptance evidence, independent review, operator queries, release and rollback record.

- [ ] **Step 1: Run focused and full relevant tests**

```powershell
.\backend\.venv\Scripts\python.exe -m pytest tests/backend/test_dow_monitor_minute_result_integration.py tests/backend/test_dow_monitor_minute_result_calculator.py tests/backend/test_dow_monitor_minute_result_source.py tests/backend/test_dow_monitor_minute_result_repository.py tests/backend/test_dow_monitor_minute_result_history.py tests/backend/test_dow_monitor_minute_result_materializer.py -q
Push-Location backend
.\.venv\Scripts\python.exe -m pytest tests/test_dow_monitor_api.py tests/test_realtime_websocket.py -q
Pop-Location

python -m pytest tests/spec_contracts/test_dow_monitor_minute_results_clickhouse_contract.py tests/spec_contracts/test_dow_monitor_list_websocket_contract.py tests/spec_contracts/test_realtime_frontend_contract.py -q

pnpm --dir frontend exec vitest run src/components/dow-monitor/monitorListPresentation.test.ts src/pages/DowMonitor.test.tsx
pnpm --dir frontend build
python scripts/check_spec_compliance.py
```

Expected: all feature/relevant regression tests and build PASS; spec checker introduces no new finding.

- [ ] **Step 2: Perform an independent requirement-to-evidence review**

For each of the three IDs, inspect the authoritative MUST text, production implementation, exact executable test, browser/API/ClickHouse evidence, and acceptance record. Record PASS only if lower-layer raw history and causal sample recomputation are proven independently.

- [ ] **Step 3: Build a unique candidate image and back up production**

Record:

- current image and immutable image ID;
- rollback tag;
- exact Compose project/files;
- container inspect;
- `dow_monitor_symbols.json`;
- new-table row counts if it already exists.

Build from the exact tested commit using the established `output/release/Dockerfile`; never overwrite the current tag.

- [ ] **Step 4: Deploy with the exact production Compose project**

Use:

```bash
docker compose -p dow-monitor-bfd819d438b4 \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  up -d --no-build app
```

Set `TICKFLOW_IMAGE` to the unique candidate tag for that invocation.

- [ ] **Step 5: Verify schema and today's backfill**

Run ClickHouse queries proving:

```sql
SHOW CREATE TABLE longbridge.lb_dow_monitor_minute_results;

SELECT market, symbol, min(decision_minute), max(decision_minute),
       uniqExact(decision_minute), countIf(data_quality = 'PARTIAL')
FROM longbridge.lb_dow_monitor_minute_results FINAL
WHERE toDate(decision_minute) IN (today(), yesterday())
GROUP BY market, symbol
ORDER BY market, symbol;
```

Confirm no TTL and no duplicate logical keys in `FINAL`.

- [ ] **Step 6: Recompute one sample per market**

For one enabled CN, HK, and US symbol, query raw quote/depth/trades/candlestick/capital rows up to a chosen `decision_minute`, recompute all available fields, and compare with the result row. Document exact inputs, formulas, outputs, and legitimate `NULL` fields.

- [ ] **Step 7: Verify fail-open behavior and runtime health**

Confirm:

- `/health` succeeds;
- `/api/dow-monitor/status` shows minute-result status separately;
- container is running with zero restarts;
- release-window logs have no `ERROR|CRITICAL|Traceback` outside an intentionally tested, recovered ClickHouse failure;
- monitor symbols file hash is unchanged;
- existing formal signals and timestamps are unchanged.

- [ ] **Step 8: Update acceptance, review, and runbook**

Acceptance must include test counts, DDL, per-market row counts, sample calculations, missing-field distribution, image/backup/rollback IDs, and runtime health. The independent review must map every MUST to code, executable tests, and semantic evidence. The runbook must include table/query/backfill/status/recovery instructions.

- [ ] **Step 9: Commit documentation**

```powershell
git add docs/acceptance/dow-monitor-minute-results-clickhouse.md docs/reviews/dow-monitor-minute-results-clickhouse.md
git commit -m "docs(dow-monitor): record minute result release"
```

The Obsidian runbook is outside the Git worktree and must be saved but not added to this repository commit.

---

## Final Verification Checklist

- [ ] All three stable requirements are authoritative and traceable.
- [ ] Every new behavior was observed RED before implementation.
- [ ] All 14 indicators use the same units and stable/live boundaries as the list.
- [ ] No source timestamp exceeds its result `decision_minute`.
- [ ] Every row has a complete 1m bar.
- [ ] Missing history stays `NULL` and is listed.
- [ ] The table has no TTL.
- [ ] Duplicate runs do not create duplicate logical keys in `FINAL`.
- [ ] ClickHouse failures do not change formal monitor/signal outcomes.
- [ ] Today's local market sessions are backfilled for enabled symbols.
- [ ] A/HK/US samples are independently recomputed from raw history.
- [ ] Production image, backup, rollback, health, logs, hashes, and status are recorded.
- [ ] The Obsidian runbook reflects the deployed system.
