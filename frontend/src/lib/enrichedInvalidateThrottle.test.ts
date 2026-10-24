import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createEnrichedThrottler } from './enrichedInvalidateThrottle'

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('createEnrichedThrottler', () => {
  it('首次触发立即执行 (leading)', () => {
    const invalidate = vi.fn()
    const t = createEnrichedThrottler(5000)
    t.fire(invalidate)
    expect(invalidate).toHaveBeenCalledTimes(1)
    t.cancel()
  })

  it('窗口内的后续触发被合并, trailing 定时器兜底补发一次', () => {
    const invalidate = vi.fn()
    const t = createEnrichedThrottler(5000)
    t.fire(invalidate)              // t=0 立即
    t.fire(invalidate)              // t≈0 跳过, 挂 trailing
    t.fire(invalidate)              // 已有定时器, 合并
    expect(invalidate).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(4999)
    expect(invalidate).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(1)
    expect(invalidate).toHaveBeenCalledTimes(2)
    t.cancel()
  })

  it('trailing 补发后窗口重置, 下一个窗口可再立即执行', () => {
    const invalidate = vi.fn()
    const t = createEnrichedThrottler(5000)
    t.fire(invalidate)              // t=0 立即 (第 1 次)
    t.fire(invalidate)              // 挂 trailing
    vi.advanceTimersByTime(5000)    // t=5000 trailing 补发 (第 2 次), last=5000
    expect(invalidate).toHaveBeenCalledTimes(2)
    vi.advanceTimersByTime(5000)    // t=10000, 距上次执行整一个窗口
    t.fire(invalidate)              // 立即 (第 3 次)
    expect(invalidate).toHaveBeenCalledTimes(3)
    t.cancel()
  })

  it('cancel 丢弃挂起的 trailing 补发', () => {
    const invalidate = vi.fn()
    const t = createEnrichedThrottler(5000)
    t.fire(invalidate)
    t.fire(invalidate)
    t.cancel()
    vi.advanceTimersByTime(10000)
    expect(invalidate).toHaveBeenCalledTimes(1)
  })
})
