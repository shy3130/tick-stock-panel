import type {
  EvidenceDimension,
  EvidenceDirection,
  InterpretationMarketContext,
} from './interpretationMarketContext'
import { INTERPRETATION_THRESHOLDS } from './interpretationMarketContext'
import type { SuddenAnomalyMetric } from './suddenAnomalyHighlights'

export type InterpretationCategory =
  | 'OPPORTUNITY'
  | 'RISK'
  | 'ANOMALY'
  | 'OBSERVE'
  | 'DATA'

export type InterpretationPhase =
  | 'ATTEMPT'
  | 'CONFIRMED'
  | 'INVALIDATED'
  | 'NONE'

export type InterpretationScenarioId =
  | 'DATA_UNAVAILABLE'
  | 'LIVE_WARMUP'
  | 'BREAKOUT_INVALIDATED'
  | 'BREAKDOWN_CONFIRMED'
  | 'BREAKDOWN_INVALIDATED'
  | 'BREAKDOWN_ATTEMPT'
  | 'DOWNSIDE_ACCELERATION'
  | 'HIGH_PULLBACK'
  | 'HIGH_VOLUME_STALL'
  | 'BREAKOUT_CONFIRMED'
  | 'BREAKOUT_ATTEMPT'
  | 'RETEST_HOLD'
  | 'TREND_ACCELERATION'
  | 'ANOMALY_PENDING'
  | 'NO_CLEAR_OPPORTUNITY'

export type InterpretationLevelBasis =
  | 'RANGE_60M'
  | 'REFERENCE_DAY_HIGH'
  | 'REFERENCE_DAY_LOW'
  | 'LIVE_DAY_HIGH'
  | 'LIVE_DAY_LOW'
  | 'VWAP'
  | 'CONTROL_LINE'

export interface InterpretationLevel {
  label: string
  comparator?: '>' | '<'
  price: number
  basis: InterpretationLevelBasis
}

export interface KeyInterpretation {
  scenarioId: InterpretationScenarioId
  category: InterpretationCategory
  phase: InterpretationPhase
  headline: string
  explanation: string
  levels: InterpretationLevel[]
  dimensions: EvidenceDimension[]
  accessibleText: string
}

export interface KeyInterpretationInput {
  context: InterpretationMarketContext
  anomalies: ReadonlySet<SuddenAnomalyMetric>
}

interface Candidate extends Omit<KeyInterpretation, 'accessibleText'> {}

const DIMENSION_ORDER: EvidenceDimension[] = [
  'PRICE_STRUCTURE',
  'TREND_MOMENTUM',
  'VOLUME',
  'FUNDS',
  'DEPTH',
]

const CATEGORY_LABEL: Record<InterpretationCategory, string> = {
  OPPORTUNITY: '机会',
  RISK: '风险',
  ANOMALY: '异动',
  OBSERVE: '观察',
  DATA: '数据',
}

function finite(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

export function formatInterpretationPrice(value: number): string {
  return value.toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
    useGrouping: false,
  })
}

function evidenceIs(
  context: InterpretationMarketContext,
  dimension: EvidenceDimension,
  direction: EvidenceDirection,
): boolean {
  return context.evidence[dimension].direction === direction
}

function orderedDimensions(
  values: EvidenceDimension[],
): EvidenceDimension[] {
  const unique = new Set(values)
  return DIMENSION_ORDER.filter(value => unique.has(value))
}

function hasIndependentSupport(values: EvidenceDimension[]): boolean {
  return new Set(values).size >= 2
}

function upSupport(
  context: InterpretationMarketContext,
): EvidenceDimension | null {
  if (evidenceIs(context, 'TREND_MOMENTUM', 'UP')) return 'TREND_MOMENTUM'
  if (evidenceIs(context, 'FUNDS', 'UP')) return 'FUNDS'
  if (evidenceIs(context, 'DEPTH', 'UP')) return 'DEPTH'
  return null
}

function downSupport(
  context: InterpretationMarketContext,
): EvidenceDimension | null {
  if (evidenceIs(context, 'TREND_MOMENTUM', 'DOWN')) return 'TREND_MOMENTUM'
  if (evidenceIs(context, 'FUNDS', 'DOWN')) return 'FUNDS'
  if (evidenceIs(context, 'DEPTH', 'DOWN')) return 'DEPTH'
  return null
}

function enhancedVolume(context: InterpretationMarketContext): boolean {
  return evidenceIs(context, 'VOLUME', 'UP')
}

