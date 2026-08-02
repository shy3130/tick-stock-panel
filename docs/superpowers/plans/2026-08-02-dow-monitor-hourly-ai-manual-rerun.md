# Dow Monitor Hourly AI Manual Rerun Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a durable `重跑AI分析` action that asynchronously regenerates the currently selected hourly report and replaces it only after a successful validated model result.

**Architecture:** The 3018 API writes a deduplicated manual request to a ClickHouse control/audit table. The existing independent AI worker consumes one request per cycle, rebuilds the original checkpoint with its fixed cutoff, and atomically exposes the replacement only on success. React Query submits and polls the request while keeping the old report visible.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, ClickHouse `ReplacingMergeTree`, pytest, React 18, TypeScript, TanStack Query, Vitest, Testing Library.

## Global Constraints

- Requirement ID is `REQ-DOW-MONITOR-HOURLY-AI-MANUAL-RERUN-001` under `SPEC-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001`.
- Manual rerun MAY execute outside regular exchange hours only for an already stored hourly checkpoint.
- The server MUST derive trade date, checkpoint, and cutoff from the stored report; the client cannot supply them.
- A later bar MUST NOT influence the regenerated report.
- A failed rerun MUST preserve the prior report.
- Only one active request and one model call are permitted per analysis; default model concurrency remains one.
- Legacy 30-minute reports remain read-only.
- The API process MUST NOT call the model or build the AI snapshot.
- Realtime interpretation, formal signals, minute persistence, and WebSocket ingestion remain unchanged.
- Do not stage `.playwright-cli/`, `.superpowers/brainstorm/`, or `output/`.
- Update `E:/Obsidian-alwin/alwin/longbridge-stock/dow-monitor-system-api-runbook.md` in the same task.

---

## File structure

| File | Responsibility |
| --- | --- |
| `docs/decisions/2026-08-02-dow-monitor-hourly-ai-manual-rerun-session-exception.md` | Records the user's off-session manual-retry ruling. |
| `docs/specs/dow-monitor-half-hour-ai-analysis.md` | Owns the new normative MUST behavior. |
| `backend/app/services/dow_monitor_half_hour_ai_models.py` | Defines persisted rerun request types. |
| `backend/app/services/dow_monitor_half_hour_ai_repository.py` | Owns the report and rerun-request ClickHouse contracts. |
| `backend/app/services/dow_monitor_service.py` | Validates user submission against monitored symbol and stored report. |
| `backend/app/api/dow_monitor.py` | Exposes thin POST/GET control endpoints. |
| `backend/app/workers/dow_monitor_half_hour_ai.py` | Executes exact-checkpoint reruns outside the API process. |
| `frontend/src/components/dow-monitor/types.ts` | Defines frontend request status contracts. |
| `frontend/src/lib/api.ts` | Provides POST/GET request functions. |
| `frontend/src/lib/queryKeys.ts` | Isolates rerun status by symbol and analysis ID. |
| `frontend/src/components/dow-monitor/useDowMonitor.ts` | Owns mutation, polling, and cache refresh. |
| `frontend/src/components/dow-monitor/DowMonitorAiAnalysisDialog.tsx` | Renders confirmation, state, success, and retryable failure. |
| `tests/backend/test_dow_monitor_half_hour_ai.py` | Proves repository, worker, service, and API semantics. |
| `frontend/src/components/dow-monitor/DowMonitorHalfHourAiButton.test.tsx` | Proves selected-tab UI and request lifecycle. |

---

### Task 1: Establish authority and traceability

**Files:**
- Create: `docs/decisions/2026-08-02-dow-monitor-hourly-ai-manual-rerun-session-exception.md`
- Create: `docs/acceptance/dow-monitor-hourly-ai-manual-rerun.md`
- Create: `docs/reviews/2026-08-02-dow-monitor-hourly-ai-manual-rerun-review.md`
- Modify: `docs/specs/dow-monitor-half-hour-ai-analysis.md`
- Modify: `docs/spec-index.yaml`
- Modify: `docs/traceability.yaml`
- Modify: `tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py`

**Interfaces:**
- Consumes: approved design `docs/superpowers/specs/2026-08-02-dow-monitor-hourly-ai-manual-rerun-design.md`.
- Produces: authoritative `REQ-DOW-MONITOR-HOURLY-AI-MANUAL-RERUN-001` and indexed decision `DEC-20260802-DOW-MONITOR-HOURLY-AI-MANUAL-RERUN-SESSION-001`.

