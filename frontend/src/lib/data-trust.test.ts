import { describe, expect, it } from 'vitest'

import { buildDataTrustRows, getDataTrustSummaryLabel } from './data-trust'

describe('buildDataTrustRows', () => {
  it('keeps provider, as-of date, coverage and partial state visible', () => {
    const rows = buildDataTrustRows({
      overall_status: 'warning',
      audits: [
        {
          schema_version: 1,
          provider: 'tushare',
          dataset: 'daily',
          status: 'partial',
          row_count: 240,
          returned_symbols: ['600000.SH'],
          missing_symbols: ['000001.SZ'],
          coverage_ratio: 0.5,
          fallback_used: false,
          synthetic: false,
          issues: [],
          observed_start: '2026-01-02',
          observed_end: '2026-07-24',
          recorded_at: '2026-07-24T16:00:00+00:00',
        },
      ],
    })

    expect(rows).toEqual([
      {
        dataset: 'daily',
        datasetLabel: '日K',
        provider: 'tushare',
        status: 'partial',
        statusLabel: '部分覆盖',
        rowCount: 240,
        coverageLabel: '50.00%',
        observedEnd: '2026-07-24',
        issueText: '缺少 1 只标的',
      },
    ])
  })

  it('uses readable labels for audited financial tables', () => {
    const rows = buildDataTrustRows({
      overall_status: 'ok',
      audits: [
        {
          schema_version: 1,
          provider: 'tushare',
          dataset: 'financial_metrics',
          status: 'ok',
          row_count: 1,
          returned_symbols: ['600000.SH'],
          missing_symbols: [],
          coverage_ratio: 1,
          fallback_used: false,
          synthetic: false,
          issues: [],
          observed_start: '2026-03-31',
          observed_end: '2026-03-31',
          recorded_at: '2026-04-22T08:00:00+00:00',
        },
      ],
    })

    expect(rows[0].datasetLabel).toBe('核心财务指标')
    expect(rows[0].observedEnd).toBe('2026-03-31')
  })

  it('uses a readable label for derived daily data', () => {
    const rows = buildDataTrustRows({
      overall_status: 'warning',
      audits: [
        {
          schema_version: 1,
          provider: 'derived',
          dataset: 'daily_enriched',
          status: 'partial',
          row_count: 5_522,
          returned_symbols: [],
          missing_symbols: ['000001.SZ'],
          coverage_ratio: 0.9986,
          fallback_used: false,
          synthetic: false,
          issues: [],
          observed_start: '2026-07-31',
          observed_end: '2026-07-31',
          recorded_at: '2026-07-31T07:30:00+00:00',
        },
      ],
    })

    expect(rows[0].datasetLabel).toBe('衍生日K')
  })

  it('does not round a partial audit up to 100 percent', () => {
    const rows = buildDataTrustRows({
      overall_status: 'warning',
      audits: [
        {
          schema_version: 1,
          provider: 'tickflow',
          dataset: 'daily',
          status: 'partial',
          row_count: 5_528,
          returned_symbols: [],
          missing_symbols: ['000001.SZ', '000002.SZ'],
          coverage_ratio: 0.9996383363,
          fallback_used: false,
          synthetic: false,
          issues: [],
          observed_start: '2026-07-31',
          observed_end: '2026-07-31',
          recorded_at: '2026-07-31T07:30:00+00:00',
        },
      ],
    })

    expect(rows[0].coverageLabel).toBe('99.96%')
  })
})

describe('getDataTrustSummaryLabel', () => {
  it('describes high-coverage partial receipts as usable with minor gaps', () => {
    expect(getDataTrustSummaryLabel({
      overall_status: 'warning',
      audits: [
        {
          schema_version: 1,
          provider: 'tickflow',
          dataset: 'daily',
          status: 'partial',
          row_count: 5_528,
          returned_symbols: [],
          missing_symbols: ['000001.SZ', '000002.SZ'],
          coverage_ratio: 0.9996383363,
          fallback_used: false,
          synthetic: false,
          issues: [],
          observed_start: '2026-07-31',
          observed_end: '2026-07-31',
          recorded_at: '2026-07-31T07:30:00+00:00',
        },
      ],
    })).toBe('基本可用，少量缺失')
  })

  it('keeps sub-threshold required market coverage visibly blocked', () => {
    expect(getDataTrustSummaryLabel({
      overall_status: 'warning',
      audits: [
        {
          schema_version: 1,
          provider: 'tickflow',
          dataset: 'daily_enriched',
          status: 'partial',
          row_count: 5_200,
          returned_symbols: [],
          missing_symbols: ['000001.SZ'],
          coverage_ratio: 0.94,
          fallback_used: false,
          synthetic: false,
          issues: [],
          observed_start: '2026-07-31',
          observed_end: '2026-07-31',
          recorded_at: '2026-07-31T07:30:00+00:00',
        },
      ],
    })).toBe('覆盖不足')
  })
})
