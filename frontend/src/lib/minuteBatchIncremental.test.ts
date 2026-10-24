import { describe, expect, it, vi, beforeEach } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import {
  MINUTE_BATCH_CHUNK,
  MINUTE_CACHE_MAX_SYMBOLS,
  fetchMinuteBatchIncremental,
  lastBarTs,
  mergeInto,
  pruneMinuteCache,
} from './minuteBatchIncremental'
import { QK } from './queryKeys'
import { api } from './api'
import type { MinuteKlineRow } from './api'

vi.mock('./api', () => ({
  api: {
    klineMinuteBatch: vi.fn(),
  },
}))

const bars = (times: string[]): MinuteKlineRow[] =>
  times.map(datetime => ({ datetime, open: 1, high: 1, low: 1, close: 1, volume: 1, amount: 1 }))

beforeEach(() => {
  vi.clearAllMocks()
})

describe('lastBarTs', () => {
  it('取请求范围内最旧的最后一根', () => {
    const data = {
      a: bars(['09:31', '09:40']),
      b: bars(['09:31', '09:35']),
      c: bars(['09:31', '09:50']),
    }
    expect(lastBarTs(data, ['a', 'c'])).toBe('09:40')
    expect(lastBarTs(data)).toBe('09:35')
    expect(lastBarTs({}, ['a'])).toBeNull()
  })
})

describe('mergeInto', () => {
  it('新值按 datetime 覆盖旧值并保持时序', () => {
    const base = { s: bars(['09:31', '09:32']) }
    const patch = { s: bars(['09:32', '09:33']) }
    const merged = mergeInto(base, patch)
    expect(merged.s.map(r => r.datetime)).toEqual(['09:31', '09:32', '09:33'])
    // 09:32 被新值覆盖 (引用来自 patch)
    expect(merged.s[1]).toBe(patch.s[0])
  })

  it('patch 里未涉及的 symbol 引用原样保留', () => {
    const base = { a: bars(['09:31']), b: bars(['09:31']) }
    const merged = mergeInto(base, { b: bars(['09:31', '09:32']) })
    expect(merged.a).toBe(base.a)
  })
})

describe('pruneMinuteCache', () => {
  it('超上限时删最久未 touch 且本次未请求的, requested 恒保留', () => {
    const mk = (n: number) => Object.fromEntries(
      Array.from({ length: n }, (_, i) => [`s${i}`, bars(['09:31'])]),
    )
    const data = mk(6)
    const lastTouch = new Map([
      ['s0', 1], ['s1', 2], ['s2', 3], ['s3', 4], ['s4', 5], ['s5', 6],
    ])
    const out = pruneMinuteCache(data, ['s5'], lastTouch, 3)
    // requested(s5) + touch 最新的 s4、s3 保留; s0/s1/s2 淘汰
    expect(Object.keys(out).sort()).toEqual(['s3', 's4', 's5'])
    // 保留序列引用不变
    expect(out.s5).toBe(data.s5)
  })

  it('未超上限原样返回', () => {
    const data = { a: bars(['09:31']), b: bars(['09:31']) }
    expect(pruneMinuteCache(data, [], new Map(), MINUTE_CACHE_MAX_SYMBOLS)).toBe(data)
  })
})

describe('fetchMinuteBatchIncremental', () => {
  it('按 300/块分块请求并合并, 块间 since 取自缓存对应块', async () => {
    const qc = new QueryClient()
    const symbols = Array.from({ length: 651 }, (_, i) => `s${i}`)
    const mock = vi.mocked(api.klineMinuteBatch)
    mock.mockResolvedValue({ data: {}, incremental: true } as any)

    await fetchMinuteBatchIncremental(qc, symbols, true)

    expect(mock).toHaveBeenCalledTimes(3)
    expect(mock.mock.calls[0][0]).toHaveLength(MINUTE_BATCH_CHUNK)
    expect(mock.mock.calls[1][0]).toHaveLength(MINUTE_BATCH_CHUNK)
    expect(mock.mock.calls[2][0]).toHaveLength(51)
    expect(mock.mock.calls[0][2]).toBe(true)
  })

  it('缓存存在时带 since, 增量合并为完整序列', async () => {
    const qc = new QueryClient()
    qc.setQueryData(QK.minuteBatch(), {
      data: { a: bars(['09:31', '09:40']) },
    })
    const mock = vi.mocked(api.klineMinuteBatch)
    mock.mockResolvedValue({ data: { a: bars(['09:41']) }, incremental: true } as any)

    const out = await fetchMinuteBatchIncremental(qc, ['a'], true)

    expect(mock.mock.calls[0][3]).toBe('09:40')
    expect(out.data.a.map(r => r.datetime)).toEqual(['09:31', '09:40', '09:41'])
  })

  it('无缓存时不带 since, 响应按 symbol 合并进同一份结果', async () => {
    const qc = new QueryClient()
    const mock = vi.mocked(api.klineMinuteBatch)
    mock.mockResolvedValue({ data: { a: bars(['09:31']), b: bars(['09:32']) }, incremental: false } as any)

    const out = await fetchMinuteBatchIncremental(qc, ['a', 'b'], false)

    expect(mock).toHaveBeenCalledTimes(1)
    expect(mock.mock.calls[0][3]).toBeUndefined()
    expect(Object.keys(out.data).sort()).toEqual(['a', 'b'])
  })
})
