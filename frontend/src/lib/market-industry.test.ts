import { describe, expect, it } from 'vitest'

import { marketIndustryDimensionData } from './analysis-adapter'

describe('market industry dimension data', () => {
  it('turns HK full ClickHouse industry rows into dimension input without A-share ext data', () => {
    const result = marketIndustryDimensionData('hk', {
      market: 'hk',
      as_of: '2026-06-25 02:54:33.613',
      source: 'lb_eastmoney_f10_profiles',
      rows: [{
        symbol: '700.HK',
        name: 'TENCENT',
        main_sector: '',
        sub_industry: '软件服务',
        industry: '软件服务',
        is_leader: true,
      }],
    })

    expect(result?.data.id).toBe('clickhouse-industries-hk')
    expect(result?.data.rows[0].symbol).toBe('700.HK')
    expect(result?.config.fields.map(field => field.name)).toContain('industry')
    expect(result?.config.fields.map(field => field.name)).toContain('is_leader')
    expect(result?.sourceLabel).toBe('ClickHouse 全量行业分类')
  })

  it('does not replace the configured A-share industry source', () => {
    expect(marketIndustryDimensionData('cn', null)).toBeNull()
  })
})
