// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type ExtDataConfig } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { ExtDataPullPanel } from './ExtDataPullPanel'

vi.mock('@/lib/api', () => ({ api: { extDataPullRun: vi.fn() } }))

const config: ExtDataConfig = {
  id: 'sparse', label: '稀疏数据', mode: 'timeseries', fields: [],
  created_at: '', updated_at: '',
  pull: {
    url: 'https://example.test/sparse', method: 'GET', response_path: 'items',
    schedule_minutes: 1440, timeout_seconds: 30, enabled: false,
  },
}
const auctionKey = QK.extSeries('auction:intraday:sparse:price', '2026-11-06', '600000.SH')
const otherKeys = [QK.klineMinute('600000.SH', '2026-11-06'), QK.preferences]
let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })
  for (const key of [auctionKey, QK.extDataSchemaAll, QK.extSchemaAll,
    QK.extDataRows('sparse'), QK.watchlistEnriched('sparse.price'),
    QK.screenerCached('2026-11-06', 'sparse.price'),
    QK.kline('600000.SH', '2026-01-01', '2026-11-06', 'sparse.price'), ...otherKeys]) {
    client.setQueryData(key, {})
  }
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function run(onSaved = vi.fn()) {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <ExtDataPullPanel config={config} onSaved={onSaved} />
  </QueryClientProvider>))
  const button = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('立即执行'))!
  await act(async () => button.click())
  return onSaved
}

it('refreshes cached empty auction series, schemas and list values after a successful pull', async () => {
  vi.mocked(api.extDataPullRun).mockResolvedValue({ status: 'ok', rows: 601, date: '2026-11-06' })
  const onSaved = await run()

  expect(onSaved).toHaveBeenCalledOnce()
  expect(client.getQueryState(auctionKey)?.isInvalidated).toBe(true)
  expect(client.getQueryState(QK.extDataSchemaAll)?.isInvalidated).toBe(true)
  expect(client.getQueryState(QK.extSchemaAll)?.isInvalidated).toBe(true)
  expect(client.getQueryState(QK.watchlistEnriched('sparse.price'))?.isInvalidated).toBe(true)
  expect(client.getQueryState(QK.screenerCached('2026-11-06', 'sparse.price'))?.isInvalidated).toBe(true)
  expect(client.getQueryState(QK.kline('600000.SH', '2026-01-01', '2026-11-06', 'sparse.price'))?.isInvalidated).toBe(true)
  for (const key of otherKeys) expect(client.getQueryState(key)?.isInvalidated).toBe(false)
})

it('keeps cached data intact when pulling fails', async () => {
  vi.mocked(api.extDataPullRun).mockRejectedValue(new Error('拉取失败'))
  const onSaved = await run()

  expect(host.textContent).toContain('拉取失败')
  expect(onSaved).not.toHaveBeenCalled()
  expect(client.getQueryState(auctionKey)?.isInvalidated).toBe(false)
})
