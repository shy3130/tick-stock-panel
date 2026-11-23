import { describe, expect, it, vi, beforeEach } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import {
  MINUTE_CACHE_MAX_SYMBOLS,
  fetchMinuteBatchIncremental,
  mergeInto,
  pruneMinuteCache,
} from './minuteBatchIncremental'
import { getMinuteBatchChunkSize, resetKlineBatchLimits, setKlineBatchLimits } from './klineBatchLimits'
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
  resetKlineBatchLimits()
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
  it('按偏好的单请求上限分块并合并, 块间 since 取自缓存对应块', async () => {
    // 分块大小 = 后端校验的同一上限 (默认 400)。改设置后两端同步, 不会必然 400。
    const qc = new QueryClient()
    const chunkSize = getMinuteBatchChunkSize()
    const symbols = Array.from({ length: chunkSize * 2 + 51 }, (_, i) => `s${i}`)
    const mock = vi.mocked(api.klineMinuteBatch)
    mock.mockResolvedValue({ data: {}, incremental: true } as any)

    await fetchMinuteBatchIncremental(qc, symbols, true)

    expect(mock).toHaveBeenCalledTimes(3)
    expect(mock.mock.calls[0][0]).toHaveLength(chunkSize)
    expect(mock.mock.calls[1][0]).toHaveLength(chunkSize)
    expect(mock.mock.calls[2][0]).toHaveLength(51)
    expect(mock.mock.calls[0][2]).toBe(true)
  })

  it('偏好调小后分块立即跟随 (与后端同源, 不残留旧硬编码)', async () => {
    const qc = new QueryClient()
    setKlineBatchLimits({ minute: 120 })
    const symbols = Array.from({ length: 250 }, (_, i) => `s${i}`)
    const mock = vi.mocked(api.klineMinuteBatch)
    mock.mockResolvedValue({ data: {}, incremental: true } as any)

    await fetchMinuteBatchIncremental(qc, symbols, true)

    expect(mock).toHaveBeenCalledTimes(3)
    expect(mock.mock.calls[0][0]).toHaveLength(120)
    expect(mock.mock.calls[2][0]).toHaveLength(10)
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

  it('缓存只有 1 根残序列时自愈: 不带 since 全量重取 (手动刷新可恢复)', async () => {
    // 历史遗留的 1 根残序列若不重取, since 恒为那一根 → 每轮只回 1 根, 永远画不出分时
    const qc = new QueryClient()
    qc.setQueryData(QK.minuteBatch(), {
      data: { a: bars(['15:00']) },
    })
    const mock = vi.mocked(api.klineMinuteBatch)
    mock.mockResolvedValue({ data: { a: bars(['09:31', '09:35', '15:00']) }, incremental: false } as any)

    const out = await fetchMinuteBatchIncremental(qc, ['a'], true)

    expect(mock.mock.calls[0][3]).toBeUndefined()   // 不带 since → 全天
    expect(out.data.a).toHaveLength(3)
  })

  it('已缓存与未缓存 symbol 混合时按 since 拆成两请求 — 新 symbol 不能被裁成新增 (回归)', async () => {
    // 真实 bug: 整块共用最旧末根作 since, 未缓存的新 symbol 只回 1 根
    // → 分时图空白, 且手动点刷新也永远凑不满 2 根
    const qc = new QueryClient()
    qc.setQueryData(QK.minuteBatch(), {
      data: { old: bars(['09:31', '09:40']) },
    })
    const mock = vi.mocked(api.klineMinuteBatch)
    mock.mockImplementation(async (syms: string[]) =>
      syms.includes('old')
        ? { data: { old: bars(['09:41']) }, incremental: true } as any
        : { data: { fresh: bars(['09:31', '09:35']) }, incremental: false } as any)

    const out = await fetchMinuteBatchIncremental(qc, ['old', 'fresh'], true)

    const withSince = mock.mock.calls.filter(c => c[3] != null)
    const withoutSince = mock.mock.calls.filter(c => c[3] == null)
    expect(withSince).toHaveLength(1)
    expect(withSince[0][0]).toEqual(['old'])
    expect(withSince[0][3]).toBe('09:40')
    expect(withoutSince).toHaveLength(1)
    expect(withoutSince[0][0]).toEqual(['fresh'])
    // 新 symbol 拿到全天序列, 分时图可正常绘制
    expect(out.data.fresh.map(r => r.datetime)).toEqual(['09:31', '09:35'])
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
