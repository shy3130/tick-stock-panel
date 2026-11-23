// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from './api'
import { useAuctionSeries, type AuctionSeriesMap } from './intraday-auction'

let host: HTMLDivElement
let root: Root | undefined
let client: QueryClient
let result: AuctionSeriesMap

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.appendChild(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})
afterEach(async () => {
  if (root) await act(async () => root!.unmount())
  root = undefined
  client.clear()
  host.remove()
  vi.restoreAllMocks()
})

async function render(symbols: string[]) {
  function Probe() {
    result = useAuctionSeries({ configId: 'auction', priceField: 'price', matchField: 'volume', unmatchedField: 'unmatched' }, symbols, '2026-11-05')
    return null
  }
  root = createRoot(host)
  await act(async () => { root!.render(<QueryClientProvider client={client}><Probe /></QueryClientProvider>) })
}

it('竞价叠加显式读取逐盘序列, 保持价格与量在同一时间网格', async () => {
  const series = vi.spyOn(api, 'extDataSeries').mockImplementation(async (id, field) => ({
    config_id: id, field, date: '2026-11-05', granularity: 'intraday',
    items: { '600000.SH': [['09:20:00', 10], ['09:25:00', 20]] },
  }))
  await render(['600000.SH', '600000.SH'])
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
  for (const field of ['price', 'volume', 'unmatched']) {
    expect(series).toHaveBeenCalledWith('auction', field, '2026-11-05', ['600000.SH'], undefined, 'intraday')
  }
  expect(series).toHaveBeenCalledTimes(3)
  expect(result['600000.SH']).toEqual({
    price: [['09:20:00', 10], ['09:25:00', 20]],
    match: [['09:20:00', 10], ['09:25:00', 20]],
    unmatched: [['09:20:00', 10], ['09:25:00', 20]],
  })
})

it('空标的列表不发竞价请求', async () => {
  const series = vi.spyOn(api, 'extDataSeries')
  await render([])
  expect(series).not.toHaveBeenCalled()
})
