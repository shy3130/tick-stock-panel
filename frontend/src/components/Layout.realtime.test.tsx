// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Layout } from './Layout'
import { api, type Preferences } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'

const layoutMocks = vi.hoisted(() => ({ desktop: true, prefs: {} as Partial<Preferences> }))

vi.mock('@/lib/api', () => ({ api: {
  dataSources: vi.fn(async () => ({
    builtin: [], custom: [], plugins: [{ name: 'fuyao', display_name: 'fuyao' }],
  })),
  analysisMenus: vi.fn(async () => ({ items: [] })),
  indexQuotes: vi.fn(async () => ({ rows: [] })),
  watchlistGroups: vi.fn(async () => ({ groups: [] })),
  watchlistList: vi.fn(async () => ({ symbols: [] })),
  watchlistEnriched: vi.fn(async () => ({ rows: [] })),
  pipelineJobs: vi.fn(async () => ({ active_id: null, items: [] })),
  dataIntegrity: vi.fn(async () => ({ report: null, blocking_assets: [], data_revisions: {} })),
  alertsList: vi.fn(async () => ({ total: 0 })),
  quoteStatus: vi.fn(),
  updateRealtimeQuotes: vi.fn(async (enabled: boolean) => ({
    realtime_quotes_enabled: enabled, realtime_allowed: true,
  })),
  miningRuns: vi.fn(async () => ({ items: [] })),
  intradayRefresh: vi.fn(async () => ({})),
} }))
vi.mock('@/lib/useSharedQueries', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/useSharedQueries')>(),
  useSettings: () => ({ data: { onboarding_completed: true } }),
  useCapabilityMatrix: () => ({ data: { capabilities: [] } }),
  usePreferences: () => ({ data: layoutMocks.prefs }),
  useVersion: () => ({ data: { version: '0.3.20' } }),
}))
vi.mock('@/lib/useQuoteStream', () => ({
  useQuoteStream: () => {}, useQuoteStreamStatus: () => 'connected',
}))
vi.mock('@/lib/updateCheck', () => ({ useUpdateCheck: () => ({ status: 'latest' }) }))
vi.mock('@/lib/useMediaQuery', () => ({ useIsDesktop: () => layoutMocks.desktop }))
vi.mock('@/extensions/registry', () => ({ getFrontendExtensionNavigation: () => [] }))
vi.mock('@/extensions/ExtensionSlot', () => ({ ExtensionSlot: () => null }))
vi.mock('@/components/Toast', () => ({ ToastContainer: () => null, toast: vi.fn() }))
vi.mock('@/components/AlertToast', () => ({ AlertToastContainer: () => null }))
vi.mock('@/components/financials/AiAnalysisHost', () => ({ AiAnalysisHost: () => null }))
vi.mock('@/components/financials/AiReportBubble', () => ({ AiReportBubble: () => null }))
vi.mock('@/components/stock-analysis/StockAnalysisHost', () => ({ StockAnalysisHost: () => null }))
vi.mock('@/components/stock-analysis/StockAnalysisBubble', () => ({ StockAnalysisBubble: () => null }))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  layoutMocks.desktop = true
  layoutMocks.prefs = { realtime_data_provider: 'fuyao', realtime_quotes_enabled: false }
  vi.mocked(api.quoteStatus).mockResolvedValue({ enabled: false, running: false, paused: false,
    is_trading_hours: false, mode: 'none', realtime_allowed: false } as Awaited<ReturnType<typeof api.quoteStatus>>)
  localStorage.clear()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: {
    retry: false, staleTime: Infinity,
  } } })
})

const groupedLayout: NonNullable<Preferences['nav_layout']> = { v: 1, items: [
  { type: 'page', id: '/' },
  { type: 'group', id: 'group:workspace', label: '交易工作台', children: [
    { type: 'page', id: '/watchlist' }, { type: 'page', id: '/data' },
  ] },
] }

async function mountGrouped(path = '/watchlist') {
  layoutMocks.prefs = { ...layoutMocks.prefs, nav_layout: groupedLayout, watchlist_groups_in_nav: true }
  vi.mocked(api.watchlistGroups).mockResolvedValue({ groups: [{ id: 'short', name: '短线', color: 'blue' }] } as Awaited<ReturnType<typeof api.watchlistGroups>>)
  client.setQueryData(QK.watchlistGroups, { groups: [{ id: 'short', name: '短线', color: 'blue' }] })
  await act(async () => root.render(<MemoryRouter initialEntries={[path]}><QueryClientProvider client={client}>
    <Layout />
  </QueryClientProvider></MemoryRouter>))
}

