# Dow Monitor Mobile Layout and Version Refresh Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Apply `spec-guard` before every production edit and stop if repository specifications conflict.

**Goal:** Deliver the approved mobile Scheme B for the trend monitor and reliably notify or safely refresh long-running pages after a new frontend release.

**Architecture:** Keep the existing desktop table unchanged and render a dedicated mobile row below the Tailwind `md` breakpoint using the already-derived row and interpretation objects. A root-level version guard compares the frontend build ID with the unauthenticated backend health response every 60 seconds; visible tabs prompt, while hidden idle tabs reload automatically.

**Tech Stack:** React 18, TypeScript 5.5, Tailwind CSS 3.4, TanStack Query 5, Vite 5, Vitest, Testing Library, Playwright, FastAPI, Docker Compose.

## Global Constraints

- Requirements: `REQ-DOW-MONITOR-FRONTEND-VERSION-REFRESH-001` and `REQ-DOW-MONITOR-MOBILE-COMPACT-LIST-001`.
- Mobile breakpoint is exactly `<768px`; desktop behavior begins at Tailwind `md`.
- Mobile primary order is stock identity, price/change, Mini daily trend, full-width key interpretation.
- Raw indicator columns may be hidden only in the mobile list; all remain reachable in details.
- Desktop keeps the complete table, 20 symbols per page, current column semantics, anomaly highlights, signals, and toggle behavior.
- No page-level horizontal scrolling at a 390px viewport.
- Version check interval is 60 seconds.
- Visible tabs prompt; hidden tabs auto-reload only when there are zero active mutations and no open dialog.
- A version endpoint failure must not affect market data or authentication.
- Update `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md`.

---

## File Structure

- Create `docs/specs/dow-monitor-mobile-version-refresh.md`: narrow authoritative spec.
- Create `frontend/src/components/dow-monitor/DowMonitorMobileRow.tsx`: mobile Scheme B row.
- Create `frontend/src/components/dow-monitor/DowMonitorMobileRow.test.tsx`: mobile content/accessibility tests.
- Modify `frontend/src/components/dow-monitor/DowMonitorList.tsx`: share already-derived data between desktop and mobile renderers.
- Modify `frontend/src/components/dow-monitor/DowMonitorList.test.tsx`: desktop regression and mobile wrapper.
- Modify `frontend/src/pages/DowMonitor.tsx`: responsive header/form and detail placement.
- Modify `frontend/src/pages/DowMonitor.test.tsx`: mobile page composition.
- Create `frontend/src/components/AppVersionGuard.tsx`: polling, prompt, safe hidden-tab reload.
- Create `frontend/src/components/AppVersionGuard.test.tsx`: fake-timer and visibility tests.
- Create `frontend/src/lib/appVersion.ts`: pure build comparison and refresh predicate.
- Create `frontend/src/lib/appVersion.test.ts`.
- Modify `frontend/src/main.tsx`: mount the guard inside QueryClient.
- Modify `frontend/src/vite-env.d.ts`: `VITE_BUILD_ID` typing.
- Modify `frontend/vite.config.ts`: deterministic build metadata.
- Modify `backend/app/api/routes.py`: health build metadata.
- Modify `backend/tests/test_routes.py` or create it if absent.
- Modify `Dockerfile`: pass one build ID to both stages.
- Modify `docker-compose.yml`: build arguments and runtime environment.
- Create `docs/acceptance/dow-monitor-mobile-version-refresh.md`.
- Create `docs/reviews/dow-monitor-mobile-version-refresh.md`.
- Modify `docs/spec-index.yaml` and `docs/traceability.yaml`.

## Interfaces

```typescript
// frontend/src/lib/appVersion.ts
export interface AppVersion {
  build_id: string
  published_at: string | null
}

export function isNewBuild(currentBuildId: string, remote: AppVersion): boolean
export function mayAutoReload(input: {
  visibility: DocumentVisibilityState
  activeMutations: number
  dialogOpen: boolean
}): boolean
```

```typescript
// frontend/src/components/dow-monitor/DowMonitorMobileRow.tsx
export interface DowMonitorMobileRowProps {
  item: DowMonitorOverviewSymbol
  row: MonitorListRow
  interpretation: KeyInterpretation
  selected: boolean
  pendingToggle: boolean
  pendingRemoval: boolean
  onSelect(symbol: string): void
  onToggle(symbol: string, enabled: boolean): void
  onRemove(symbol: string): void
  auxiliaryAction?: ReactNode
}
```

