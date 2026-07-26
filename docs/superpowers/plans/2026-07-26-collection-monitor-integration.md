# Collection Monitor TickFlow Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy an observation-only collection-monitor page inside the existing TickFlow application on port 3018.

**Architecture:** TickFlow exposes four fixed, GET-only same-origin proxy routes to the Longbridge API configured by `LONGBRIDGE_API_URL`. A lazy-loaded React page uses those routes inside the existing authenticated router and navigation shell, preserving evidence state and provenance without mutation controls.

**Tech Stack:** FastAPI, httpx, pytest, React 18, TypeScript, TanStack Query, Vitest, Tailwind CSS, Docker Compose.

## Global Constraints

- Implement `REQ-COLLECTION-MONITOR-PROXY-001`, `REQ-COLLECTION-MONITOR-PAGE-001`, and `REQ-COLLECTION-MONITOR-PREACCEPTANCE-001`.
- Only four fixed GET routes are allowed; no arbitrary proxy and no mutation.
- Preserve unavailable/degraded/shadow/stale/live semantics and provenance; never fabricate fallback evidence.
- Keep alerts, actions, restarts, repairs, and schedule mutations out of scope.
- Deployment before Monday is observation-only and MUST NOT be recorded as live semantic acceptance.
- Do not restart or modify Longbridge collectors or Chronicle schedules.

---

### Task 1: Read-only backend proxy

**Files:**
- Create: `backend/app/api/collection_monitor.py`
- Modify: `backend/app/main.py`
- Create: `tests/backend/test_collection_monitor_proxy.py`

**Interfaces:**
- Consumes: `LONGBRIDGE_API_URL`, defaulting to `http://127.0.0.1:19912`.
- Produces: `GET /api/collection-monitor/overview`, `/markets/{market}`, `/tasks`, and `/gaps`.

- [ ] **Step 1: Write failing proxy tests**

Test fixed upstream paths and canonical query forwarding; reject invalid market,
date, status, technology, mode, dataset, symbol, limit, and offset; assert there
are no POST routes; assert upstream 503 becomes sanitized
`{"detail":"collection_monitoring_evidence_unavailable"}`; assert network,
non-JSON, and other upstream errors become a sanitized 502 without URL, body,
or credential leakage.

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest -q tests/backend/test_collection_monitor_proxy.py`

Expected: FAIL because `app.api.collection_monitor` does not exist.

- [ ] **Step 3: Implement the minimal proxy**

Create an `APIRouter(prefix="/api/collection-monitor")`. Use literal upstream
path constants, FastAPI `Query` constraints, a finite httpx timeout, a maximum
`limit` of 500, and an allowlist for every enum. Return parsed JSON only.
Translate evidence-unavailable 503 without changing its meaning and sanitize
all other failures. Register the router in `backend/app/main.py`.

- [ ] **Step 4: Run focused and adjacent tests**

Run:

```powershell
python -m pytest -q tests/backend/test_collection_monitor_proxy.py backend/tests/test_dow_strategy_proxy.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/api/collection_monitor.py backend/app/main.py tests/backend/test_collection_monitor_proxy.py
git commit -m "feat: add read-only collection monitor proxy"
```

### Task 2: Native monitoring page

**Files:**
- Create: `frontend/src/components/collection-monitor/types.ts`
- Create: `frontend/src/components/collection-monitor/useCollectionMonitor.ts`
- Create: `frontend/src/pages/CollectionMonitor.tsx`
- Create: `frontend/src/pages/CollectionMonitor.test.tsx`
- Modify: `frontend/src/router.tsx`
- Modify: `frontend/src/components/Layout.tsx`
- Modify: `frontend/src/pages/dow-monitor-route.test.tsx`

**Interfaces:**
- Consumes: Task 1's same-origin four-route API and the existing TickFlow QueryClient.
- Produces: lazy route `/collection-monitor` and desktop/mobile navigation label `采集监控`.

- [ ] **Step 1: Write failing page and route tests**

Use mocked fetch responses to assert the page renders the daily summary, CN/HK/US
market states, task rows, gap rows, provenance, and observation mode. Assert a
503 shows evidence unavailable without a healthy fallback. Assert
`/collection-monitor` exists and `采集监控` is reachable from desktop and mobile
navigation. Assert no restart, repair, acknowledge, schedule, or action control
is rendered.

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
pnpm --dir frontend test --run src/pages/CollectionMonitor.test.tsx src/pages/dow-monitor-route.test.tsx
```

Expected: FAIL because the page and route do not exist.

- [ ] **Step 3: Implement typed data access and page**

