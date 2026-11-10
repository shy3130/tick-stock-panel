// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type MinuteKlineRow, type PriceLimitInfo } from '@/lib/api'
import { useMinuteBatchQuery } from '@/lib/useMinuteBatchQuery'
import { StockIntradayChart } from './StockIntradayChart'
import { StockMultiDayIntradayChart } from './StockMultiDayIntradayChart'

vi.mock('@/lib/api', () => ({ api: {
  klineMinuteBatch: vi.fn(), klineMinute: vi.fn(), klineMinuteRange: vi.fn(), syncMinuteSingle: vi.fn(),
} }))
vi.mock('@/components/EChartsIntraday', () => ({ EChartsIntraday: () => <div>分时图</div> }))
vi.mock('@/components/EChartsMultiDayIntraday', () => ({
  EChartsMultiDayIntraday: ({ sessions }: { sessions: { rows: MinuteKlineRow[] }[] }) =>
    <div data-testid="multi-count">{sessions.reduce((n, session) => n + session.rows.length, 0)}</div>,
}))
vi.mock('@/components/charts/LightweightIntraday', () => ({
  LightweightIntraday: ({ sessions, data, priceLimit }: { sessions?: { rows: MinuteKlineRow[] }[]; data?: MinuteKlineRow[]; priceLimit?: PriceLimitInfo }) => <>
    <div data-testid="multi-count">{sessions ? sessions.reduce((n, session) => n + session.rows.length, 0) : data?.length ?? 0}</div>
    <span data-testid="minute-meta">{JSON.stringify({ priceLimit })}</span>
  </>,
}))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

const SYMBOL = '159558.SZ'
const DAY = '2026-11-05'
const bars: MinuteKlineRow[] = ['09:31', '09:32'].map(time => ({
  datetime: `${DAY}T${time}:00`, open: 0.75, high: 0.77, low: 0.74, close: 0.76, volume: 100, amount: 76,
}))
let host: HTMLDivElement
let root: Root
let client: QueryClient
let available: boolean
let completeSync: () => void

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  vi.clearAllMocks()
  available = false
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.mocked(api.klineMinuteBatch).mockImplementation(async (): Promise<{ data: Record<string, MinuteKlineRow[]> }> => ({
    data: available ? { [SYMBOL]: bars } : {},
  }))
  vi.mocked(api.klineMinute).mockImplementation(async () => ({
    symbol: SYMBOL, date: DAY, asset_type: 'etf', rows: available ? bars : [], source: available ? 'local' : 'none',
  }))
  vi.mocked(api.klineMinuteRange).mockImplementation(async () => ({
    symbol: SYMBOL, asset_type: 'etf', requested_days: 1, source: available ? 'local' : 'none',
    sessions: available ? [{ date: DAY, prev_close: 0.75, rows: bars }] : [],
  }))
  vi.mocked(api.syncMinuteSingle).mockImplementation(() => new Promise(resolve => {
    completeSync = () => { available = true; resolve({ status: 'ok', symbol: SYMBOL, rows: 2 }) }
  }))
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.useRealTimers()
})

function ListPreview() {
  const query = useMinuteBatchQuery([SYMBOL], { enabled: true, preferLocal: true, refetchInterval: false })
  return <output>{query.data?.data[SYMBOL]?.length ?? 0}</output>
}

async function tick() {
  await act(async () => { await vi.advanceTimersByTimeAsync(100) })
}

it.each([
  ['single', 'echarts'], ['multi', 'echarts'], ['single', 'lightweight'], ['multi', 'lightweight'],
] as const)('ETF %s %s detail sync refreshes the visible list without polling', async (mode, renderer) => {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <ListPreview />
    {mode === 'single' ? <StockIntradayChart symbol={SYMBOL} date={DAY} renderer={renderer} /> : <StockMultiDayIntradayChart symbol={SYMBOL} days={1} renderer={renderer} />}
  </QueryClientProvider>))
  await tick()
  expect(host.querySelector('output')?.textContent).toBe('0')
  if (mode === 'single') {
    const button = Array.from(host.querySelectorAll('button')).find(el => el.textContent === '重新获取')!
    await act(async () => button.click())
  }
  expect(api.syncMinuteSingle).toHaveBeenCalledOnce()
  await act(async () => completeSync())
  await tick()
  expect(host.querySelector('output')?.textContent).toBe('2')
  expect(api.klineMinuteBatch).toHaveBeenCalledTimes(2)
})