The `auxiliaryAction` slot is intentionally optional. The half-hour AI plan will fill it without coupling the mobile layout to AI transport.

### Task 1: Register the Mobile and Version Specification

**Files:**
- Create: `docs/specs/dow-monitor-mobile-version-refresh.md`
- Modify: `docs/spec-index.yaml`

**Interfaces:**
- Consumes: approved design sections for Scheme B and version refresh.
- Produces: `SPEC-DOW-MONITOR-MOBILE-VERSION-REFRESH-001`.

- [ ] **Step 1: Write the local authoritative spec**

Declare exactly:

```yaml
requirements:
  - REQ-DOW-MONITOR-FRONTEND-VERSION-REFRESH-001
  - REQ-DOW-MONITOR-MOBILE-COMPACT-LIST-001
```

Record the authority decision:

```markdown
The existing keep-all-columns requirement remains authoritative for desktop.
This specification is the explicit mobile-only exception below 768px; details retain access to every indicator.
```

- [ ] **Step 2: Register it**

Add the specification to `docs/spec-index.yaml` with `status: authoritative`. Do not register the broad design document as another authority.

- [ ] **Step 3: Check authority**

```powershell
python scripts/check_spec_compliance.py
```

Expected: no conflicts; traceability may remain incomplete only until Task 6.

- [ ] **Step 4: Commit**

```powershell
git add docs/specs/dow-monitor-mobile-version-refresh.md docs/spec-index.yaml
git commit -m "docs: specify monitor mobile layout and version refresh"
```

### Task 2: Create the Mobile Scheme B Row

**Files:**
- Create: `frontend/src/components/dow-monitor/DowMonitorMobileRow.tsx`
- Create: `frontend/src/components/dow-monitor/DowMonitorMobileRow.test.tsx`
- Modify: `frontend/src/components/dow-monitor/DowMonitorList.tsx`

**Interfaces:**
- Consumes: the same `item`, `row`, `interpretation`, and anomaly decisions already computed once in `DowMonitorList`.
- Produces: mobile semantic row and optional auxiliary action slot.

- [ ] **Step 1: Write the failing mobile row test**

```tsx
it('shows Scheme B information in scan order', () => {
  render(
    <DowMonitorMobileRow
      item={overviewItem({ symbol: 'RNG.US', name: 'RingCentral' })}
      row={monitorRow({ price: 54.01, changePct: -6.21 })}
      interpretation={interpretation({ title: '实时下行，周期待确认' })}
      selected={false}
      pendingToggle={false}
      pendingRemoval={false}
      onSelect={vi.fn()}
      onToggle={vi.fn()}
      onRemove={vi.fn()}
      auxiliaryAction={<button>AI综合分析 · 10:30 · 可查看</button>}
    />,
  )

  const row = screen.getByTestId('dow-monitor-mobile-RNG.US')
  expect(within(row).getByText('RingCentral')).toBeInTheDocument()
  expect(within(row).getByText('54.01')).toBeInTheDocument()
  expect(within(row).getByLabelText('RNG.US 日内走势')).toBeInTheDocument()
  expect(within(row).getByText('实时下行，周期待确认')).toBeInTheDocument()
  expect(within(row).getByRole('button', { name: /AI综合分析/ })).toBeInTheDocument()
})
```

Add keyboard selection and stop-propagation tests for pause/delete.

- [ ] **Step 2: Run and observe failure**

```powershell
cd frontend
pnpm test -- --run src/components/dow-monitor/DowMonitorMobileRow.test.tsx
```

- [ ] **Step 3: Implement the three-level row**

Use semantic structure:

```tsx
<article
  data-testid={`dow-monitor-mobile-${item.symbol}`}
  aria-selected={selected}
  tabIndex={0}
  onClick={() => onSelect(item.symbol)}
  onKeyDown={selectOnEnterOrSpace}
  className="border-b border-border px-3 py-3"
>
  <div className="grid grid-cols-[minmax(0,1fr)_auto_88px] items-center gap-2">
    <StockIdentity />
    <PriceAndChange />
    <DowMonitorSparkline symbol={item.symbol} values={row.sparkline} />
  </div>
  <div className="mt-2 min-w-0">
    <KeyInterpretationCell interpretation={interpretation} />
  </div>
  <div className="mt-2 flex min-w-0 items-center justify-between gap-2">
    {auxiliaryAction ?? <span />}
    <CompactActions />
  </div>
</article>
```