it('expands the active folder, preserves watchlist groups and stops hidden group-stat queries', async () => {
  await mountGrouped()
  const folder = host.querySelector<HTMLButtonElement>('button[aria-label="交易工作台"]')!
  expect(folder.getAttribute('aria-expanded')).toBe('true')
  expect(host.textContent).toContain('短线')
  const stats = client.getQueryCache().find({ queryKey: QK.watchlistEnriched(), exact: true })!
  expect(stats.isActive()).toBe(true)
  await act(async () => folder.click())
  expect(folder.getAttribute('aria-expanded')).toBe('false')
  expect(stats.isActive()).toBe(false)
  expect(host.querySelector('a[href="/settings?tab=pages&section=menus"]')).not.toBeNull()
})

it('opens folder links in a portal in rail mode and closes with Escape', async () => {
  localStorage.setItem('tf-nav-state', 'rail')
  await mountGrouped('/data')
  const folder = host.querySelector<HTMLButtonElement>('button[aria-label="交易工作台"]')!
  await act(async () => folder.click())
  const popup = document.querySelector('[role=dialog][aria-label="交易工作台子菜单"]')!
  expect(popup).not.toBeNull()
  expect(host.contains(popup)).toBe(false)
  expect(popup.querySelector('a[href="/data"]')).not.toBeNull()
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(document.querySelector('[role=dialog][aria-label="交易工作台子菜单"]')).toBeNull()
})

it('does not fetch root watchlist statistics when an unrelated rail folder opens', async () => {
  localStorage.setItem('tf-nav-state', 'rail')
  layoutMocks.prefs = { ...layoutMocks.prefs, watchlist_groups_in_nav: true, nav_layout: { v: 1, items: [
    { type: 'page', id: '/' }, { type: 'page', id: '/watchlist' },
    { type: 'group', id: 'group:data', label: '数据工具', children: [{ type: 'page', id: '/data' }] },
  ] } }
  client.setQueryData(QK.watchlistGroups, { groups: [{ id: 'short', name: '短线', color: 'blue' }] })
  await act(async () => root.render(<MemoryRouter initialEntries={['/watchlist']}><QueryClientProvider client={client}>
    <Layout />
  </QueryClientProvider></MemoryRouter>))
  await act(async () => host.querySelector<HTMLAnchorElement>('a[href="/screener"]')!.click())
  expect(api.watchlistEnriched).not.toHaveBeenCalled()
  await act(async () => host.querySelector<HTMLButtonElement>('button[aria-label="数据工具"]')!.click())
  const popup = document.querySelector('[role=dialog][aria-label="数据工具子菜单"]')!
  expect(popup).not.toBeNull()
  expect(popup.querySelector('a[href="/watchlist"]')).toBeNull()
  expect(api.watchlistList).not.toHaveBeenCalled()
  expect(api.watchlistEnriched).not.toHaveBeenCalled()
})

it('enables grouped watchlist statistics only while its rail popup is open', async () => {
  localStorage.setItem('tf-nav-state', 'rail')
  await mountGrouped()
  const stats = client.getQueryCache().find({ queryKey: QK.watchlistEnriched(), exact: true })!
  expect(stats.isActive()).toBe(false)
  await act(async () => host.querySelector<HTMLButtonElement>('button[aria-label="交易工作台"]')!.click())
  expect(document.querySelector('[role=dialog][aria-label="交易工作台子菜单"]')?.textContent).toContain('短线')
  expect(stats.isActive()).toBe(true)
  expect(api.watchlistList).toHaveBeenCalledOnce()
  expect(api.watchlistEnriched).toHaveBeenCalledOnce()
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(stats.isActive()).toBe(false)
})

it('shows a saved empty category and explains that it has no child menus', async () => {
  layoutMocks.prefs.nav_layout = { v: 1, items: [{ type: 'page', id: '/' },
    { type: 'group', id: 'group:new', label: '新分类', children: [] }] }
  await act(async () => root.render(<MemoryRouter><QueryClientProvider client={client}><Layout /></QueryClientProvider></MemoryRouter>))
  const folder = host.querySelector<HTMLButtonElement>('button[aria-label="新分类"]')!
  expect(folder).not.toBeNull()
  await act(async () => folder.click())
  expect(host.textContent).toContain('暂无子菜单')
})