Define response types that preserve upstream field names and evidence states.
Implement TanStack queries with a 30-second refresh, date/market/status/
technology/dataset filters, fixed same-origin URLs, and no mutation hook.
Implement a responsive page using existing TickFlow surface, border, text,
success, warning, and danger tokens. Keep daily overview, market matrix, task
table, and gap table visible as separate evidence levels. Display timestamps,
mode, source/provenance, and last-confirmed evidence exactly as returned.

- [ ] **Step 4: Register route and navigation**

Lazy-load `CollectionMonitor` in `frontend/src/router.tsx`; add
`{ path: 'collection-monitor', element: <CollectionMonitor /> }`; add a
`采集监控` navigation item using `Activity` or `RadioTower` in
`frontend/src/components/Layout.tsx`.

- [ ] **Step 5: Run focused frontend tests and build**

Run:

```powershell
pnpm --dir frontend test --run src/pages/CollectionMonitor.test.tsx src/pages/dow-monitor-route.test.tsx
pnpm --dir frontend build
```

Expected: PASS and a successful Vite production build.

- [ ] **Step 6: Commit**

```powershell
git add frontend/src/components/collection-monitor frontend/src/pages/CollectionMonitor.tsx frontend/src/pages/CollectionMonitor.test.tsx frontend/src/router.tsx frontend/src/components/Layout.tsx frontend/src/pages/dow-monitor-route.test.tsx
git commit -m "feat: add collection monitoring page"
```

### Task 3: Contract evidence, independent review, and deployment

**Files:**
- Create: `tests/spec_contracts/test_collection_monitor_integration_contract.py`
- Modify: `docs/acceptance/collection-monitor-integration.md`
- Modify: `docs/reviews/collection-monitor-integration.md`
- Modify: `docs/traceability.yaml` only if implementation paths differ from the authoritative mapping.

**Interfaces:**
- Consumes: Tasks 1–2 plus the production Docker Compose deployment.
- Produces: executable traceability checks, pre-acceptance deployment evidence, and an independently reviewed deployment.

- [ ] **Step 1: Write and run the contract test**

Assert all three requirement IDs occur in the authoritative spec, spec index,
traceability file, acceptance record, and independent review; assert mapped
implementation/test files exist; assert the acceptance status remains pending.

Run:

```powershell
python -m pytest -q tests/spec_contracts/test_collection_monitor_integration_contract.py tests/spec_contracts/test_spec_guard_contract.py
```

Expected: PASS.

- [ ] **Step 2: Run the complete relevant suite**

Run:

```powershell
python -m pytest -q tests/backend/test_collection_monitor_proxy.py tests/spec_contracts/test_collection_monitor_integration_contract.py tests/spec_contracts/test_spec_guard_contract.py
pnpm --dir frontend test --run src/pages/CollectionMonitor.test.tsx src/pages/dow-monitor-route.test.tsx
pnpm --dir frontend build
```

Expected: PASS.

- [ ] **Step 3: Obtain independent requirements-to-evidence review**

The reviewer checks every requirement against implementation, executable tests,
and pending acceptance evidence; verifies that tests do not substitute for
Monday live semantics; and records findings in
`docs/reviews/collection-monitor-integration.md`.

- [ ] **Step 4: Back up and deploy only TickFlow**

On `192.168.10.28`, record the current source commit, image ID, compose
configuration, sanitized container configuration, health response, and
collector PIDs/restart counters in a timestamped mode-700 backup directory.
Build a versioned image from the reviewed commit, update only the TickFlow image
reference, and recreate only the TickFlow container without deleting volumes.
Keep the previous image reference as the exact rollback target.

- [ ] **Step 5: Verify production**

Verify HTTP 200 for `/`, `/health`, the existing `/dow-monitor?market=hk`, and
`/collection-monitor`; verify all four same-origin monitor API routes; verify
honest degraded/unavailable content; verify the TickFlow container remains
stable with no restart loop; verify Longbridge collector PIDs and restart
counters are unchanged. If any required check fails, restore the previous image
and verify rollback.

- [ ] **Step 6: Record pre-acceptance evidence and commit**

Record exact commit/image/container, checks, rollback target, and unchanged
collector evidence in `docs/acceptance/collection-monitor-integration.md`.
Keep status `pending live semantic acceptance`.

```powershell
git add tests/spec_contracts/test_collection_monitor_integration_contract.py docs/acceptance/collection-monitor-integration.md docs/reviews/collection-monitor-integration.md docs/traceability.yaml
git commit -m "docs: record collection monitor deployment evidence"
```

## Self-Review

- Spec coverage: all three requirements map to Tasks 1–3.
- Placeholder scan: no TBD/TODO or unspecified error-handling step remains.
- Type consistency: the frontend consumes exactly the four routes produced by
  Task 1; route and navigation both use `/collection-monitor`.
- Layer gate: deployment is explicitly pre-acceptance and Monday lower-layer
  semantic acceptance remains mandatory.