Do not apply fixed text truncation to the key interpretation title/body. Preserve the existing interpretation component's accessible text.

- [ ] **Step 4: Reuse existing derived data**

In `DowMonitorList`, keep `interpretedItems` as the single derivation. Render:

```tsx
<div className="md:hidden">
  {interpretedItems.map(({ item, row, interpretation }) => (
    <DowMonitorMobileRow ... />
  ))}
</div>
<div data-testid="dow-monitor-table-scroll" className="hidden max-w-full overflow-x-auto md:block">
  <table>...</table>
</div>
```

Do not duplicate `deriveMonitorRow`, anomaly tracking, or `deriveKeyInterpretation` inside the mobile component.

- [ ] **Step 5: Run component tests**

```powershell
pnpm test -- --run src/components/dow-monitor/DowMonitorMobileRow.test.tsx src/components/dow-monitor/DowMonitorList.test.tsx
```

Expected: mobile order passes and all current desktop assertions remain unchanged.

- [ ] **Step 6: Commit**

```powershell
git add src/components/dow-monitor/DowMonitorMobileRow.tsx src/components/dow-monitor/DowMonitorMobileRow.test.tsx src/components/dow-monitor/DowMonitorList.tsx src/components/dow-monitor/DowMonitorList.test.tsx
git commit -m "feat: add compact mobile monitor rows"
```

### Task 3: Make the Trend Monitor Page Phone-Safe

**Files:**
- Modify: `frontend/src/pages/DowMonitor.tsx`
- Modify: `frontend/src/pages/DowMonitor.test.tsx`
- Modify: `frontend/src/components/dow-monitor/DowMonitorDetailPanel.tsx`
- Modify: `frontend/src/components/dow-monitor/DowMonitorDetailPanel.test.tsx`

**Interfaces:**
- Consumes: responsive `DowMonitorList`.
- Produces: no horizontal page overflow and reachable detail information.

- [ ] **Step 1: Write failing responsive composition tests**

Assert:

```tsx
expect(screen.getByRole('form', { name: '添加监控股票' })).toHaveClass('w-full', 'sm:w-auto')
expect(screen.getByLabelText('股票代码')).toHaveClass('min-w-0')
expect(screen.getByTestId('dow-monitor-detail-region')).toHaveClass('min-w-0')
```

Add `aria-label="添加监控股票"` to the form.

- [ ] **Step 2: Run and observe failure**

```powershell
pnpm test -- --run src/pages/DowMonitor.test.tsx src/components/dow-monitor/DowMonitorDetailPanel.test.tsx
```

- [ ] **Step 3: Apply responsive classes**

Required behavior:

```text
PageHeader right actions wrap below title on narrow screens.
Search input/form use available width instead of fixed 20rem/13rem.
Filter strip scrolls internally if necessary but the page body does not.
Detail panel uses min-w-0 and responsive grids.
```

Keep desktop `w-52` behavior behind `sm:w-52`; use `w-full min-w-0` by default.

- [ ] **Step 4: Run tests and production build**

```powershell
pnpm test -- --run src/pages/DowMonitor.test.tsx src/components/dow-monitor/DowMonitorDetailPanel.test.tsx
pnpm build
```

- [ ] **Step 5: Commit**

```powershell
git add src/pages/DowMonitor.tsx src/pages/DowMonitor.test.tsx src/components/dow-monitor/DowMonitorDetailPanel.tsx src/components/dow-monitor/DowMonitorDetailPanel.test.tsx
git commit -m "fix: make trend monitor phone safe"
```

### Task 4: Add a Shared Backend/Frontend Build ID

**Files:**
- Modify: `backend/app/api/routes.py`
- Create or modify: `backend/tests/test_routes.py`
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `frontend/src/vite-env.d.ts`
- Modify: `frontend/vite.config.ts`
- Create: `frontend/src/lib/appVersion.ts`
- Create: `frontend/src/lib/appVersion.test.ts`

