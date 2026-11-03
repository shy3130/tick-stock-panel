// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

let host: HTMLDivElement
let root: Root | undefined
let current: ReturnType<typeof import('./useStrategyPool').useStrategyPool>
let remote: Record<string, unknown>
let read: () => Promise<Response>
let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetModules()
  vi.useFakeTimers()
  localStorage.clear()
  remote = {}
  read = async () => Response.json({ ui_prefs: { ...remote } })
  fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === 'PUT') {
      Object.assign(remote, JSON.parse(init.body as string))
      return Response.json({ ui_prefs: remote })
    }
    return read()
  })
  vi.stubGlobal('fetch', fetchMock)
  host = document.createElement('div')
  document.body.appendChild(host)
})

afterEach(async () => {
  if (root) await act(async () => root!.unmount())
  root = undefined
  host.remove()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

async function mount() {
  const { useStrategyPool } = await import('./useStrategyPool')
  function Probe() {
    current = useStrategyPool()
    return <output>{JSON.stringify(current.pool)}</output>
  }
  root = createRoot(host)
  await act(async () => root!.render(<Probe />))
}

async function flushWrites() {
  await act(async () => { await vi.advanceTimersByTimeAsync(300) })
}

it('restores the chosen pool after restart with a different origin cache', async () => {
  await mount()
  await act(async () => { current.addToPool('daily'); current.addToPool('minute') })
  await flushWrites()
  expect(remote.strategy_pool).toBe('["daily","minute"]')

  await act(async () => root!.unmount())
  root = undefined
  localStorage.clear()
  vi.resetModules()
  await mount()
  expect(current.pool).toEqual(['daily', 'minute'])
  expect(localStorage.getItem('strategy-pool')).toBe('["daily","minute"]')
})

it('restores an explicitly empty pool over a stale cache from an old port', async () => {
  remote.strategy_pool = '[]'
  localStorage.setItem('strategy-pool', '["stale"]')
  await mount()
  await flushWrites()
  expect(current.pool).toEqual([])
  expect(remote.strategy_pool).toBe('[]')
})

it('migrates the old daily and minute pools without losing order', async () => {
  localStorage.setItem('strategy-pool', '["daily"]')
  localStorage.setItem('strategy-pool-1m', '["minute","daily"]')
  await mount()
  await flushWrites()
  expect(current.pool).toEqual(['daily', 'minute'])
  expect(remote.strategy_pool).toBe('["daily","minute"]')
  expect(localStorage.getItem('strategy-pool-1m')).toBeNull()
})

it('keeps a user choice made while startup hydration is pending', async () => {
  let resolve!: (response: Response) => void
  read = () => new Promise<Response>(done => { resolve = done })
  await mount()
  await act(async () => current.addToPool('clicked'))
  await act(async () => resolve(Response.json({ ui_prefs: { strategy_pool: '["old"]' } })))
  await flushWrites()
  expect(current.pool).toEqual(['clicked'])
  expect(remote.strategy_pool).toBe('["clicked"]')
})

it('does not write the initial empty pool over saved choices during slow hydration', async () => {
  let resolve!: (response: Response) => void
  read = () => new Promise<Response>(done => { resolve = done })
  await mount()
  await flushWrites()
  expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'PUT')).toHaveLength(0)
  await act(async () => resolve(Response.json({ ui_prefs: { strategy_pool: '["saved"]' } })))
  expect(current.pool).toEqual(['saved'])
})

it('applies an already-loaded strategy list to a pool restored later', async () => {
  let resolve!: (response: Response) => void
  read = () => new Promise<Response>(done => { resolve = done })
  await mount()
  await act(async () => current.prune(['valid']))
  await act(async () => resolve(Response.json({ ui_prefs: { strategy_pool: '["deleted","valid"]' } })))
  await flushWrites()
  expect(current.pool).toEqual(['valid'])
  expect(remote.strategy_pool).toBe('["valid"]')
})

it('keeps local choices when the backend is unavailable', async () => {
  localStorage.setItem('strategy-pool', '["saved"]')
  read = async () => { throw new Error('offline') }
  await mount()
  expect(current.pool).toEqual(['saved'])
  await act(async () => current.addToPool('new'))
  expect(current.pool).toEqual(['saved', 'new'])
  expect(localStorage.getItem('strategy-pool')).toBe('["saved","new"]')
})

it('retries hydration after login if the initial preferences request was unauthorized', async () => {
  const { hydrateUiPrefs } = await import('./uiPrefs')
  read = async () => new Response(null, { status: 401 })
  await hydrateUiPrefs()
  remote.strategy_pool = '["saved"]'
  read = async () => Response.json({ ui_prefs: remote })
  await mount()
  expect(current.pool).toEqual(['saved'])
})

it('preserves local-first behavior for other existing UI preferences', async () => {
  localStorage.setItem('alert_toast_max', '3')
  remote.alert_toast_max = 9
  remote.strategy_pool = '["saved"]'
  await mount()
  await flushWrites()
  expect(localStorage.getItem('alert_toast_max')).toBe('3')
  expect(remote.alert_toast_max).toBe(3)
  expect(current.pool).toEqual(['saved'])
})
