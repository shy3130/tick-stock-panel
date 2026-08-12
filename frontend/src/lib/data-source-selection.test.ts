import { describe, expect, it } from 'vitest'

import {
  buildProviderPreferencePatch,
  buildProviderStatusIndicators,
  resolveInitialProviderSelection,
} from './data-source-selection'

describe('buildProviderPreferencePatch', () => {
  it('only changes datasets explicitly supported by a non-builtin provider', () => {
    expect(
      buildProviderPreferencePatch('tushare', ['instruments', 'daily', 'adj_factor']),
    ).toEqual({
      daily_data_provider: 'tushare',
      adj_factor_provider: 'same_as_daily',
    })
  })

  it('keeps an adjustment-only provider independent from daily data', () => {
    expect(
      buildProviderPreferencePatch('corporate-actions', ['adj_factor']),
    ).toEqual({
      adj_factor_provider: 'corporate-actions',
    })
  })

  it('routes Tushare financials only when the plugin declares support', () => {
    expect(
      buildProviderPreferencePatch(
        'tushare',
        ['instruments', 'daily', 'adj_factor', 'financial'],
      ),
    ).toEqual({
      daily_data_provider: 'tushare',
      adj_factor_provider: 'same_as_daily',
      financial_data_provider: 'tushare',
    })
  })

  it('resets all routed datasets only when TickFlow is explicitly selected', () => {
    expect(buildProviderPreferencePatch('tickflow', [])).toEqual({
      daily_data_provider: 'tickflow',
      adj_factor_provider: 'same_as_daily',
      realtime_data_provider: 'tickflow',
      minute_data_provider: 'tickflow',
      financial_data_provider: 'tickflow',
    })
  })

  it('shows all six capability slots without claiming unsupported datasets', () => {
    expect(
      buildProviderStatusIndicators([
        'instruments',
        'daily',
        'adj_factor',
        'financial',
      ]),
    ).toEqual([
      { dataset: 'instruments', label: '证券主表', supported: true },
      { dataset: 'daily', label: '日K', supported: true },
      { dataset: 'adj_factor', label: '复权因子', supported: true },
      { dataset: 'financial', label: '财务', supported: true },
      { dataset: 'realtime', label: '实时', supported: false },
      { dataset: 'minute', label: '分钟', supported: false },
    ])
  })

  it('opens the active provider once without overriding later manual selection', () => {
    expect(
      resolveInitialProviderSelection({
        current: 'tickflow',
        active: 'tushare',
        preferencesLoaded: true,
        initialized: false,
      }),
    ).toBe('tushare')
    expect(
      resolveInitialProviderSelection({
        current: 'akshare',
        active: 'tushare',
        preferencesLoaded: true,
        initialized: true,
      }),
    ).toBe('akshare')
  })
})
