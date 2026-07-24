import { describe, expect, it } from 'vitest'
import { normalizeMarketCode, withMarketParam } from './market-scope'
import { QK } from './queryKeys'

describe('global market scope', () => {
  it('keeps the current path and existing query parameters', () => {
    expect(withMarketParam('/screener?as_of=2026-07-17', 'hk'))
      .toBe('/screener?as_of=2026-07-17&market=hk')
    expect(withMarketParam('/backtest?market=cn&tab=strategy', 'us'))
      .toBe('/backtest?market=us&tab=strategy')
  })

  it('normalizes invalid markets to cn', () => {
    expect(normalizeMarketCode('HK')).toBe('hk')
    expect(normalizeMarketCode('invalid')).toBe('cn')
    expect(normalizeMarketCode(null)).toBe('cn')
  })

  it('isolates query caches between markets', () => {
    expect(QK.overviewMarket('cn', '2026-07-17')).not.toEqual(QK.overviewMarket('hk', '2026-07-17'))
    expect(QK.marketSnapshot('hk')).toEqual(['market-snapshot', 'hk'])
  })
})