**Interfaces:**
- Consumes: Docker build args `BUILD_ID`, `PUBLISHED_AT`.
- Produces: `window` bundle build ID and `/health` response with identical values.

- [ ] **Step 1: Write failing backend contract test**

```python
def test_health_exposes_build_metadata(monkeypatch):
    monkeypatch.setenv("BUILD_ID", "20260731-deadbee")
    monkeypatch.setenv("PUBLISHED_AT", "2026-07-31T10:00:00+08:00")
    response = TestClient(app).get("/health")
    assert response.json()["build_id"] == "20260731-deadbee"
    assert response.json()["published_at"] == "2026-07-31T10:00:00+08:00"
    assert response.headers["Cache-Control"] == "no-store"
```

- [ ] **Step 2: Run and observe failure**

```powershell
cd backend
uv run pytest tests/test_routes.py -v
```

- [ ] **Step 3: Implement health metadata**

```python
@router.get("/health")
def health(response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {
        "status": "ok",
        "version": __version__,
        "build_id": os.getenv("BUILD_ID", __version__),
        "published_at": os.getenv("PUBLISHED_AT") or None,
        "mode": tf_client.current_mode(),
    }
```

- [ ] **Step 4: Add Docker build args**

In the frontend stage:

```dockerfile
ARG BUILD_ID=dev
ARG PUBLISHED_AT=
ENV VITE_BUILD_ID=$BUILD_ID
ENV VITE_PUBLISHED_AT=$PUBLISHED_AT
```

In the runtime stage:

```dockerfile
ARG BUILD_ID=dev
ARG PUBLISHED_AT=
ENV BUILD_ID=$BUILD_ID PUBLISHED_AT=$PUBLISHED_AT
```

Expose both args under `app.build.args` in `docker-compose.yml`.

- [ ] **Step 5: Add pure frontend comparison**

```typescript
export const CURRENT_BUILD_ID = import.meta.env.VITE_BUILD_ID || 'dev'

export function isNewBuild(currentBuildId: string, remote: AppVersion) {
  return Boolean(remote.build_id && remote.build_id !== currentBuildId)
}

export function mayAutoReload({ visibility, activeMutations, dialogOpen }: RefreshState) {
  return visibility === 'hidden' && activeMutations === 0 && !dialogOpen
}
```

- [ ] **Step 6: Run tests**

```powershell
cd backend
uv run pytest tests/test_routes.py -v
cd ..\frontend
pnpm test -- --run src/lib/appVersion.test.ts
```

- [ ] **Step 7: Commit**

```powershell
git add backend/app/api/routes.py backend/tests/test_routes.py Dockerfile docker-compose.yml frontend/src/vite-env.d.ts frontend/vite.config.ts frontend/src/lib/appVersion.ts frontend/src/lib/appVersion.test.ts
git commit -m "feat: expose immutable frontend build id"
```

### Task 5: Add the Version Update Guard

**Files:**
- Create: `frontend/src/components/AppVersionGuard.tsx`
- Create: `frontend/src/components/AppVersionGuard.test.tsx`
- Modify: `frontend/src/main.tsx`
- Modify: `frontend/src/lib/api.ts`

**Interfaces:**
- Consumes: `/health`, `CURRENT_BUILD_ID`, `useIsMutating`.
- Produces: visible update banner or safe hidden-tab reload.

- [ ] **Step 1: Write failing guard tests**

With fake timers, prove:

```text
same build => no banner, no reload
different build + visible => banner, click reloads
different build + hidden + zero mutations + no dialog => reload
different build + hidden + active mutation => wait
different build + hidden + dialog open => wait
health failure => no banner and no throw
same remote build is prompted only once
```

Example:

```tsx
it('prompts instead of reloading a visible page', async () => {
  vi.mocked(api.appVersion).mockResolvedValue({ build_id: 'new', published_at: null })
  renderGuard({ currentBuildId: 'old' })
  await vi.advanceTimersByTimeAsync(60_000)
  expect(screen.getByRole('status')).toHaveTextContent('发现新版本')
  expect(reload).not.toHaveBeenCalled()
})
```

- [ ] **Step 2: Run and observe failure**

```powershell
pnpm test -- --run src/components/AppVersionGuard.test.tsx
```