it.each(['echarts', 'lightweight'] as const)('%s: a delayed live response cannot replace newer persisted minute bars', async renderer => {
  available = true
  vi.mocked(api.klineMinute).mockResolvedValue({ symbol: SYMBOL, date: DAY, asset_type: 'etf', rows: bars.slice(0, 1), source: 'live' })
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StockMultiDayIntradayChart symbol={SYMBOL} days={1} renderer={renderer} />
  </QueryClientProvider>))
  await tick()
  expect(host.querySelector('[data-testid="multi-count"]')?.textContent).toBe('2')
  expect(api.syncMinuteSingle).not.toHaveBeenCalled()
})

it.each([[DAY, 1, true], ['2026-11-04', 1, false], [DAY, 2, false]] as const)(
  'passes limit metadata only for one matching session: latest=%s days=%s', async (latestDay, days, expected) => {
    available = true
    const priceLimit = { rate: 0.1, limit_up: 0.825, limit_down: 0.675, source: 'instrument' } as const
    const previousRows = bars.map(row => ({ ...row, datetime: row.datetime.replace(DAY, '2026-11-04') }))
    vi.mocked(api.klineMinute).mockResolvedValue({ symbol: SYMBOL, date: latestDay, asset_type: 'etf', prev_close: 0.75,
      rows: latestDay === DAY ? bars : previousRows, source: 'live', price_limit: priceLimit })
    vi.mocked(api.klineMinuteRange).mockResolvedValue({ symbol: SYMBOL, asset_type: 'etf', requested_days: days, source: 'local', sessions: [
      { date: DAY, prev_close: 0.75, rows: bars }, ...(days === 2 ? [{ date: '2026-11-04', prev_close: 0.75, rows: previousRows }] : []),
    ] })
    await act(async () => root.render(<QueryClientProvider client={client}>
      <StockMultiDayIntradayChart symbol={SYMBOL} days={days} renderer="lightweight" />
    </QueryClientProvider>))
    await tick()
    expect(JSON.parse(host.querySelector('[data-testid="minute-meta"]')!.textContent!).priceLimit).toEqual(expected ? priceLimit : undefined)
    expect(api.syncMinuteSingle).not.toHaveBeenCalled()
  },
)

it.each(['echarts', 'lightweight'] as const)('%s: afternoon-only live data keeps local morning bars and flags a partial historical session', async renderer => {
  const afternoon = { ...bars[0], datetime: `${DAY}T13:01:00` }
  vi.mocked(api.klineMinute).mockResolvedValue({ symbol: SYMBOL, date: DAY, asset_type: 'stock', rows: [afternoon], source: 'live' })
  vi.mocked(api.klineMinuteRange).mockResolvedValue({ symbol: SYMBOL, asset_type: 'stock', requested_days: 1, source: 'local',
    sessions: [{ date: DAY, partial: true, prev_close: 0.75, rows: [...bars, afternoon] }],
  })
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StockMultiDayIntradayChart symbol={SYMBOL} days={1} renderer={renderer} />
  </QueryClientProvider>))
  await tick()
  expect(host.querySelector('[data-testid="multi-count"]')?.textContent).toBe('3')
  expect(host.textContent).toContain(`本地历史分时不完整：${DAY}`)
  expect(api.syncMinuteSingle).not.toHaveBeenCalled()
})

it('reads repaired morning bars into an afternoon-only list cache', async () => {
  const afternoon = bars.map(row => ({ ...row, datetime: row.datetime.replace('T09:', 'T13:') }))
  vi.mocked(api.klineMinuteBatch).mockImplementation(async (_symbols, _date, _preferLocal, since) => ({
    data: { [SYMBOL]: (available ? [...bars, ...afternoon] : afternoon).filter(row => !since || row.datetime >= since) },
  }))
  await act(async () => root.render(<QueryClientProvider client={client}>
    <ListPreview /><StockMultiDayIntradayChart symbol={SYMBOL} days={1} />
  </QueryClientProvider>))
  await tick()
  expect(host.querySelector('output')?.textContent).toBe('2')
  await act(async () => completeSync())
  await tick()
  expect(host.querySelector('output')?.textContent).toBe('4')
})