- [ ] **Step 1: Add the failing specification contract**

Extend `REQUIREMENTS` and add:

```python
MANUAL_RERUN_DESIGN_PATH = (
    "docs/superpowers/specs/2026-08-02-"
    "dow-monitor-hourly-ai-manual-rerun-design.md"
)
MANUAL_RERUN_DECISION_PATH = (
    "docs/decisions/2026-08-02-"
    "dow-monitor-hourly-ai-manual-rerun-session-exception.md"
)

def test_manual_hourly_ai_rerun_is_authoritative_and_traced() -> None:
    spec = Path(SPEC_PATH).read_text(encoding="utf-8")
    index = yaml.safe_load(Path("docs/spec-index.yaml").read_text(encoding="utf-8"))
    traceability = yaml.safe_load(
        Path("docs/traceability.yaml").read_text(encoding="utf-8")
    )
    requirement = "REQ-DOW-MONITOR-HOURLY-AI-MANUAL-RERUN-001"
    assert requirement in spec
    assert Path(MANUAL_RERUN_DESIGN_PATH).is_file()
    assert Path(MANUAL_RERUN_DECISION_PATH).is_file()
    assert requirement in next(
        item["requirements"]
        for item in index["specifications"]
        if item["id"] == SPECIFICATION
    )
    traced = next(
        item for item in traceability["requirements"]
        if item["id"] == requirement
    )
    assert traced["specification"] == SPECIFICATION
```

- [ ] **Step 2: Run the contract and observe RED**

Run:

```powershell
python -m pytest tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py -q
```

Expected: FAIL because the decision file and requirement mapping do not exist.

- [ ] **Step 3: Record the authoritative exception and requirement**

The decision document MUST state that scheduled analysis remains session-bound,
while a user-requested retry of an existing hourly checkpoint may run at any
time with the original cutoff. Append the following normative section to the
parent specification:

```markdown
## REQ-DOW-MONITOR-HOURLY-AI-MANUAL-RERUN-001

An authenticated user MAY request regeneration of the currently selected,
already stored hourly analysis. The request MUST be durable, deduplicated per
analysis while active, and executed by the independent AI worker. Manual retry
MAY run outside regular exchange hours, but MUST reuse the stored trade date,
window end and data cutoff and MUST NOT read observations after that cutoff.

The previous report MUST remain readable until a validated replacement is
stored under the same logical key. Failure MUST preserve the previous report.
Legacy 30-minute analyses MUST remain read-only. The API process MUST NOT call
the model, and this path MUST NOT mutate realtime interpretation, formal
signals, minute persistence or WebSocket ingestion.
```

Add the requirement to the existing specification entry in `spec-index.yaml`.
Add traceability using the production, test, acceptance, and review paths from
this plan. Create the acceptance and review files with `Status: pending` and
the exact semantic criteria from the design; do not claim passing evidence yet.

- [ ] **Step 4: Run authority checks**

Run:

```powershell
python -m pytest tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py -q
python scripts/check_spec_compliance.py
```

Expected: the contract and compliance checker pass with the new stable ID.

- [ ] **Step 5: Commit the authority gate**

```powershell
git add -- docs/decisions/2026-08-02-dow-monitor-hourly-ai-manual-rerun-session-exception.md docs/acceptance/dow-monitor-hourly-ai-manual-rerun.md docs/reviews/2026-08-02-dow-monitor-hourly-ai-manual-rerun-review.md docs/specs/dow-monitor-half-hour-ai-analysis.md docs/spec-index.yaml docs/traceability.yaml tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py
git commit -m "docs(dow-monitor): authorize manual hourly AI reruns"
```

### Task 2: Add durable rerun request models and repository

**Files:**
- Modify: `backend/app/services/dow_monitor_half_hour_ai_models.py`
- Modify: `backend/app/services/dow_monitor_half_hour_ai_repository.py`
- Modify: `tests/backend/test_dow_monitor_half_hour_ai.py`

**Interfaces:**
- Consumes: existing `HalfHourAiAnalysis` and ClickHouse execute/query adapters.
- Produces: `HourlyAiRerunRequest`, `save_rerun_request()`, `latest_rerun_request()`, `active_rerun_request()`, and `next_runnable_rerun()`.

