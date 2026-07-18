import { describe, expect, it } from 'vitest'

import {
  currencyForMarket,
  currencyLabel,
  marketFromSymbol,
  marketLabel,
} from './market-display'

describe('market display', () => {
  it('maps three markets to Chinese labels', () => {
    expect(marketLabel('cn')).toBe('A股')
    expect(marketLabel('hk')).toBe('港股')
    expect(marketLabel('us')).toBe('美股')
  })

  it('keeps unknown values visible', () => {
    expect(marketLabel('other')).toBe('other')
    expect(currencyLabel('CNY')).toBe('人民币')
    expect(currencyLabel('HKD')).toBe('港元')
    expect(currencyLabel('USD')).toBe('美元')
    expect(currencyLabel('EUR')).toBe('EUR')
  })

  it('infers legacy trade identity from standardized symbols', () => {
    expect(marketFromSymbol('000001.SZ')).toBe('cn')
    expect(marketFromSymbol('1.HK')).toBe('hk')
    expect(marketFromSymbol('A.US')).toBe('us')
    expect(currencyForMarket('hk')).toBe('HKD')
  })
})
