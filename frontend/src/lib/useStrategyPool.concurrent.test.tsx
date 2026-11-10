// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useStrategyPool } from './useStrategyPool'
import { QK } from './queryKeys'
import type { StrategyPoolSnapshot } from './api'

const mocks = vi.hoisted(() => ({ get: vi.fn(), add: vi.fn(), set: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  strategyPoolSnapshot: mocks.get, strategyPoolAdd: mocks.add, strategyPoolSetEntries: mocks.set,
} }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

const snap = (pool: string[], revision = 0): StrategyPoolSnapshot => ({ groups: [], pool, meta: {}, revision })
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
const roots: Root[] = [], clients: QueryClient[] = [], hosts: HTMLElement[] = []
let current: ReturnType<typeof useStrategyPool>

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  vi.clearAllMocks()
  localStorage.clear()
  mocks.get.mockResolvedValue(snap([]))
})
afterEach(async () => {
  for (const root of roots.splice(0)) await act(async () => root.unmount())
  clients.splice(0).forEach(client => client.clear())
  hosts.splice(0).forEach(host => host.remove())
  vi.clearAllTimers()
  vi.useRealTimers()
})
async function flush() { await act(async () => { await vi.advanceTimersByTimeAsync(0) }) }
async function mount(client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  hosts.push(host)
  clients.push(client)
  function Probe() { current = useStrategyPool(); return null }
  const root = createRoot(host)
  roots.push(root)
  await act(async () => root.render(<QueryClientProvider client={client}><Probe /></QueryClientProvider>))
  await flush()
  return client
}

it('serializes rapid changes across consumers so later actions cannot overtake earlier ones', async () => {
  localStorage.setItem('tsp-strategy-pool-v2-migrated', 'true')
  const first = deferred<StrategyPoolSnapshot>(), second = deferred<StrategyPoolSnapshot>()
  mocks.add.mockImplementation((id: string) => id === 'a' ? first.promise : second.promise)
  const client = await mount()
  const firstConsumer = current
  await mount(client)
  await act(async () => { firstConsumer.addToPool('a'); current.addToPool('b') })
  await flush()
  expect(mocks.add).toHaveBeenCalledTimes(1)
  await act(async () => first.resolve(snap(['a'], 1)))
  await flush()
  expect(mocks.add).toHaveBeenCalledTimes(2)
  await act(async () => second.resolve(snap(['a', 'b'], 2)))
  await flush()
  expect(current.pool).toEqual(['a', 'b'])
  expect(localStorage.getItem('strategy-pool')).toBe('["a","b"]')
})

it('does not replace a newer cached revision with an older read response', async () => {
  localStorage.setItem('tsp-strategy-pool-v2-migrated', 'true')
  const pending = deferred<StrategyPoolSnapshot>()
  mocks.get.mockReturnValueOnce(pending.promise)
  const client = await mount()
  await act(async () => { client.setQueryData(QK.strategyPool, snap(['new'], 5)) })
  await act(async () => pending.resolve(snap(['old'], 4)))
  await flush()
  expect(current.pool).toEqual(['new'])
})

it('retries a failed legacy migration after remount and marks it only after success', async () => {
  localStorage.setItem('strategy-pool', '["legacy"]')
  mocks.set.mockRejectedValueOnce(new Error('network unavailable')).mockResolvedValue(snap(['legacy'], 1))
  await mount()
  await flush()
  expect(mocks.set).toHaveBeenCalledTimes(1)
  expect(localStorage.getItem('tsp-strategy-pool-v2-migrated')).not.toBe('true')
  expect(localStorage.getItem('strategy-pool')).toBe('["legacy"]')
  await mount()
  await flush()
  expect(mocks.set).toHaveBeenCalledTimes(2)
  expect(localStorage.getItem('tsp-strategy-pool-v2-migrated')).toBe('true')
  expect(current.pool).toEqual(['legacy'])
})

it('continues the mutation queue after a failed request', async () => {
  localStorage.setItem('tsp-strategy-pool-v2-migrated', 'true')
  mocks.add.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(snap(['b'], 1))
  await mount()
  await act(async () => { current.addToPool('a'); current.addToPool('b') })
  await flush()
  expect(current.pool).toEqual(['b'])
})