- [ ] **Step 1: Write failing model and repository tests**

Add tests that instantiate the requested contract and verify serialization,
active deduplication, FIFO queued selection, and stale-running recovery:

```python
def test_rerun_repository_round_trips_request_and_finds_active() -> None:
    request = HourlyAiRerunRequest(
        request_id="rerun-1",
        analysis_id="analysis-1",
        market="us",
        symbol="RNG.US",
        trade_date=date(2026, 7, 31),
        window_end=beijing("2026-07-31T23:00:00"),
        data_cutoff=beijing("2026-07-31T23:00:00"),
        status="queued",
        requested_at=beijing("2026-08-02T10:00:00"),
        updated_at=beijing("2026-08-02T10:00:00"),
    )
    repository.save_rerun_request(request)
    assert repository.active_rerun_request("analysis-1") == request
    assert repository.next_runnable_rerun(
        now=beijing("2026-08-02T10:01:00"),
        stale_after=timedelta(minutes=10),
    ) == request
```

Also assert `create_rerun_table_sql` contains
`lb_dow_monitor_hourly_ai_rerun_requests`, `ReplacingMergeTree(updated_at)`,
and `ORDER BY request_id`.

- [ ] **Step 2: Run the focused tests and observe RED**

```powershell
python -m pytest tests/backend/test_dow_monitor_half_hour_ai.py -k "rerun_repository" -q
```

Expected: collection or assertion failure because the request model and methods
do not exist.

- [ ] **Step 3: Implement the minimal persisted request contract**

Add:

```python
HourlyAiRerunStatus = Literal["queued", "running", "completed", "failed"]

class HourlyAiRerunRequest(BaseModel):
    request_id: str
    analysis_id: str
    market: Literal["cn", "hk", "us"]
    symbol: str
    trade_date: date
    window_end: datetime
    data_cutoff: datetime
    status: HourlyAiRerunStatus
    requested_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime
    error_code: str | None = None
    error_message: str | None = None
```

Add a second schema property/table and repository methods. Every timestamp
written to ClickHouse must pass through `_utc_text`; every row read must pass
through `_with_utc_datetimes`. `next_runnable_rerun()` selects the oldest
`queued` row or a `running` row whose `updated_at <= now - stale_after`.

- [ ] **Step 4: Run repository tests GREEN**

```powershell
python -m pytest tests/backend/test_dow_monitor_half_hour_ai.py -k "repository or rerun" -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit persistence**

```powershell
git add -- backend/app/services/dow_monitor_half_hour_ai_models.py backend/app/services/dow_monitor_half_hour_ai_repository.py tests/backend/test_dow_monitor_half_hour_ai.py
git commit -m "feat(dow-monitor): persist hourly AI rerun requests"
```

### Task 3: Add thin submission and status APIs

**Files:**
- Modify: `backend/app/services/dow_monitor_service.py`
- Modify: `backend/app/api/dow_monitor.py`
- Modify: `tests/backend/test_dow_monitor_half_hour_ai.py`

**Interfaces:**
- Consumes: `active_rerun_request()` and `save_rerun_request()` from Task 2.
- Produces: `request_hourly_ai_rerun(market, symbol, analysis_id)` and `get_hourly_ai_rerun(market, symbol, analysis_id)`, plus POST/GET route responses.

- [ ] **Step 1: Write failing API behavior tests**

Add a FastAPI test with one enabled monitored symbol and one completed hourly
analysis. Assert:

```python
response = client.post(
    "/api/dow-monitor/RNG.US/ai-analyses/analysis-1/rerun"
)
assert response.status_code == 202
assert response.json()["request"]["analysis_id"] == "analysis-1"
assert response.json()["request"]["status"] == "queued"
assert response.json()["deduplicated"] is False

