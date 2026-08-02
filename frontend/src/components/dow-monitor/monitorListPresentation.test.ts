import { describe, expect, it } from 'vitest'

import type { RealtimeSymbolState } from '@/lib/realtimeMarketData'

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
    provisional?: boolean
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
      provisional: options.provisional ?? false,
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

function realtimeFixture(
  overrides: Partial<RealtimeSymbolState> = {},
): RealtimeSymbolState {
  return {
    symbol: '700.HK',
    streamId: 'stream-1',
    sequence: 10,
    eventAt: '2026-07-29T09:35:30+08:00',
    publishedAt: '2026-07-29T09:35:30+08:00',
    quote: {
      lastDone: 101,
      prevClose: 100,
      high: 102,
      low: 95,
      timestamp: '2026-07-29T09:35:30+08:00',
    },
    candlestick: {
      period: 'min_1',
      timestamp: '2026-07-29T09:35:00+08:00',
      open: 100,
      close: 101,
      volume: 40,
    },
    depth: {
      bids: [100, 80, 60, 40, 20].map((volume, index) => ({ position: index + 1, volume })),
      asks: [70, 60, 50, 40, 30].map((volume, index) => ({ position: index + 1, volume })),
      timestamp: '2026-07-29T09:35:25+08:00',
    },
    quoteDelayed: false,
    depthDelayed: false,
    candlestickDelayed: false,
    ...overrides,
  }
}

