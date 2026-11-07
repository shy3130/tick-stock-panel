import { expect, it } from 'vitest'
import { formatAxisPercent, intradayScale } from './intraday-scale'

it('centers a falling index on yesterday close while covering the observed highs and lows', () => {
  const scale = intradayScale([5059.65, 5160.75, 5128.90], 5211.79)!
  expect(scale.range.min).toBeLessThanOrEqual(5059.65)
  expect(scale.range.max).toBeGreaterThan(5211.79)
  expect((scale.range.min + scale.range.max) / 2).toBeCloseTo(5211.79)
  expect(scale.centered).toBe(true)
  expect(scale.ticks[2]).toEqual({ price: 5211.79, percent: 0 })
  expect(scale.ticks[0].percent).toBeCloseTo(-scale.ticks[4].percent!)
})

it.each([[12, 13, 12.5], [7, 8, 7.5]])('uses the same symmetric range for a one-sided session: %j', (...prices) => {
  const scale = intradayScale(prices, 10)!
  expect(scale.range).toEqual({ min: 7, max: 13 })
  expect(scale.centered).toBe(true)
  expect(scale.ticks[2]).toEqual({ price: 10, percent: 0 })
  for (const tick of scale.ticks) expect(tick.percent).toBeCloseTo((tick.price / 10 - 1) * 100)
})

it('keeps zero at the middle tick and both axes symmetric when prices cross yesterday close', () => {
  const scale = intradayScale([13.3, 13.59, 13.18, 13.47], 13.34)!
  expect(scale.centered).toBe(true)
  expect(scale.ticks[2]).toEqual({ price: 13.34, percent: 0 })
  expect(scale.range.min).toBeCloseTo(13.34 * 0.98)
  expect(scale.range.max).toBeCloseTo(13.34 * 1.02)
  for (const tick of scale.ticks) expect(tick.percent).toBeCloseTo((tick.price / 13.34 - 1) * 100)
})

it('keeps the limit mode symmetric and derives both axes from one range', () => {
  const scale = intradayScale([13.3, 13.59, 13.18, 13.47], 13.34, 2, undefined, 'limit')!
  expect(scale.centered).toBe(true)
  expect((scale.range.min + scale.range.max) / 2).toBeCloseTo(13.34)
  expect(scale.range.max).toBeGreaterThanOrEqual(13.59)
  expect(scale.ticks[2]).toEqual({ price: 13.34, percent: 0 })
  expect(scale.ticks[0].percent).toBeCloseTo(-scale.ticks[4].percent!)
  for (const tick of scale.ticks) expect(tick.percent).toBeCloseTo((tick.price / 13.34 - 1) * 100)
})

it('keeps flat, low-priced and ETF ranges legible and covers all actual extrema', () => {
  const flat = intradayScale([12], 12)!
  expect(flat.range.max - flat.range.min).toBeCloseTo(0.072)
  const etf = intradayScale([0.761, 0.762], 0.76, 3)!
  expect((etf.range.min + etf.range.max) / 2).toBeCloseTo(0.76)
  expect(etf.range.min).toBeGreaterThan(0)
  expect(new Set(etf.ticks.map(tick => tick.price.toFixed(3))).size).toBe(5)
  const limit = intradayScale([9, 11, 14], 10)!
  expect(limit.range).toEqual({ min: 6, max: 14 })
})

it('handles missing references and extreme gains without a fictitious zero axis or negative prices', () => {
  expect(intradayScale([null, NaN, 0, -1], 10)).toBeUndefined()
  const missing = intradayScale([10, 11], NaN)!
  expect(missing.reference).toBeUndefined()
  expect(missing.ticks.every(tick => tick.percent == null)).toBe(true)
  const extreme = intradayScale([10, 24], 10)!
  expect(extreme.centered).toBe(false)
  expect(extreme.range).toEqual({ min: 9.44, max: 24.56 })
  expect(extreme.ticks.at(-1)?.percent).toBeCloseTo(145.6)
  const tiny = intradayScale([0.01], 0.01)!
  expect(tiny.range.min).toBeGreaterThan(0)
  expect(tiny.range.max).toBeGreaterThan(tiny.range.min)
  expect(formatAxisPercent(-0.000001)).toBe('0.00%')
  expect(formatAxisPercent(-0.004)).toBe('0.00%')
  expect(formatAxisPercent(1.25)).toBe('+1.25%')
  expect(formatAxisPercent(-1.25)).toBe('-1.25%')
})

it('bounds outward tick rounding by actual limit prices while keeping yesterday close centered', () => {
  const scale = intradayScale([100, 110.01], 100, 2, { min: 90.01, max: 110.01 }, 'limit')!
  expect(scale.range.min).toBeCloseTo(89.99)
  expect(scale.range.max).toBeCloseTo(110.01)
  expect((scale.range.min + scale.range.max) / 2).toBe(100)
  const rounded = intradayScale([10.01, 11.01], 10.01, 2, { min: 9.01, max: 11.01 }, 'limit')!
  expect(rounded.range.min).toBeCloseTo(9.01)
  expect(rounded.range.max).toBeCloseTo(11.01)
  const small = intradayScale([100, 101], 100, 2, { min: 90.01, max: 110.01 }, 'limit')!
  expect(small.range).toEqual({ min: 99, max: 101 })
})

it('keeps real out-of-band prices visible without rounding the extended range further', () => {
  const scale = intradayScale([100, 115], 100, 2, { min: 90.01, max: 110.01 }, 'limit')!
  expect(scale.range).toEqual({ min: 85, max: 115 })
  const invalid = intradayScale([100, 110.01], 100, 2, { min: NaN, max: 110.01 }, 'limit')!
  expect(invalid.range).toEqual({ min: 88, max: 112 })
})

it('lets a low-priced instrument use its actual limits instead of enlarging the minimum span', () => {
  const scale = intradayScale([0.1, 0.11], 0.1, 2, { min: 0.09, max: 0.11 })!
  expect(scale.range.min).toBeCloseTo(0.09)
  expect(scale.range.max).toBeCloseTo(0.11)
  expect(scale.ticks).toHaveLength(3)
  expect(new Set(scale.ticks.map(t => t.price.toFixed(2))).size).toBe(3)
})
