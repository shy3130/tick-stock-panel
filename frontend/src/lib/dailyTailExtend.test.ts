import { expect, it } from 'vitest'
import { extendDailyTail } from './dailyTailExtend'

const stock = '600519.SH', etf = '159558.SZ'
const bar = { date: '2026-11-04', open: 10, high: 10, low: 10, close: 10 }
const base = { [stock]: [bar], [etf]: [{ ...bar, close: 1 }] }
const live = [
  { symbol: stock, asset_type: 'stock', date: '2026-11-05', open: 11, high: 12, low: 10, close: 12 },
  { symbol: etf, asset_type: 'etf', date: '2026-11-04', open: 1, high: 1, low: 1, close: 1 },
]

it('mixed stock/ETF dates never overwrite an earlier stock candle with today prices', () => {
  const result = extendDailyTail(base, live, { stock: '2026-11-05', etf: '2026-11-04' })
  expect(result[stock]).toEqual([bar, { date: '2026-11-05', open: 11, high: 12, low: 10, close: 12 }])
  expect(result[etf]).toHaveLength(1)
  expect(base[stock]).toEqual([bar])
})

it('uses the row date over the asset snapshot date, and tolerates legacy rows without date', () => {
  expect(extendDailyTail(base, live, { stock: '2026-11-06' })[stock].at(-1)?.date).toBe('2026-11-05')
  const legacy = live.map(({ date: _date, ...row }) => row)
  expect(extendDailyTail(base, legacy, { stock: '2026-11-05', etf: '2026-11-04' })[stock]).toHaveLength(2)
  expect(extendDailyTail(base, legacy, undefined)[stock]).toBe(base[stock])
})

it('ignores invalid prices and dates older than the last stored candle', () => {
  expect(extendDailyTail(base, [{ ...live[0], close: NaN }], { stock: '2026-11-05' })[stock]).toBe(base[stock])
  expect(extendDailyTail(base, [{ ...live[0], date: '2026-11-03' }], { stock: '2026-11-05' })[stock]).toBe(base[stock])
})