- [ ] **Step 3: Add the lightweight API**

```typescript
appVersion: async (): Promise<AppVersion> => {
  const response = await fetch(uncached('/health'), { cache: 'no-store' })
  if (!response.ok) throw new Error(`version ${response.status}`)
  const body = await response.json()
  return { build_id: body.build_id ?? body.version, published_at: body.published_at ?? null }
}
```

Do not use the toast-producing authenticated `request()` wrapper for this non-critical check.

- [ ] **Step 4: Implement the guard**

Use:

```typescript
const activeMutations = useIsMutating()
const dialogOpen = Boolean(document.querySelector('[role="dialog"]'))
```

Poll on mount and every `60_000` ms. Also recheck on `visibilitychange`. When visible, render:

```tsx
<div role="status" className="fixed inset-x-3 top-3 z-[100] ...">
  <span>发现新版本，为避免继续使用旧页面，请刷新。</span>
  <button onClick={() => window.location.reload()}>立即刷新</button>
</div>
```

When hidden, call reload only if `mayAutoReload` returns true. Store the last prompted remote ID in a ref.

- [ ] **Step 5: Mount inside QueryClient**

```tsx
<QueryClientProvider client={queryClient}>
  <AppVersionGuard />
  <RouterProvider router={router} />
</QueryClientProvider>
```

- [ ] **Step 6: Run tests**

```powershell
pnpm test -- --run src/components/AppVersionGuard.test.tsx src/lib/appVersion.test.ts
pnpm build
```

- [ ] **Step 7: Commit**

```powershell
git add src/components/AppVersionGuard.tsx src/components/AppVersionGuard.test.tsx src/main.tsx src/lib/api.ts
git commit -m "feat: prompt or safely refresh stale frontend builds"
```

### Task 6: Responsive and Version Semantic Acceptance

**Files:**
- Modify: `docs/traceability.yaml`
- Create: `docs/acceptance/dow-monitor-mobile-version-refresh.md`
- Create: `docs/reviews/dow-monitor-mobile-version-refresh.md`
- Modify: `E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md`

**Interfaces:**
- Consumes: built frontend and candidate backend.
- Produces: requirement evidence and independent review.

- [ ] **Step 1: Run the complete relevant suite**

```powershell
cd backend
uv run pytest tests/test_routes.py tests/test_dow_monitor_api.py -v
cd ..\frontend
pnpm test -- --run src/components/dow-monitor/DowMonitorMobileRow.test.tsx src/components/dow-monitor/DowMonitorList.test.tsx src/pages/DowMonitor.test.tsx src/components/AppVersionGuard.test.tsx src/lib/appVersion.test.ts
pnpm build
```

- [ ] **Step 2: Run real viewport checks**

Serve the production bundle and use Playwright at `390x844` and `1440x900`. Assert:

```javascript
await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
await expect(page.getByTestId('dow-monitor-mobile-RNG.US')).toBeVisible()
await expect(page.getByTestId('dow-monitor-table-scroll')).toBeHidden()
```

At desktop width assert the inverse and confirm all original headers exist.

- [ ] **Step 3: Verify details retain hidden indicators**

At mobile width click a stock twice:

```text
first click opens details
trend/momentum/volume/funds/risk fields are reachable
second click collapses details
```

- [ ] **Step 4: Verify build mismatch behavior**

Run a candidate with backend `BUILD_ID=new` and a bundle built with `old`, then verify visible prompt. Repeat with the tab hidden and no mutations to verify automatic refresh. Repeat with an open dialog to verify deferral.

- [ ] **Step 5: Record traceability and run compliance**

Map both requirements to implementation, executable tests, semantic acceptance, and independent review:

```powershell
python scripts/check_spec_compliance.py
```

- [ ] **Step 6: Update the runbook**

Add:

- build arguments and `/health` fields;
- 60-second polling and safe-refresh conditions;
- mobile breakpoint and visible fields;
- desktop/mobile Playwright verification commands;
- release and rollback checks.

- [ ] **Step 7: Commit evidence**

```powershell
git add docs/traceability.yaml docs/acceptance/dow-monitor-mobile-version-refresh.md docs/reviews/dow-monitor-mobile-version-refresh.md
git commit -m "docs: accept monitor mobile layout and version refresh"
```
