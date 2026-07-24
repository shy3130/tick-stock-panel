# Mobile Responsive Phase One Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing web application usable on phones by adding a mobile top bar and drawer and adapting the dashboard, watchlist, screener, and settings pages without changing desktop behavior.

**Architecture:** Keep the existing desktop sidebar at `md` and above. Add one isolated `MobileNavigation` component that owns drawer interaction and receives the existing sidebar as children, then apply page-local Tailwind breakpoint changes to the four selected pages. Reuse all current queries, routes, market state, and table components; this work changes presentation only.

**Tech Stack:** React 18, TypeScript, React Router 6, Tailwind CSS 3, Framer Motion, Vitest, Testing Library, Vite.

## Global Constraints

- Mobile layout applies below `768px`; desktop layout remains active at `768px` and above.
- Validate at `390×844` and `430×932`.
- Do not add a UI framework, state library, backend endpoint, network request, PWA feature, or native application.
- Preserve current market selection, realtime quote behavior, strategy logic, permissions, and desktop information architecture.
- Preserve all pre-existing uncommitted work and stage only files named by the current task.

---

## File Structure

- Create `frontend/src/components/MobileNavigation.tsx`: isolated phone top bar, drawer, Escape handling, scroll lock, and route-change close behavior.
- Create `frontend/src/components/MobileNavigation.test.tsx`: interaction tests for open, Escape close, and navigation close.
- Modify `frontend/src/components/Layout.tsx`: keep shared data ownership, render the existing sidebar in desktop and drawer variants, and place mobile navigation above the outlet.
- Modify `frontend/src/components/PageHeader.tsx`: make page title and action regions wrap and scroll safely on narrow screens.
- Create `frontend/src/components/PageHeader.test.tsx`: assert the narrow-safe semantic structure and class contract.
- Modify `frontend/src/pages/Dashboard.tsx`: responsive padding, toolbar, index cells, KPI cells, and stock lists.
- Modify `frontend/src/pages/Watchlist.tsx`: responsive toolbar, filter spacing, content padding, card grid, and table containment.
- Modify `frontend/src/pages/Screener.tsx`: responsive toolbar, strategy grid, result header, filter actions, and content padding.
- Create `frontend/src/pages/settings/SettingsTabs.tsx`: accessible responsive settings tab navigation.
- Create `frontend/src/pages/settings/SettingsTabs.test.tsx`: settings tab selection contract.
- Modify `frontend/src/pages/Settings.tsx`: use `SettingsTabs` and switch content layout at the `md` breakpoint.

---

### Task 1: Mobile Top Bar and Drawer

**Files:**
- Create: `frontend/src/components/MobileNavigation.tsx`
- Create: `frontend/src/components/MobileNavigation.test.tsx`
- Modify: `frontend/src/components/Layout.tsx:1-713`

**Interfaces:**
- Consumes: `MarketCode`, `MarketFilterTabs`, `useLocation`, `ReactNode`, and the already-rendered sidebar content supplied as `children`.
- Produces: `MobileNavigation({ title, market, onMarketChange, children }): JSX.Element`.
- Produces: `mobilePageTitle(pathname: string, items: ReadonlyArray<{to: string; label: string}>): string`.

- [ ] **Step 1: Write the failing mobile navigation tests**

Create `frontend/src/components/MobileNavigation.test.tsx` with three observable behaviors:

