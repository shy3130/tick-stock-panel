// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useWatchlistDailyBatch } from '@/lib/useWatchlistDailyBatch'
import { Watchlist } from './Watchlist'

vi.mock('@/extensions/ExtensionSlot', () => ({ ExtensionSlot: () => null }))
vi.mock('@/lib/watchlist-columns', async importOriginal => {
  const mod = await importOriginal<typeof import('@/lib/watchlist-columns')>()
  return { ...mod, loadColumnConfig: async () => mod.BUILTIN_COLUMNS
    .filter(c => ['builtin:symbol', 'builtin:candle', 'builtin:intraday'].includes(c.id))
    .map(c => ({ ...c, visible: true })) }
})
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: { minute_intraday_refresh: false } }),
  useCapabilities: () => ({ data: { capabilities: { 'kline.minute.batch': {} } } }),
  useQuoteStatus: () => ({ data: { running: false } }),
}))
vi.mock('@/components/stock-table/MiniCandlestick', () => ({
  MiniCandlestick: ({ rows }: { rows: unknown[] }) => <output data-chart="daily">{rows.length}</output>,
}))
vi.mock('@/components/stock-table/MiniIntraday', () => ({
  MiniIntraday: ({ rows }: { rows: unknown[] }) => <output data-chart="minute">{rows.length}</output>,
}))

const existing = '600000.SH'
const added = '000001.SZ'
const daily = ['2026-11-03', '2026-11-04'].map(date => ({ date, open: 10, high: 11, low: 9, close: 10 }))
const minute = ['09:31', '09:32'].map(time => ({ ...daily[1], datetime: `2026-11-04T${time}:00`, volume: 1, amount: 1000 }))
const host = document.createElement('div')
let root: ReturnType<typeof createRoot>
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

async function settle() {
  for (let i = 0; i < 5; i++) await act(async () => { await vi.advanceTimersByTimeAsync(20) })
}

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

it.each([1, 65])('loads both charts after a quick add to %i existing rows while enriched is delayed and polling is off', async count => {
  const entries = Array.from({ length: count }, (_, i) => `${600000 + i}.SH`)
  const list = () => ({ symbols: entries.map(symbol => ({ symbol, added_at: '2026-11-04' })) })
  const enriched = () => ({ rows: entries.map(symbol => ({ symbol, asset_type: 'stock', close: 10 })), as_of: null, elapsed_ms: 0 })
  let release!: () => void
  const blocked = new Promise<void>(resolve => { release = resolve })
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}')))
  vi.spyOn(api, 'watchlistList').mockImplementation(async () => list())
  vi.spyOn(api, 'watchlistGroups').mockResolvedValue({ groups: [] })
  vi.spyOn(api, 'watchlistEnriched').mockImplementation(async () => {
    if (entries.includes(added)) await blocked
    return enriched()
  })
  vi.spyOn(api, 'watchlistAdd').mockImplementation(async symbol => { entries.push(symbol); return list() })
  vi.spyOn(api, 'instrumentSearch').mockResolvedValue({ results: [{ symbol: added, name: '新股', code: '000001' }] })
  const dailyRequest = vi.spyOn(api, 'klineDailyBatch').mockImplementation(async symbols => ({ data: Object.fromEntries(symbols.map(s => [s, daily])) }))
  vi.spyOn(api, 'klineMinuteBatch').mockImplementation(async symbols => ({ data: Object.fromEntries(symbols.map(s => [s, minute])) }))
  const counts = (symbol: string) => {
    const row = Array.from(host.querySelectorAll('tbody tr')).find(tr => tr.textContent?.includes(symbol))
    return Array.from(row?.querySelectorAll('output[data-chart]') ?? [], el => el.textContent)
  }
  await act(async () => root.render(<MemoryRouter><QueryClientProvider client={client}><Watchlist /></QueryClientProvider></MemoryRouter>))
  await settle()
  expect(counts(existing)).toEqual(['2', '2'])
  dailyRequest.mockClear()
  const input = host.querySelector<HTMLInputElement>('input[placeholder="搜索…"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '000001')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await settle()
  const add = host.querySelector<HTMLButtonElement>(`button[aria-label="快速加入自选 ${added}"]`)!
  await act(async () => add.click())
  await settle()
  await act(async () => release())
  await settle()
  expect(counts(added)).toEqual(['2', '2'])
  expect(counts(existing)).toEqual(['2', '2'])
  expect(dailyRequest.mock.calls.map(([symbols]) => symbols)).toEqual([[added]])
})

it('fills the latest daily scope after an older request finishes and retries empty results on explicit refresh', async () => {
  let release!: () => void
  const blocked = new Promise<void>(resolve => { release = resolve })
  const request = vi.spyOn(api, 'klineDailyBatch')
    .mockImplementationOnce(async () => { await blocked; return { data: { [existing]: daily } } })
    .mockResolvedValueOnce({ data: {} })
    .mockImplementation(async symbols => ({ data: Object.fromEntries(symbols.map(s => [s, daily])) }))
  function Probe({ symbols }: { symbols: string[] }) {
    const query = useWatchlistDailyBatch(symbols, 12, true)
    return <output>{symbols.map(symbol => query.data?.data[symbol]?.length ?? '-').join(',')}</output>
  }
  async function render(symbols: string[]) {
    await act(async () => root.render(<QueryClientProvider client={client}><Probe symbols={symbols} /></QueryClientProvider>))
    await settle()
  }
  await render([existing])
  await render([existing, added])
  expect(request).toHaveBeenCalledTimes(1)
  await act(async () => release())
  await settle()
  await render([existing, added])
  expect(host.textContent).toBe('2,0')
  expect(request.mock.calls.map(([symbols]) => symbols)).toEqual([[existing], [added]])
  await act(async () => { await client.invalidateQueries({ queryKey: QK.watchlistKlineBatch(12) }) })
  await settle()
  expect(host.textContent).toBe('2,2')
  expect(request).toHaveBeenLastCalledWith([existing, added], 12)
})
