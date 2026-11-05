// @vitest-environment node
import { expect, it } from 'vitest'
import { mergeLatestKlineRow } from './kline'
import type { KlineDailyResponse } from './api'

it('实时增量为旧缓存补充资产类型, 原始价格与旧缓存保持不变', () => {
  const current: KlineDailyResponse = { symbol: '159558.SZ', rows: [
    { date: '2026-11-05', open: 0.751, high: 0.828, low: 0.747, close: 0.827 },
  ] }
  const next = mergeLatestKlineRow(current, { symbol: current.symbol, asset_type: 'etf', row: null, source: 'none' })!
  expect(next.asset_type).toBe('etf')
  expect(next.rows[0].close).toBe(0.827)
  expect(current.asset_type).toBeUndefined()
  expect(mergeLatestKlineRow(current, { symbol: '600000.SH', asset_type: 'stock', row: null, source: 'none' })).toBe(current)
})