function candidate(
  value: Candidate,
): Candidate | null {
  if (
    (value.category === 'OPPORTUNITY' || value.category === 'RISK')
    && !hasIndependentSupport(value.dimensions)
  ) return null
  return {
    ...value,
    levels: value.levels.filter(level => finite(level.price)).slice(0, 3),
    dimensions: orderedDimensions(value.dimensions),
  }
}

function upperLevel(
  context: InterpretationMarketContext,
  kind: 'attempt' | 'confirmation' | 'prior',
): { price: number; basis: InterpretationLevelBasis } | null {
  const range = kind === 'attempt'
    ? context.attemptRange60m
    : kind === 'confirmation'
      ? context.confirmationRange60m
      : context.priorConfirmationRange60m
  if (finite(range?.high)) return { price: range.high, basis: 'RANGE_60M' }
  const price = kind === 'attempt'
    ? context.referenceDayHigh
    : kind === 'confirmation'
      ? context.confirmationReferenceDayHigh
      : context.priorConfirmationReferenceDayHigh
  return finite(price) ? { price, basis: 'REFERENCE_DAY_HIGH' } : null
}

function lowerLevel(
  context: InterpretationMarketContext,
  kind: 'attempt' | 'confirmation' | 'prior',
): { price: number; basis: InterpretationLevelBasis } | null {
  const range = kind === 'attempt'
    ? context.attemptRange60m
    : kind === 'confirmation'
      ? context.confirmationRange60m
      : context.priorConfirmationRange60m
  if (finite(range?.low)) return { price: range.low, basis: 'RANGE_60M' }
  const price = kind === 'attempt'
    ? context.referenceDayLow
    : kind === 'confirmation'
      ? context.confirmationReferenceDayLow
      : context.priorConfirmationReferenceDayLow
  return finite(price) ? { price, basis: 'REFERENCE_DAY_LOW' } : null
}

function liveHighLevel(
  context: InterpretationMarketContext,
): InterpretationLevel[] {
  return finite(context.liveDayHigh)
    ? [{
        label: '日高',
        price: context.liveDayHigh,
        basis: 'LIVE_DAY_HIGH',
      }]
    : []
}

function liveLowLevel(
  context: InterpretationMarketContext,
): InterpretationLevel[] {
  return finite(context.liveDayLow)
    ? [{
        label: '日低',
        price: context.liveDayLow,
        basis: 'LIVE_DAY_LOW',
      }]
    : []
}

function dataUnavailable({
  context,
}: KeyInterpretationInput): Candidate | null {
  if (!context.realtimeDelayed && !context.delayed && finite(context.currentPrice)) return null
  return {
    scenarioId: 'DATA_UNAVAILABLE',
    category: 'DATA',
    phase: 'NONE',
    headline: '关键数据延迟',
    explanation: '行情或K线时效不足，暂停实时机会判断',
    levels: [],
    dimensions: [],
  }
}

