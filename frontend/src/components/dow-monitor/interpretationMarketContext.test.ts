import { describe, expect, it } from 'vitest'

import type { RealtimeSymbolState } from '@/lib/realtimeMarketData'

import {
  deriveInterpretationMarketContext,
  INTERPRETATION_THRESHOLDS,
} from './interpretationMarketContext'
import type { MonitorRowPresentation } from './monitorListPresentation'
import type {
  DowMonitorBar,
  DowMonitorOverviewSymbol,
  DowMonitorTimeframeState,
} from './types'

function bar(
  index: number,
  high: number,
  low: number,
  close = (high + low) / 2,
  date = '2026-07-30',
): DowMonitorBar {
  const timestamp = new Date(`${date}T01:30:00.000Z`)
  timestamp.setUTCMinutes(timestamp.getUTCMinutes() + index * 5)
  return {
    index,
    timestamp: timestamp.toISOString().replace('.000Z', '+08:00'),
    open: close,
    high,
    low,
    close,
    volume: 100,
  }
}

const COMPLETED_5M = [
  bar(0, 645.0, 640.1),
  bar(1, 646.0, 641.6),
  bar(2, 647.0, 641.9),
  bar(3, 647.2, 642.8),
  bar(4, 647.4, 643.0),
  bar(5, 647.6, 643.2),
  bar(6, 647.8, 643.4),
  bar(7, 648.0, 643.6),
  bar(8, 648.2, 643.8),
  bar(9, 648.4, 644.0),
  bar(10, 648.6, 644.2),
  bar(11, 649.0, 644.4),
  bar(12, 649.4, 644.6),
  bar(13, 649.8, 644.8),
  bar(14, 650.2, 645.0, 650.1),
]

function state(
  timeframe: '5m' | '15m' | '30m',
  bars: DowMonitorBar[],
  overrides: Partial<DowMonitorTimeframeState['snapshot']> = {},
): DowMonitorTimeframeState {
  return {
    symbol: '700.HK',
    market: 'hk',
    timeframe,
    freshness_state: 'LIVE',
    source_timestamp: '2026-07-30T10:45:00+08:00',
    snapshot: {
      bar_time: bars.at(-1)?.timestamp,
      bar_completion: 'FINAL',
      provisional: false,
      line_role: 'SUPPORT',
      line_value: 645.8,
      ...overrides,
    },
    chart: { bars },
    updated_at: '2026-07-30T10:45:02+08:00',
  }
}

function itemFixture(
  overrides: Partial<DowMonitorOverviewSymbol> = {},
): DowMonitorOverviewSymbol {
  const forming = bar(15, 999, 1, 888)
  return {
    symbol: '700.HK',
    market: 'hk',
    enabled: true,
    created_at: '2026-07-30T09:00:00+08:00',
    updated_at: '2026-07-30T10:45:02+08:00',
    name: '腾讯控股',
    last_price: 650.6,
    change_pct: 0.02,
    quote_timestamp: '2026-07-30T10:45:30+08:00',
    analysis_status: 'READY',
    intraday_capital: {
      total_in: 55,
      total_out: 45,
      quality: 'COMPLETE',
    },
    minute_decision: {
      symbol: '700.HK',
      market: 'hk',
      decision_minute: '2026-07-30T10:45:00+08:00',
      direction: 'BULLISH',
      direction_label: '偏涨',
      action: 'WATCH_BUY',
      action_label: '买入观察',
      confidence: 70,
      dominant_timeframe: '15m',
      confirmation_timeframes: ['15m', '30m'],
      supporting_reasons: [],
      contrary_risks: [],
      invalidation_conditions: [],
      data_status: 'COMPLETE',
      status_label: '数据完整',
      source_timestamp: '2026-07-30T10:45:00+08:00',
      daily_summary: {
        as_of_minute: '2026-07-30T10:45:00+08:00',
        direction: 'BULLISH',
        direction_label: '偏涨',
        action: 'WATCH_BUY',
        action_label: '买入观察',
        confidence: 70,
        phase_path: [],
        summary_text: '偏强',
        key_evidence: [],
        reversal_condition: '跌回VWAP',
        data_status: 'COMPLETE',
        status_label: '数据完整',
        current_price: 650.6,
        vwap_price: 645.8,
        vwap_distance_pct: 0.74,
        input_event_ids: [],
      },
    },
    states: {
      '5m': state('5m', [...COMPLETED_5M, forming], {
        bar_time: forming.timestamp,
        bar_completion: 'FORMING',
      }),
      '15m': state('15m', [
        bar(0, 646, 642, 644),
        bar(3, 650, 645, 649),
      ], {
        line_role: 'SUPPORT',
        line_value: 645.8,
        volume_ratio_20: 1.5,
      }),
      '30m': state('30m', [
        bar(0, 644, 640, 642),
        bar(6, 651, 644, 650),
      ]),
    },
    latest_notification: null,
    last_success_at: '2026-07-30T10:45:02+08:00',
    last_error: null,
    ...overrides,
  }
}

