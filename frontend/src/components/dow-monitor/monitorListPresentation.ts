import type {
  RealtimeCandlestick,
  RealtimeSymbolState,
} from '@/lib/realtimeMarketData'

import type {
  DowMonitorBar,
  DowMonitorNotification,
  DowMonitorOverviewSymbol,
  DowMonitorTimeframeState,
  DowTimeframe,
} from './types'

export const DOW_MONITOR_PAGE_SIZE = 20

export interface MonitorMomentum {
  direction: 'UP' | 'DOWN' | 'FLAT' | 'UNKNOWN'
  valuePct: number | null
}

export interface MonitorChannel {
  code: 'UP' | 'DOWN' | 'RANGE' | 'PENDING' | 'UNKNOWN'
  label: string
}

export interface MonitorControl {
  timeframe: DowTimeframe
  role: string
  distancePct: number
}

export interface MonitorRelativeVolume {
  timeframe: DowTimeframe
  ratio: number
}

export interface MonitorActiveFunds {
  confirmed: boolean
  buyRatioPct: number | null
}

export interface MonitorSignal {
  level: 'CONFIRMED' | 'WARNING'
  side: 'BUY' | 'SELL' | 'RISK'
  label: string
  occurredAt: string | null
}

export interface MonitorRowPresentation {
  price: number | null
  changePct: number | null
  trendPosition: {
    channel: MonitorChannel
    control: MonitorControl | null
    costDistancePct: number | null
  }
  momentumSpeed: {
    momentum1m: MonitorMomentum
    momentum5m: MonitorMomentum
    momentum15m: MonitorMomentum
  }
  volumeFunds: {
    relativeVolume: MonitorRelativeVolume | null
    volumeSpeed: number | null
    activeFunds: MonitorActiveFunds
    depthPressurePct: number | null
  }
  breakoutRisk: {
    toDayHighPct: number | null
    fromDayLowPct: number | null
    atr14Pct: number | null
    confirmedTimeframes: number
    totalTimeframes: 2
    riskTitle: string | null
  }
  signal: MonitorSignal | null
  delayed: boolean
  sparkline: number[]
}