```tsx
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, NavLink } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { MobileNavigation, mobilePageTitle } from './MobileNavigation'

const items = [
  { to: '/', label: '看板' },
  { to: '/watchlist', label: '自选股' },
]

function renderNavigation() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <MobileNavigation title="看板" market="cn" onMarketChange={vi.fn()}>
        <NavLink to="/watchlist">自选股</NavLink>
      </MobileNavigation>
    </MemoryRouter>,
  )
}

describe('MobileNavigation', () => {
  it('maps nested routes to the longest matching page title', () => {
    expect(mobilePageTitle('/watchlist/detail', items)).toBe('自选股')
    expect(mobilePageTitle('/', items)).toBe('看板')
  })

  it('opens the drawer and closes it with Escape', async () => {
    const user = userEvent.setup()
    renderNavigation()
    await user.click(screen.getByRole('button', { name: '打开导航菜单' }))
    expect(screen.getByRole('dialog', { name: '主导航' })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog', { name: '主导航' })).not.toBeInTheDocument()
  })

  it('closes the drawer after route navigation', async () => {
    const user = userEvent.setup()
    renderNavigation()
    await user.click(screen.getByRole('button', { name: '打开导航菜单' }))
    await user.click(screen.getByRole('link', { name: '自选股' }))
    expect(screen.queryByRole('dialog', { name: '主导航' })).not.toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run the test and verify the missing component failure**

Run:

```powershell
pnpm test --run src/components/MobileNavigation.test.tsx
```

Expected: FAIL because `./MobileNavigation` does not exist.

- [ ] **Step 3: Implement the minimal mobile navigation component**

Create `frontend/src/components/MobileNavigation.tsx` with this structure:

```tsx
import { useEffect, useState, type ReactNode } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Menu, X } from 'lucide-react'
import { useLocation } from 'react-router-dom'
import { Logo } from './Logo'
import { MarketFilterTabs } from './MarketFilterTabs'
import type { MarketCode } from '@/lib/api'

export function mobilePageTitle(
  pathname: string,
  items: ReadonlyArray<{ to: string; label: string }>,
): string {
  return [...items]
    .sort((a, b) => b.to.length - a.to.length)
    .find(item => item.to === '/' ? pathname === '/' : pathname === item.to || pathname.startsWith(`${item.to}/`))
    ?.label ?? 'TickFlow'
}

export function MobileNavigation({
  title, market, onMarketChange, children,
}: {
  title: string
  market: MarketCode
  onMarketChange: (market: MarketCode) => void
  children: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const location = useLocation()

  useEffect(() => setOpen(false), [location.pathname])
  useEffect(() => {
    if (!open) return
    const previous = document.body.style.overflow
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
    document.body.style.overflow = 'hidden'
    window.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previous
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <>
      <header className="fixed inset-x-0 top-0 z-40 flex h-14 items-center gap-2 border-b border-border bg-surface/95 px-3 backdrop-blur md:hidden">
        <button type="button" aria-label="打开导航菜单" onClick={() => setOpen(true)} className="inline-flex h-10 w-10 items-center justify-center rounded-btn bg-elevated text-secondary">
          <Menu className="h-5 w-5" />
        </button>
        <Logo size={24} className="shrink-0 text-accent" />
        <span className="min-w-0 flex-1 truncate text-sm font-semibold">{title}</span>
        <MarketFilterTabs value={market} includeAll={false} className="shrink-0" onChange={next => { if (next !== 'all') onMarketChange(next) }} />
      </header>
      <AnimatePresence>
        {open && (
          <div className="fixed inset-0 z-50 md:hidden">
            <motion.button type="button" aria-label="关闭导航菜单" className="absolute inset-0 bg-black/65" onClick={() => setOpen(false)} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} />
            <motion.div role="dialog" aria-modal="true" aria-label="主导航" className="relative h-full w-[min(19rem,86vw)] bg-surface shadow-2xl" initial={{ x: '-100%' }} animate={{ x: 0 }} exit={{ x: '-100%' }}>
              <button type="button" aria-label="关闭导航菜单" onClick={() => setOpen(false)} className="absolute right-2 top-2 z-10 inline-flex h-9 w-9 items-center justify-center rounded-btn bg-elevated text-secondary">
                <X className="h-4 w-4" />
              </button>
              {children}
            </motion.div>
          </div>
        )}
      </AnimatePresence>
    </>
  )
}
```

- [ ] **Step 4: Run the focused test and verify it passes**

Run:

```powershell
pnpm test --run src/components/MobileNavigation.test.tsx
```

Expected: 3 tests PASS.

- [ ] **Step 5: Integrate the component into `Layout`**

In `frontend/src/components/Layout.tsx`:

- Import `useLocation`, `MobileNavigation`, and `mobilePageTitle`.
- Add `const location = useLocation()` and compute `const mobileTitle = mobilePageTitle(location.pathname, visibleNavItems)`.
- Extract the existing `<aside>...</aside>` JSX into a local `renderSidebar(mobile: boolean)` function. The returned aside uses `h-full` for both variants, `hidden md:flex` for desktop, and `flex` inside the drawer for mobile.
- Add `onClick={mobile ? () => navigate(to) : undefined}` to each navigation item without changing the route target.
- Change the root class from `grid grid-cols-[14rem_1fr]` to `grid grid-cols-1 md:grid-cols-[14rem_1fr]`.
- Render the desktop sidebar once, then render:

```tsx
<MobileNavigation title={mobileTitle} market={market} onMarketChange={setMarket}>
  {renderSidebar(true)}
</MobileNavigation>
```

- Change the main content class to `h-full min-w-0 overflow-auto pt-14 md:pt-0 scrollbar-gutter-stable` so phone content receives the full viewport width below the fixed header.

- [ ] **Step 6: Re-run the test and production build**

Run:

```powershell
pnpm test --run src/components/MobileNavigation.test.tsx
pnpm build
```

Expected: 3 tests PASS and the Vite production build exits 0.

- [ ] **Step 7: Commit only the mobile shell files**

```powershell
git add frontend/src/components/MobileNavigation.tsx frontend/src/components/MobileNavigation.test.tsx frontend/src/components/Layout.tsx
git commit -m "feat: add mobile navigation drawer"
```

---

### Task 2: Responsive Page Header and Dashboard

**Files:**
- Create: `frontend/src/components/PageHeader.test.tsx`
- Modify: `frontend/src/components/PageHeader.tsx:1-29`
- Modify: `frontend/src/pages/Dashboard.tsx:680-840`

**Interfaces:**
- Consumes: existing `PageHeader` props without signature changes.
- Produces: `PageHeader` with a wrapping title region and a horizontally safe action region labeled `页面操作`.

- [ ] **Step 1: Write the failing PageHeader test**

Create `frontend/src/components/PageHeader.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PageHeader } from './PageHeader'

