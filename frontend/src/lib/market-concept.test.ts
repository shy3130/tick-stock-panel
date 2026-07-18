import { describe, expect, it } from 'vitest'

import { marketConceptDimensionData, resolveDimension } from './analysis-adapter'

describe('market concept dimension data', () => {
  it('turns HK ClickHouse event themes into concept input without A-share ext data', () => {
    const result = marketConceptDimensionData('hk', {
      market: 'hk',
      as_of: '2026-07-17',
      source: 'lb_sentiment_impact_events',
      window_days: 30,
      rows: [{ symbol: '700.HK', name: 'TENCENT', concept: '云计算' }],
    })

    expect(result?.data.id).toBe('clickhouse-concepts-hk')
    expect(result?.data.rows[0]).toMatchObject({ symbol: '700.HK', concept: ['云计算'] })
    expect(result?.config.fields.map(field => field.name)).toContain('concept')
    expect(result?.sourceLabel).toBe('ClickHouse 动态事件主题')
  })

  it('filters rows that do not belong to the selected market', () => {
    const result = marketConceptDimensionData('us', {
      market: 'us',
      as_of: '2026-07-18',
      source: 'lb_sentiment_impact_events',
      window_days: 30,
      rows: [
        { symbol: 'NVDA.US', name: 'NVIDIA', concept: 'AI芯片' },
        { symbol: '700.HK', name: 'TENCENT', concept: '互联网平台' },
      ],
    })

    expect(result?.data.rows).toHaveLength(1)
    expect(result?.data.rows[0].symbol).toBe('NVDA.US')
  })

  it('does not replace the configured A-share concept source', () => {
    expect(marketConceptDimensionData('cn', null)).toBeNull()
  })

  it('preserves a slash-delimited event theme as one exact classification', () => {
    const input = marketConceptDimensionData('us', {
      market: 'us',
      as_of: '2026-07-18',
      source: 'lb_sentiment_impact_events',
      window_days: 30,
      rows: [{ symbol: 'NBIS.US', name: 'Nebius', concept: '数据中心/算力服务' }],
    })

    const resolved = resolveDimension(input?.data, input?.config, ['concept'])

    expect(resolved.groups.map(group => group.key)).toEqual(['数据中心/算力服务'])
  })
})
