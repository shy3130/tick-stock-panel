import { describe, expect, it } from 'vitest'

import type { DowMonitorChart } from '@/components/dow-monitor/types'
import type { MinuteKlineRow } from './api'
import {
  bestBidAsk,
  mergeMinuteRows,
  overlayDowChart,
  overlayQuote,
} from './realtimeOverlays'
import type { RealtimeSymbolState } from './realtimeMarketData'

const realtime: RealtimeSymbolState = {
  symbol: '700.HK',
  streamId: 'stream-1',
  sequence: 3,
  eventAt: '2026-07-24T10:00:00+08:00',
  publishedAt: '2026-07-24T10:00:00.100+08:00',
  quote: {
    lastDone: 551,
    prevClose: 548,
    timestamp: '2026-07-24T10:00:00+08:00',
  },
  depth: {
    bids: [{ position: 1, price: 550.8, volume: 1000, orderCount: 3 }],
    asks: [{ position: 1, price: 551, volume: 800, orderCount: 2 }],
  },
  candlestick: {
    period: 'min_1',
    timestamp: '2026-07-24T10:00:00+08:00',
    open: 550,
    high: 551.2,
    low: 549.8,
    close: 551,
    volume: 2200,
    turnover: 1_210_000,
  },
  quoteDelayed: false,
  depthDelayed: false,
  candlestickDelayed: false,
}

describe('realtime view overlays', () => {
  it('overlays quote and best bid/ask without mutating the HTTP object', () => {
    const httpItem = {
      symbol: '700.HK',
      last_price: 550,
      change_pct: 0.001,
      quote_timestamp: 'old',
      keep: 'history',
    }

    const result = overlayQuote(httpItem, realtime)

    expect(result.last_price).toBe(551)
    expect(result.change_pct).toBeCloseTo((551 - 548) / 548)
    expect(result.quote_timestamp).toBe(realtime.quote?.timestamp)
    expect(result.keep).toBe('history')
    expect(httpItem.last_price).toBe(550)
    expect(bestBidAsk(realtime.depth)).toEqual({ bid: 550.8, ask: 551 })
  })

  it('replaces only the matching current minute and can append a new minute', () => {
    const rows: MinuteKlineRow[] = [{
      datetime: '2026-07-24T10:00:00',
      open: 549,
      high: 550,
      low: 548,
      close: 550,
      volume: 100,
      amount: 55_000,
    }]

    const merged = mergeMinuteRows(rows, realtime.candlestick)
    expect(merged).toHaveLength(1)
    expect(merged.at(-1)?.close).toBe(551)
    expect(merged.at(-1)?.amount).toBe(1_210_000)
    expect(rows.at(-1)?.close).toBe(550)

    const appended = mergeMinuteRows(merged, {
      ...realtime.candlestick!,
      timestamp: '2026-07-24T10:01:00+08:00',
      close: 552,
    })
    expect(appended).toHaveLength(2)
    expect(appended.at(-1)?.close).toBe(552)
  })

  it('updates only the latest Dow bar and preserves analytical sidecars', () => {
    const chart: DowMonitorChart = {
      bars: [
        {
          index: 0,
          timestamp: '2026-07-24T09:55:00+08:00',
          open: 548,
          high: 550,
          low: 547,
          close: 549,
          volume: 10_000,
        },
        {
          index: 1,
          timestamp: '2026-07-24T10:00:00+08:00',
          open: 550,
          high: 550.5,
          low: 549.9,
          close: 550,
          volume: 1000,
        },
      ],
      lines: [{ id: 'preserved' } as any],
      signals: [{ side: 'BUY' } as any],
      longTerm: { operation: '观察' },
    }

    const result = overlayDowChart(chart, realtime.candlestick, '5m')

    expect(result.bars?.[0]).toEqual(chart.bars?.[0])
    expect(result.bars?.at(-1)).toEqual(expect.objectContaining({
      open: 550,
      high: 551.2,
      low: 549.8,
      close: 551,
    }))
    expect(result.lines).toBe(chart.lines)
    expect(result.signals).toBe(chart.signals)
    expect(result.longTerm).toBe(chart.longTerm)
  })

  it('keeps the Dow chart reference when only quote or depth changes', () => {
    const chart: DowMonitorChart = {
      bars: [{
        index: 0,
        timestamp: '2026-07-24T10:00:00+08:00',
        open: 550,
        high: 551.2,
        low: 549.8,
        close: 551,
        volume: 1000,
      }],
      lines: [{ id: 'preserved' } as any],
      signals: [{ side: 'BUY' } as any],
    }

    const result = overlayDowChart(chart, realtime.candlestick, '5m')

    expect(result).toBe(chart)
    expect(result.bars).toBe(chart.bars)
  })
})
