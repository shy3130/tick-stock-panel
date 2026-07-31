# Dow Monitor Half-Hour AI Analysis Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Apply `spec-guard` before every production edit, keep the AI path isolated from real-time interpretation and formal signals, and stop if repository specifications conflict.

**Goal:** Add a per-stock, every-30-minute, regular-session AI analysis that summarizes the current trading day's cumulative monitor indicators, permanently stores each completed window in ClickHouse, and exposes the long-form result through an independent desktop/mobile entry and dialog.

**Architecture:** Run a dedicated single-concurrency worker in its own container. It reads enabled monitor symbols from the shared monitor store and cumulative minute results from ClickHouse, derives exchange-aware completed 30-minute windows, builds a bounded evidence snapshot, calls the existing AI provider, validates the structured response, and writes an immutable logical version for each symbol/window. The 3018 panel service only reads lightweight status/history metadata and never runs the AI scheduling loop. The real-time key interpretation and formal signal paths remain unchanged.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, ClickHouse, `exchange-calendars`, existing `ai_provider`, React 18, TypeScript, TanStack Query, Vitest, pytest, Docker Compose.

**Active requirements:** `REQ-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001`, `REQ-DOW-MONITOR-HALF-HOUR-AI-VIEW-001`

---

## Non-negotiable semantic constraints

- Only analyze symbols currently enabled in the trend monitor.
- Only schedule completed 30-minute windows during the symbol's regular exchange session; exclude pre-market, after-hours, holidays, and lunch breaks.
- Use only observations whose event time is at or before the stored `data_cutoff`; do not leak later data into an earlier window.
- Analyze the cumulative same-day series from regular-session open through the window end, not merely the last 30 minutes.
- Keep this feature independent from real-time key interpretation, formal buy/sell signals, WebSocket ingestion, and minute-result persistence.
- Default worker concurrency is one; failures for one symbol/window cannot block later jobs.
- Store every logical half-hour result permanently in ClickHouse without TTL. Retries may replace the same logical key but must not create duplicate visible versions.
- The overview response carries only status, latest window, title, and short summary. The full narrative is fetched only when the user opens the independent dialog.
- Model-generated claims may reference only backend-supplied evidence keys. Numeric display values are rendered from the snapshot by backend code rather than trusted from model prose.
- AI text is advisory and must include uncertainty/data-quality context. It must not be written into or consumed by the formal signal evaluator.

## Task 1: Register authoritative specifications and exchange-calendar semantics

**Files:**

- Create: `docs/specs/dow-monitor-half-hour-ai-analysis.md`
- Modify: `docs/spec-index.yaml`
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Create: `backend/app/services/dow_monitor_half_hour_ai_calendar.py`
- Create: `backend/tests/test_dow_monitor_half_hour_ai_calendar.py`

- [ ] **Step 1: Write failing behavior tests for exchange sessions**

Cover at least:

```python
def test_cn_first_due_window_is_1000_beijing():
    calendar = HalfHourWindowCalendar()
    assert calendar.completed_window_ends(
        market="cn",
        now=datetime.fromisoformat("2026-07-31T10:00:01+08:00"),
    ) == [datetime.fromisoformat("2026-07-31T10:00:00+08:00")]


def test_hk_lunch_break_does_not_create_1230_window():
    calendar = HalfHourWindowCalendar()
    ends = calendar.session_window_ends("hk", date(2026, 7, 31))
    assert datetime.fromisoformat("2026-07-31T12:30:00+08:00") not in ends


def test_us_dst_session_maps_to_beijing_time():
    calendar = HalfHourWindowCalendar()
    ends = calendar.session_window_ends("us", date(2026, 7, 31))
    assert ends[0] == datetime.fromisoformat("2026-07-31T22:00:00+08:00")


def test_exchange_holiday_has_no_due_windows():
    assert HalfHourWindowCalendar().session_window_ends(
        "us", date(2026, 7, 3)
    ) == []
```

Run:

```powershell
Set-Location E:\my_project\.worktrees\tickflow-monitor-list-v2\backend
uv run pytest tests/test_dow_monitor_half_hour_ai_calendar.py -q
```

Expected: FAIL because the calendar service and dependency do not exist.

- [ ] **Step 2: Add the narrow authoritative specification**

The spec must define:

