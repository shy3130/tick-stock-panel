import { describe, expect, it } from 'vitest'
import { indexMatchesMarket, pinnedIndexesForMarket } from './market-indices'

describe('market index catalog', () => {
  it('provides Hong Kong core indices', () => {
    expect(pinnedIndexesForMarket('hk').map(item => item.symbol)).toEqual([
      'HSI.HK',
      'HSTECH.HK',
      'HSCEI.HK',
    ])
  })

  it('provides US core indices with Longbridge leading-dot symbols', () => {
    expect(pinnedIndexesForMarket('us').map(item => item.symbol)).toEqual([
      '.SPX.US',
      '.IXIC.US',
      '.DJI.US',
      '.VIX.US',
    ])
    expect(indexMatchesMarket('.SPX.US', 'us')).toBe(true)
    expect(indexMatchesMarket('000004.SH', 'us')).toBe(false)
  })
})