function signedPercent(value: number): string {
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`
}

function liveWarmup({
  context,
  anomalies,
}: KeyInterpretationInput): Candidate | null {
  if (!context.strategyDelayed || context.realtimeDelayed || !finite(context.currentPrice)) {
    return null
  }
  const momentum = context.metrics.momentum1m
  const momentumValue = finite(momentum.valuePct) ? momentum.valuePct : null
  const volumeSpeed = context.metrics.volumeSpeed
  const depthPressure = context.metrics.depthPressurePct
  const isAnomaly = anomalies.size > 0
    || (finite(volumeSpeed) && volumeSpeed >= INTERPRETATION_THRESHOLDS.volumeRatio)
    || (finite(depthPressure) && Math.abs(depthPressure) >= INTERPRETATION_THRESHOLDS.depthUpPct)
  const direction = momentum.direction === 'UP'
    ? '上行'
    : momentum.direction === 'DOWN' ? '下行' : '波动'
  const evidenceText = [
    finite(momentumValue) ? `1分钟 ${signedPercent(momentumValue)}` : null,
    finite(volumeSpeed) ? `量速 ${volumeSpeed.toFixed(2)}×` : null,
    finite(depthPressure) ? `五档 ${depthPressure >= 0 ? '+' : ''}${depthPressure.toFixed(1)}%` : null,
  ].filter((value): value is string => value != null)
  return {
    scenarioId: 'LIVE_WARMUP',
    category: isAnomaly ? 'ANOMALY' : 'OBSERVE',
    phase: isAnomaly ? 'ATTEMPT' : 'NONE',
    headline: isAnomaly
      ? `实时${finite(volumeSpeed) && volumeSpeed >= INTERPRETATION_THRESHOLDS.volumeRatio ? '放量' : ''}${direction}，周期待确认`
      : '实时行情正常，策略仍在预热',
    explanation: `${evidenceText.join('，') || `现价 ${formatInterpretationPrice(context.currentPrice)}`}；5m/15m与资金仍在预热，不生成正式买卖信号`,
    levels: [
      ...liveHighLevel(context),
      ...liveLowLevel(context),
    ].slice(0, 3),
    dimensions: orderedDimensions([
      ...(finite(momentumValue) ? ['TREND_MOMENTUM' as const] : []),
      ...(finite(volumeSpeed) ? ['VOLUME' as const] : []),
      ...(finite(depthPressure) ? ['DEPTH' as const] : []),
    ]),
  }
}

function breakoutInvalidated({
  context,
}: KeyInterpretationInput): Candidate | null {
  const prior = upperLevel(context, 'prior')
  const support = downSupport(context)
  if (
    !prior
    || !finite(context.previousCompleted5mClose)
    || !finite(context.latestCompleted5mClose)
    || context.previousCompleted5mClose <= prior.price
    || context.latestCompleted5mClose >= prior.price
    || !support
  ) return null
  return candidate({
    scenarioId: 'BREAKOUT_INVALIDATED',
    category: 'RISK',
    phase: 'INVALIDATED',
    headline: '突破条件已经失效',
    explanation: '5分钟收盘重新跌回突破位，上攻动能没有延续',
    levels: [
      { label: '收复 5m收', comparator: '>', ...prior },
      { label: '继续转弱 5m收', comparator: '<', ...prior },
      ...liveLowLevel(context),
    ],
    dimensions: ['PRICE_STRUCTURE', support],
  })
}

function breakdownConfirmed({
  context,
}: KeyInterpretationInput): Candidate | null {
  const lower = lowerLevel(context, 'confirmation')
  const sellSupport = downSupport(context)
  const extra = enhancedVolume(context) ? 'VOLUME' : sellSupport
  if (
    !lower
    || !finite(context.latestCompleted5mClose)
    || context.latestCompleted5mClose >= lower.price
    || !evidenceIs(context, 'TREND_MOMENTUM', 'DOWN')
    || !extra
  ) return null
  return candidate({
    scenarioId: 'BREAKDOWN_CONFIRMED',
    category: 'RISK',
    phase: 'CONFIRMED',
    headline: '向下破位已确认',
    explanation: '5分钟收盘跌破结构下沿，卖压与短周期方向一致',
    levels: [
      { label: '确认 5m收', comparator: '<', ...lower },
      { label: '解除 5m收', comparator: '>', ...lower },
      ...liveLowLevel(context),
    ],
    dimensions: ['PRICE_STRUCTURE', 'TREND_MOMENTUM', extra],
  })
}

function downsideAcceleration({
  context,
}: KeyInterpretationInput): Candidate | null {
  const nearThreshold = finite(context.metrics.atr14Pct)
    ? context.metrics.atr14Pct * INTERPRETATION_THRESHOLDS.nearAtrFraction
    : INTERPRETATION_THRESHOLDS.nearFallbackPct
  const nearLow = finite(context.metrics.fromDayLowPct)
    && context.metrics.fromDayLowPct <= nearThreshold
  const sellSupport = evidenceIs(context, 'FUNDS', 'DOWN')
    ? 'FUNDS'
    : evidenceIs(context, 'DEPTH', 'DOWN') ? 'DEPTH' : null
  const extra = enhancedVolume(context) ? 'VOLUME' : sellSupport
  if (
    !nearLow
    || !evidenceIs(context, 'TREND_MOMENTUM', 'DOWN')
    || !extra
  ) return null
  const lower = lowerLevel(context, 'attempt')
  return candidate({
    scenarioId: 'DOWNSIDE_ACCELERATION',
    category: 'RISK',
    phase: 'ATTEMPT',
    headline: '下跌正在加速',
    explanation: '价格逼近日低，主动卖盘与放量方向一致',
    levels: [
      ...(lower ? [
        { label: '确认', comparator: '<' as const, ...lower },
        { label: '解除', comparator: '>' as const, ...lower },
      ] : []),
      ...liveLowLevel(context),
    ],
    dimensions: ['PRICE_STRUCTURE', 'TREND_MOMENTUM', extra],
  })
}

function highPullback({
  context,
}: KeyInterpretationInput): Candidate | null {
  const pullbackThreshold = finite(context.metrics.atr14Pct)
    ? context.metrics.atr14Pct * 0.5
    : 1
  const movedFromHigh = finite(context.metrics.toDayHighPct)
    && context.metrics.toDayHighPct >= pullbackThreshold
  const belowVwap = finite(context.vwap)
    && finite(context.currentPrice)
    && context.currentPrice < context.vwap
  const shortWeak = context.metrics.momentum1m.direction === 'DOWN'
    || context.metrics.momentum5m.direction === 'DOWN'
  const support = downSupport(context)
  if (!movedFromHigh || !belowVwap || (!shortWeak && !support)) return null
  const dimension = support ?? 'TREND_MOMENTUM'
  return candidate({
    scenarioId: 'HIGH_PULLBACK',
    category: 'RISK',
    phase: 'ATTEMPT',
    headline: '冲高回落，卖压增强',
    explanation: '价格离开日高并跌回VWAP下方，买盘承接开始减弱',
    levels: [
      {
        label: '解除',
        comparator: '>',
        price: context.vwap!,
        basis: 'VWAP',
      },
      {
        label: '确认',
        comparator: '<',
        price: context.vwap!,
        basis: 'VWAP',
      },
      ...liveHighLevel(context),
    ],
    dimensions: ['PRICE_STRUCTURE', dimension],
  })
}

function highVolumeStall({
  context,
}: KeyInterpretationInput): Candidate | null {
  const highPosition = finite(context.metrics.intradayPositionPct)
    && context.metrics.intradayPositionPct >= 80
  const momentumNotUp = context.metrics.momentum1m.direction !== 'UP'
    && context.metrics.momentum5m.direction !== 'UP'
  const buySupport = evidenceIs(context, 'FUNDS', 'UP')
    || evidenceIs(context, 'DEPTH', 'UP')
  if (!highPosition || !enhancedVolume(context) || !momentumNotUp || buySupport) return null
  const upper = upperLevel(context, 'attempt')
  const lower = finite(context.vwap)
    ? { price: context.vwap, basis: 'VWAP' as const }
    : lowerLevel(context, 'attempt')
  return candidate({
    scenarioId: 'HIGH_VOLUME_STALL',
    category: 'RISK',
    phase: 'ATTEMPT',
    headline: '放量滞涨，效率下降',
    explanation: '成交放大但价格没有继续上行，上攻资金转化不足',
    levels: [
      ...(upper ? [{ label: '恢复', comparator: '>' as const, ...upper }] : []),
      ...(lower ? [{ label: '转弱', comparator: '<' as const, ...lower }] : []),
      ...liveHighLevel(context),
    ],
    dimensions: ['PRICE_STRUCTURE', 'VOLUME'],
  })
}

function breakoutConfirmed({
  context,
}: KeyInterpretationInput): Candidate | null {
  const upper = upperLevel(context, 'confirmation')
  const support = upSupport(context)
  if (
    !upper
    || !finite(context.latestCompleted5mClose)
    || context.latestCompleted5mClose <= upper.price
    || !enhancedVolume(context)
    || !support
  ) return null
  return candidate({
    scenarioId: 'BREAKOUT_CONFIRMED',
    category: 'OPPORTUNITY',
    phase: 'CONFIRMED',
    headline: '放量突破已确认',
    explanation: '5分钟收盘站稳结构上沿，量能与主动承接继续配合',
    levels: [
      { label: '维持 5m收', comparator: '>', ...upper },
      { label: '失效 5m收', comparator: '<', ...upper },
      ...liveHighLevel(context),
    ],
    dimensions: ['PRICE_STRUCTURE', 'VOLUME', support],
  })
}

function nearestSupport(
  context: InterpretationMarketContext,
): { price: number; basis: InterpretationLevelBasis } | null {
  if (!finite(context.currentPrice)) return null
  const candidates = [
    finite(context.vwap)
      ? { price: context.vwap, basis: 'VWAP' as const }
      : null,
    context.controlLine?.role.toUpperCase().includes('SUPPORT')
      ? { price: context.controlLine.price, basis: 'CONTROL_LINE' as const }
      : null,
    finite(context.attemptRange60m?.high)
      ? { price: context.attemptRange60m.high, basis: 'RANGE_60M' as const }
      : null,
  ].filter(value => value != null)
  return candidates.sort((left, right) => (
    Math.abs(context.currentPrice! - left.price)
    - Math.abs(context.currentPrice! - right.price)
  ))[0] ?? null
}

function retestHold({
  context,
}: KeyInterpretationInput): Candidate | null {
  if (context.channel.code !== 'UP' || !finite(context.currentPrice)) return null
  const supportLevel = nearestSupport(context)
  if (!supportLevel) return null
  const distancePct = Math.abs(context.currentPrice - supportLevel.price)
    / context.currentPrice * 100
  const nearThreshold = finite(context.metrics.atr14Pct)
    ? context.metrics.atr14Pct * INTERPRETATION_THRESHOLDS.nearAtrFraction
    : INTERPRETATION_THRESHOLDS.nearFallbackPct
  const renewed = context.metrics.momentum1m.direction === 'UP'
    ? 'TREND_MOMENTUM'
    : upSupport(context)
  if (
    distancePct > nearThreshold
    || evidenceIs(context, 'TREND_MOMENTUM', 'DOWN')
    || !renewed
  ) return null
  const upper = upperLevel(context, 'attempt')
  return candidate({
    scenarioId: 'RETEST_HOLD',
    category: 'OPPORTUNITY',
    phase: 'ATTEMPT',
    headline: '回踩承接正在形成',
    explanation: '上升结构未破，回到关键位后卖压没有继续扩大',
    levels: [
      { label: '确认', comparator: '>', ...supportLevel },
      { label: '失效 5m收', comparator: '<', ...supportLevel },
      ...(upper ? [{ label: '压力', ...upper }] : []),
    ],
    dimensions: ['PRICE_STRUCTURE', renewed],
  })
}

function trendAcceleration({
  context,
}: KeyInterpretationInput): Candidate | null {
  const extra = enhancedVolume(context)
    ? 'VOLUME'
    : upSupport(context)
  if (
    context.channel.code !== 'UP'
    || !finite(context.currentPrice)
    || !finite(context.vwap)
    || context.currentPrice <= context.vwap
    || !evidenceIs(context, 'TREND_MOMENTUM', 'UP')
    || !extra
  ) return null
  const upper = upperLevel(context, 'attempt')
  return candidate({
    scenarioId: 'TREND_ACCELERATION',
    category: 'OPPORTUNITY',
    phase: 'ATTEMPT',
    headline: '趋势正在加速',
    explanation: '价格维持VWAP上方，短周期方向与增量资金形成合力',
    levels: [
      {
        label: '维持',
        comparator: '>',
        price: context.vwap,
        basis: 'VWAP',
      },
      {
        label: '失效',
        comparator: '<',
        price: context.vwap,
        basis: 'VWAP',
      },
      ...(upper ? [{ label: '压力', ...upper }] : []),
    ],
    dimensions: ['PRICE_STRUCTURE', 'TREND_MOMENTUM', extra],
  })
}

function breakdownAttempt({
  context,
}: KeyInterpretationInput): Candidate | null {
  const lower = lowerLevel(context, 'attempt')
  const sellSupport = downSupport(context)
  const extra = enhancedVolume(context) ? 'VOLUME' : sellSupport
  if (
    !lower
    || !finite(context.currentPrice)
    || context.currentPrice >= lower.price
    || !evidenceIs(context, 'TREND_MOMENTUM', 'DOWN')
    || !extra
  ) return null
  return candidate({
    scenarioId: 'BREAKDOWN_ATTEMPT',
    category: 'RISK',
    phase: 'ATTEMPT',
    headline: '向下破位正在形成',
    explanation: '实时价格跌破结构下沿，卖压正在等待5分钟确认',
    levels: [
      { label: '确认 5m收', comparator: '<', ...lower },
      { label: '解除 5m收', comparator: '>', ...lower },
      ...liveLowLevel(context),
    ],
    dimensions: ['PRICE_STRUCTURE', 'TREND_MOMENTUM', extra],
  })
}

function breakoutAttempt({
  context,
}: KeyInterpretationInput): Candidate | null {
  const upper = upperLevel(context, 'attempt')
  const support = upSupport(context)
  if (
    !upper
    || !finite(context.currentPrice)
    || context.currentPrice <= upper.price
    || !enhancedVolume(context)
    || !support
  ) return null
  return candidate({
    scenarioId: 'BREAKOUT_ATTEMPT',
    category: 'OPPORTUNITY',
    phase: 'ATTEMPT',
    headline: '放量突破正在形成',
    explanation: '买盘主动抬价，量能与短周期方向形成共振',
    levels: [
      { label: '确认 5m收', comparator: '>', ...upper },
      { label: '失效 5m收', comparator: '<', ...upper },
      ...liveHighLevel(context),
    ],
    dimensions: ['PRICE_STRUCTURE', 'VOLUME', support],
  })
}

function breakdownInvalidated({
  context,
}: KeyInterpretationInput): Candidate | null {
  const prior = lowerLevel(context, 'prior')
  if (
    !prior
    || !finite(context.previousCompleted5mClose)
    || !finite(context.latestCompleted5mClose)
    || context.previousCompleted5mClose >= prior.price
    || context.latestCompleted5mClose <= prior.price
  ) return null
  const support = upSupport(context)
  return {
    scenarioId: 'BREAKDOWN_INVALIDATED',
    category: 'OBSERVE',
    phase: 'INVALIDATED',
    headline: '下破风险暂时解除',
    explanation: '5分钟收盘重新站回结构下沿，卖压延续性减弱',
    levels: [
      { label: '维持 5m收', comparator: '>', ...prior },
      { label: '再度转弱', comparator: '<', ...prior },
    ],
    dimensions: orderedDimensions([
      'PRICE_STRUCTURE',
      ...(support ? [support] : []),
    ]),
  }
}

function anomalyPending({
  context,
  anomalies,
}: KeyInterpretationInput): Candidate | null {
  if (anomalies.size === 0) return null
  const depth = anomalies.has('depthPressurePct')
  const volume = anomalies.has('volumeSpeed')
  const explanation = depth
    ? '挂单快速变化，但成交价格和主动资金尚未同步'
    : volume
      ? '量能突然放大，但价格结构尚未完成确认'
      : '实时指标快速变化，但尚未获得第二维度确认'
  const upper = upperLevel(context, 'attempt')
  const lower = lowerLevel(context, 'attempt')
  return {
    scenarioId: 'ANOMALY_PENDING',
    category: 'ANOMALY',
    phase: 'ATTEMPT',
    headline: depth ? '盘口突变，价格待确认' : '实时异动，结构待确认',
    explanation,
    levels: [
      ...(upper ? [{ label: '站上确认', comparator: '>' as const, ...upper }] : []),
      ...(lower ? [{ label: '跌破转弱', comparator: '<' as const, ...lower }] : []),
    ].slice(0, 3),
    dimensions: orderedDimensions(
      DIMENSION_ORDER.filter(dimension => context.evidence[dimension].available),
    ),
  }
}

function noClearOpportunity({
  context,
}: KeyInterpretationInput): Candidate {
  const upper = upperLevel(context, 'attempt')
  const lower = lowerLevel(context, 'attempt')
  return {
    scenarioId: 'NO_CLEAR_OPPORTUNITY',
    category: 'OBSERVE',
    phase: 'NONE',
    headline: '暂无清晰机会',
    explanation: '周期方向冲突，量能和资金没有形成合力',
    levels: [
      ...(upper ? [{ label: '等待上破', comparator: '>' as const, ...upper }] : []),
      ...(lower ? [{ label: '或下破', comparator: '<' as const, ...lower }] : []),
    ],
    dimensions: [],
  }
}

function finalize(value: Candidate): KeyInterpretation {
  const category = CATEGORY_LABEL[value.category]
  const levelText = value.levels.length > 0
    ? value.levels.map(level => (
        `${level.label}${level.comparator ?? ''}${formatInterpretationPrice(level.price)}`
      )).join('，')
    : '关键价待确认'
  return {
    ...value,
    accessibleText: `${category}，${value.headline}。${value.explanation}。${levelText}`,
  }
}

export function deriveKeyInterpretation(
  input: KeyInterpretationInput,
): KeyInterpretation {
  const candidates: Array<Candidate | null> = [
    dataUnavailable(input),
    liveWarmup(input),
    breakoutInvalidated(input),
    breakdownConfirmed(input),
    downsideAcceleration(input),
    highPullback(input),
    highVolumeStall(input),
    breakoutConfirmed(input),
    retestHold(input),
    trendAcceleration(input),
    breakdownAttempt(input),
    breakoutAttempt(input),
    breakdownInvalidated(input),
    anomalyPending(input),
  ]
  return finalize(candidates.find(value => value != null) ?? noClearOpportunity(input))
}