- CN=`XSHG`, HK=`XHKG`, US=`XNYS`.
- The exchange calendar, not fixed local clock ranges, owns holidays, DST, half-days, and lunch breaks.
- A window is due only after all regular-session minutes in that 30-minute chunk are complete.
- An incomplete closing chunk on a half-day is due at the actual regular close.
- A newly added symbol starts at the first completed checkpoint after `created_at`; historical minute rows may enrich that checkpoint but must not cause earlier AI calls.
- Stable ClickHouse logical key: `(market, symbol, trade_date, window_end)`.
- All constraints in this plan's “Non-negotiable semantic constraints”.

Register the spec and both requirement IDs in `docs/spec-index.yaml`. If an existing applicable spec disagrees, stop before production code and request an authoritative decision.

- [ ] **Step 3: Add and lock the calendar dependency**

Add:

```toml
"exchange-calendars>=4.10,<5",
```

Then run:

```powershell
Set-Location E:\my_project\.worktrees\tickflow-monitor-list-v2\backend
uv lock
```

- [ ] **Step 4: Implement the calendar wrapper**

Use `exchange_calendars.get_calendar()` and the calendar's session minutes. Split minutes wherever the interval exceeds one minute so an exchange lunch break never becomes a synthetic window. Convert every public result to timezone-aware Asia/Shanghai time.

The core interface must be:

```python
MARKET_CALENDARS = {"cn": "XSHG", "hk": "XHKG", "us": "XNYS"}


class HalfHourWindowCalendar:
    def session_window_ends(
        self, market: str, beijing_trade_date: date
    ) -> list[datetime]: ...

    def completed_window_ends(
        self, market: str, now: datetime
    ) -> list[datetime]: ...

    def is_regular_session_time(
        self, market: str, observed_at: datetime
    ) -> bool: ...
```

Generate a due end for each 30 regular minutes in a continuous segment and the actual close for a final partial chunk. Normalize the library's minute-label convention inside this wrapper so tests assert wall-clock checkpoint semantics rather than library internals.

- [ ] **Step 5: Run the focused tests**

```powershell
uv run pytest tests/test_dow_monitor_half_hour_ai_calendar.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the calendar/spec slice**

```powershell
git add docs/specs/dow-monitor-half-hour-ai-analysis.md docs/spec-index.yaml backend/pyproject.toml backend/uv.lock backend/app/services/dow_monitor_half_hour_ai_calendar.py backend/tests/test_dow_monitor_half_hour_ai_calendar.py
git commit -m "feat: define half-hour monitor analysis schedule"
```

## Task 2: Add durable models and ClickHouse repository

**Files:**

- Create: `backend/app/services/dow_monitor_half_hour_ai_models.py`
- Create: `backend/app/services/dow_monitor_half_hour_ai_repository.py`
- Create: `backend/tests/test_dow_monitor_half_hour_ai_repository.py`

- [ ] **Step 1: Write failing repository contract tests**

Test schema creation, insert, latest summary, full history, detail lookup, retry replacement, and absence of a TTL:

```python
def test_retry_replaces_same_logical_window(repository):
    repository.save(completed_analysis(revision=1, summary="first"))
    repository.save(completed_analysis(revision=2, summary="second"))

    rows = repository.list_history("us", "RNG.US", date(2026, 7, 31))

    assert len(rows) == 1
    assert rows[0].summary == "second"


def test_schema_has_no_ttl(repository):
    assert "TTL" not in repository.create_table_sql.upper()
```

Use a recording/fake ClickHouse client for deterministic unit tests and add one optional integration-marked test against ClickHouse.

Run:

```powershell
uv run pytest tests/test_dow_monitor_half_hour_ai_repository.py -q
```

Expected: FAIL because the repository does not exist.

- [ ] **Step 2: Implement typed models**

Use explicit status values and separate lightweight/full records:

```python
HalfHourAiStatus = Literal[
    "pending", "running", "completed", "failed", "insufficient_data"
]


class HalfHourAiSummary(BaseModel):
    analysis_id: str
    market: Literal["cn", "hk", "us"]
    symbol: str
    trade_date: date
    window_end: datetime
    status: HalfHourAiStatus
    title: str | None = None
    summary: str | None = None
    updated_at: datetime