describe('PageHeader responsive structure', () => {
  it('keeps actions in a named horizontally scrollable region', () => {
    render(<PageHeader title="自选股" subtitle="说明" right={<button>刷新</button>} />)
    const actions = screen.getByLabelText('页面操作')
    expect(actions).toHaveClass('overflow-x-auto')
    expect(screen.getByText('说明').parentElement).toHaveClass('flex-wrap')
  })
})
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run:

```powershell
pnpm test --run src/components/PageHeader.test.tsx
```

Expected: FAIL because the action region has no `页面操作` label and the title row does not wrap.

- [ ] **Step 3: Make PageHeader narrow-safe**

Update the rendered classes and wrap `right`:

```tsx
<header className={cn('flex flex-col gap-2 border-b border-border px-3 pb-2 pt-3 sm:px-5 md:flex-row md:items-center md:justify-between md:gap-4', className)}>
  <div className="flex min-w-0 flex-wrap items-center gap-2">
    <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
    {titleExtra}
    {subtitle && <span className="text-xs text-muted">{subtitle}</span>}
  </div>
  {right && <div aria-label="页面操作" className="w-full overflow-x-auto pb-0.5 md:w-auto md:overflow-visible md:pb-0">{right}</div>}
</header>
```

- [ ] **Step 4: Run the focused test and verify it passes**

Run:

```powershell
pnpm test --run src/components/PageHeader.test.tsx
```

Expected: 1 test PASS.

- [ ] **Step 5: Adapt dashboard classes without changing data rendering**

In `frontend/src/pages/Dashboard.tsx`:

- Add phone-safe outer padding: `px-2 py-2 sm:px-3` on the page root.
- Change the toolbar action row to `w-full flex-wrap gap-2 sm:w-auto sm:flex-nowrap` and allow its date picker to retain usable width.
- Change the index grid from `grid-cols-4` to `grid-cols-2 sm:grid-cols-4`.
- Change the KPI grid from `grid-cols-6` to `grid-cols-2 sm:grid-cols-3 xl:grid-cols-6`.
- Keep the primary content at one column until the existing `xl` split.
- Keep heat ranks one column until `md`.
- Change stock lists from `sm:grid-cols-2` to `grid-cols-1 sm:grid-cols-2 xl:grid-cols-4`.
- Add `min-w-0` to every direct grid child that contains charts or rank rows.

- [ ] **Step 6: Run focused tests and build**

Run:

```powershell
pnpm test --run src/components/PageHeader.test.tsx src/lib/overviewRefresh.test.ts
pnpm build
```

