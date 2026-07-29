import type {
  DowMonitorChart,
  DowMonitorTimeframeState,
  DowTimeframe,
} from '@/components/dow-monitor/types'
import type { MinuteKlineRow } from './api'
import type {
  RealtimeCandlestick,
  RealtimeDepth,
  RealtimeSymbolState,
} from './realtimeMarketData'

type QuoteTarget = {
  last_price: number | null
  change_pct: number | null
  quote_timestamp: number | string | null
}

export function overlayQuote<T extends QuoteTarget>(
  item: T,
  realtime?: RealtimeSymbolState,
): T {
  const quote = realtime?.quote
  if (!quote || typeof quote.lastDone !== 'number') return item
  const changePct = typeof quote.prevClose === 'number' && quote.prevClose !== 0
    ? (quote.lastDone - quote.prevClose) / quote.prevClose
    : item.change_pct
  return {
    ...item,
    last_price: quote.lastDone,
    change_pct: changePct,
    quote_timestamp: quote.timestamp ?? realtime.eventAt,
  }
}

export function bestBidAsk(
  depth?: RealtimeDepth,
): { bid: number | null; ask: number | null } {
  const bid = depth?.bids.find(level => Number.isFinite(level.price))?.price
  const ask = depth?.asks.find(level => Number.isFinite(level.price))?.price
  return {
    bid: typeof bid === 'number' ? bid : null,
    ask: typeof ask === 'number' ? ask : null,
  }
}

function minuteKey(timestamp: string): string {
  return timestamp.trim().replace(' ', 'T').slice(0, 16)
}

function candleRow(
  candle: RealtimeCandlestick,
  previous?: MinuteKlineRow,
): MinuteKlineRow | null {
  const open = candle.open ?? previous?.open
  const high = candle.high ?? previous?.high
  const low = candle.low ?? previous?.low
  const close = candle.close ?? previous?.close
  if (![open, high, low, close].every(value => typeof value === 'number')) return null
  return {
    datetime: candle.timestamp,
    open: open as number,
    high: high as number,
    low: low as number,
    close: close as number,
    volume: candle.volume ?? previous?.volume ?? 0,
    amount: candle.turnover ?? previous?.amount ?? 0,
  }
}

export function mergeMinuteRows(
  rows: MinuteKlineRow[],
  candle?: RealtimeCandlestick,
): MinuteKlineRow[] {
  if (!candle || candle.period !== 'min_1') return rows
  const matchingIndex = rows.findIndex(
    row => minuteKey(row.datetime) === minuteKey(candle.timestamp),
  )
  if (matchingIndex >= 0) {
    const replacement = candleRow(candle, rows[matchingIndex])
    if (!replacement) return rows
    const result = [...rows]
    result[matchingIndex] = replacement
    return result
  }
  const appended = candleRow(candle)
  if (!appended) return rows
  return [...rows, appended].sort(
    (left, right) => minuteKey(left.datetime).localeCompare(minuteKey(right.datetime)),
  )
}

export function overlayDowChart(
  chart: DowMonitorChart,
  candle: RealtimeCandlestick | undefined,
  timeframe: DowTimeframe,
): DowMonitorChart {
  const bars = chart.bars
  if (!candle || !bars?.length || timeframe === 'day') return chart
  const latest = bars[bars.length - 1]
  const candleTime = Date.parse(candle.timestamp)
  const latestTime = Date.parse(latest.timestamp)
  if (
    Number.isFinite(candleTime)
    && Number.isFinite(latestTime)
    && candleTime < latestTime
  ) return chart
  if (typeof candle.close !== 'number') return chart
  const nextLatest = {
    ...latest,
    high: typeof candle.high === 'number'
      ? Math.max(latest.high, candle.high)
      : latest.high,
    low: typeof candle.low === 'number'
      ? Math.min(latest.low, candle.low)
      : latest.low,
    close: candle.close,
  }
  if (
    nextLatest.high === latest.high
    && nextLatest.low === latest.low
    && nextLatest.close === latest.close
  ) return chart
  return {
    ...chart,
    bars: [...bars.slice(0, -1), nextLatest],
  }
}

export function overlayDowTimeframeState(
  state: DowMonitorTimeframeState | undefined,
  realtime: RealtimeSymbolState | undefined,
): DowMonitorTimeframeState | undefined {
  if (!state || !realtime?.candlestick) return state
  return {
    ...state,
    chart: overlayDowChart(state.chart, realtime.candlestick, state.timeframe),
  }
}
