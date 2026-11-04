// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type MinuteKlineRow } from './api'
import { QK } from './queryKeys'
import { pollingInterval } from './refreshPolicy'
import { useMinuteBatchQuery } from './useMinuteBatchQuery'

vi.mock('./api', () => ({ api: { klineMinuteBatch: vi.fn() } }))

let host: HTMLDivElement
let root: Root
let client: QueryClient

const bars: MinuteKlineRow[] = ['09:31', '09:32'].map(datetime => ({
  datetime, open: 10, close: 10, high: 10, low: 10, volume: 100, amount: 1000,
}))

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  vi.clearAllMocks()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.mocked(api.klineMinuteBatch).mockImplementation(async symbols => ({
    data: Object.fromEntries(symbols.map(symbol => [symbol, bars])),
  }))
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

async function tick(ms = 1) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

function Probe({ symbols, polling = false, visible = true, renderTick = 0 }: {
  symbols: string[]; polling?: boolean; visible?: boolean; renderTick?: number
}) {
  const query = useMinuteBatchQuery(symbols, {
    enabled: visible,
    preferLocal: true,
    refetchInterval: polling ? pollingInterval(100, false, visible) : false,
  })
  return <output data-tick={renderTick}>{symbols.map(symbol =>
    `${symbol}:${query.data?.data[symbol]?.length ?? 0}`).join(',')}</output>
}

async function render(props: Parameters<typeof Probe>[0]) {
  await act(async () => root.render(
    <QueryClientProvider client={client}><Probe {...props} /></QueryClientProvider>,
  ))
  await tick()
}

it('loads newly selected symbols immediately even with fresh shared cache and polling disabled', async () => {
  await render({ symbols: ['a', 'shared'] })
  expect(host.textContent).toBe('a:2,shared:2')
  await render({ symbols: ['b', 'shared'] })
  await tick()
  expect(host.textContent).toBe('b:2,shared:2')
  expect(client.getQueryData<{ data: object }>(QK.minuteBatch())?.data).toHaveProperty('a')
  expect(api.klineMinuteBatch).toHaveBeenCalledWith(['b'], undefined, true, undefined)
})

it('does not postpone polling when quote updates repeatedly rerender the page', async () => {
  let randomCalls = 0
  vi.spyOn(Math, 'random').mockImplementation(() => (++randomCalls % 2 ? 0.1 : 0.9))
  await render({ symbols: ['a'], polling: true })
  for (let renderTick = 1; renderTick <= 20; renderTick++) {
    await tick(20)
    await render({ symbols: ['a'], polling: true, renderTick })
  }
  expect(vi.mocked(api.klineMinuteBatch).mock.calls.length).toBeGreaterThanOrEqual(4)
})

it('waits for an in-flight old scope, then fetches the latest selected symbols', async () => {
  let finish!: (value: { data: Record<string, MinuteKlineRow[]> }) => void
  vi.mocked(api.klineMinuteBatch).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  await render({ symbols: ['a'] })
  await render({ symbols: ['b'] })
  await render({ symbols: ['c'] })
  await act(async () => finish({ data: { a: bars } }))
  await tick()
  await tick()
  expect(host.textContent).toBe('c:2')
  expect(vi.mocked(api.klineMinuteBatch).mock.calls.map(call => call[0])).toEqual([['a'], ['c']])
})

it('does not repeatedly request genuine empty data or errors', async () => {
  vi.mocked(api.klineMinuteBatch).mockResolvedValueOnce({ data: {} })
  await render({ symbols: ['empty'] })
  await render({ symbols: ['empty'], renderTick: 1 })
  await tick(1000)
  expect(api.klineMinuteBatch).toHaveBeenCalledTimes(1)
  vi.mocked(api.klineMinuteBatch).mockRejectedValueOnce(new Error('unavailable'))
  await render({ symbols: ['failed'] })
  await tick(1000)
  await render({ symbols: ['failed'], renderTick: 2 })
  expect(api.klineMinuteBatch).toHaveBeenCalledTimes(2)
})

it('pauses hidden scopes and loads their latest symbols on visibility restore', async () => {
  await render({ symbols: ['a'] })
  await render({ symbols: ['b'], visible: false, polling: true })
  await tick(1000)
  expect(api.klineMinuteBatch).toHaveBeenCalledTimes(1)
  await render({ symbols: ['b'] })
  await tick()
  expect(host.textContent).toBe('b:2')
})