Expected: all selected tests PASS and production build exits 0.

- [ ] **Step 7: Commit only header and dashboard files**

```powershell
git add frontend/src/components/PageHeader.tsx frontend/src/components/PageHeader.test.tsx frontend/src/pages/Dashboard.tsx
git commit -m "feat: adapt dashboard for mobile screens"
```

---

### Task 3: Watchlist and Screener Layouts

**Files:**
- Modify: `frontend/src/pages/Watchlist.tsx:950-1425`
- Modify: `frontend/src/pages/Screener.tsx:600-930`
- Modify: `frontend/src/components/screener/ScreenerTable.tsx:143-430`

**Interfaces:**
- Consumes: existing `StockDataTable` horizontal scrolling and existing watchlist/screener state.
- Produces: phone-safe toolbars and content containers; no prop or API changes.

- [ ] **Step 1: Capture the current overflow failure before editing**

At a running development or production page, use a `390×844` browser viewport and record the current root metrics for both routes:

```js
({
  viewport: window.innerWidth,
  rootScrollWidth: document.documentElement.scrollWidth,
  rootClientWidth: document.documentElement.clientWidth,
})
```

Expected before Task 1 integration is deployed: main content is constrained by the fixed sidebar. After Task 1, any remaining overflow must originate within the page toolbar or content container.

- [ ] **Step 2: Adapt the watchlist layout**

In `frontend/src/pages/Watchlist.tsx`:

- Change the PageHeader action wrapper from `flex items-center gap-2` to `flex min-w-max items-center gap-2 md:min-w-0` so it scrolls inside the new PageHeader action region.
- Change filter panel padding to `px-3 sm:px-5`.
- Change list content padding to `px-2 py-2 sm:px-5 sm:py-3`.
- Keep `StockDataTable` inside `overflow-x-auto` and add `max-w-full` to its class.
- Change card gaps from `gap-3` to `gap-2 sm:gap-3` in both normal and virtualized card grids.
- Keep two phone columns because the existing cards are already compact; verify truncation rather than widening the page.

- [ ] **Step 3: Adapt the screener toolbar and content**

In `frontend/src/pages/Screener.tsx`:

- Change the PageHeader action wrapper to `flex min-w-max items-center gap-2 md:min-w-0` so all controls remain reachable through horizontal scrolling.
- Change page content padding from `px-8 py-4` to `px-2 py-3 sm:px-5 lg:px-8 lg:py-4`.
- Make result summary `flex-col items-start gap-2 sm:flex-row sm:items-center sm:justify-between`.
- Make result actions `w-full flex-wrap gap-2 sm:w-auto sm:flex-nowrap`.
- Keep strategy cards at one phone column unless `cardWrapCls` already returns a narrower safe grid; update its phone prefix to `grid-cols-1 sm:grid-cols-2`.
- Keep result tables in a local `max-w-full overflow-x-auto` container.

In `frontend/src/components/screener/ScreenerTable.tsx`, pass `className="max-w-full rounded-card border border-border overflow-x-auto"` to the shared `StockDataTable`.

- [ ] **Step 4: Run the frontend test suite and build**

Run:

```powershell
pnpm test --run
pnpm build
```

Expected: all frontend tests PASS and production build exits 0.

- [ ] **Step 5: Commit only watchlist and screener files**

```powershell
git add frontend/src/pages/Watchlist.tsx frontend/src/pages/Screener.tsx frontend/src/components/screener/ScreenerTable.tsx
git commit -m "feat: adapt watchlist and screener for phones"
```

---

### Task 4: Responsive Settings Tabs

**Files:**
- Create: `frontend/src/pages/settings/SettingsTabs.tsx`
- Create: `frontend/src/pages/settings/SettingsTabs.test.tsx`
- Modify: `frontend/src/pages/Settings.tsx:1-100`

**Interfaces:**
- Consumes: `TabDef`, current active tab key, and `onChange(key: string): void`.
- Produces: `SettingsTabs({ tabs, activeKey, onChange }): JSX.Element` with `role="tablist"` and phone horizontal scrolling.

- [ ] **Step 1: Write the failing tab behavior test**

