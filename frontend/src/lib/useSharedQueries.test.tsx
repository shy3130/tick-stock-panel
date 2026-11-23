// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type DataStatus } from './api'
import { useDataStatus } from './useSharedQueries'

vi.mock('./api', () => ({ api: { dataStatus: vi.fn() } }))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  vi.clearAllMocks()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.useRealTimers()
})

async function tick(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

async function mount(opts?: Parameters<typeof useDataStatus>[0]) {
  function Probe() {
    const { data } = useDataStatus(opts)
    return <output>{data?.enriched?.latest_date}</output>
  }
  await act(async () => root.render(
    <QueryClientProvider client={client}><Probe /></QueryClientProvider>,
  ))
  await tick(1)
}

it('cold snapshot is polled until statistics arrive, then polling stops', async () => {
  vi.mocked(api.dataStatus).mockResolvedValueOnce({ stats_pending: true } as DataStatus)
    .mockResolvedValue({ stats_pending: false, enriched: { latest_date: '2026-11-04' } } as DataStatus)
  await mount()
  expect(api.dataStatus).toHaveBeenCalledTimes(1)
  await tick(2_001)
  expect(host.textContent).toBe('2026-11-04')
  expect(api.dataStatus).toHaveBeenCalledTimes(2)
  await tick(60_000)
  expect(api.dataStatus).toHaveBeenCalledTimes(2)
})

it('an explicit polling opt-out is preserved', async () => {
  vi.mocked(api.dataStatus).mockResolvedValue({ stats_pending: true } as DataStatus)
  await mount({ refetchInterval: false })
  await tick(10_000)
  expect(api.dataStatus).toHaveBeenCalledTimes(1)
})
