import { describe, it, expect } from 'vitest'
import { sameBar, sameSeries, reuseUnchangedSeries, EMPTY_ROWS } from './series-identity'

const bar = (over: Record<string, unknown> = {}) => ({
  date: '2026-10-21', open: 10, high: 11, low: 9, close: 10.5, volume: 100, ...over,
})

describe('sameBar / sameSeries', () => {
  it('内容全等的不同对象视为相等', () => {
    expect(sameBar(bar(), bar())).toBe(true)
    expect(sameSeries([bar(), bar({ close: 11 })], [bar(), bar({ close: 11 })])).toBe(true)
  })

  it('任一字段不同即不相等 (全字段比较, 无白名单)', () => {
    expect(sameBar(bar(), bar({ volume: 101 }))).toBe(false)
    expect(sameBar(bar(), { date: '2026-10-21' })).toBe(false) // key 集合不同
    expect(sameSeries([bar()], [bar({ close: 10.6 })])).toBe(false)
  })

  it('长度不同 / 单侧缺失不相等; 同引用直接相等', () => {
    expect(sameSeries([bar()], [bar(), bar()])).toBe(false)
    expect(sameSeries(undefined, [])).toBe(false)
    const a = [bar()]
    expect(sameSeries(a, a)).toBe(true)
  })
})

describe('reuseUnchangedSeries', () => {
  it('内容未变的 symbol 沿用旧数组引用, 变化的保留新数组', () => {
    const prev = { A: [bar()], B: [bar({ close: 1 })] }
    const next = { A: [bar()], B: [bar({ close: 2 })] }
    const out = reuseUnchangedSeries(prev, next)
    expect(out.A).toBe(prev.A)
    expect(out.B).toBe(next.B)
  })

  it('新增 symbol 无旧参照沿用新数组; 移除的 symbol 不再出现', () => {
    const prev = { A: [bar()] }
    const next = { A: [bar()], C: [bar()] }
    const out = reuseUnchangedSeries(prev, next)
    expect(out.A).toBe(prev.A)
    expect(out.C).toBe(next.C)
    expect('A' in reuseUnchangedSeries(next, {})).toBe(false)
  })

  it('空序列内容相等时也沿用旧引用', () => {
    const prev = { A: [] as typeof EMPTY_ROWS }
    const next = { A: [] as typeof EMPTY_ROWS }
    expect(reuseUnchangedSeries(prev, next).A).toBe(prev.A)
  })
})