duplicate = client.post(
    "/api/dow-monitor/RNG.US/ai-analyses/analysis-1/rerun"
)
assert duplicate.status_code == 200
assert duplicate.json()["request"]["request_id"] == response.json()["request"]["request_id"]
assert duplicate.json()["deduplicated"] is True
```

Add 404 mismatch, 409 legacy report, 409 disabled symbol, 503 repository, and
`GET` returning `{ "request": null }` cases.

- [ ] **Step 2: Run the API tests and observe RED**

```powershell
python -m pytest tests/backend/test_dow_monitor_half_hour_ai.py -k "manual_rerun_api" -q
```

Expected: 404 because the POST/GET routes do not exist.

- [ ] **Step 3: Implement service validation and thin routes**

Use `uuid4().hex` for a new request ID and `self._now()` for timestamps. Derive
all market/checkpoint fields from `get_by_id(analysis_id)`. Find the monitored
symbol in `store.list_symbols()` and require both matching market and
`enabled=True`. Return an existing queued/running request before creating one.

Add route handlers:

```python
@router.post("/{symbol}/ai-analyses/{analysis_id}/rerun")
def rerun_hourly_ai(symbol: str, analysis_id: str, request: Request) -> Response:
    normalized, market = _symbol_and_market(symbol)
    result, deduplicated = _service(request).request_hourly_ai_rerun(
        market, normalized, analysis_id
    )
    return JSONResponse(
        {"request": result, "deduplicated": deduplicated},
        status_code=200 if deduplicated else 202,
    )

@router.get("/{symbol}/ai-analyses/{analysis_id}/rerun")
def hourly_ai_rerun_status(symbol: str, analysis_id: str, request: Request) -> dict:
    normalized, market = _symbol_and_market(symbol)
    return {"request": _service(request).get_hourly_ai_rerun(
        market, normalized, analysis_id
    )}
```

Map explicit service exceptions to 404, 409, and 503; do not expose raw
ClickHouse or provider errors.

- [ ] **Step 4: Run API and existing detail tests GREEN**

```powershell
python -m pytest tests/backend/test_dow_monitor_half_hour_ai.py -k "api or overview_is_lightweight" -q
```

Expected: all selected tests pass and existing GET detail behavior is unchanged.

- [ ] **Step 5: Commit API control plane**

```powershell
git add -- backend/app/services/dow_monitor_service.py backend/app/api/dow_monitor.py tests/backend/test_dow_monitor_half_hour_ai.py
git commit -m "feat(dow-monitor): queue selected hourly AI reruns"
```

### Task 4: Execute manual requests in the independent worker

**Files:**
- Modify: `backend/app/workers/dow_monitor_half_hour_ai.py`
- Modify: `tests/backend/test_dow_monitor_half_hour_ai.py`

**Interfaces:**
- Consumes: `next_runnable_rerun()`, stored analysis identity, existing snapshot builder, offline bootstrap, and prompt service.
- Produces: `run_next_manual_rerun(now)` and success-only replacement semantics.

- [ ] **Step 1: Write failing worker behavior tests**

Add separate tests proving:

```python
assert await worker.run_next_manual_rerun(off_session_now) == 1
replacement = repository.saved[-1]
assert replacement.analysis_id == previous.analysis_id
assert replacement.window_end == previous.window_end
assert replacement.data_cutoff == previous.data_cutoff
assert replacement.attempt == previous.attempt + 1
assert prompt.snapshots[-1].latest_price == price_at_cutoff
```

Include a row one minute after cutoff and assert it is excluded. In a prompt
failure test, assert `repository.saved` receives no replacement and the prior
completed report remains returned by `get_by_id`; only the request becomes
`failed`. Add a stale-running recovery test and prove one failing request does
not prevent the scheduled scan in the same cycle.

- [ ] **Step 2: Run worker tests and observe RED**

```powershell
python -m pytest tests/backend/test_dow_monitor_half_hour_ai.py -k "manual_rerun_worker" -q
```

Expected: failure because the worker has no manual request consumer.

- [ ] **Step 3: Implement success-only regeneration**

Add `run_next_manual_rerun(now)` and call it once before `run_due_jobs()` scans
scheduled symbols. It must not use the calendar's session gate. Resolve the
stored source analysis and monitored symbol, then use its exact `window_end`
and `data_cutoff`. Reuse the scheduled snapshot builder and bounded offline
bootstrap, but keep request status separate from report status.

The critical persistence branch is:

```python
try:
    parsed = await self._prompt_service.analyze(snapshot)
except Exception as exc:
    self._analysis_repository.save_rerun_request(
        request.model_copy(update={
            "status": "failed",
            "completed_at": current,
            "updated_at": current,
            "error_code": type(exc).__name__,
            "error_message": str(exc)[:500],
        })
    )
    return 0