class HalfHourAiAnalysis(HalfHourAiSummary):
    data_cutoff: datetime
    model_name: str | None = None
    conclusion: str | None = None
    evidence: list["ValidatedEvidence"] = []
    risks: list[str] = []
    scenarios: list["AnalysisScenario"] = []
    data_quality: list[str] = []
    input_snapshot: dict[str, Any]
    error_code: str | None = None
    error_message: str | None = None
```

Avoid mutable list defaults by using `Field(default_factory=list)` in production.

`analysis_id` must be deterministic from the logical key, for example SHA-256 of:

```text
market|normalized_symbol|trade_date|window_end_utc
```

- [ ] **Step 3: Implement the repository and schema**

Create:

```sql
CREATE TABLE IF NOT EXISTS longbridge.lb_dow_monitor_half_hour_ai_analyses
(
    analysis_id String,
    market LowCardinality(String),
    symbol String,
    trade_date Date,
    window_end DateTime64(3, 'UTC'),
    data_cutoff DateTime64(3, 'UTC'),
    status LowCardinality(String),
    title Nullable(String),
    summary Nullable(String),
    conclusion Nullable(String),
    evidence_json String,
    risks_json String,
    scenarios_json String,
    data_quality_json String,
    input_snapshot_json String,
    model_name Nullable(String),
    attempt UInt16,
    error_code Nullable(String),
    error_message Nullable(String),
    created_at DateTime64(3, 'UTC'),
    updated_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(trade_date)
ORDER BY (market, symbol, trade_date, window_end)
```

Do not add TTL. Queries must use `argMax(..., updated_at)` or an equivalent deterministic latest-row projection, rather than waiting for background merges.

Expose:

```python
ensure_schema()
save(record)
exists_completed(market, symbol, trade_date, window_end)
latest_summaries(keys)
list_history(market, symbol, trade_date)
get_by_id(analysis_id)
```

- [ ] **Step 4: Run focused tests**

```powershell
uv run pytest tests/test_dow_monitor_half_hour_ai_repository.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/services/dow_monitor_half_hour_ai_models.py backend/app/services/dow_monitor_half_hour_ai_repository.py backend/tests/test_dow_monitor_half_hour_ai_repository.py
git commit -m "feat: persist half-hour monitor analyses"
```

## Task 3: Build a cutoff-safe cumulative snapshot and validated prompt contract

**Files:**

- Create: `backend/app/services/dow_monitor_half_hour_ai_snapshot.py`
- Create: `backend/app/services/dow_monitor_half_hour_ai_prompt.py`
- Create: `backend/tests/test_dow_monitor_half_hour_ai_snapshot.py`
- Create: `backend/tests/test_dow_monitor_half_hour_ai_prompt.py`
- Modify: `backend/app/services/dow_monitor_minute_result_repository.py`

- [ ] **Step 1: Write failing snapshot tests**

Cover cutoff safety, cumulative scope, invalid values, minimum observations, and exact evidence rendering:

```python
def test_snapshot_excludes_rows_after_data_cutoff(builder):
    snapshot = builder.build(
        market="us",
        symbol="RNG.US",
        session_open=dt("2026-07-31T21:30:00+08:00"),
        data_cutoff=dt("2026-07-31T22:30:00+08:00"),
        rows=[
            minute_row("22:29", price=54.0),
            minute_row("22:31", price=99.0),
        ],
    )
    assert snapshot.latest_price == 54.0
    assert snapshot.observation_count == 1


def test_second_window_uses_all_same_day_rows_from_open(builder):
    snapshot = builder.build(..., data_cutoff=dt("2026-07-31T23:00:00+08:00"))
    assert snapshot.range_start == dt("2026-07-31T21:30:00+08:00")
    assert snapshot.range_end == dt("2026-07-31T23:00:00+08:00")
```

Add a repository query test asserting:

```sql
observed_at >= %(session_open)s AND observed_at <= %(data_cutoff)s
```

Run:

```powershell
uv run pytest tests/test_dow_monitor_half_hour_ai_snapshot.py -q
```

Expected: FAIL.

- [ ] **Step 2: Add a bounded batch query to the minute-result repository**

Add:

```python
load_cumulative_rows(
    keys: Sequence[tuple[str, str]],
    session_open: datetime,
    data_cutoff: datetime,
) -> dict[tuple[str, str], list[DowMonitorMinuteResult]]
```

Batch symbols that share a due cutoff to avoid one query per stock. Order rows by symbol and event time. Reject/flag non-finite numeric values during snapshot construction.

- [ ] **Step 3: Implement deterministic snapshot derivation**

The snapshot must include:

- exact market, symbol, session open, window end, and `data_cutoff`;
- observation count, coverage start/end, largest gap, and stale-data flag;
- first/latest/high/low price and session change;
- VWAP position/direction when available;
- 1m/5m/15m momentum and consistency;
- same-time RVOL/volume acceleration/direction when available;
- active-buy/OFI/book imbalance/spread evidence when available;
- formal signal history as read-only context;
- structured real-time interpretation context already present in minute-result `result_payload`;
- deterministic trend/opportunity/risk changes across the cumulative series.

Do not import frontend interpretation code. Keep one backend evidence dictionary with stable keys such as:

```python
snapshot.evidence_values = {
    "latest_price": Decimal("54.01"),
    "session_high": Decimal("56.67"),
    "session_low": Decimal("53.13"),
    "vwap_distance_pct": Decimal("-1.42"),
    "momentum_5m_pct": Decimal("-0.33"),
    "relative_volume": Decimal("2.10"),
}
```

If the input cannot support a defensible analysis, return an `insufficient_data` result with precise reasons instead of calling the model.

- [ ] **Step 4: Write failing prompt/parser tests**

Test:

- only allowed evidence keys are accepted;
- unknown keys fail validation;
- backend formats numeric evidence values from the snapshot;
- fenced JSON is parsed but arbitrary prose is rejected;
- missing uncertainty/risk fields fail validation;
- a model cannot change a formal signal.

Example:

```python
def test_model_cannot_invent_numeric_evidence(parser, snapshot):
    raw = model_json(evidence=[{"metric_key": "made_up_metric", "meaning": "strong"}])
    with pytest.raises(InvalidAiAnalysis):
        parser.parse_and_validate(raw, snapshot)


def test_backend_owns_numeric_evidence_value(parser, snapshot):
    output = parser.parse_and_validate(
        model_json(evidence=[{"metric_key": "session_high", "meaning": "near high"}]),
        snapshot,
    )
    assert output.evidence[0].value == "56.67"
```

Run:

```powershell
uv run pytest tests/test_dow_monitor_half_hour_ai_prompt.py -q
```

Expected: FAIL.

- [ ] **Step 5: Implement the structured prompt and validator**

Require this model output shape:

```json
{
  "title": "短标题",
  "summary": "不超过80字",
  "conclusion": "综合分析",
  "evidence": [
    {"metric_key": "session_high", "meaning": "为什么重要"}
  ],
  "risks": ["风险一"],
  "scenarios": [
    {"condition": "条件", "implication": "可能含义", "invalidates_when": "失效条件"}
  ],
  "data_quality": ["缺失或时效说明"]
}
```

The parser maps `metric_key` to the snapshot's authoritative label/value/unit and rejects keys absent from that snapshot. Strip one optional Markdown JSON fence, parse JSON, validate with Pydantic, and bound all strings/list lengths before persistence.

The system prompt must explicitly state:

- no trade instruction or guaranteed outcome;
- no invented prices, percentages, volumes, or events;
- distinguish observed evidence, inference, scenario, and invalidation;
- analyze cumulative same-day structure through the cutoff;
- return JSON only.

- [ ] **Step 6: Run the focused tests**

```powershell
uv run pytest tests/test_dow_monitor_half_hour_ai_snapshot.py tests/test_dow_monitor_half_hour_ai_prompt.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add backend/app/services/dow_monitor_half_hour_ai_snapshot.py backend/app/services/dow_monitor_half_hour_ai_prompt.py backend/app/services/dow_monitor_minute_result_repository.py backend/tests/test_dow_monitor_half_hour_ai_snapshot.py backend/tests/test_dow_monitor_half_hour_ai_prompt.py
git commit -m "feat: build validated monitor analysis snapshots"
```

## Task 4: Implement the isolated scheduler/worker

**Files:**

- Create: `backend/app/workers/__init__.py`
- Create: `backend/app/workers/dow_monitor_half_hour_ai.py`
- Create: `backend/tests/test_dow_monitor_half_hour_ai_worker.py`
- Modify: `docker-compose.yml`
- Modify: `.env.example`

- [ ] **Step 1: Write failing scheduler and isolation tests**

Cover:

- disabled symbols are ignored;
- symbols outside regular session are ignored;
- symbol added at 10:45 first runs at 11:00;
- already-completed logical windows are not called again;
- missed current-day windows after `created_at` are recovered after restart;
- worker processes one AI call at a time by default;
- one failed symbol does not stop the queue;
- worker writes no formal signal or real-time interpretation state;
- the 3018 application lifecycle does not start the worker.

Example:

```python
async def test_worker_never_mutates_formal_signal_store(worker, monitor_store):
    before = monitor_store.read_symbol("RNG.US")
    await worker.run_due_jobs(now=dt("2026-07-31T23:00:01+08:00"))
    after = monitor_store.read_symbol("RNG.US")
    assert after == before


async def test_new_symbol_starts_at_next_checkpoint(worker, ai_provider):
    worker.monitor_store.add(symbol="RNG.US", market="us", created_at=dt("22:45"))
    await worker.run_due_jobs(now=dt("23:00:01"))
    assert ai_provider.calls[0].window_end == dt("23:00")
```

Run:

```powershell
uv run pytest tests/test_dow_monitor_half_hour_ai_worker.py -q
```

Expected: FAIL.

- [ ] **Step 2: Implement a single-concurrency worker**

Composition root:

```python
async def build_worker() -> DowMonitorHalfHourAiWorker:
    store = DowMonitorStore(settings.data_dir)
    minute_repository = DowMonitorMinuteResultRepository(...)
    analysis_repository = DowMonitorHalfHourAiRepository(...)
    return DowMonitorHalfHourAiWorker(
        monitor_store=store,
        minute_repository=minute_repository,
        analysis_repository=analysis_repository,
        calendar=HalfHourWindowCalendar(),
        snapshot_builder=HalfHourAiSnapshotBuilder(),
        prompt_service=HalfHourAiPromptService(generate_ai_text),
        concurrency=settings.dow_ai_worker_concurrency,
    )
```

The worker loop should poll every 15 seconds by default, derive due windows from enabled symbols and their `created_at`, batch-load cumulative rows for compatible cutoffs, and execute provider calls behind an `asyncio.Semaphore(1)`.

Persist `running` before a call and one of `completed`, `failed`, or `insufficient_data` afterward. Retry transient provider failures at most twice with bounded backoff. A stale `running` row must be recoverable after restart.

Do not instantiate a quote context, call the panel API for source data, write minute rows, or import the real-time signal evaluator.

- [ ] **Step 3: Add a dedicated Compose service**

Use the existing backend image but a separate command:

```yaml
TickFlow_Dow_AI_Worker:
  build:
    context: .
    dockerfile: Dockerfile
  command: ["uv", "run", "python", "-m", "app.workers.dow_monitor_half_hour_ai"]
  restart: unless-stopped
  environment:
    DOW_AI_WORKER_ENABLED: "true"
    DOW_AI_WORKER_CONCURRENCY: "1"
    DOW_AI_WORKER_POLL_SECONDS: "15"
  volumes:
    - ./data:/app/data:ro
```

Reuse the existing AI-provider and ClickHouse environment wiring. Do not publish an HTTP port for the worker.

Document environment defaults in `.env.example`. The worker must exit clearly when explicitly disabled and keep retrying infrastructure connections without taking down the 3018 service.

- [ ] **Step 4: Run focused tests and validate Compose**

```powershell
uv run pytest tests/test_dow_monitor_half_hour_ai_worker.py -q
Set-Location E:\my_project\.worktrees\tickflow-monitor-list-v2
docker compose config --quiet
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/workers backend/tests/test_dow_monitor_half_hour_ai_worker.py docker-compose.yml .env.example
git commit -m "feat: schedule isolated half-hour AI analysis"
```

## Task 5: Expose lightweight overview and on-demand history/detail APIs

**Files:**

- Modify: `backend/app/services/dow_monitor_models.py`
- Modify: `backend/app/services/dow_monitor_service.py`
- Modify: `backend/app/api/dow_monitor.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/test_dow_monitor_api.py`
- Create: `backend/tests/test_dow_monitor_half_hour_ai_api.py`

- [ ] **Step 1: Write failing API and non-regression tests**

Required routes:

```text
GET /api/dow-monitor/{symbol}/ai-analyses?trade_date=YYYY-MM-DD
GET /api/dow-monitor/{symbol}/ai-analyses/{analysis_id}
```

Test:

- history is newest-first and scoped to normalized symbol/market;
- detail refuses an `analysis_id` owned by another symbol;
- overview contains only a lightweight latest summary;
- missing table/provider degrades to `unavailable` without breaking monitor overview;
- long narrative and `input_snapshot` never appear in overview;
- existing formal signal response is byte-for-byte unchanged when the repository is empty or unavailable.

Run:

```powershell
uv run pytest tests/test_dow_monitor_half_hour_ai_api.py tests/test_dow_monitor_api.py -q
```

Expected: FAIL.

- [ ] **Step 2: Add a read-only repository to the panel composition root**

`backend/app/main.py` may construct and schema-check `DowMonitorHalfHourAiRepository`, then inject it into the monitor service/router. It must not import or start the worker loop.

If schema initialization or ClickHouse reads fail, capture a concise availability state and keep all current monitor endpoints operational.

- [ ] **Step 3: Add lightweight latest summaries to overview**

Batch-load latest summaries for visible symbols after the real-time evaluation result is assembled. Add only:

```json
{
  "half_hour_ai_analysis": {
    "status": "completed",
    "analysis_id": "...",
    "window_end": "2026-07-31T23:00:00+08:00",
    "title": "量价背离仍待确认",
    "summary": "价格回升但主动资金持续性不足"
  }
}
```

Do not block or fail the overview when the AI repository is slow/unavailable. Apply a short read timeout and return `status="unavailable"` when needed.

- [ ] **Step 4: Implement history/detail routes**

Declare the `/ai-analyses` routes before the generic `/{symbol}` route so FastAPI cannot consume the suffix incorrectly. Validate `trade_date`, normalize the symbol through the existing rules, and return 404 for cross-symbol or missing IDs.

- [ ] **Step 5: Run API and real-time regression tests**

```powershell
uv run pytest tests/test_dow_monitor_half_hour_ai_api.py tests/test_dow_monitor_api.py tests/test_dow_monitor_minute_result_calculator.py -q
```

Expected: PASS, including the formal-signal immutability assertions.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/services/dow_monitor_models.py backend/app/services/dow_monitor_service.py backend/app/api/dow_monitor.py backend/app/main.py backend/tests/test_dow_monitor_api.py backend/tests/test_dow_monitor_half_hour_ai_api.py
git commit -m "feat: expose monitor AI analysis history"
```

## Task 6: Add independent desktop/mobile entry and long-form dialog

**Files:**

- Modify: `frontend/src/components/dow-monitor/types.ts`
- Modify: `frontend/src/components/dow-monitor/api.ts`
- Modify: `frontend/src/components/dow-monitor/useDowMonitor.ts`
- Create: `frontend/src/components/dow-monitor/DowMonitorHalfHourAiButton.tsx`
- Create: `frontend/src/components/dow-monitor/DowMonitorAiAnalysisDialog.tsx`
- Create: `frontend/src/components/dow-monitor/DowMonitorHalfHourAiButton.test.tsx`
- Create: `frontend/src/components/dow-monitor/DowMonitorAiAnalysisDialog.test.tsx`
- Modify: `frontend/src/components/dow-monitor/DowMonitorList.tsx`
- Modify: `frontend/src/components/dow-monitor/DowMonitorMobileRow.tsx`

- [ ] **Step 1: Write failing component tests**

Test:

- desktop shows a separate compact “半小时分析” column/entry;
- mobile renders the entry as the third row below key interpretation;
- `pending`, `running`, `completed`, `failed`, `insufficient_data`, and `unavailable` have distinct labels;
- full detail is not fetched until the dialog opens;
- history can switch among today's completed checkpoints;
- dialog closing returns focus to the trigger;
- real-time key interpretation text remains in its own component and is unchanged.

Run:

```powershell
Set-Location E:\my_project\.worktrees\tickflow-monitor-list-v2\frontend
pnpm test -- --run src/components/dow-monitor/DowMonitorHalfHourAiButton.test.tsx src/components/dow-monitor/DowMonitorAiAnalysisDialog.test.tsx
```

Expected: FAIL.

- [ ] **Step 2: Add API types and lazy hooks**

Add:

```typescript
export interface DowMonitorHalfHourAiSummary {
  analysis_id: string | null
  status: 'pending' | 'running' | 'completed' | 'failed' | 'insufficient_data' | 'unavailable'
  window_end: string | null
  title: string | null
  summary: string | null
}

export interface DowMonitorHalfHourAiAnalysis extends DowMonitorHalfHourAiSummary {
  conclusion: string | null
  evidence: Array<{ metric_key: string; label: string; value: string; meaning: string }>
  risks: string[]
  scenarios: Array<{ condition: string; implication: string; invalidates_when: string }>
  data_quality: string[]
}
```

Implement history/detail query hooks with `enabled: dialogOpen`. Do not add the full analysis to the overview query.

- [ ] **Step 3: Build the independent entry**

The compact entry should show only:

- latest completed checkpoint in Beijing time;
- status;
- title or concise fallback;
- an “查看分析” action when detail exists.

It must be visually separated from the real-time interpretation. Desktop uses a narrow independent column; mobile uses the `auxiliaryAction`/third-row slot created by the mobile plan.

- [ ] **Step 4: Build the dialog**

Use the project's existing dialog primitives and render:

1. checkpoint/history selector;
2. data cutoff and coverage quality;
3. conclusion;
4. evidence cards with exact backend values;
5. risks;
6. conditional scenarios and invalidation conditions;
7. data-quality notes;
8. an advisory disclaimer.

Keep long content scrollable inside the viewport. Do not open a browser popup. On mobile use nearly full-screen width/height.

- [ ] **Step 5: Run frontend tests**

```powershell
pnpm test -- --run src/components/dow-monitor/DowMonitorHalfHourAiButton.test.tsx src/components/dow-monitor/DowMonitorAiAnalysisDialog.test.tsx
pnpm build
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add frontend/src/components/dow-monitor
git commit -m "feat: add independent monitor AI analysis view"
```

## Task 7: Complete traceability and independent requirements-to-evidence review

**Files:**

- Modify: `docs/traceability.yaml`
- Create: `docs/acceptance/dow-monitor-half-hour-ai-analysis.md`
- Create: `docs/reviews/2026-07-31-dow-monitor-half-hour-ai-analysis-review.md`
- Modify: `E:/Obsidian-alwin/alwin/longbridge-stock/dow-monitor-system-api-runbook.md`

- [ ] **Step 1: Record requirement traceability**

For `REQ-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001` and `REQ-DOW-MONITOR-HALF-HOUR-AI-VIEW-001`, record:

- authoritative spec path;
- implementation files;
- executable tests;
- acceptance evidence path;
- production verification evidence.

Do not cite snapshot/golden output as semantic acceptance.

- [ ] **Step 2: Run lower-layer acceptance before UI acceptance**

Execute in order:

```powershell
Set-Location E:\my_project\.worktrees\tickflow-monitor-list-v2\backend
uv run pytest tests/test_dow_monitor_half_hour_ai_calendar.py tests/test_dow_monitor_half_hour_ai_repository.py tests/test_dow_monitor_half_hour_ai_snapshot.py tests/test_dow_monitor_half_hour_ai_prompt.py tests/test_dow_monitor_half_hour_ai_worker.py -q
uv run pytest tests/test_dow_monitor_half_hour_ai_api.py tests/test_dow_monitor_api.py tests/test_dow_monitor_minute_result_calculator.py -q

Set-Location E:\my_project\.worktrees\tickflow-monitor-list-v2\frontend
pnpm test -- --run src/components/dow-monitor/DowMonitorHalfHourAiButton.test.tsx src/components/dow-monitor/DowMonitorAiAnalysisDialog.test.tsx
pnpm build
```

Record command, timestamp, result, and relevant assertions in the acceptance document.

- [ ] **Step 3: Perform an independent requirements-to-evidence review**

Review each requirement without relying on the implementation author's conclusion. Explicitly verify:

- calendar behavior for CN/HK/US, holidays, lunch, DST, and half-days;
- exact cutoff and absence of future rows;
- new-symbol first checkpoint;
- worker/container isolation;
- no formal-signal or real-time interpretation mutation;
- permanent history/no TTL;
- overview payload remains light;
- dialog provides all saved checkpoints;
- desktop/mobile separation;
- degraded behavior when AI or ClickHouse is unavailable.

Record pass/fail and concrete evidence in the review document. Any failure returns to the relevant task before deployment.

- [ ] **Step 4: Update the system runbook**

Document:

- AI worker container and environment variables;
- ClickHouse table and no-TTL policy;
- schedule semantics and exchange calendars;
- API paths and overview lightweight field;
- desktop/mobile entry and dialog verification;
- logs, retry behavior, and degraded-mode checks;
- confirmation that 3018 does not run the worker.

- [ ] **Step 5: Run specification compliance**

```powershell
Set-Location E:\my_project\.worktrees\tickflow-monitor-list-v2
python scripts/check_spec_compliance.py
```

Expected: PASS.

- [ ] **Step 6: Commit documentation evidence**

```powershell
git add docs/traceability.yaml docs/acceptance/dow-monitor-half-hour-ai-analysis.md docs/reviews/2026-07-31-dow-monitor-half-hour-ai-analysis-review.md
git commit -m "docs: verify half-hour monitor AI analysis"

Set-Location E:\Obsidian-alwin\alwin\longbridge-stock
git add dow-monitor-system-api-runbook.md
git commit -m "docs: document monitor AI analysis worker"
```

## Task 8: Integrate with the other approved plans and deploy to 192.168.10.28

Do this only after the data-safety/backfill and mobile/version plans have passed their own lower-layer acceptance.

- [ ] **Step 1: Run the combined regression suite**

```powershell
Set-Location E:\my_project\.worktrees\tickflow-monitor-list-v2\backend
uv run pytest tests/test_dow_monitor_api.py tests/test_dow_monitor_bars.py tests/test_dow_monitor_client.py tests/test_dow_monitor_half_hour_ai_calendar.py tests/test_dow_monitor_half_hour_ai_repository.py tests/test_dow_monitor_half_hour_ai_snapshot.py tests/test_dow_monitor_half_hour_ai_prompt.py tests/test_dow_monitor_half_hour_ai_worker.py tests/test_dow_monitor_half_hour_ai_api.py -q

Set-Location E:\my_project\.worktrees\tickflow-monitor-list-v2\frontend
pnpm test -- --run
pnpm build

Set-Location E:\my_project\.worktrees\tickflow-monitor-list-v2
docker compose config --quiet
git diff --check
```

Expected: PASS.

- [ ] **Step 2: Deploy only to the approved remote host**

Use the repository's established 192.168.10.28 deployment procedure from the runbook. Do not publish a local production instance. Build the exact committed source state, deploy the saved artifact/image, and bring up both:

- the existing 3018 panel service;
- the new unexposed AI worker service.

- [ ] **Step 3: Verify production semantics**

Verify on 192.168.10.28:

```text
GET /health
GET /api/dow-monitor/overview
GET /api/dow-monitor/RNG.US/ai-analyses?trade_date=<current-market-date>
```

Also verify:

- build ID matches the deployed commit;
- RNG.US no longer produces daily-evaluation HTTP 400;
- a new test symbol reports history warmup status and receives usable structure;
- desktop and mobile layouts match their respective requirements;
- version mismatch behavior works with an older open tab;
- the worker has exactly one active process and no public port;
- a due in-session checkpoint persists one logical AI row;
- opening the entry fetches full analysis on demand;
- formal signal and real-time interpretation remain unchanged before/after the AI row;
- WebSocket freshness and queue depth remain healthy while backfill and AI persistence run.

Record redacted requests, responses, container status, ClickHouse counts, and timestamps in all three acceptance evidence documents and the runbook.

- [ ] **Step 4: Run specification compliance**

```powershell
Set-Location E:\my_project\.worktrees\tickflow-monitor-list-v2
python scripts/check_spec_compliance.py
```

Expected: PASS with both AI requirement IDs mapped to implementation, executable tests, semantic acceptance, and independent review evidence.

- [ ] **Step 5: Final independent review**

Repeat the combined requirements-to-evidence review across all six approved requirement IDs. Do not declare completion if any lower-layer semantic evidence is missing, even if UI screenshots or snapshot tests pass.