Create `frontend/src/pages/settings/SettingsTabs.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Key, Radio } from 'lucide-react'
import { describe, expect, it, vi } from 'vitest'
import { SettingsTabs } from './SettingsTabs'

describe('SettingsTabs', () => {
  it('exposes the selected tab and reports a new selection', async () => {
    const onChange = vi.fn()
    const user = userEvent.setup()
    render(<SettingsTabs tabs={[
      { key: 'account', label: 'TickFlow', icon: Key },
      { key: 'monitoring', label: '实时监控', icon: Radio },
    ]} activeKey="account" onChange={onChange} />)
    expect(screen.getByRole('tab', { name: 'TickFlow' })).toHaveAttribute('aria-selected', 'true')
    await user.click(screen.getByRole('tab', { name: '实时监控' }))
    expect(onChange).toHaveBeenCalledWith('monitoring')
  })
})
```

- [ ] **Step 2: Run the test and verify the missing component failure**

Run:

```powershell
pnpm test --run src/pages/settings/SettingsTabs.test.tsx
```

Expected: FAIL because `SettingsTabs` does not exist.

- [ ] **Step 3: Implement `SettingsTabs`**

Create `frontend/src/pages/settings/SettingsTabs.tsx` with exported input type and responsive classes:

```tsx
import type { ComponentType } from 'react'
import { cn } from '@/lib/cn'

export type SettingsTabItem = {
  key: string
  label: string
  icon: ComponentType<{ className?: string }>
  badge?: string
}

export function SettingsTabs({ tabs, activeKey, onChange }: {
  tabs: readonly SettingsTabItem[]
  activeKey: string
  onChange: (key: string) => void
}) {
  return (
    <nav aria-label="设置分类" className="w-full shrink-0 md:w-36">
      <div role="tablist" aria-orientation="horizontal" className="flex gap-1 overflow-x-auto pb-1 md:sticky md:top-6 md:min-h-[60vh] md:flex-col md:justify-center md:overflow-visible md:pb-0">
        {tabs.map(({ key, label, icon: Icon, badge }) => (
          <button key={key} type="button" role="tab" aria-selected={activeKey === key} onClick={() => onChange(key)} className={cn('relative flex shrink-0 items-center gap-2 rounded-btn px-3 py-2 text-left text-sm transition-colors', activeKey === key ? 'bg-accent/10 font-medium text-accent' : 'text-secondary hover:bg-elevated/60 hover:text-foreground')}>
            <Icon className="h-3.5 w-3.5 shrink-0" />
            <span>{label}</span>
            {badge && <span className="ml-auto rounded-full border border-amber-400/30 bg-amber-400/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase text-amber-400">{badge}</span>}
          </button>
        ))}
      </div>
    </nav>
  )
}
```

- [ ] **Step 4: Run the test and verify it passes**

Run:

```powershell
pnpm test --run src/pages/settings/SettingsTabs.test.tsx
```

Expected: 1 test PASS.

- [ ] **Step 5: Integrate responsive settings layout**

In `frontend/src/pages/Settings.tsx`:

- Export or reuse the existing tab item shape with `SettingsTabs`.
- Replace the inline tab `<nav>` with:

```tsx
<SettingsTabs
  tabs={TABS}
  activeKey={activeTab.key}
  onChange={key => setSearchParams({ tab: key }, { replace: true })}
/>
```

- Change content wrapper to `px-3 py-4 sm:px-5 lg:px-8 lg:py-6`.
- Change the layout row to `flex flex-col gap-4 md:flex-row md:items-stretch md:gap-6`.
- Keep the panel `min-w-0 flex-1 w-full`.
- Do not alter tab keys, URL query parameters, panels, or highlight behavior.

- [ ] **Step 6: Run focused and full frontend verification**

Run:

```powershell
pnpm test --run src/pages/settings/SettingsTabs.test.tsx src/pages/password-change-flow.test.tsx src/pages/settings/AccountSecurity.test.tsx
pnpm test --run
pnpm build
```

Expected: all focused tests PASS, all frontend tests PASS, and production build exits 0.

- [ ] **Step 7: Commit only settings files**

```powershell
git add frontend/src/pages/settings/SettingsTabs.tsx frontend/src/pages/settings/SettingsTabs.test.tsx frontend/src/pages/Settings.tsx
git commit -m "feat: adapt settings tabs for phones"
```

---

### Task 5: Cross-Viewport Verification and Deployment

