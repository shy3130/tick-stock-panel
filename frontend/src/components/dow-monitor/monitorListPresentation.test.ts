import { describe, expect, it } from 'vitest'

import type {
  DowMonitorNotification,
  DowMonitorOverviewSymbol,
  DowMonitorTimeframeState,
} from './types'
import {
  buildIntradaySparkline,
  deriveMonitorRow,
  paginateMonitorSymbols,
} from './monitorListPresentation'

function state(
  timeframe: '5m' | '15m' | '30m',
  closes: number[],
  options: {
    completion?: string
    priceToLinePct?: number
    volumeRatio?: number
    upward?: boolean
    downward?: boolean
  } = {},
): DowMonitorTimeframeState {
  const bars = closes.map((close, index) => ({
    index,
    timestamp: `2026-07-29T09:${String(30 + index * 5).padStart(2, '0')}:00+08:00`,
    open: close,
    high: close,
    low: close,
    close,
    volume: 100,
    ma5: options.upward ? close - 1 : options.downward ? close + 1 : close,
    ma10: options.upward ? close - 2 : options.downward ? close + 2 : close,
    ma20: options.upward ? close - 3 : options.downward ? close + 3 : close,
  }))
  return {
    symbol: '700.HK',
    market: 'hk',
    timeframe,
    freshness_state: 'LIVE',
    source_timestamp: '2026-07-29T09:35:00+08:00',
    snapshot: {
      bar_time: bars.at(-1)?.timestamp,
      bar_completion: options.completion ?? 'FINAL',
      price_to_line_pct: options.priceToLinePct,
      line_role: 'SUPPORT',
      volume_ratio_20: options.volumeRatio,
    },
    chart: { bars },
    updated_at: '2026-07-29T09:35:02+08:00',
  }
}

function symbolFixture(
  overrides: Partial<DowMonitorOverviewSymbol> = {},
): DowMonitorOverviewSymbol {
  return {
    symbol: '700.HK',
    market: 'hk',
    enabled: true,
    created_at: '2026-07-29T09:00:00+08:00',
    updated_at: '2026-07-29T09:35:02+08:00',
    name: '腾讯控股',
    last_price: 500,
    change_pct: 1.25,
    quote_timestamp: '2026-07-29T09:35:00+08:00',
    analysis_status: 'READY',
    intraday_capital: {
      total_in: 60,
      total_out: 40,
      quality: 'COMPLETE',
    },
    minute_decision: {
      symbol: '700.HK',
      market: 'hk',
      decision_minute: '2026-07-29T09:35:00+08:00',
      direction: 'BULLISH',
      direction_label: '偏涨',
      action: 'WATCH_BUY',
      action_label: '买入观察',
      confidence: 0.72,
      dominant_timeframe: '15m',
      confirmation_timeframes: ['5m', '15m'],
      supporting_reasons: [],
      contrary_risks: [],
      invalidation_conditions: [],
      data_status: 'COMPLETE',
      status_label: '数据完整',
      source_timestamp: '2026-07-29T09:35:00+08:00',
    },
    states: {
      '5m': state('5m', [10, 10.2]),
      '15m': state('15m', [10, 10.5], {
        upward: true,
        priceToLinePct: 1.2,
        volumeRatio: 1.6,
      }),
      '30m': state('30m', [9.8, 10.5], { upward: true }),
    },
    latest_notification: null,
    last_success_at: '2026-07-29T09:35:02+08:00',
    last_error: null,
    ...overrides,
  }
}

function notification(
  overrides: Partial<DowMonitorNotification> = {},
): DowMonitorNotification {
  return {
    notification_id: 'n-1',
    event_key: 'e-1',
    symbol: '700.HK',
    market: 'hk',
    timeframe: '15m',
    side: 'BUY',
    action_name: '买入确认',
    shape_name: '双重突破',
    triggered_at: '2026-07-29T09:31:00+08:00',
    trigger_price: 499,
    snapshot_payload: {},
    read_at: null,
    ...overrides,
  }
}

