import type {
  ChartMarker,
  ChartPriceLine,
  OHLC,
} from '@/components/EChartsCandlestick'

const SUPPORT_BLUE = '#3B82F6'
const RESISTANCE_MAGENTA = '#D946EF'
const LONG_TERM_AMBER = '#F59E0B'
const BUY_GREEN = '#22C55E'
const SELL_RED = '#EF4444'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function isTimestamp(value: unknown): value is string {
  return typeof value === 'string'
    && value.length > 0
    && Number.isFinite(Date.parse(value))
}

export function toChartBars(entries: unknown): OHLC[] {
  if (!Array.isArray(entries)) return []
  return entries.flatMap(entry => {
    if (
      !isRecord(entry)
      || !isTimestamp(entry.timestamp)
      || !isFiniteNumber(entry.open)
      || !isFiniteNumber(entry.high)
      || !isFiniteNumber(entry.low)
      || !isFiniteNumber(entry.close)
      || !isFiniteNumber(entry.volume)
    ) {
      return []
    }
    const mapped: OHLC = {
      date: entry.timestamp,
      open: entry.open,
      high: entry.high,
      low: entry.low,
      close: entry.close,
      volume: entry.volume,
    }
    for (const key of [
      'ma5',
      'ma10',
      'ma20',
      'ma60',
      'macd_dif',
      'macd_dea',
      'macd_hist',
      'rsi_6',
      'rsi_14',
      'rsi_24',
      'kdj_k',
      'kdj_d',
      'kdj_j',
      'boll_upper',
      'boll_lower',
      'vol_ma5',
      'vol_ma10',
      'vol_ratio_5d',
    ] as const) {
      const value = entry[key]
      if (value == null || isFiniteNumber(value)) mapped[key] = value
    }
    return [mapped]
  })
}

export function toChartMarkers(signals: unknown): ChartMarker[] {
  if (!Array.isArray(signals)) return []
  return signals.flatMap(signal => {
    if (
      !isRecord(signal)
      || (signal.side !== 'BUY' && signal.side !== 'SELL' && signal.side !== 'RISK')
      || !isTimestamp(signal.barTime)
      || !isFiniteNumber(signal.price)
    ) {
      return []
    }
    const buy = signal.side === 'BUY'
    return [{
      date: signal.barTime,
      kind: buy ? 'buy' as const : 'sell' as const,
      above: !buy,
      price: signal.price,
      color: buy ? BUY_GREEN : SELL_RED,
      label: buy ? '买' : signal.side === 'RISK' ? '风险' : '卖',
    }]
  })
}

function mapEngineLine(line: unknown, barTimes: Set<string>): ChartPriceLine | null {
  if (
    !isRecord(line)
    || typeof line.id !== 'string'
    || line.id.length === 0
    || (line.side !== 'SUPPORT' && line.side !== 'RESISTANCE')
    || (line.role !== 'MAIN' && line.role !== 'ACCELERATION')
    || !Array.isArray(line.anchorTimes)
    || line.anchorTimes.length !== 2
    || !isTimestamp(line.anchorTimes[0])
    || !isTimestamp(line.anchorTimes[1])
    || !barTimes.has(line.anchorTimes[0])
    || !barTimes.has(line.anchorTimes[1])
    || !Array.isArray(line.anchorPrices)
    || line.anchorPrices.length !== 2
    || !isFiniteNumber(line.anchorPrices[0])
    || !isFiniteNumber(line.anchorPrices[1])
  ) {
    return null
  }
  const support = line.side === 'SUPPORT'
  const acceleration = line.role === 'ACCELERATION'
  return {
    id: line.id,
    start: line.anchorTimes[0],
    end: line.anchorTimes[1],
    value: line.anchorPrices[0],
    endValue: line.anchorPrices[1],
    label: `${acceleration ? '加速' : '主'}${support ? '支撑' : '阻力'}`,
    color: support ? SUPPORT_BLUE : RESISTANCE_MAGENTA,
    lineType: acceleration ? 'dashed' : 'solid',
    width: acceleration ? 1.5 : 2,
  }
}

function mapLongTerm(longTerm: unknown, barTimes: Set<string>): ChartPriceLine | null {
  if (
    !isRecord(longTerm)
    || !isTimestamp(longTerm.first_anchor_time)
    || !isTimestamp(longTerm.second_anchor_time)
    || !barTimes.has(longTerm.first_anchor_time)
    || !barTimes.has(longTerm.second_anchor_time)
    || !isFiniteNumber(longTerm.first_anchor_price)
    || !isFiniteNumber(longTerm.second_anchor_price)
  ) {
    return null
  }
  return {
    id: 'long-term',
    start: longTerm.first_anchor_time,
    end: longTerm.second_anchor_time,
    value: longTerm.first_anchor_price,
    endValue: longTerm.second_anchor_price,
    label: '长期趋势',
    color: LONG_TERM_AMBER,
    lineType: 'solid',
    width: 2,
  }
}

export function toPriceLines(
  lines: unknown,
  bars: unknown,
  longTerm?: unknown,
): ChartPriceLine[] {
  const validBars = toChartBars(bars)
  const barTimes = new Set(validBars.map(bar => bar.date))
  const mapped = Array.isArray(lines)
    ? lines.flatMap(line => {
      const result = mapEngineLine(line, barTimes)
      return result ? [result] : []
    })
    : []
  const longTermLine = mapLongTerm(longTerm, barTimes)
  if (longTermLine) mapped.push(longTermLine)
  return mapped
}
