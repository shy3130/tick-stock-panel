// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from './api'
import { useWatchlistPrepare } from './useWatchlistPrepare'

vi.mock('./api', () => ({ api: { watchlistPrepare: vi.fn() } }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
const ETF = '159558.SZ'
const roots: ReturnType<typeof createRoot>[] = []
const clients: QueryClient[] = []
const hosts: HTMLDivElement[] = []

afterEach(async () => {
  await act(async () => roots.splice(0).forEach(root => root.unmount()))
  clients.splice(0).forEach(client => client.clear())
  hosts.splice(0).forEach(host => host.remove())
  vi.clearAllMocks()
})

function Panel({ ready = false }: { ready?: boolean }) {
  const query = useWatchlistPrepare([{ symbol: ETF, close: ready ? 1 : null }])
  return <output>{query.isFetching ? 'loading' : query.data?.[0]?.status}</output>
}

async function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(client)
  const host = document.createElement('div')
  document.body.append(host)
  hosts.push(host)
  const root = createRoot(host)
  roots.push(root)
  const render = async (ready = false) => {
    await act(async () => root.render(<QueryClientProvider client={client}><Panel ready={ready} /></QueryClientProvider>))
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 15)) })
  }
  return { client, host, render }
}

it('only missing data triggers preparation, ready results invalidate list and candle caches', async () => {
  vi.mocked(api.watchlistPrepare).mockResolvedValue({ results: [{ symbol: ETF, status: 'ready', message: '' }] })
  const { client, render } = await mount()
  client.setQueryData(['watchlist-enriched', ''], { rows: [] })
  client.setQueryData(['kline-batch', 20], { data: {} })
  await render(true)
  expect(api.watchlistPrepare).not.toHaveBeenCalled()
  await render()
  expect(api.watchlistPrepare).toHaveBeenCalledWith([ETF])
  expect(client.getQueryState(['watchlist-enriched', ''])?.isInvalidated).toBe(true)
  expect(client.getQueryState(['kline-batch', 20])?.isInvalidated).toBe(true)
})

it('an empty/unsupported result never creates a preparation loop after list refreshes', async () => {
  vi.mocked(api.watchlistPrepare).mockResolvedValue({ results: [{ symbol: ETF, status: 'unsupported', message: '配置数据源' }] })
  const { render, host } = await mount()
  await render()
  await render()
  await render()
  expect(api.watchlistPrepare).toHaveBeenCalledOnce()
  expect(host.textContent).toBe('unsupported')
})
