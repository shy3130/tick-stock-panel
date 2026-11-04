import { describe, expect, it } from 'vitest'
import { createRevisionTracker, pendingStrategyIds, pollingInterval, retryDelay } from './refreshPolicy'

describe('shared data refresh policy', () => {
  it('keeps polling fallback, pauses hidden pages, and never polls faster than configured', () => {
    expect(pollingInterval(6000, false, true, () => 0)).toBe(6000)
    expect(pollingInterval(6000, true, true, () => 0)).toBe(60000)
    expect(pollingInterval(6000, false, false)).toBe(false)
    expect(pollingInterval(6000, false, true, () => 1)).toBe(6600)
  })
  it('ignores duplicate revisions but accepts legacy events and server restarts', () => {
    const tracker = createRevisionTracker()
    expect(tracker.accept('quotes', '{"revision":"server1:1"}')).toBe(true)
    expect(tracker.accept('quotes', '{"revision":"server1:1"}')).toBe(false)
    expect(tracker.accept('strategy', '{"revision":"server1:1"}')).toBe(true)
    expect(tracker.accept('quotes', '{"revision":"server2:1"}')).toBe(true)
    expect(tracker.accept('quotes', '{}')).toBe(true)
  })
  it('respects server retry delay and caps ordinary exponential retry', () => {
    expect(retryDelay(0, 3000, () => 0)).toBe(3000)
    expect(retryDelay(0, undefined, () => 0)).toBe(1000)
    expect(retryDelay(10, undefined, () => 0)).toBe(30000)
  })
  it('does not mistake a recent placeholder result for completion of a new task', () => {
    const pending = { ids: ['a'], startedAt: 10000, taskId: 'new-task' }
    const summary = { results: { a: { total: 3, as_of: '2026-11-04', computed_at: 10001 } } }
    expect(pendingStrategyIds(pending, summary, '2026-11-04')).toEqual(['a'])
    expect(pendingStrategyIds(pending, { ...summary, task: {
      id: 'previous-task', complete: true, pending: [], errors: {}, error: null,
    } }, '2026-11-04')).toEqual(['a'])
  })
  it('uses backend task progress even when cached data has not changed', () => {
    const pending = { ids: ['a', 'b'], startedAt: 10000, taskId: 'task' }
    expect(pendingStrategyIds(pending, { results: {}, task: {
      id: 'task', complete: false, pending: ['b'], errors: {}, error: null,
    } }, '2026-11-04')).toEqual(['b'])
    expect(pendingStrategyIds(pending, { results: {}, task: null }, '2026-11-04')).toBeNull()
  })
  it('keeps legacy timestamp-based completion for older servers', () => {
    expect(pendingStrategyIds({ ids: ['a'], startedAt: 10000 }, {
      results: { a: { total: 3, as_of: '2026-11-04', computed_at: 11000 } },
    }, '2026-11-04')).toEqual([])
  })
})