describe('monitor list presentation', () => {
  it('uses only completed 15m/30m bars for channel and momentum', () => {
    const item = symbolFixture({
      states: {
        '5m': state('5m', [10, 10.4, 8], { completion: 'FORMING' }),
        '15m': state('15m', [10, 10.5], { upward: true }),
        '30m': state('30m', [10, 10.7], { upward: true }),
      },
    })

    const row = deriveMonitorRow(item, [], undefined, Date.parse('2026-07-29T09:35:30+08:00'))

    expect(row.channel).toEqual({ code: 'UP', label: '上升通道' })
    expect(row.momentum5m.direction).toBe('UP')
    expect(row.momentum5m.valuePct).toBeCloseTo(4)
    expect(row.momentum15m.direction).toBe('UP')
    expect(row.momentum15m.valuePct).toBeCloseTo(5)
  })

  it('falls back for control distance and leaves missing values explicit', () => {
    const item = symbolFixture({
      states: {
        '5m': state('5m', [10, 10.2], { priceToLinePct: -0.8, volumeRatio: 0.9 }),
        '15m': state('15m', [10, 10.2]),
      },
    })

    const row = deriveMonitorRow(item, [], undefined, Date.parse('2026-07-29T09:35:30+08:00'))

    expect(row.control).toEqual({
      timeframe: '5m',
      role: '支撑线',
      distancePct: -0.8,
    })
    expect(row.relativeVolume).toEqual({ timeframe: '5m', ratio: 0.9 })
    expect(row.channel.label).toBe('待确认')
  })

  it('requires complete active-funds data', () => {
    const complete = deriveMonitorRow(
      symbolFixture(),
      [],
      undefined,
      Date.parse('2026-07-29T09:35:30+08:00'),
    )
    const delayed = deriveMonitorRow(
      symbolFixture({
        intraday_capital: { total_in: 60, total_out: 40, quality: 'DELAYED' },
      }),
      [],
      undefined,
      Date.parse('2026-07-29T09:35:30+08:00'),
    )

    expect(complete.activeFunds).toEqual({ confirmed: true, buyRatioPct: 60 })
    expect(delayed.activeFunds).toEqual({ confirmed: false, buyRatioPct: null })
  })

  it('keeps the newest persisted formal signal and timestamp even when data is stale', () => {
    const older = notification()
    const newer = notification({
      notification_id: 'n-2',
      side: 'SELL',
      action_name: '卖出确认',
      triggered_at: '2026-07-29T09:34:00+08:00',
    })
    const item = symbolFixture({
      analysis_status: 'QUOTE_DELAYED',
      latest_notification: older,
    })

    const row = deriveMonitorRow(
      item,
      [older, newer],
      undefined,
      Date.parse('2026-07-29T09:40:00+08:00'),
    )

    expect(row.delayed).toBe(true)
    expect(row.signal).toMatchObject({
      level: 'CONFIRMED',
      side: 'SELL',
      label: '卖出确认',
      occurredAt: '2026-07-29T09:34:00+08:00',
    })
  })

  it('does not promote failed or stale warnings to a formal signal', () => {
    const warningState = state('15m', [10, 10.2])
    warningState.chart.turning = {
      signals: [{
        side: 'BUY',
        stage: 'WARNING',
        detectedIndex: 1,
        detectedTime: '2026-07-29T09:34:00+08:00',
        actionableIndex: 1,
        actionableTime: '2026-07-29T09:34:00+08:00',
        price: 10.2,
        trendStateBefore: 'RANGE',
        trendStateAfter: 'UP',
        lineId: 'L1',
        lineRole: 'SUPPORT',
        lineGeneration: 1,
        parentLineId: null,
        lineValue: 10,
        breakDistanceNormalized: 0.02,
        structurePivotId: null,
        structurePivotPrice: null,
        triggerPath: 'line',
        reasonCodes: [],
        signalQuality: { replayOutcome: 'FAILED' },
      }],
    }
    const failed = deriveMonitorRow(
      symbolFixture({ states: { '15m': warningState } }),
      [],
      undefined,
      Date.parse('2026-07-29T09:35:30+08:00'),
    )
    const stale = deriveMonitorRow(
      symbolFixture({
        states: { '15m': state('15m', [10, 10.2]) },
        minute_decision: {
          ...symbolFixture().minute_decision!,
          data_status: 'DELAYED',
        },
      }),
      [],
      undefined,
      Date.parse('2026-07-29T09:35:30+08:00'),
    )

    expect(failed.signal).toBeNull()
    expect(stale.signal).toBeNull()
  })

  it('shows a backend completed-bar warning without promoting it to confirmation', () => {
    const warningState = state('15m', [10, 10.2])
    warningState.chart.turning = {
      signals: [{
        side: 'BUY',
        stage: 'WARNING',
        detectedIndex: 1,
        detectedTime: '2026-07-29T09:34:00+08:00',
        actionableIndex: 1,
        actionableTime: '2026-07-29T09:34:00+08:00',
        price: 10.2,
        trendStateBefore: 'RANGE',
        trendStateAfter: 'UP',
        lineId: 'L1',
        lineRole: 'SUPPORT',
        lineGeneration: 1,
        parentLineId: null,
        lineValue: 10,
        breakDistanceNormalized: 0.02,
        structurePivotId: null,
        structurePivotPrice: null,
        triggerPath: 'line',
        reasonCodes: [],
        signalQuality: { replayOutcome: 'PENDING' },
      }],
    }

    const row = deriveMonitorRow(
      symbolFixture({ states: { '15m': warningState } }),
      [],
      undefined,
      Date.parse('2026-07-29T09:35:30+08:00'),
    )

    expect(row.signal).toEqual({
      level: 'WARNING',
      side: 'BUY',
      label: '买入预警',
      occurredAt: '2026-07-29T09:34:00+08:00',
    })
  })

  it('builds one current-day price series and replaces the matching realtime endpoint', () => {
    const item = symbolFixture()
    item.states['5m']!.chart.bars = [
      { ...item.states['5m']!.chart.bars![0], timestamp: '2026-07-28T15:55:00+08:00', close: 9 },
      { ...item.states['5m']!.chart.bars![0], timestamp: '2026-07-29T09:30:00+08:00', close: 10 },
      { ...item.states['5m']!.chart.bars![1], timestamp: '2026-07-29T09:35:00+08:00', close: 10.2 },
    ]

    expect(buildIntradaySparkline(item, {
      period: 'min_1',
      timestamp: '2026-07-29T09:35:00+08:00',
      close: 10.35,
    })).toEqual([10, 10.35])
  })

  it('paginates with a fixed page size of twenty', () => {
    const items = Array.from({ length: 45 }, (_, index) =>
      symbolFixture({ symbol: `${index + 1}.HK` }))

    expect(paginateMonitorSymbols(items, 2)).toMatchObject({
      page: 2,
      pageCount: 3,
      total: 45,
    })
    expect(paginateMonitorSymbols(items, 2).items.map(item => item.symbol)).toEqual(
      Array.from({ length: 20 }, (_, index) => `${index + 21}.HK`),
    )
    expect(paginateMonitorSymbols(items, 99).page).toBe(3)
  })
})