self._analysis_repository.save(
    self._record(
        source.analysis_id,
        source.market,
        source.symbol,
        source.trade_date,
        source.window_end,
        snapshot,
        status="completed",
        attempt=source.attempt + 1,
        report=parsed.report,
        title=parsed.title,
        summary=parsed.summary,
        conclusion=parsed.conclusion,
        evidence=parsed.evidence,
        risks=parsed.risks,
        scenarios=parsed.scenarios,
        data_quality=parsed.data_quality,
    )
)
```

Only after the report save succeeds may the request become `completed`.
Snapshot/data errors follow the same request-only failure rule.

- [ ] **Step 4: Run the full worker module tests GREEN**

```powershell
python -m pytest tests/backend/test_dow_monitor_half_hour_ai.py -q
python -m pytest tests/backend/test_dow_monitor_offline_ai_bootstrap_integration.py -q
```

Expected: all tests pass; scheduled cadence and offline bootstrap remain green.

- [ ] **Step 5: Commit worker execution**

```powershell
git add -- backend/app/workers/dow_monitor_half_hour_ai.py tests/backend/test_dow_monitor_half_hour_ai.py
git commit -m "feat(dow-monitor): execute manual hourly AI reruns"
```

### Task 5: Add the selected-report rerun interaction

**Files:**
- Modify: `frontend/src/components/dow-monitor/types.ts`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/lib/queryKeys.ts`
- Modify: `frontend/src/components/dow-monitor/useDowMonitor.ts`
- Modify: `frontend/src/components/dow-monitor/DowMonitorAiAnalysisDialog.tsx`
- Modify: `frontend/src/components/dow-monitor/DowMonitorHalfHourAiButton.test.tsx`
- Modify: `tests/frontend/test_dow_monitor_half_hour_ai_frontend.py`

**Interfaces:**
- Consumes: Task 3 POST/GET response contract.
- Produces: `DowMonitorHourlyAiRerunRequest`, `useDowMonitorAiRerunStatus()`, `useRerunDowMonitorAi()`, and visible `重跑AI分析` states.

- [ ] **Step 1: Write the failing selected-tab UI tests**

Mock the two new API functions. Open the dialog, select the second hourly tab,
confirm the action, and assert:

```tsx
expect(window.confirm).toHaveBeenCalledWith(
  '将重新分析当前时间点。新报告成功后会替换当前报告，是否继续？',
)
expect(api.rerunDowMonitorAi).toHaveBeenCalledWith('RNG.US', 'analysis-2')
expect(screen.getByRole('button', { name: '重跑AI分析' })).toBeDisabled()
expect(screen.getByText('排队中')).toBeInTheDocument()
```

Add cases for no button on `half_hour`, failed status retaining report text and
showing `重跑失败，可再次尝试`, and completed polling causing detail/history/
overview queries to refetch.

- [ ] **Step 2: Run the component test and observe RED**

```powershell
pnpm --dir frontend exec vitest run src/components/dow-monitor/DowMonitorHalfHourAiButton.test.tsx
```

Expected: no `重跑AI分析` button exists.

- [ ] **Step 3: Add frontend contracts and hooks**

Define:

```ts
export interface DowMonitorHourlyAiRerunRequest {
  request_id: string
  analysis_id: string
  market: DowMonitorSymbolMarket
  symbol: string
  trade_date: string
  window_end: string
  data_cutoff: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  requested_at: string
  started_at: string | null
  completed_at: string | null
  updated_at: string
  error_code: string | null
  error_message: string | null
}
```

Add API methods `rerunDowMonitorAi(symbol, analysisId)` and
`dowMonitorAiRerunStatus(symbol, analysisId)`. Add query key
`QK.dowMonitorAiRerun(symbol, analysisId)`. Poll every 2,000 ms only for queued
or running. On completed, invalidate the exact detail, exact trade-date history,
and prefix `['dow-monitor', 'overview']` once per request ID.

- [ ] **Step 4: Render confirmation and lifecycle state**

In the dialog, derive the selected history item by `selectedId`. Render the
button only when `report_frequency === 'hourly'`. Use these exact labels:

```ts
const label = mutation.isPending
  ? '提交中'
  : request?.status === 'queued'
    ? '排队中'
    : request?.status === 'running'
      ? '重跑中'
      : request?.status === 'completed'
        ? '已更新'
        : '重跑AI分析'
```

