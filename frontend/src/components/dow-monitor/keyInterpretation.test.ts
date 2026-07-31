import { describe, expect, it } from 'vitest'

import type {
  EvidenceDimension,
  EvidenceDirection,
  InterpretationMarketContext,
} from './interpretationMarketContext'
import {
  deriveKeyInterpretation,
  formatInterpretationPrice,
} from './keyInterpretation'
import type { SuddenAnomalyMetric } from './suddenAnomalyHighlights'

const DIMENSIONS: EvidenceDimension[] = [
  'PRICE_STRUCTURE',
  'TREND_MOMENTUM',
  'VOLUME',
  'FUNDS',
  'DEPTH',
]

function evidenceFixture(
  overrides: Partial<Record<EvidenceDimension, EvidenceDirection>> = {},
): InterpretationMarketContext['evidence'] {
  return Object.fromEntries(DIMENSIONS.map(dimension => {
    const direction = overrides[dimension] ?? 'NEUTRAL'
    return [dimension, {
      direction,
      available: direction !== 'UNKNOWN',
    }]
  })) as InterpretationMarketContext['evidence']
}

function contextFixture(
  overrides: Partial<InterpretationMarketContext> = {},
): InterpretationMarketContext {
  return {
    market: 'hk',
    channel: { code: 'RANGE', label: '震荡/过渡' },
    currentPrice: 100,
    liveDayHigh: 102,
    liveDayLow: 98,
    referenceDayHigh: 101,
    referenceDayLow: 99,
    confirmationReferenceDayHigh: 101,
    confirmationReferenceDayLow: 99,
    priorConfirmationReferenceDayHigh: 101,
    priorConfirmationReferenceDayLow: 99,
    attemptRange60m: { low: 99, high: 101 },
    confirmationRange60m: { low: 99, high: 101 },
    priorConfirmationRange60m: { low: 99, high: 101 },
    latestCompleted5mClose: 100,
    previousCompleted5mClose: 100,
    vwap: 100,
    controlLine: null,
    metrics: {
      changePct: 0,
      intradayPositionPct: 50,
      momentum1m: { direction: 'FLAT', valuePct: 0 },
      momentum5m: { direction: 'FLAT', valuePct: 0 },
      momentum15m: { direction: 'FLAT', valuePct: 0 },
      relativeVolumeRatio: 1,
      volumeSpeed: 1,
      capitalInflowPct: 50,
      depthPressurePct: 0,
      toDayHighPct: 2,
      fromDayLowPct: 2,
      atr14Pct: 1,
    },
    evidence: evidenceFixture(),
    stableTimeframesAvailable: true,
    capitalAvailable: true,
    warmupMissing: [],
    strategyDelayed: false,
    realtimeDelayed: false,
    delayed: false,
    ...overrides,
  }
}

function interpretation(
  context: InterpretationMarketContext,
  anomalies: SuddenAnomalyMetric[] = [],
) {
  return deriveKeyInterpretation({
    context,
    anomalies: new Set(anomalies),
  })
}

