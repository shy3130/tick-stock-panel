import { describe, expect, it } from 'vitest'

import { marketIndustryDimensionData } from './analysis-adapter'

describe('market industry dimension data', () => {
  it('turns HK ClickHouse industry rows into dimension input without A-share ext data', () => {
    const result = marketIndustryDimensionData('hk', {
      market: 'hk',
      as_of: '2026-07-14',
      source: 'lb_company_background_industry_leaders',
      rows: [{
        symbol: '700.HK',
        name: 'TENCENT',
        main_sector: 'TMT',
        sub_industry: '互联网',
        industry: 'TMT-互联网',
      }],
    })

    expect(result?.data.id).toBe('clickhouse-industries-hk')
    expect(result?.data.rows[0].symbol).toBe('700.HK')
    expect(result?.config.fields.map(field => field.name)).toContain('industry')
    expect(result?.sourceLabel).toBe('ClickHouse 行业代表快照')
  })

  it('does not replace the configured A-share industry source', () => {
    expect(marketIndustryDimensionData('cn', null)).toBeNull()
  })
})
