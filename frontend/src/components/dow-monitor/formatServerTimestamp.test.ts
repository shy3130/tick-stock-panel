import { describe, expect, it } from 'vitest'

import {
  formatExchangeTradeDate,
  formatServerTimestamp,
} from './formatServerTimestamp'


describe('formatServerTimestamp', () => {
  it('treats a naive ClickHouse datetime as UTC and renders Beijing time', () => {
    expect(formatServerTimestamp('2026-07-31 03:30:00.000')).toBe(
      '2026-07-31 11:30',
    )
  })

  it('uses the exchange date for US history queries across Beijing midnight', () => {
    expect(formatExchangeTradeDate('2026-01-06T02:00:00Z', 'RNG.US')).toBe(
      '2026-01-05',
    )
  })
})
