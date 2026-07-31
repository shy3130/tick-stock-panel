import type { RealtimeSymbolState } from '@/lib/realtimeMarketData'

import type {
  MonitorChannel,
  MonitorMomentum,
  MonitorRowPresentation,
} from './monitorListPresentation'
import type {
  DowMonitorBar,
  DowMonitorOverviewSymbol,
  DowMonitorSymbolMarket,
  DowMonitorTimeframeState,
} from './types'

export const INTERPRETATION_THRESHOLDS = {
  volumeRatio: 1.5,
  fundsUpPct: 55,
  fundsDownPct: 45,
  depthUpPct: 20,
  depthDownPct: -20,
  nearAtrFraction: 0.25,
  nearFallbackPct: 0.5,
} as const

export type EvidenceDimension =
  | 'PRICE_STRUCTURE'
  | 'TREND_MOMENTUM'
  | 'VOLUME'
  | 'FUNDS'
  | 'DEPTH'

export type EvidenceDirection = 'UP' | 'DOWN' | 'NEUTRAL' | 'UNKNOWN'

export interface PriceRange {
  low: number
  high: number
}

export interface InterpretationEvidence {
  direction: EvidenceDirection
  available: boolean
}

export interface InterpretationMarketMetrics {
  changePct: number | null
  intradayPositionPct: number | null
  momentum1m: MonitorMomentum
  momentum5m: MonitorMomentum
  momentum15m: MonitorMomentum
  relativeVolumeRatio: number | null
  volumeSpeed: number | null
  capitalInflowPct: number | null
  depthPressurePct: number | null
  toDayHighPct: number | null
  fromDayLowPct: number | null
  atr14Pct: number | null
}

export interface InterpretationMarketContextInput {
  item: DowMonitorOverviewSymbol
  row: MonitorRowPresentation
  realtime?: RealtimeSymbolState
}

export interface InterpretationMarketContext {
  market: DowMonitorSymbolMarket
  channel: MonitorChannel
  currentPrice: number | null
  liveDayHigh: number | null
  liveDayLow: number | null
  referenceDayHigh: number | null
  referenceDayLow: number | null
  confirmationReferenceDayHigh: number | null
  confirmationReferenceDayLow: number | null
  priorConfirmationReferenceDayHigh: number | null
  priorConfirmationReferenceDayLow: number | null
  attemptRange60m: PriceRange | null
  confirmationRange60m: PriceRange | null
  priorConfirmationRange60m: PriceRange | null
  latestCompleted5mClose: number | null
  previousCompleted5mClose: number | null
  vwap: number | null
  controlLine: {
    price: number
    role: string
    timeframe: '15m' | '30m'
  } | null
  metrics: InterpretationMarketMetrics
  evidence: Record<EvidenceDimension, InterpretationEvidence>
  stableTimeframesAvailable: boolean
  capitalAvailable: boolean
  warmupMissing: string[]
  strategyDelayed: boolean
  realtimeDelayed: boolean
  delayed: boolean
}