describe('monitor list presentation', () => {
  it('derives 1m momentum, day-range distance, and five-level depth pressure independently', () => {
    const row = deriveMonitorRow(
      symbolFixture(),
      [],
      realtimeFixture(),
      Date.parse('2026-07-29T09:35:30+08:00'),
    )

    expect(row.momentumSpeed.momentum1m.valuePct).toBeCloseTo(1)
    expect(row.breakoutRisk.toDayHighPct).toBeCloseTo(100 / 101)
    expect(row.breakoutRisk.fromDayLowPct).toBeCloseTo(600 / 101)
    expect(row.volumeFunds.depthPressurePct).toBeCloseTo((300 - 250) / 550 * 100)
    expect(row.trendPosition.intradayPositionPct).toBeCloseTo(6 / 7 * 100)
  })

  it('presents VWAP, capital inflow, timeframe confirmation, and source ages with their real semantics', () => {
    const row = deriveMonitorRow(
      symbolFixture(),
      [],
      realtimeFixture(),
      Date.parse('2026-07-29T09:35:30+08:00'),
    )

    expect(row.trendPosition.vwap).toEqual({
      price: 10.48,
      distancePct: 0.19,
    })
    expect(row.volumeFunds.capitalInflow).toEqual({
      confirmed: true,
      inflowRatioPct: 60,
    })
    expect(row.breakoutRisk.confirmationTimeframes).toEqual([
      { timeframe: '15m', confirmed: true },
      { timeframe: '30m', confirmed: true },
    ])
    expect(row.freshness).toEqual({
      quote: { ageSeconds: 0, delayed: false },
      depth: { ageSeconds: 5, delayed: false },
      candlestick: { ageSeconds: 30, delayed: false },
      analysis: { ageSeconds: 30, delayed: false },
    })
  })

  it('keeps intraday position, range over ATR, and freshness unavailable when their source is invalid', () => {
    const completedCloses = Array.from({ length: 15 }, (_, index) => 100 + index)
    const fifteen = state('15m', completedCloses)
    fifteen.chart.bars?.forEach(bar => {
      bar.high = bar.close + 1
      bar.low = bar.close - 1
    })
    const valid = deriveMonitorRow(
      symbolFixture({ states: { '15m': fifteen } }),
      [],
      realtimeFixture({
        quote: {
          lastDone: 110,
          prevClose: 100,
          high: 114,
          low: 107,
          timestamp: '2026-07-29T09:35:30+08:00',
        },
      }),
      Date.parse('2026-07-29T09:35:30+08:00'),
    )
    expect(valid.breakoutRisk.dayRangeAtrRatio).toBeCloseTo(3.5)

    const invalid = deriveMonitorRow(
      symbolFixture({ states: { '15m': fifteen } }),
      [],
      realtimeFixture({
        quote: {
          lastDone: 110,
          prevClose: 100,
          high: 110,
          low: 110,
          timestamp: '2026-07-29T09:30:00+08:00',
        },
        quoteDelayed: true,
        depth: undefined,
        candlestick: undefined,
      }),
      Date.parse('2026-07-29T09:35:30+08:00'),
    )
    expect(invalid.trendPosition.intradayPositionPct).toBeNull()
    expect(invalid.breakoutRisk.dayRangeAtrRatio).toBeNull()
    expect(invalid.freshness.quote).toEqual({ ageSeconds: 330, delayed: true })
    expect(invalid.freshness.depth).toEqual({ ageSeconds: null, delayed: false })
    expect(invalid.freshness.candlestick).toEqual({ ageSeconds: null, delayed: false })
  })

  it('projects volume speed only within the valid 1m observation window', () => {
    const fiveMinute = state('5m', Array.from({ length: 12 }, () => 100))
    fiveMinute.chart.bars?.forEach(bar => { bar.volume = 500 })
    const item = symbolFixture({ states: { '5m': fiveMinute } })
    const realtime = realtimeFixture()

    expect(deriveMonitorRow(item, [], realtime, Date.parse('2026-07-29T09:35:30+08:00'))
      .volumeFunds.volumeSpeed).toBeCloseTo(0.8)
    expect(deriveMonitorRow(item, [], realtime, Date.parse('2026-07-29T09:35:10+08:00'))
      .volumeFunds.volumeSpeed).toBeNull()
    expect(deriveMonitorRow(item, [], realtime, Date.parse('2026-07-29T09:36:10+08:00'))
      .volumeFunds.volumeSpeed).toBeNull()
    expect(deriveMonitorRow(item, [], realtime, Date.parse('2026-07-29T09:36:15+08:00'))
      .volumeFunds.volumeSpeed).toBeNull()
    expect(deriveMonitorRow(
      symbolFixture({ states: { '5m': state('5m', Array.from({ length: 11 }, () => 100)) } }),
      [],
      realtime,
      Date.parse('2026-07-29T09:35:30+08:00'),
    ).volumeFunds.volumeSpeed).toBeNull()
    expect(deriveMonitorRow(item, [], realtimeFixture({ candlestickDelayed: true }),
      Date.parse('2026-07-29T09:35:30+08:00')).volumeFunds.volumeSpeed).toBeNull()
  })

  it('degrades each delayed realtime feed independently', () => {
    const now = Date.parse('2026-07-29T09:35:30+08:00')
    const candle = deriveMonitorRow(symbolFixture(), [], realtimeFixture({ candlestickDelayed: true }), now)
    expect(candle.momentumSpeed.momentum1m.valuePct).toBeNull()
    expect(candle.volumeFunds.depthPressurePct).not.toBeNull()
    expect(candle.breakoutRisk.toDayHighPct).not.toBeNull()
    expect(candle.breakoutRisk.fromDayLowPct).not.toBeNull()
    const depth = deriveMonitorRow(symbolFixture(), [], realtimeFixture({ depthDelayed: true }), now)
    expect(depth.volumeFunds.depthPressurePct).toBeNull()
    expect(depth.momentumSpeed.momentum1m.valuePct).not.toBeNull()
    expect(depth.breakoutRisk.toDayHighPct).not.toBeNull()
    expect(depth.breakoutRisk.fromDayLowPct).not.toBeNull()
    const row = deriveMonitorRow(symbolFixture(), [], realtimeFixture({ quoteDelayed: true }), now)
    expect(row.breakoutRisk.toDayHighPct).toBeNull()
    expect(row.breakoutRisk.fromDayLowPct).toBeNull()
    expect(row.momentumSpeed.momentum1m.valuePct).not.toBeNull()
    expect(row.volumeFunds.depthPressurePct).not.toBeNull()
  })

  it('does not let realtime depth change a formal BUY signal', () => {
    const item = symbolFixture({ latest_notification: notification() })
    const bidHeavy = deriveMonitorRow(item, [], realtimeFixture(), Date.parse('2026-07-29T09:35:30+08:00'))
    const askHeavy = deriveMonitorRow(item, [], realtimeFixture({
      depth: {
        bids: [{ position: 1, volume: 10 }],
        asks: [{ position: 1, volume: 100 }],
      },
    }), Date.parse('2026-07-29T09:35:30+08:00'))

    expect(bidHeavy.signal).toEqual(askHeavy.signal)
    expect(bidHeavy.signal).toMatchObject({ level: 'CONFIRMED', side: 'BUY' })
  })

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

  it('rejects forming or provisional stable metrics and never falls back to 5m', () => {
    const five = state('5m', [10, 10.2], {
      priceToLinePct: -0.8,
      volumeRatio: 0.9,
    })
    const fiveOnly = deriveMonitorRow(symbolFixture({
      states: {
        '5m': five,
        '15m': state('15m', [10, 10.2]),
      },
    }), [], undefined, Date.parse('2026-07-29T09:35:30+08:00'))
    expect(fiveOnly.trendPosition.control).toBeNull()
    expect(fiveOnly.volumeFunds.relativeVolume).toBeNull()
    expect(fiveOnly.trendPosition.channel.code).toBe('PENDING')

    const thirty = state('30m', [10, 10.2], {
      priceToLinePct: 0.7,
      volumeRatio: 2.4,
    })
    for (const fifteen of [
      state('15m', [10, 10.2], {
        completion: 'FORMING',
        priceToLinePct: 1.5,
        volumeRatio: 1.6,
      }),
      state('15m', [10, 10.2], {
        provisional: true,
        priceToLinePct: 1.5,
        volumeRatio: 1.6,
      }),
    ]) {
      const row = deriveMonitorRow(symbolFixture({
        states: { '5m': five, '15m': fifteen, '30m': thirty },
      }), [], undefined, Date.parse('2026-07-29T09:35:30+08:00'))
      expect(row.trendPosition.control).toMatchObject({
        timeframe: '30m',
        distancePct: 0.7,
      })
      expect(row.volumeFunds.relativeVolume).toEqual({
        timeframe: '30m',
        ratio: 2.4,
      })
    }
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

    expect(row.trendPosition.vwap).toEqual({ price: 10.48, distancePct: 0.19 })
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

    expect(complete.volumeFunds.capitalInflow).toEqual({ confirmed: true, inflowRatioPct: 60 })
    expect(delayed.volumeFunds.capitalInflow).toEqual({ confirmed: false, inflowRatioPct: null })
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

  it('keeps list semantics identical after the approved compact state projection', () => {
    const full = symbolFixture()
    const makeBars = (count: number, minutes: number) => Array.from(
      { length: count },
      (_, index) => {
        const timestamp = new Date(Date.UTC(2026, 6, 29, 1, 30 + index * minutes))
        const close = 10 + index * 0.1
        return {
          index,
          timestamp: timestamp.toISOString(),
          open: close - 0.05,
          high: close + 0.2,
          low: close - 0.2,
          close,
          volume: 100 + index,
          ma5: close - 0.1,
          ma10: close - 0.2,
          ma20: close - 0.3,
        }
      },
    )
    full.states['5m']!.chart.bars = [
      { ...makeBars(1, 5)[0], timestamp: '2026-07-28T15:55:00+08:00' },
      ...makeBars(18, 5),
    ]
    full.states['15m'] = state('15m', [10, 10.5], {
      upward: true,
      priceToLinePct: 1.2,
      volumeRatio: 1.6,
    })
    full.states['15m']!.chart.bars = makeBars(24, 15)
    full.states['30m'] = state('30m', [9.8, 10.5], { upward: true })
    full.states['30m']!.chart.bars = makeBars(8, 30)

    const compact = structuredClone(full)
    compact.states['5m']!.chart.bars = compact.states['5m']!.chart.bars!.filter(
      bar => bar.timestamp.startsWith('2026-07-29'),
    )
    compact.states['15m']!.chart.bars = compact.states['15m']!.chart.bars!.slice(-16)
    compact.states['30m']!.chart.bars = compact.states['30m']!.chart.bars!.slice(-2)

    const now = Date.parse('2026-07-29T09:35:30+08:00')
    const fullRow = deriveMonitorRow(full, [], undefined, now)
    const compactRow = deriveMonitorRow(compact, [], undefined, now)

    expect(compactRow.trendPosition).toEqual(fullRow.trendPosition)
    expect(compactRow.momentumSpeed).toEqual(fullRow.momentumSpeed)
    expect(compactRow.volumeFunds).toEqual(fullRow.volumeFunds)
    expect(compactRow.breakoutRisk).toEqual(fullRow.breakoutRisk)
    expect(compactRow.signal).toEqual(fullRow.signal)
    expect(compactRow.sparkline).toEqual(fullRow.sparkline)
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