it('keeps grouped pages reachable in the mobile drawer', async () => {
  layoutMocks.desktop = false
  await mountGrouped('/')
  await act(async () => host.querySelector<HTMLButtonElement>('button[title="打开菜单"]')!.click())
  const folder = host.querySelector<HTMLButtonElement>('button[aria-label="交易工作台"]')!
  await act(async () => folder.click())
  expect(folder.getAttribute('aria-expanded')).toBe('true')
  await act(async () => host.querySelector<HTMLAnchorElement>('a[href="/data"]')!.click())
  expect(host.querySelector('aside')?.className).toContain('-translate-x-full')
})

it('restores saved folder expansion without overwriting a later manual collapse', async () => {
  layoutMocks.prefs.ui_prefs = { nav_groups: JSON.stringify({ 'group:workspace': true }) }
  await mountGrouped('/')
  const folder = host.querySelector<HTMLButtonElement>('button[aria-label="交易工作台"]')!
  expect(folder.getAttribute('aria-expanded')).toBe('true')
  await act(async () => folder.click())
  expect(folder.getAttribute('aria-expanded')).toBe('false')
  await mountGrouped('/')
  expect(folder.getAttribute('aria-expanded')).toBe('false')
})

it('loads visible mobile group statistics even when desktop rail mode was saved', async () => {
  layoutMocks.desktop = false
  localStorage.setItem('tf-nav-state', 'rail')
  await mountGrouped('/watchlist')
  const stats = client.getQueryCache().find({ queryKey: QK.watchlistEnriched(), exact: true })!
  expect(stats.isActive()).toBe(false)
  await act(async () => host.querySelector<HTMLButtonElement>('button[title="打开菜单"]')!.click())
  expect(stats.isActive()).toBe(true)
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function toggleWithCachedPermission(cachedAllowed: boolean, currentAllowed: boolean) {
  const status = (allowed: boolean) => ({
    enabled: false, running: false, paused: false, is_trading_hours: false,
    mode: allowed ? 'full_market' : 'none', realtime_allowed: allowed,
  }) as Awaited<ReturnType<typeof api.quoteStatus>>
  client.setQueryData(QK.quoteStatus, status(cachedAllowed))
  vi.mocked(api.quoteStatus).mockResolvedValue(status(currentAllowed))
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><Layout /></QueryClientProvider></MemoryRouter>,
  ))
  const button = host.querySelector<HTMLButtonElement>('button[aria-label="开启实时行情"]')
  expect(button).not.toBeNull()
  await act(async () => button!.click())
  // Query/mutation observer notifications are batched on a timer tick.
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

it('切到 fuyao 后开启时重新校验权限, 不复用 TickFlow 的旧不可用状态', async () => {
  await toggleWithCachedPermission(false, true)
  expect(api.quoteStatus).toHaveBeenCalled()
  expect(api.updateRealtimeQuotes).toHaveBeenCalledWith(true)
  expect(toast).not.toHaveBeenCalled()
})

it('当前源已不可用时不复用旧可用状态开启', async () => {
  await toggleWithCachedPermission(true, false)
  expect(api.quoteStatus).toHaveBeenCalled()
  expect(api.updateRealtimeQuotes).not.toHaveBeenCalled()
  expect(toast).toHaveBeenCalledWith(expect.stringContaining('数据源'), 'error')
})

it.each(['succeeded', 'failed'] as const)('离开数据页后, 分钟补拉 %s 仍刷新体检和分钟缓存', async status => {
  vi.mocked(api.pipelineJobs).mockResolvedValue({ active_id: null, jobs: [{
    id: 'repair-job', status, stage: status === 'succeeded' ? 'done' : 'sync_minute', progress: 100,
    stage_pct: 100, started_at: null, finished_at: null, duration_s: 1,
    result: status === 'succeeded' ? { minute_rows: 480, universe_size: 2 } : null,
    error: status === 'failed' ? '部分补拉失败' : null,
  }] })
  const keys = [QK.minuteGaps, QK.minuteBatch(), QK.klineMinute('600000.SH', '2026-10-20'), QK.klineMinuteRange('600000.SH', 10)]
  keys.forEach(key => client.setQueryData(key, {}))
  const unaffected = QK.indexMinute('000001.SH', '2026-10-20')
  client.setQueryData(unaffected, {})
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><Layout /></QueryClientProvider></MemoryRouter>,
  ))
  await act(async () => { await new Promise(r => setTimeout(r, 20)) })
  expect(client.getQueryData(QK.minuteBatch())).toBeUndefined()
  keys.filter(key => key[0] !== 'minute-batch').forEach(key => expect(client.getQueryState(key)?.isInvalidated).toBe(true))
  expect(client.getQueryState(unaffected)?.isInvalidated).toBe(false)
})