**Files:**
- Modify only if verification reveals a specific responsive regression in files already listed in Tasks 1-4.

**Interfaces:**
- Consumes: built `frontend/dist` and the current deployed image as the rollback base.
- Produces: a uniquely tagged deployment image on `192.168.10.28:3018` with the same backend and updated static frontend.

- [ ] **Step 1: Run fresh local verification**

Run:

```powershell
pnpm test --run
pnpm build
git diff --check
```

Expected: all frontend tests PASS, build exits 0, and `git diff --check` reports no errors.

- [ ] **Step 2: Verify phone layouts in a real browser**

At both `390×844` and `430×932`, inspect:

- `/?market=hk`
- `/watchlist?market=hk`
- `/screener?market=hk`
- `/settings?tab=monitoring`

For each route, collect:

```js
({
  viewport: window.innerWidth,
  rootScrollWidth: document.documentElement.scrollWidth,
  rootClientWidth: document.documentElement.clientWidth,
  mainWidth: Math.round(document.querySelector('#root > div > main')?.getBoundingClientRect().width ?? 0),
})
```

Expected on all routes: `rootScrollWidth === rootClientWidth` and `mainWidth === viewport`. Open and close the drawer, change market once, navigate through the drawer, and confirm local table containers scroll without widening the document.

- [ ] **Step 3: Verify desktop layout**

Reset the viewport override and inspect the dashboard and settings page at the normal desktop size.

Expected: fixed 224px sidebar is visible, mobile top bar is hidden, page content occupies the remaining width, and settings tabs remain vertical.

- [ ] **Step 4: Build a unique layered image on the deployment host**

Use the currently running image as the `FROM` image and copy only `frontend/dist` to `/app/static`. Generate and retain the unique tag in the current PowerShell session, then verify the candidate still contains 11 system strategy patterns:

```powershell
$candidateTag = "tickflow-stock-panel-app:mobile-responsive-$((Get-Date).ToString('yyyyMMdd-HHmmss'))"
$currentImage = ssh alwin@192.168.10.28 "docker inspect -f '{{.Config.Image}}' TickFlow_Stock_Panel"
$buildDir = "/tmp/tickflow-mobile-responsive-$((Get-Date).ToString('yyyyMMdd-HHmmss'))"
ssh alwin@192.168.10.28 "mkdir -p $buildDir/dist && readlink -f $buildDir"
scp -r frontend/dist/* "alwin@192.168.10.28:${buildDir}/dist/"
scp Dockerfile.mobile-runtime "alwin@192.168.10.28:${buildDir}/Dockerfile"
ssh alwin@192.168.10.28 "cd $buildDir && docker build --build-arg BASE_IMAGE=$currentImage -t $candidateTag ."
ssh alwin@192.168.10.28 "docker run --rm --entrypoint /app/.venv/bin/python $candidateTag -c 'from longbridge_stock.system_patterns import system_pattern_manifest; print(len(system_pattern_manifest()))'"
```

Create `Dockerfile.mobile-runtime` with `apply_patch` before these commands and delete it with `apply_patch` after the image build:

```dockerfile
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY dist/ /app/static/
```

Expected: `11`.

- [ ] **Step 5: Deploy and preserve realtime preferences**

Back up `/app/data/user_data/preferences.json` from the running container, switch with:

```powershell
ssh alwin@192.168.10.28 "cd /home/alwin/apps/tickflow-stock-panel && TICKFLOW_IMAGE=$candidateTag docker compose up -d --no-build"
```

Restore `realtime_quotes_enabled` to `true` if graceful shutdown saved `false`, then stop and start the exact `TickFlow_Stock_Panel` container so startup reads the restored preference.

- [ ] **Step 6: Verify production runtime**

Confirm:

- The running image is the unique mobile-responsive tag.
- The container status is `running` with restart count 0.
- `realtime_quotes_enabled` is `true`.
- Logs continue to show quote refreshes.
- The four mobile routes return HTTP 200 and pass the phone viewport checks against `http://alwinzhang.com:3018`.

- [ ] **Step 7: Commit any verification-only correction and report**

If browser verification required a correction, stage only the corrected responsive files and commit with:

```powershell
git commit -m "fix: close mobile responsive gaps"
```

If no correction was required, do not create an empty commit.
