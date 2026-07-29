import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { DowMonitorDetailResponse, DowTimeframe } from './types'
import { DowMonitorDetailPanel } from './DowMonitorDetailPanel'

const detailCalls = vi.hoisted(() => [] as Array<[string, DowTimeframe]>)

const bars = [
  {
    index: 0,
    timestamp: '2026-07-29T09:30:00+08:00',
    open: 10,
    high: 10.2,
    low: 9.9,
    close: 10.1,
    volume: 100,
  },
  {
    index: 1,
    timestamp: '2026-07-29T09:35:00+08:00',
    open: 10.1,
    high: 10.4,
    low: 10,
    close: 10.3,
    volume: 120,
  },
]

vi.mock('./useDowMonitor', () => ({
  useDowMonitorDetail: (symbol: string, timeframe: DowTimeframe) => {
    detailCalls.push([symbol, timeframe])
    return {
      isLoading: false,
      isError: false,
      data: {
        symbol,
        market: 'hk',
        timeframe,
        freshness_state: 'LIVE',
        source_timestamp: '2026-07-29T09:35:00+08:00',
        snapshot: {
          action: '观察',
          phase: '上升通道',
          bar_completion: 'FINAL',
        },
        chart: { bars, lines: [], signals: [] },
        updated_at: '2026-07-29T09:35:02+08:00',
        last_success_at: '2026-07-29T09:35:02+08:00',
        last_error: null,
      } satisfies DowMonitorDetailResponse,
    }
  },
}))

vi.mock('@/components/EChartsCandlestick', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/components/EChartsCandlestick')>()
  return {
    ...actual,
    EChartsCandlestick: () => <div data-testid="inline-intraday-chart" />,
  }
})

vi.mock('@/components/StockDailyKChart', () => ({
  StockDailyKChart: () => <div data-testid="inline-daily-chart" />,
}))

describe('DowMonitorDetailPanel', () => {
  it('renders as an inline region and keeps timeframe and overlay controls', async () => {
    const user = userEvent.setup()
    render(<DowMonitorDetailPanel symbol="700.HK" initialTimeframe="15m" />)

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: '700.HK 详细走势' })).toBeInTheDocument()
    expect(screen.getByTestId('inline-intraday-chart')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '15分钟' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('switch', { name: '显示趋势线和压力线' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '5分钟' }))
    expect(detailCalls.at(-1)).toEqual(['700.HK', '5m'])
  })
})