function rowFixture(
  overrides: Partial<MonitorRowPresentation> = {},
): MonitorRowPresentation {
  return {
    price: 650.6,
    changePct: 2.78,
    trendPosition: {
      channel: { code: 'UP', label: '上升通道' },
      control: { timeframe: '15m', role: '支撑线', distancePct: 0.74 },
      vwap: { price: 645.8, distancePct: 0.74 },
      intradayPositionPct: 96,
    },
    momentumSpeed: {
      momentum1m: { direction: 'UP', valuePct: 0.4 },
      momentum5m: { direction: 'UP', valuePct: 0.7 },
      momentum15m: { direction: 'UP', valuePct: 1.1 },
    },
    volumeFunds: {
      relativeVolume: { timeframe: '15m', ratio: 1.5 },
      volumeSpeed: 1.49,
      capitalInflow: { confirmed: true, inflowRatioPct: 55 },
      depthPressurePct: 20,
    },
    breakoutRisk: {
      toDayHighPct: 0.22,
      fromDayLowPct: 2.1,
      atr14Pct: 1.2,
      dayRangeAtrRatio: 1.1,
      confirmedTimeframes: 2,
      totalTimeframes: 2,
      confirmationTimeframes: [
        { timeframe: '15m', confirmed: true },
        { timeframe: '30m', confirmed: true },
      ],
      riskTitle: null,
    },
    freshness: {
      quote: { ageSeconds: 0, delayed: false },
      depth: { ageSeconds: 1, delayed: false },
      candlestick: { ageSeconds: 0, delayed: false },
      analysis: { ageSeconds: 30, delayed: false },
    },
    signal: null,
    delayed: false,
    sparkline: [],
    ...overrides,
  }
}

function realtimeFixture(
  overrides: Partial<RealtimeSymbolState> = {},
): RealtimeSymbolState {
  return {
    symbol: '700.HK',
    streamId: 'stream-1',
    sequence: 1,
    eventAt: '2026-07-30T10:45:30+08:00',
    publishedAt: '2026-07-30T10:45:30+08:00',
    quote: {
      lastDone: 650.6,
      prevClose: 633,
      high: 652,
      low: 638.4,
      timestamp: '2026-07-30T10:45:30+08:00',
    },
    candlestick: {
      period: 'min_1',
      timestamp: '2026-07-30T10:45:00+08:00',
      open: 648,
      high: 651,
      low: 648,
      close: 650.6,
      volume: 1000,
    },
    depth: {
      bids: [],
      asks: [],
      timestamp: '2026-07-30T10:45:30+08:00',
    },
    quoteDelayed: false,
    depthDelayed: false,
    candlestickDelayed: false,
    ...overrides,
  }
}