function finite(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function timestampDate(value: string | null | undefined): string | null {
  if (!value || value.length < 10) return null
  return value.slice(0, 10)
}

function completedBars(
  state: DowMonitorTimeframeState | undefined,
): DowMonitorBar[] {
  if (!state || state.freshness_state === 'STALE_DATA') return []
  const bars = state.chart.bars ?? []
  if (
    state.snapshot.bar_completion !== 'FORMING'
    && !state.snapshot.provisional
  ) return bars
  const formingTime = state.snapshot.bar_time
  if (formingTime) {
    const filtered = bars.filter(bar => bar.timestamp !== formingTime)
    if (filtered.length !== bars.length) return filtered
  }
  return bars.slice(0, -1)
}

function validBarsForDate(
  state: DowMonitorTimeframeState | undefined,
  date: string | null,
): DowMonitorBar[] {
  if (!date) return []
  return completedBars(state)
    .filter(bar => (
      timestampDate(bar.timestamp) === date
      && finite(bar.high)
      && finite(bar.low)
      && finite(bar.close)
      && bar.high >= bar.low
    ))
    .sort((left, right) => left.timestamp.localeCompare(right.timestamp))
}

function localClock(value: string): string {
  return value.slice(11, 16)
}

function isExpectedAdjacentBar(
  left: DowMonitorBar,
  right: DowMonitorBar,
  market: DowMonitorSymbolMarket,
): boolean {
  const minutes = (Date.parse(right.timestamp) - Date.parse(left.timestamp)) / 60_000
  if (minutes === 5) return true
  if (timestampDate(left.timestamp) !== timestampDate(right.timestamp)) return false
  const leftClock = localClock(left.timestamp)
  const rightClock = localClock(right.timestamp)
  if (market === 'hk') {
    return (leftClock === '11:55' || leftClock === '12:00') && rightClock === '13:00'
  }
  if (market === 'cn') {
    return (leftClock === '11:25' || leftClock === '11:30') && rightClock === '13:00'
  }
  return false
}

function priceRange(
  bars: DowMonitorBar[],
  market: DowMonitorSymbolMarket,
): PriceRange | null {
  if (bars.length !== 12) return null
  if (bars.some((bar, index) => (
    index > 0 && !isExpectedAdjacentBar(bars[index - 1], bar, market)
  ))) return null
  return {
    low: Math.min(...bars.map(bar => bar.low)),
    high: Math.max(...bars.map(bar => bar.high)),
  }
}

function highLow(
  bars: DowMonitorBar[],
): Pick<PriceRange, 'high' | 'low'> | null {
  if (bars.length === 0) return null
  return {
    low: Math.min(...bars.map(bar => bar.low)),
    high: Math.max(...bars.map(bar => bar.high)),
  }
}

function stableControlLine(
  item: DowMonitorOverviewSymbol,
): InterpretationMarketContext['controlLine'] {
  for (const timeframe of ['15m', '30m'] as const) {
    const state = item.states[timeframe]
    const price = state?.snapshot.line_value
    const role = state?.snapshot.line_role?.trim()
    if (
      !state
      || state.freshness_state === 'STALE_DATA'
      || state.snapshot.bar_completion === 'FORMING'
      || state.snapshot.provisional
      || !finite(price)
      || !role
    ) continue
    return { price, role, timeframe }
  }
  return null
}

function evidence(
  direction: EvidenceDirection,
): InterpretationEvidence {
  return {
    direction,
    available: direction !== 'UNKNOWN',
  }
}

function trendMomentumEvidence(
  row: MonitorRowPresentation,
): InterpretationEvidence {
  const five = row.momentumSpeed.momentum5m.direction
  const fifteen = row.momentumSpeed.momentum15m.direction
  if (five === 'UNKNOWN' || fifteen === 'UNKNOWN') return evidence('UNKNOWN')
  if (five === 'UP' && fifteen === 'UP') return evidence('UP')
  if (five === 'DOWN' && fifteen === 'DOWN') return evidence('DOWN')
  return evidence('NEUTRAL')
}

function volumeEvidence(
  row: MonitorRowPresentation,
): InterpretationEvidence {
  const values = [
    row.volumeFunds.relativeVolume?.ratio,
    row.volumeFunds.volumeSpeed,
  ].filter(finite)
  if (values.length === 0) return evidence('UNKNOWN')
  return evidence(values.some(value => value >= INTERPRETATION_THRESHOLDS.volumeRatio)
    ? 'UP'
    : 'NEUTRAL')
}

function fundsEvidence(
  row: MonitorRowPresentation,
): InterpretationEvidence {
  const capital = row.volumeFunds.capitalInflow
  if (!capital.confirmed || !finite(capital.inflowRatioPct)) {
    return evidence('UNKNOWN')
  }
  if (capital.inflowRatioPct >= INTERPRETATION_THRESHOLDS.fundsUpPct) {
    return evidence('UP')
  }
  if (capital.inflowRatioPct <= INTERPRETATION_THRESHOLDS.fundsDownPct) {
    return evidence('DOWN')
  }
  return evidence('NEUTRAL')
}

function depthEvidence(
  row: MonitorRowPresentation,
  realtime: RealtimeSymbolState | undefined,
): InterpretationEvidence {
  const pressure = row.volumeFunds.depthPressurePct
  if (realtime?.depthDelayed || !finite(pressure)) return evidence('UNKNOWN')
  if (pressure >= INTERPRETATION_THRESHOLDS.depthUpPct) return evidence('UP')
  if (pressure <= INTERPRETATION_THRESHOLDS.depthDownPct) return evidence('DOWN')
  return evidence('NEUTRAL')
}

function priceStructureEvidence({
  currentPrice,
  attemptRange60m,
  referenceDayHigh,
  referenceDayLow,
  vwap,
}: Pick<
  InterpretationMarketContext,
  | 'currentPrice'
  | 'attemptRange60m'
  | 'referenceDayHigh'
  | 'referenceDayLow'
  | 'vwap'
>): InterpretationEvidence {
  if (!finite(currentPrice)) return evidence('UNKNOWN')
  const upper = attemptRange60m?.high ?? referenceDayHigh
  const lower = attemptRange60m?.low ?? referenceDayLow
  if (finite(upper) && currentPrice > upper) return evidence('UP')
  if (finite(lower) && currentPrice < lower) return evidence('DOWN')
  if (finite(vwap)) {
    if (currentPrice > vwap) return evidence('UP')
    if (currentPrice < vwap) return evidence('DOWN')
    return evidence('NEUTRAL')
  }
  if (finite(upper) || finite(lower)) return evidence('NEUTRAL')
  return evidence('UNKNOWN')
}

export function deriveInterpretationMarketContext({
  item,
  row,
  realtime,
}: InterpretationMarketContextInput): InterpretationMarketContext {
  const fiveMinute = item.states['5m']
  const latestBarDate = timestampDate(fiveMinute?.chart.bars?.at(-1)?.timestamp)
  const date = timestampDate(realtime?.candlestick?.timestamp) ?? latestBarDate
  const sameDayBars = validBarsForDate(fiveMinute, date)
  const attemptDay = highLow(sameDayBars)
  const confirmationDay = highLow(sameDayBars.slice(0, -1))
  const priorConfirmationDay = highLow(sameDayBars.slice(0, -2))
  const realtimeDelayed = Boolean(
    realtime?.quoteDelayed
    || realtime?.candlestickDelayed
    || row.freshness.quote.delayed
    || row.freshness.candlestick.delayed,
  )
  const strategyDelayed = Boolean(
    row.delayed
    || row.freshness.analysis.delayed,
  )
  const stable5mAvailable = completedBars(item.states['5m']).length > 0
  const stable15mAvailable = completedBars(item.states['15m']).length > 0
  const stableTimeframesAvailable = stable5mAvailable && stable15mAvailable
  const capitalAvailable = Boolean(
    row.volumeFunds.capitalInflow.confirmed
    || item.intraday_capital?.quality === 'COMPLETE',
  )
  const warmupMissing = [
    ...(!stable5mAvailable ? ['5m周期'] : []),
    ...(!stable15mAvailable ? ['15m周期'] : []),
    ...(!capitalAvailable ? ['资金'] : []),
  ]
  const delayed = realtimeDelayed
  const realtimePrice = realtime?.quote?.lastDone
  const currentPrice = delayed
    ? null
    : finite(realtimePrice)
      ? realtimePrice
      : !strategyDelayed && finite(row.price) ? row.price : null
  const quote = realtime?.quote
  const liveDayHigh = !delayed && finite(quote?.high) ? quote.high : null
  const liveDayLow = !delayed && finite(quote?.low) ? quote.low : null
  const dailySummary = item.minute_decision?.daily_summary
  const vwap = (
    !delayed
    && item.minute_decision?.data_status === 'COMPLETE'
    && dailySummary?.data_status === 'COMPLETE'
    && finite(row.trendPosition.vwap.price)
  ) ? row.trendPosition.vwap.price : null
  const contextWithoutEvidence = {
    market: item.market,
    channel: row.trendPosition.channel,
    currentPrice,
    liveDayHigh,
    liveDayLow,
    referenceDayHigh: attemptDay?.high ?? null,
    referenceDayLow: attemptDay?.low ?? null,
    confirmationReferenceDayHigh: confirmationDay?.high ?? null,
    confirmationReferenceDayLow: confirmationDay?.low ?? null,
    priorConfirmationReferenceDayHigh: priorConfirmationDay?.high ?? null,
    priorConfirmationReferenceDayLow: priorConfirmationDay?.low ?? null,
    attemptRange60m: priceRange(sameDayBars.slice(-12), item.market),
    confirmationRange60m: priceRange(sameDayBars.slice(-13, -1), item.market),
    priorConfirmationRange60m: priceRange(sameDayBars.slice(-14, -2), item.market),
    latestCompleted5mClose: sameDayBars.at(-1)?.close ?? null,
    previousCompleted5mClose: sameDayBars.at(-2)?.close ?? null,
    vwap,
    controlLine: delayed ? null : stableControlLine(item),
    metrics: {
      changePct: row.changePct,
      intradayPositionPct: row.trendPosition.intradayPositionPct,
      momentum1m: row.momentumSpeed.momentum1m,
      momentum5m: row.momentumSpeed.momentum5m,
      momentum15m: row.momentumSpeed.momentum15m,
      relativeVolumeRatio: row.volumeFunds.relativeVolume?.ratio ?? null,
      volumeSpeed: row.volumeFunds.volumeSpeed,
      capitalInflowPct: row.volumeFunds.capitalInflow.confirmed
        ? row.volumeFunds.capitalInflow.inflowRatioPct
        : null,
      depthPressurePct: realtime?.depthDelayed
        ? null
        : row.volumeFunds.depthPressurePct,
      toDayHighPct: delayed ? null : row.breakoutRisk.toDayHighPct,
      fromDayLowPct: delayed ? null : row.breakoutRisk.fromDayLowPct,
      atr14Pct: row.breakoutRisk.atr14Pct,
    },
    stableTimeframesAvailable,
    capitalAvailable,
    warmupMissing,
    strategyDelayed,
    realtimeDelayed,
    delayed,
  }
  return {
    ...contextWithoutEvidence,
    evidence: {
      PRICE_STRUCTURE: priceStructureEvidence(contextWithoutEvidence),
      TREND_MOMENTUM: trendMomentumEvidence(row),
      VOLUME: volumeEvidence(row),
      FUNDS: fundsEvidence(row),
      DEPTH: depthEvidence(row, realtime),
    },
  }
}