describe('key interpretation scenarios', () => {
  it('distinguishes a live breakout attempt from a completed-5m confirmation', () => {
    const attempt = interpretation(contextFixture({
      currentPrice: 101.2,
      latestCompleted5mClose: 100.8,
      evidence: evidenceFixture({
        PRICE_STRUCTURE: 'UP',
        VOLUME: 'UP',
        FUNDS: 'UP',
      }),
    }))
    expect(attempt).toMatchObject({
      scenarioId: 'BREAKOUT_ATTEMPT',
      category: 'OPPORTUNITY',
      phase: 'ATTEMPT',
      headline: '放量突破正在形成',
    })
    expect(attempt.dimensions).toEqual([
      'PRICE_STRUCTURE',
      'VOLUME',
      'FUNDS',
    ])
    expect(attempt.levels).toMatchObject([
      { label: '确认 5m收', comparator: '>', price: 101 },
      { label: '失效 5m收', comparator: '<', price: 101 },
      { label: '日高', price: 102 },
    ])

    const confirmed = interpretation(contextFixture({
      currentPrice: 101.4,
      latestCompleted5mClose: 101.2,
      evidence: evidenceFixture({
        PRICE_STRUCTURE: 'UP',
        TREND_MOMENTUM: 'UP',
        VOLUME: 'UP',
      }),
    }), ['depthPressurePct'])
    expect(confirmed).toMatchObject({
      scenarioId: 'BREAKOUT_CONFIRMED',
      category: 'OPPORTUNITY',
      phase: 'CONFIRMED',
      headline: '放量突破已确认',
    })
  })

  it('does not promote a price cross without volume and a second directional confirmation', () => {
    const result = interpretation(contextFixture({
      currentPrice: 101.2,
      evidence: evidenceFixture({
        PRICE_STRUCTURE: 'UP',
      }),
    }), ['changePct'])

    expect(result.scenarioId).toBe('ANOMALY_PENDING')
    expect(result.category).toBe('ANOMALY')
    expect(result.headline).toContain('待确认')
  })

  it('detects completed breakdown risk and its later recovery', () => {
    const breakdown = interpretation(contextFixture({
      currentPrice: 98.7,
      latestCompleted5mClose: 98.8,
      evidence: evidenceFixture({
        PRICE_STRUCTURE: 'DOWN',
        TREND_MOMENTUM: 'DOWN',
        VOLUME: 'UP',
        FUNDS: 'DOWN',
      }),
    }))
    expect(breakdown).toMatchObject({
      scenarioId: 'BREAKDOWN_CONFIRMED',
      category: 'RISK',
      phase: 'CONFIRMED',
    })
    expect(breakdown.levels[0]).toMatchObject({
      label: '确认 5m收',
      comparator: '<',
      price: 99,
    })

    const recovered = interpretation(contextFixture({
      previousCompleted5mClose: 98.8,
      latestCompleted5mClose: 99.2,
      evidence: evidenceFixture({
        PRICE_STRUCTURE: 'UP',
        TREND_MOMENTUM: 'UP',
      }),
    }))
    expect(recovered).toMatchObject({
      scenarioId: 'BREAKDOWN_INVALIDATED',
      category: 'OBSERVE',
      phase: 'INVALIDATED',
    })
  })

  it('detects an upward retest hold without calling it a breakout', () => {
    const result = interpretation(contextFixture({
      channel: { code: 'UP', label: '上升通道' },
      currentPrice: 100.1,
      vwap: 100,
      attemptRange60m: { low: 97, high: 104 },
      confirmationRange60m: { low: 97, high: 104 },
      controlLine: { price: 99.8, role: 'SUPPORT', timeframe: '15m' },
      metrics: {
        ...contextFixture().metrics,
        momentum1m: { direction: 'UP', valuePct: 0.12 },
        momentum5m: { direction: 'UP', valuePct: 0.2 },
        momentum15m: { direction: 'UP', valuePct: 0.3 },
      },
      evidence: evidenceFixture({
        PRICE_STRUCTURE: 'UP',
        TREND_MOMENTUM: 'UP',
        FUNDS: 'UP',
      }),
    }))

    expect(result).toMatchObject({
      scenarioId: 'RETEST_HOLD',
      category: 'OPPORTUNITY',
      phase: 'ATTEMPT',
      headline: '回踩承接正在形成',
    })
    expect(result.levels[0].price).toBe(100)
  })

  it('detects downside acceleration, high pullback, and high-volume stall from combined evidence', () => {
    const downside = interpretation(contextFixture({
      channel: { code: 'DOWN', label: '下降通道' },
      currentPrice: 98.2,
      liveDayLow: 98,
      metrics: {
        ...contextFixture().metrics,
        atr14Pct: 1,
        fromDayLowPct: 0.2,
        momentum1m: { direction: 'DOWN', valuePct: -0.2 },
        momentum5m: { direction: 'DOWN', valuePct: -0.4 },
        momentum15m: { direction: 'DOWN', valuePct: -0.6 },
      },
      evidence: evidenceFixture({
        PRICE_STRUCTURE: 'DOWN',
        TREND_MOMENTUM: 'DOWN',
        VOLUME: 'UP',
        FUNDS: 'DOWN',
      }),
    }))
    expect(downside.scenarioId).toBe('DOWNSIDE_ACCELERATION')

    const pullback = interpretation(contextFixture({
      currentPrice: 100,
      liveDayHigh: 105,
      vwap: 101,
      metrics: {
        ...contextFixture().metrics,
        toDayHighPct: 4.8,
        atr14Pct: 1,
        momentum1m: { direction: 'DOWN', valuePct: -0.3 },
        momentum5m: { direction: 'DOWN', valuePct: -0.2 },
      },
      evidence: evidenceFixture({
        PRICE_STRUCTURE: 'DOWN',
        TREND_MOMENTUM: 'DOWN',
        FUNDS: 'DOWN',
      }),
    }))
    expect(pullback.scenarioId).toBe('HIGH_PULLBACK')

    const stall = interpretation(contextFixture({
      currentPrice: 101,
      liveDayHigh: 102,
      metrics: {
        ...contextFixture().metrics,
        intradayPositionPct: 90,
        momentum1m: { direction: 'FLAT', valuePct: 0 },
        momentum5m: { direction: 'FLAT', valuePct: 0 },
      },
      evidence: evidenceFixture({
        PRICE_STRUCTURE: 'UP',
        VOLUME: 'UP',
        FUNDS: 'NEUTRAL',
      }),
    }))
    expect(stall.scenarioId).toBe('HIGH_VOLUME_STALL')
  })

  it('keeps one depth anomaly pending and returns no clear opportunity without a combination', () => {
    const anomaly = interpretation(
      contextFixture({ evidence: evidenceFixture({ DEPTH: 'UP' }) }),
      ['depthPressurePct'],
    )
    expect(anomaly).toMatchObject({
      scenarioId: 'ANOMALY_PENDING',
      category: 'ANOMALY',
    })
    expect(anomaly.explanation).toContain('成交价格和主动资金尚未同步')

    const observe = interpretation(contextFixture())
    expect(observe).toMatchObject({
      scenarioId: 'NO_CLEAR_OPPORTUNITY',
      category: 'OBSERVE',
      phase: 'NONE',
    })
    expect(observe.levels).toMatchObject([
      { label: '等待上破', comparator: '>', price: 101 },
      { label: '或下破', comparator: '<', price: 99 },
    ])
  })

  it('prioritizes delayed data and never emits directive trading copy', () => {
    const delayed = interpretation(contextFixture({
      delayed: true,
      currentPrice: null,
    }), ['volumeSpeed'])
    expect(delayed).toMatchObject({
      scenarioId: 'DATA_UNAVAILABLE',
      category: 'DATA',
      headline: '关键数据延迟',
      levels: [],
    })

    const outputs = [
      delayed,
      interpretation(contextFixture()),
      interpretation(contextFixture({
        currentPrice: 101.2,
        evidence: evidenceFixture({
          PRICE_STRUCTURE: 'UP',
          VOLUME: 'UP',
          FUNDS: 'UP',
        }),
      })),
    ]
    for (const output of outputs) {
      expect(output.levels.length).toBeLessThanOrEqual(3)
      expect(output.accessibleText).not.toMatch(/建议买入|建议卖出|立即操作|止盈|止损/)
      if (output.category === 'OPPORTUNITY' || output.category === 'RISK') {
        expect(new Set(output.dimensions).size).toBeGreaterThanOrEqual(2)
      }
    }
    expect(deriveKeyInterpretation({
      context: contextFixture(),
      anomalies: new Set(),
    })).toEqual(deriveKeyInterpretation({
      context: contextFixture(),
      anomalies: new Set(),
    }))
  })

  it('explains fresh realtime movement while completed strategy history is warming up', () => {
    const warmup = interpretation({
      ...contextFixture({
        currentPrice: 101,
        liveDayHigh: 102,
        liveDayLow: 98,
        referenceDayHigh: null,
        referenceDayLow: null,
        confirmationReferenceDayHigh: null,
        confirmationReferenceDayLow: null,
        priorConfirmationReferenceDayHigh: null,
        priorConfirmationReferenceDayLow: null,
        attemptRange60m: null,
        confirmationRange60m: null,
        priorConfirmationRange60m: null,
        latestCompleted5mClose: null,
        previousCompleted5mClose: null,
        metrics: {
          ...contextFixture().metrics,
          momentum1m: { direction: 'UP', valuePct: 1 },
          momentum5m: { direction: 'UNKNOWN', valuePct: null },
          momentum15m: { direction: 'UNKNOWN', valuePct: null },
          volumeSpeed: 2.4,
          capitalInflowPct: null,
          depthPressurePct: 30,
        },
      }),
      strategyDelayed: true,
      realtimeDelayed: false,
      stableTimeframesAvailable: false,
      capitalAvailable: false,
      warmupMissing: ['5m周期', '15m周期', '资金'],
    } as InterpretationMarketContext, ['volumeSpeed'])

    expect(warmup).toMatchObject({
      scenarioId: 'LIVE_WARMUP',
      category: 'ANOMALY',
      phase: 'ATTEMPT',
      headline: '实时放量上行，正式分析更新中',
    })
    expect(warmup.explanation).toContain('1分钟 +1.00%')
    expect(warmup.explanation).toContain('量速 2.40×')
    expect(warmup.explanation).toContain('五档 +30.0%')
    expect(warmup.explanation).toContain('5m周期、15m周期、资金仍在预热')
    expect(warmup.levels).toEqual([
      { label: '日高', price: 102, basis: 'LIVE_DAY_HIGH' },
      { label: '日低', price: 98, basis: 'LIVE_DAY_LOW' },
    ])
  })

  it('shows analysis updating before the strategy age threshold when stable periods are absent', () => {
    const result = interpretation(contextFixture({
      strategyDelayed: false,
      realtimeDelayed: false,
      stableTimeframesAvailable: false,
      warmupMissing: ['5m-period', '15m-period'],
      currentPrice: 101,
    }))

    expect(result.scenarioId).toBe('LIVE_WARMUP')
  })

  it('keeps stable interpretation available when only scheduler age is delayed', () => {
    const result = interpretation(contextFixture({
      strategyDelayed: true,
      realtimeDelayed: false,
      stableTimeframesAvailable: true,
      capitalAvailable: true,
      warmupMissing: [],
      channel: { code: 'UP', label: '上升通道' },
      currentPrice: 101.2,
      attemptRange60m: { low: 99, high: 101 },
      confirmationRange60m: { low: 99, high: 101 },
      metrics: {
        ...contextFixture().metrics,
        volumeSpeed: 2,
      },
      evidence: evidenceFixture({
        PRICE_STRUCTURE: 'UP',
        TREND_MOMENTUM: 'UP',
        VOLUME: 'UP',
        FUNDS: 'UP',
      }),
    }))

    expect(result.scenarioId).not.toBe('LIVE_WARMUP')
    expect(result.headline).not.toContain('正式分析更新中')
  })

  it('formats decision prices with two to four decimals without grouping', () => {
    expect(formatInterpretationPrice(650.2)).toBe('650.20')
    expect(formatInterpretationPrice(0.1234)).toBe('0.1234')
    expect(formatInterpretationPrice(1234.5)).toBe('1234.50')
  })
})
