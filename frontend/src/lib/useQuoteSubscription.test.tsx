// @vitest-environment jsdom
import { act, StrictMode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from './api'
import { useQuoteSubscription } from './useQuoteSubscription'

vi.mock('./api', () => ({ api: { subscribeQuotes: vi.fn(async () => ({ lease_seconds: 90 })), unsubscribeQuotes: vi.fn(async () => ({})) } }))
let root: Root
let host: HTMLDivElement
function Detail({ symbol }: { symbol: string }) { useQuoteSubscription(symbol); return null }
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers(); vi.clearAllMocks()
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  host = document.createElement('div'); root = createRoot(host)
})
afterEach(async () => { await act(async () => root.unmount()); vi.useRealTimers(); vi.restoreAllMocks() })

it('renews the same lease, replaces it on navigation and releases on close', async () => {
  await act(async () => root.render(<Detail symbol="000985.SH" />))
  const first = vi.mocked(api.subscribeQuotes).mock.calls[0][0]
  await act(async () => vi.advanceTimersByTimeAsync(20_000))
  expect(vi.mocked(api.subscribeQuotes).mock.calls[1][0]).toBe(first)
  await act(async () => root.render(<Detail symbol="159558.SZ" />))
  expect(api.unsubscribeQuotes).toHaveBeenCalledWith(first)
  const calls = vi.mocked(api.subscribeQuotes).mock.calls
  expect(calls.at(-1)?.[1]).toEqual(['159558.SZ'])
  expect(calls.at(-1)?.[0]).not.toBe(first)
})

it('two consumers have independent leases; StrictMode cleanup cannot cancel the new lease', async () => {
  await act(async () => root.render(<StrictMode><Detail symbol="000985.SH" /><Detail symbol="000985.SH" /></StrictMode>))
  const calls = vi.mocked(api.subscribeQuotes).mock.calls
  expect(new Set(calls.map(c => c[0])).size).toBe(calls.length)
  const released = new Set(vi.mocked(api.unsubscribeQuotes).mock.calls.map(c => c[0]))
  expect(calls.filter(c => !released.has(c[0]))).toHaveLength(2)
})

it('a hidden window stops renewing and renews immediately on return', async () => {
  await act(async () => root.render(<Detail symbol="000985.SH" />))
  const visibility = vi.spyOn(document, 'visibilityState', 'get')
  visibility.mockReturnValue('hidden')
  await act(async () => vi.advanceTimersByTimeAsync(100_000))
  expect(api.subscribeQuotes).toHaveBeenCalledTimes(1)
  visibility.mockReturnValue('visible')
  await act(async () => document.dispatchEvent(new Event('visibilitychange')))
  expect(api.subscribeQuotes).toHaveBeenCalledTimes(2)
})
