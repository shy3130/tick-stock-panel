import { describe, expect, it } from 'vitest'

import type {
  DowMinuteDecision,
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
    change_pct: 0.0125,
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
      confirmation_timeframes: ['30m'],
      supporting_reasons: [],
      contrary_risks: [],
      invalidation_conditions: [],
      data_status: 'COMPLETE',
      status_label: '数据完整',
      source_timestamp: '2026-07-29T09:35:00+08:00',
      risk_warning: {
        family: 'KEY_LEVEL_BREAKDOWN',
        stage: 'WARNING',
        title: ' 跌破关键位 ',
        message: '价格跌破关键支撑位',
      },
      daily_summary: {
        as_of_minute: '2026-07-29T09:35:00+08:00',
        direction: 'BULLISH',
        direction_label: '偏强' as DowMinuteDecision['direction_label'],
        action: 'WATCH_BUY',
        action_label: '买入观察',
        confidence: 72,
        phase_path: [],
        summary_text: '走势偏强',
        key_evidence: [],
        reversal_condition: '跌回控制线下方',
        data_status: 'COMPLETE',
        status_label: '数据完整',
        current_price: 10.5,
        vwap_price: 10.48,
        vwap_distance_pct: 0.19,
        input_event_ids: [],
      },
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
  it('converts the HTTP decimal change ratio to display percent units', () => {
    const row = deriveMonitorRow(
      symbolFixture({ last_price: 500, change_pct: 0.0125 }),
      [],
      undefined,
      Date.parse('2026-07-29T09:35:30+08:00'),
    )

    expect(row.price).toBe(500)
    expect(row.changePct).toBe(1.25)
  })

  it('uses only completed 15m/30m bars for channel and momentum', () => {
    const item = symbolFixture({
      states: {
        '5m': state('5m', [10, 10.4, 8], { completion: 'FORMING' }),
        '15m': state('15m', [10, 10.5], { upward: true }),
        '30m': state('30m', [10, 10.7], { upward: true }),
      },
    })

    const row = deriveMonitorRow(item, [], undefined, Date.parse('2026-07-29T09:35:30+08:00'))

    expect(row.trendPosition.channel.code).toBe('UP')
    expect(row.momentumSpeed.momentum5m.direction).toBe('UP')
    expect(row.momentumSpeed.momentum5m.valuePct).toBeCloseTo(4)
    expect(row.momentumSpeed.momentum15m.direction).toBe('UP')
    expect(row.momentumSpeed.momentum15m.valuePct).toBeCloseTo(5)
  })

  it('does not fall back to 5m for control distance or relative volume', () => {
    const item = symbolFixture({
      states: {
        '5m': state('5m', [10, 10.2], { priceToLinePct: -0.8, volumeRatio: 0.9 }),
        '15m': state('15m', [10, 10.2]),
      },
    })

    const row = deriveMonitorRow(item, [], undefined, Date.parse('2026-07-29T09:35:30+08:00'))

    expect(row.trendPosition.control).toBeNull()
    expect(row.volumeFunds.relativeVolume).toBeNull()
    expect(row.trendPosition.channel.code).toBe('PENDING')
  })

  it('prioritizes 15m relative volume independently of the control timeframe', () => {
    const item = symbolFixture({
      states: {
        '15m': state('15m', [10, 10.2], { volumeRatio: 1.5 }),
        '30m': state('30m', [10, 10.2], { priceToLinePct: 0.7, volumeRatio: 2.4 }),
      },
    })

    const row = deriveMonitorRow(item, [], undefined, Date.parse('2026-07-29T09:35:30+08:00'))

    expect(row.trendPosition.control?.timeframe).toBe('30m')
    expect(row.volumeFunds.relativeVolume).toEqual({ timeframe: '15m', ratio: 1.5 })
  })

  it('derives stable grouped decision metrics from completed bars and decisions', () => {
    const completedCloses = Array.from({ length: 16 }, (_, index) => 100 + index)
    const fifteen = state('15m', [...completedCloses, 1000], { completion: 'FORMING' })
    fifteen.chart.bars?.forEach(bar => {
      bar.high = bar.close + 1
      bar.low = bar.close - 1
    })
    const item = symbolFixture({
      states: {
        '15m': fifteen,
        '30m': state('30m', [100, 101], { priceToLinePct: 0.7, volumeRatio: 1.3 }),
      },
    })

    const row = deriveMonitorRow(item, [], undefined, Date.parse('2026-07-29T09:35:30+08:00'))

    expect(row.trendPosition.costDistancePct).toBe(0.19)
    expect(row.trendPosition.control).toMatchObject({ timeframe: '30m', distancePct: 0.7 })
    expect(row.breakoutRisk.atr14Pct).toBeCloseTo(2 / 115 * 100, 6)
    expect(row.breakoutRisk).toMatchObject({
      confirmedTimeframes: 2,
      totalTimeframes: 2,
      riskTitle: '跌破关键位',
    })
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

    expect(complete.volumeFunds.activeFunds).toEqual({ confirmed: true, buyRatioPct: 60 })
    expect(delayed.volumeFunds.activeFunds).toEqual({ confirmed: false, buyRatioPct: null })
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