Disable only submission/queued/running. Keep the report component mounted in
every request state. Wrap failure text with `break-words` for mobile.

- [ ] **Step 5: Run frontend tests GREEN**

```powershell
pnpm --dir frontend exec vitest run src/components/dow-monitor/DowMonitorHalfHourAiButton.test.tsx src/components/dow-monitor/DowMonitorAiStageReport.test.tsx
python -m pytest tests/frontend/test_dow_monitor_half_hour_ai_frontend.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit the UI**

```powershell
git add -- frontend/src/components/dow-monitor/types.ts frontend/src/lib/api.ts frontend/src/lib/queryKeys.ts frontend/src/components/dow-monitor/useDowMonitor.ts frontend/src/components/dow-monitor/DowMonitorAiAnalysisDialog.tsx frontend/src/components/dow-monitor/DowMonitorHalfHourAiButton.test.tsx tests/frontend/test_dow_monitor_half_hour_ai_frontend.py
git commit -m "feat(dow-monitor): add selected hourly AI rerun action"
```

### Task 6: Verify semantics, document operations, and review independently

**Files:**
- Modify: `docs/acceptance/dow-monitor-hourly-ai-manual-rerun.md`
- Modify: `docs/reviews/2026-08-02-dow-monitor-hourly-ai-manual-rerun-review.md`
- Modify: `E:/Obsidian-alwin/alwin/longbridge-stock/dow-monitor-system-api-runbook.md`

**Interfaces:**
- Consumes: all implementation and executable tests from Tasks 1-5.
- Produces: semantic acceptance, independent requirements-to-evidence review, and deployment/rollback instructions. This task does not authorize production deployment.

- [ ] **Step 1: Run backend and contract verification**

```powershell
python -m pytest tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py tests/backend/test_dow_monitor_half_hour_ai.py tests/backend/test_dow_monitor_offline_ai_bootstrap_integration.py -q
python scripts/check_spec_compliance.py
```

Record exact counts and exit codes; do not mark acceptance passed if any command
fails.

- [ ] **Step 2: Run frontend verification and build**

```powershell
pnpm --dir frontend exec vitest run
pnpm --dir frontend build
python -m pytest tests/frontend/test_dow_monitor_half_hour_ai_frontend.py -q
```

Record exact test counts and the generated `DowMonitor-*.js` filename.

- [ ] **Step 3: Perform semantic acceptance**

Using a real stored hourly report in the release candidate environment, verify:

1. selected checkpoint submission creates exactly one request;
2. an off-session request starts without creating a new checkpoint;
3. current report remains visible through queued/running;
4. successful content replaces the same analysis ID and increments attempt;
5. input snapshot and report evidence contain nothing after original cutoff;
6. failure preserves the existing report;
7. desktop and narrow mobile controls remain usable;
8. 3018, 19912, AI worker, WebSocket, formal signals, and minute persistence
   show no restart or unintended mutation.

Write observed IDs, timestamps, hashes, and health results to the acceptance
document without including credentials or raw user data.

- [ ] **Step 4: Update the runbook**

Document the two API paths, ClickHouse request table, worker polling rule,
off-session exception, duplicate behavior, status diagnostics, release bundle
verification, rollback boundary, and the fact that rolling back 3018 must not
restart 19912 or the market-data WebSocket.

- [ ] **Step 5: Conduct independent requirements-to-evidence review**

Review each MUST in `REQ-DOW-MONITOR-HOURLY-AI-MANUAL-RERUN-001` against exact
implementation paths, executable test assertions, and semantic observations.
Explicitly reject the production bundle or a screenshot as standalone semantic
proof. Mark the review `可合并` only if there are no P0-P2 gaps.

- [ ] **Step 6: Run final repository checks**

```powershell
git diff --check
git status --short
git diff --stat
```

Confirm only planned files and the required external runbook changed; leave the
three pre-existing untracked directories untouched.

- [ ] **Step 7: Commit acceptance evidence**

```powershell
git add -- docs/acceptance/dow-monitor-hourly-ai-manual-rerun.md docs/reviews/2026-08-02-dow-monitor-hourly-ai-manual-rerun-review.md
git commit -m "docs(dow-monitor): verify manual hourly AI reruns"
```

Do not deploy or push unless the user separately authorizes that action.