describe('interpretation market context', () => {
  it('derives current-day reference prices without using the forming bar', () => {
    const context = deriveInterpretationMarketContext({
      item: itemFixture(),
      row: rowFixture(),
      realtime: realtimeFixture(),
    })

    expect(context.liveDayHigh).toBe(652)
    expect(context.liveDayLow).toBe(638.4)
    expect(context.referenceDayHigh).toBe(650.2)
    expect(context.referenceDayLow).toBe(640.1)
    expect(context.confirmationReferenceDayHigh).toBe(649.8)
    expect(context.confirmationReferenceDayLow).toBe(640.1)
    expect(context.priorConfirmationReferenceDayHigh).toBe(649.4)
    expect(context.priorConfirmationReferenceDayLow).toBe(640.1)
    expect(context.attemptRange60m).toEqual({ low: 642.8, high: 650.2 })
    expect(context.confirmationRange60m).toEqual({ low: 641.9, high: 649.8 })
    expect(context.priorConfirmationRange60m).toEqual({ low: 641.6, high: 649.4 })
    expect(context.latestCompleted5mClose).toBe(650.1)
    expect(context.controlLine).toEqual({
      price: 645.8,
      role: 'SUPPORT',
      timeframe: '15m',
    })
    expect(context.vwap).toBe(645.8)
  })

  it('rejects previous-day, incomplete-window, provisional-line, and delayed data', () => {
    const priorDay = bar(-1, 900, 2, 400, '2026-07-29')
    const item = itemFixture()
    item.states['5m']!.chart.bars = [
      priorDay,
      ...item.states['5m']!.chart.bars!.slice(-11),
    ]
    item.states['15m']!.snapshot.provisional = true

    const context = deriveInterpretationMarketContext({
      item,
      row: rowFixture({ delayed: true }),
      realtime: realtimeFixture({ quoteDelayed: true }),
    })

    expect(context.referenceDayHigh).not.toBe(900)
    expect(context.attemptRange60m).toBeNull()
    expect(context.confirmationRange60m).toBeNull()
    expect(context.controlLine).toBeNull()
    expect(context.liveDayHigh).toBeNull()
    expect(context.liveDayLow).toBeNull()
    expect(context.currentPrice).toBeNull()
    expect(context.delayed).toBe(true)
  })

  it('keeps fresh websocket price visible while strategy history is warming up', () => {
    const context = deriveInterpretationMarketContext({
      item: itemFixture({
        states: {},
        minute_decision: undefined,
        intraday_capital: undefined,
      }),
      row: rowFixture({
        delayed: true,
        price: 99,
      }),
      realtime: realtimeFixture({
        quote: {
          lastDone: 101,
          prevClose: 100,
          high: 102,
          low: 98,
          timestamp: '2026-07-30T10:45:30+08:00',
        },
        quoteDelayed: false,
        candlestickDelayed: false,
      }),
    })

    expect(context.currentPrice).toBe(101)
    expect(context.liveDayHigh).toBe(102)
    expect(context.liveDayLow).toBe(98)
    expect(context.strategyDelayed).toBe(true)
    expect(context.realtimeDelayed).toBe(false)
    expect(context.stableTimeframesAvailable).toBe(false)
    expect(context.capitalAvailable).toBe(true)
    expect(context.warmupMissing).toEqual(['5m周期', '15m周期'])
  })

  it('separates scheduler delay from stable timeframe and capital availability', () => {
    const context = deriveInterpretationMarketContext({
      item: itemFixture(),
      row: rowFixture({ delayed: true }),
      realtime: realtimeFixture({ quoteDelayed: false, candlestickDelayed: false }),
    })

    expect(context.strategyDelayed).toBe(true)
    expect(context.stableTimeframesAvailable).toBe(true)
    expect(context.capitalAvailable).toBe(true)
    expect(context.warmupMissing).toEqual([])
    expect(context.controlLine).not.toBeNull()
  })

  it('keeps the last stable structure visible while reevaluation is paused', () => {
    const item = itemFixture()
    const pausedStates = Object.fromEntries(
      Object.entries(item.states).map(([timeframe, value]) => [
        timeframe,
        value ? { ...value, freshness_state: 'ANALYSIS_PAUSED' as const } : value,
      ]),
    )
    const context = deriveInterpretationMarketContext({
      item: { ...item, states: pausedStates },
      row: rowFixture({ delayed: true }),
      realtime: realtimeFixture({ quoteDelayed: false, candlestickDelayed: false }),
    })

    expect(context.stableTimeframesAvailable).toBe(true)
    expect(context.warmupMissing).toEqual([])
    expect(context.controlLine).not.toBeNull()
  })

  it('applies evidence thresholds at their exact boundaries', () => {
    const context = deriveInterpretationMarketContext({
      item: itemFixture(),
      row: rowFixture(),
      realtime: realtimeFixture(),
    })

    expect(INTERPRETATION_THRESHOLDS).toMatchObject({
      volumeRatio: 1.5,
      fundsUpPct: 55,
      fundsDownPct: 45,
      depthUpPct: 20,
      depthDownPct: -20,
    })
    expect(context.evidence.VOLUME.direction).toBe('UP')
    expect(context.evidence.FUNDS.direction).toBe('UP')
    expect(context.evidence.DEPTH.direction).toBe('UP')
    expect(context.evidence.TREND_MOMENTUM.direction).toBe('UP')
    expect(context.evidence.PRICE_STRUCTURE.direction).toBe('UP')
  })

  it('keeps neutral boundaries distinct from unavailable evidence', () => {
    const neutral = deriveInterpretationMarketContext({
      item: itemFixture(),
      row: rowFixture({
        momentumSpeed: {
          momentum1m: { direction: 'UP', valuePct: 0.4 },
          momentum5m: { direction: 'UP', valuePct: 0.2 },
          momentum15m: { direction: 'UNKNOWN', valuePct: null },
        },
        volumeFunds: {
          relativeVolume: { timeframe: '15m', ratio: 1.49 },
          volumeSpeed: 1.49,
          capitalInflow: { confirmed: true, inflowRatioPct: 54.99 },
          depthPressurePct: 19.99,
        },
      }),
      realtime: realtimeFixture(),
    })

    expect(neutral.evidence.VOLUME.direction).toBe('NEUTRAL')
    expect(neutral.evidence.FUNDS.direction).toBe('NEUTRAL')
    expect(neutral.evidence.DEPTH.direction).toBe('NEUTRAL')
    expect(neutral.evidence.TREND_MOMENTUM.direction).toBe('UNKNOWN')

    const unavailable = deriveInterpretationMarketContext({
      item: itemFixture(),
      row: rowFixture({
        volumeFunds: {
          relativeVolume: null,
          volumeSpeed: null,
          capitalInflow: { confirmed: false, inflowRatioPct: null },
          depthPressurePct: null,
        },
      }),
      realtime: realtimeFixture({ depthDelayed: true }),
    })
    expect(unavailable.evidence.VOLUME.direction).toBe('UNKNOWN')
    expect(unavailable.evidence.FUNDS.direction).toBe('UNKNOWN')
    expect(unavailable.evidence.DEPTH.direction).toBe('UNKNOWN')
  })
})
