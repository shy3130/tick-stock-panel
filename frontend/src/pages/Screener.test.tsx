// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Screener } from './Screener'
import { api } from '@/lib/api'

const state = vi.hoisted(() => ({
  pool: ['minute', 'daily'],
  prune: vi.fn(),
  builderSaved: null as null | ((id: string, researchOnly?: boolean) => Promise<void>),
  prefs: { screener_auto_run: false },
  status: { enriched: { latest_date: '2026-11-06', earliest_date: '2026-01-01' } },
}))
vi.mock('@/lib/api', async importOriginal => {
  const original = await importOriginal<typeof import('@/lib/api')>()
  return { ...original, api: { ...original.api,
    screenerStrategies: vi.fn(), screenerCachedSummary: vi.fn(), screenerCachedResult: vi.fn(),
    screenerRunPreset: vi.fn(), screenerRunAll: vi.fn(),
    watchlistList: vi.fn(async () => ({ symbols: [] })),
    monitorRulesList: vi.fn(async () => ({ rules: [] })),
    minuteRefreshStatus: vi.fn(async () => ({ enabled: false, running: false })),
  } }
})
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: state.prefs }),
  useDataStatus: () => ({ data: state.status }),
  useCapabilities: () => ({ data: { capabilities: {} } }),
  useQuoteStatus: () => ({ data: { running: false, is_trading_hours: false } }),
}))
vi.mock('@/lib/useStrategyPool', () => ({ useStrategyPool: () => ({
  pool: state.pool,
  groups: [],
  groupNames: new Map(),
  metaMap: new Map(state.pool.map(id => [id, { group: null, archived: false, archived_at: null, added_at: '2026-01-01T00:00:00' }])),
  archivedIds: [],
  isLoading: false,
  isError: false,
  addToPool: vi.fn(), removeFromPool: vi.fn(), reorderPool: vi.fn(),
  archiveStrategy: vi.fn(), unarchiveStrategy: vi.fn(), discardRecord: vi.fn(),
  setStrategyGroup: vi.fn(), createGroup: vi.fn(), renameGroup: vi.fn(),
  reorderGroups: vi.fn(), deleteGroup: vi.fn(),
  handleStrategyDeleted: vi.fn(),
  prune: state.prune, isInPool: (id: string) => state.pool.includes(id),
}) }))
vi.mock('@/lib/useSharedMutations', () => ({ useWatchlistBatchAdd: () => ({ mutate: vi.fn(), isPending: false }) }))
vi.mock('@/lib/screener-columns', () => ({
  SCREENER_BUILTIN_COLUMNS: [], SCREENER_COLUMN_GROUPS: [],
  buildExtColumnsParam: () => '', loadScreenerColumnConfig: async () => [],
  saveScreenerColumnConfig: vi.fn(), refreshExtColumns: (columns: unknown) => columns,
}))
vi.mock('@/components/ListColumnCustomizer', () => ({ ListColumnCustomizer: () => null }))
vi.mock('@/components/screener/StrategyBuilderDialog', () => ({ StrategyBuilderDialog: (props: { onSavedId: typeof state.builderSaved }) => {
  state.builderSaved = props.onSavedId
  return null
} }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
vi.mock('@/components/screener/ScreenerTable', () => ({
  ScreenerTable: ({ rows }: { rows: { symbol: string }[] }) => <output>{rows.map(row => row.symbol).join(',')}</output>,
}))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

let host: HTMLDivElement
let root: Root
let client: QueryClient
const day = '2026-11-06'
const errorMessage = '策略参数已变化，请重新计算'

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  vi.clearAllMocks()
  localStorage.clear()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} unobserve() {} })
  vi.stubGlobal('fetch', vi.fn(() => { throw new Error('Unexpected network request in Screener test') }))
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  vi.mocked(api.screenerStrategies).mockResolvedValue({ presets: [
    { id: 'minute', name: '分钟策略', source: 'custom', asset_types: ['stock'], timeframes: ['1m'] },
    { id: 'daily', name: '日线策略', source: 'custom', asset_types: ['stock'], timeframes: ['1d'] },
  ] } as any)
  vi.mocked(api.screenerCachedSummary).mockResolvedValue({ as_of: day, results: {
    minute: { as_of: day, total: 5 }, daily: { as_of: day, total: 1 },
  }, today_ever_counts: {}, minute_pending: [] } as any)
  vi.mocked(api.screenerCachedResult).mockResolvedValue({ result: {
    as_of: day, strategy: 'daily', total: 1, rows: [{ symbol: '000001.SZ', score: 1 }], elapsed_ms: 0,
  }, strategy_ids_by_symbol: {}, today_ever_rows: null } as any)
  vi.mocked(api.screenerRunPreset).mockRejectedValue(new Error(errorMessage))
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

async function tick() {
  await act(async () => { await vi.advanceTimersByTimeAsync(20) })
}

async function render() {
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><Screener /></QueryClientProvider></MemoryRouter>,
  ))
  await tick()
}

it('refreshes the complete daily and minute library before adding a saved minute strategy', async () => {
  const all = (await api.screenerStrategies(undefined, 'all')).presets
  vi.mocked(api.screenerStrategies).mockImplementation(async (_asset, timeframe = '1d') => ({
    presets: timeframe === 'all' ? all : all.filter(item => item.timeframes.includes('1d')),
    load_errors: [], legacy_migration: undefined,
  }))
  await render()
  await act(async () => { await state.builderSaved!('minute', false) })
  expect(api.screenerStrategies).toHaveBeenLastCalledWith(undefined, 'all')
  expect(api.screenerRunAll).not.toHaveBeenCalled()
})

async function clickCard(name: string) {
  const button = [...host.querySelectorAll('button')].find(el => el.textContent?.includes(name))
  expect(button).toBeTruthy()
  await act(async () => button!.click())
  await tick()
}

it('clears a failed minute run when opening a counted daily card, without recalculating the cached strategy', async () => {
  await render()
  await clickCard('分钟策略')
  expect(host.textContent).toContain(errorMessage)

  await clickCard('日线策略')

  expect(api.screenerCachedResult).toHaveBeenCalledWith('daily', undefined)
  expect(host.textContent).toContain('000001.SZ')
  expect(host.textContent).not.toContain(errorMessage)
  expect(api.screenerRunPreset).toHaveBeenCalledTimes(1)
  expect(api.screenerRunAll).not.toHaveBeenCalled()
  expect(fetch).not.toHaveBeenCalled()
})

it('still displays a genuine error from the currently selected strategy', async () => {
  await render()
  await clickCard('分钟策略')
  expect(host.textContent).toContain(errorMessage)
  expect(api.screenerRunPreset).toHaveBeenCalledWith('minute', undefined, undefined, undefined, 'stock', '1m', true)
})

it('does not attach a late failure from the previous card to the cached strategy now being viewed', async () => {
  let rejectRun!: (error: Error) => void
  vi.mocked(api.screenerRunPreset).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectRun = reject }))
  await render()
  await clickCard('分钟策略')
  await clickCard('日线策略')

  await act(async () => rejectRun(new Error(errorMessage)))
  await tick()

  expect(host.textContent).toContain('000001.SZ')
  expect(host.textContent).not.toContain(errorMessage)
  expect(api.screenerRunPreset).toHaveBeenCalledTimes(1)
})