function finite(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function timestampMs(value: number | string | null | undefined): number | null {
  if (finite(value)) {
    return value < 10_000_000_000 ? value * 1000 : value
  }
  if (typeof value !== 'string' || !value.trim()) return null
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : null
}

function completedBars(state: DowMonitorTimeframeState | undefined): DowMonitorBar[] {
  const bars = state?.chart?.bars ?? []
  if (!state || bars.length === 0) return []
  if (state.snapshot.bar_completion !== 'FORMING' && !state.snapshot.provisional) return bars
  const formingTime = state.snapshot.bar_time
  if (formingTime) {
    const withoutForming = bars.filter(bar => bar.timestamp !== formingTime)
    if (withoutForming.length !== bars.length) return withoutForming
  }
  return bars.slice(0, -1)
}

function barChannel(
  state: DowMonitorTimeframeState | undefined,
): 'UP' | 'DOWN' | 'RANGE' | null {
  const bar = completedBars(state).at(-1)
  if (
    !bar
    || !finite(bar.ma5)
    || !finite(bar.ma10)
    || !finite(bar.ma20)
  ) return null
  if (bar.close > bar.ma5 && bar.ma5 > bar.ma10 && bar.ma10 > bar.ma20) return 'UP'
  if (bar.close < bar.ma5 && bar.ma5 < bar.ma10 && bar.ma10 < bar.ma20) return 'DOWN'
  return 'RANGE'
}

function channel(item: DowMonitorOverviewSymbol): MonitorChannel {
  const fifteen = barChannel(item.states['15m'])
  const thirty = barChannel(item.states['30m'])
  if (fifteen == null && thirty == null) return { code: 'UNKNOWN', label: '--' }
  if (fifteen == null || thirty == null) return { code: 'PENDING', label: '待确认' }
  if (fifteen === 'UP' && thirty === 'UP') return { code: 'UP', label: '上升通道' }
  if (fifteen === 'DOWN' && thirty === 'DOWN') return { code: 'DOWN', label: '下降通道' }
  return { code: 'RANGE', label: '震荡/过渡' }
}

const CONTROL_TIMEFRAMES: DowTimeframe[] = ['15m', '30m']

function roleLabel(value: string | null | undefined): string {
  const normalized = value?.trim().toUpperCase()
  if (normalized === 'SUPPORT' || normalized === 'LOW') return '支撑线'
  if (normalized === 'RESISTANCE' || normalized === 'HIGH') return '压力线'
  if (normalized === 'KEY_LEVEL') return '关键位'
  return value?.trim() || '控制线'
}

function control(
  item: DowMonitorOverviewSymbol,
): MonitorControl | null {
  for (const timeframe of CONTROL_TIMEFRAMES) {
    const snapshot = item.states[timeframe]?.snapshot
    if (!finite(snapshot?.price_to_line_pct)) continue
    return {
      timeframe,
      role: roleLabel(snapshot?.line_role),
      distancePct: snapshot.price_to_line_pct,
    }
  }
  return null
}

function momentum(
  state: DowMonitorTimeframeState | undefined,
): MonitorMomentum {
  const bars = completedBars(state)
  const previous = bars.at(-2)?.close
  const current = bars.at(-1)?.close
  if (!finite(previous) || !finite(current) || previous === 0) {
    return { direction: 'UNKNOWN', valuePct: null }
  }
  const valuePct = ((current - previous) / previous) * 100
  const direction = Math.abs(valuePct) < 0.005
    ? 'FLAT'
    : valuePct > 0 ? 'UP' : 'DOWN'
  return { direction, valuePct }
}

function relativeVolume(
  item: DowMonitorOverviewSymbol,
): MonitorRelativeVolume | null {
  for (const timeframe of CONTROL_TIMEFRAMES) {
    const ratio = item.states[timeframe]?.snapshot?.volume_ratio_20
    if (finite(ratio)) return { timeframe, ratio }
  }
  return null
}

function activeFunds(
  item: DowMonitorOverviewSymbol,
): MonitorActiveFunds {
  const capital = item.intraday_capital
  const totalIn = capital?.total_in
  const totalOut = capital?.total_out
  if (
    capital?.quality !== 'COMPLETE'
    || !finite(totalIn)
    || !finite(totalOut)
    || totalIn + totalOut <= 0
  ) {
    return { confirmed: false, buyRatioPct: null }
  }
  return {
    confirmed: true,
    buyRatioPct: totalIn / (totalIn + totalOut) * 100,
  }
}

function atr14Pct(state: DowMonitorTimeframeState | undefined): number | null {
  const bars = completedBars(state)
  if (bars.length < 15) return null
  const ranges = bars.slice(1).map((bar, index) => {
    const previousClose = bars[index].close
    if (
      !finite(bar.high)
      || !finite(bar.low)
      || !finite(previousClose)
    ) return null
    return Math.max(
      bar.high - bar.low,
      Math.abs(bar.high - previousClose),
      Math.abs(bar.low - previousClose),
    )
  })
  const recent = ranges.slice(-14)
  const latestClose = bars.at(-1)?.close
  if (
    recent.length !== 14
    || recent.some(range => range == null)
    || !finite(latestClose)
    || latestClose <= 0
  ) return null
  return (recent as number[]).reduce((sum, value) => sum + value, 0) / 14 / latestClose * 100
}

function confirmedTimeframes(item: DowMonitorOverviewSymbol): number {
  const decision = item.minute_decision
  if (!decision || decision.direction === 'RANGE') return 0
  const values = new Set([
    decision.dominant_timeframe,
    ...decision.confirmation_timeframes,
  ])
  return CONTROL_TIMEFRAMES.filter(timeframe => values.has(timeframe)).length
}

const DELAYED_DECISION_STATUSES = new Set([
  'DELAYED',
  'CAPITAL_DELAYED',
  'ANALYSIS_PAUSED',
])

function isDelayed(
  item: DowMonitorOverviewSymbol,
  realtime: RealtimeSymbolState | undefined,
  nowMs: number,
): boolean {
  if (
    item.analysis_status === 'QUOTE_DELAYED'
    || item.analysis_status === 'ANALYSIS_PAUSED'
    || DELAYED_DECISION_STATUSES.has(item.minute_decision?.data_status ?? '')
    || realtime?.quoteDelayed
    || realtime?.candlestickDelayed
  ) return true
  const latestTimestamp = timestampMs(
    realtime?.quote?.timestamp
    ?? realtime?.eventAt
    ?? item.quote_timestamp
    ?? item.minute_decision?.source_timestamp,
  )
  return latestTimestamp != null && nowMs - latestTimestamp > 90_000
}

function notificationTime(notification: DowMonitorNotification): number {
  return timestampMs(notification.available_at ?? notification.triggered_at) ?? 0
}

function formalSignal(
  item: DowMonitorOverviewSymbol,
  notifications: DowMonitorNotification[],
): MonitorSignal | null {
  const candidates = [
    ...(item.latest_notification ? [item.latest_notification] : []),
    ...notifications.filter(notification => notification.symbol === item.symbol),
  ]
  const latest = candidates.sort((left, right) =>
    notificationTime(right) - notificationTime(left))[0]
  if (!latest) return null
  const label = latest.side === 'BUY'
    ? '买入确认'
    : latest.side === 'SELL' ? '卖出确认' : '风险确认'
  return {
    level: 'CONFIRMED',
    side: latest.side,
    label,
    occurredAt: latest.available_at ?? latest.triggered_at,
  }
}

function warningSignal(item: DowMonitorOverviewSymbol): MonitorSignal | null {
  const signals = Object.values(item.states)
    .filter(state => (
      state
      && state.freshness_state === 'LIVE'
      && state.snapshot.bar_completion !== 'FORMING'
      && !state.snapshot.provisional
    ))
    .flatMap(state => state?.chart?.turning?.signals ?? [])
    .filter(signal => (
      signal.stage === 'WARNING'
      && (signal.side === 'BUY' || signal.side === 'SELL')
      && signal.signalQuality?.replayOutcome !== 'FAILED'
    ))
    .sort((left, right) => (
      (timestampMs(right.actionableTime ?? right.detectedTime) ?? 0)
      - (timestampMs(left.actionableTime ?? left.detectedTime) ?? 0)
    ))
  const latest = signals[0]
  if (!latest || (latest.side !== 'BUY' && latest.side !== 'SELL')) return null
  return {
    level: 'WARNING',
    side: latest.side,
    label: latest.side === 'BUY' ? '买入预警' : '卖出预警',
    occurredAt: latest.actionableTime ?? latest.detectedTime,
  }
}

function realtimePrice(
  item: DowMonitorOverviewSymbol,
  realtime: RealtimeSymbolState | undefined,
): Pick<MonitorRowPresentation, 'price' | 'changePct'> {
  const lastDone = realtime?.quote?.lastDone
  const price = finite(lastDone) ? lastDone : item.last_price
  const prevClose = realtime?.quote?.prevClose
  const fallbackChangePct = finite(item.change_pct) ? item.change_pct * 100 : null
  const changePct = finite(price) && finite(prevClose) && prevClose !== 0
    ? (price - prevClose) / prevClose * 100
    : fallbackChangePct
  return {
    price: finite(price) ? price : null,
    changePct: finite(changePct) ? changePct : null,
  }
}

export function buildIntradaySparkline(
  item: DowMonitorOverviewSymbol,
  realtimeCandle?: RealtimeCandlestick,
): number[] {
  const bars = completedBars(item.states['5m'])
    .filter(bar => finite(bar.close) && Boolean(bar.timestamp))
  const latestDate = [
    ...bars.map(bar => bar.timestamp.slice(0, 10)),
    ...(realtimeCandle?.timestamp ? [realtimeCandle.timestamp.slice(0, 10)] : []),
  ].sort().at(-1)
  if (!latestDate) return []
  const points = bars
    .filter(bar => bar.timestamp.slice(0, 10) === latestDate)
    .map(bar => ({ timestamp: bar.timestamp, close: bar.close }))
  if (
    realtimeCandle
    && finite(realtimeCandle.close)
    && realtimeCandle.timestamp.slice(0, 10) === latestDate
  ) {
    const existingIndex = points.findIndex(point => point.timestamp === realtimeCandle.timestamp)
    if (existingIndex >= 0) {
      points[existingIndex] = {
        timestamp: realtimeCandle.timestamp,
        close: realtimeCandle.close,
      }
    } else {
      points.push({
        timestamp: realtimeCandle.timestamp,
        close: realtimeCandle.close,
      })
    }
  }
  return points
    .sort((left, right) => left.timestamp.localeCompare(right.timestamp))
    .map(point => point.close)
}

export function deriveMonitorRow(
  item: DowMonitorOverviewSymbol,
  notifications: DowMonitorNotification[],
  realtime?: RealtimeSymbolState,
  nowMs = Date.now(),
): MonitorRowPresentation {
  const selectedControl = control(item)
  const delayed = isDelayed(item, realtime, nowMs)
  const formal = formalSignal(item, notifications)
  const costDistancePct = item.minute_decision?.daily_summary?.vwap_distance_pct
  return {
    ...realtimePrice(item, realtime),
    trendPosition: {
      channel: channel(item),
      control: selectedControl,
      costDistancePct: finite(costDistancePct) ? costDistancePct : null,
    },
    momentumSpeed: {
      momentum1m: { direction: 'UNKNOWN', valuePct: null },
      momentum5m: momentum(item.states['5m']),
      momentum15m: momentum(item.states['15m']),
    },
    volumeFunds: {
      relativeVolume: relativeVolume(item),
      volumeSpeed: null,
      activeFunds: activeFunds(item),
      depthPressurePct: null,
    },
    breakoutRisk: {
      toDayHighPct: null,
      fromDayLowPct: null,
      atr14Pct: atr14Pct(item.states['15m']),
      confirmedTimeframes: confirmedTimeframes(item),
      totalTimeframes: 2,
      riskTitle: item.minute_decision?.risk_warning?.title?.trim() || null,
    },
    signal: formal ?? (delayed ? null : warningSignal(item)),
    delayed,
    sparkline: buildIntradaySparkline(item, realtime?.candlestick),
  }
}

export function paginateMonitorSymbols<T>(
  items: T[],
  requestedPage: number,
): {
  items: T[]
  page: number
  pageCount: number
  total: number
} {
  const pageCount = Math.max(1, Math.ceil(items.length / DOW_MONITOR_PAGE_SIZE))
  const page = Math.min(Math.max(1, Math.trunc(requestedPage) || 1), pageCount)
  const start = (page - 1) * DOW_MONITOR_PAGE_SIZE
  return {
    items: items.slice(start, start + DOW_MONITOR_PAGE_SIZE),
    page,
    pageCount,
    total: items.length,
  }
}
