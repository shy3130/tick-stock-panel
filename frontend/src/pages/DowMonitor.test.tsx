import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  DowMonitorOverviewResponse,
  DowMonitorOverviewSymbol,
} from '@/components/dow-monitor/types'
import type { RealtimeSymbolState } from '@/lib/realtimeMarketData'

import { DowMonitor } from './DowMonitor'

const hooks = vi.hoisted(() => ({
  add: vi.fn(),
  remove: vi.fn(),
  setEnabled: vi.fn(),
  overview: {} as Record<string, unknown>,
  notifications: {} as Record<string, unknown>,
  status: {} as Record<string, unknown>,
}))

const apiMocks = vi.hoisted(() => ({
  instrumentSearch: vi.fn(),
}))

const realtimeMocks = vi.hoisted(() => ({
  useRealtimeMarketData: vi.fn(),
  view: {
    status: 'realtime',
    states: new Map<string, RealtimeSymbolState>(),
  },
}))

vi.mock('@/lib/api', () => ({
  api: { instrumentSearch: apiMocks.instrumentSearch },
}))

vi.mock('@/lib/realtimeMarketData', () => ({
  useRealtimeMarketData: (...args: unknown[]) =>
    realtimeMocks.useRealtimeMarketData(...args),
}))

vi.mock('@/components/dow-monitor/useDowMonitor', () => ({
  useDowMonitorOverview: () => hooks.overview,
  useDowMonitorStatus: () => hooks.status,
  useDowNotifications: () => hooks.notifications,
  useAddDowMonitorSymbol: () => ({
    mutate: hooks.add,
    isPending: false,
    isError: false,
  }),
  useRemoveDowMonitorSymbol: () => ({
    mutateAsync: hooks.remove,
    isPending: false,
  }),
  useSetDowMonitorEnabled: () => ({
    mutateAsync: hooks.setEnabled,
    isPending: false,
  }),
}))

vi.mock('@/components/dow-monitor/DowMonitorDetailPanel', () => ({
  DowMonitorDetailPanel: ({ symbol }: { symbol: string }) => (
    <section role="region" aria-label={`${symbol} 详细走势`} data-testid="inline-detail">
      {symbol} 详情
    </section>
  ),
}))

function stock(
  symbol: string,
  market: 'cn' | 'hk' | 'us',
  index: number,
): DowMonitorOverviewSymbol {
  const notification = index % 3 === 0
    ? {
        notification_id: `n-${symbol}`,
        event_key: `e-${symbol}`,
        symbol,
        market,
        timeframe: '15m' as const,
        side: 'BUY' as const,
        action_name: '买入确认',
        shape_name: '双重突破',
        triggered_at: '2026-07-29T09:34:00+08:00',
        trigger_price: 100 + index,
        snapshot_payload: {},
        read_at: null,
      }
    : null
  return {
    symbol,
    market,
    enabled: true,
    created_at: '2026-07-29T09:00:00+08:00',
    updated_at: '2026-07-29T09:35:02+08:00',
    name: `股票${index}`,
    last_price: 100 + index,
    change_pct: 1,
    quote_timestamp: '2026-07-29T09:35:00+08:00',
    analysis_status: 'READY',
    intraday_capital: {
      total_in: 55,
      total_out: 45,
      quality: 'COMPLETE',
    },
    minute_decision: null,
    states: {
      '5m': {
        symbol,
        market,
        timeframe: '5m',
        freshness_state: 'LIVE',
        source_timestamp: '2026-07-29T09:35:00+08:00',
        snapshot: {
          bar_time: '2026-07-29T09:35:00+08:00',
          bar_completion: 'FINAL',
          volume_ratio_20: 1.2,
        },
        chart: {
          bars: [
            {
              index: 0,
              timestamp: '2026-07-29T09:30:00+08:00',
              open: 100,
              high: 101,
              low: 99,
              close: 100,
              volume: 100,
            },
            {
              index: 1,
              timestamp: '2026-07-29T09:35:00+08:00',
              open: 100,
              high: 102,
              low: 100,
              close: 101,
              volume: 120,
            },
          ],
        },
        updated_at: '2026-07-29T09:35:02+08:00',
      },
    },
    latest_notification: notification,
    last_success_at: '2026-07-29T09:35:02+08:00',
    last_error: null,
  }
}

const symbols: DowMonitorOverviewSymbol[] = [
  ...Array.from({ length: 45 }, (_, index) => stock(`${index + 1}.HK`, 'hk', index + 1)),
  stock('600000.SH', 'cn', 100),
  stock('AAPL.US', 'us', 101),
]

function overview(): DowMonitorOverviewResponse {
  return {
    symbols,
    source: 'dow-monitor',
    source_timestamp: '2026-07-29T09:35:00+08:00',
  }
}

describe('Dow monitor list page', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/dow-monitor?market=hk')
    hooks.add.mockReset()
    hooks.remove.mockReset()
    hooks.setEnabled.mockReset()
    hooks.remove.mockResolvedValue(undefined)
    hooks.setEnabled.mockResolvedValue(undefined)
    hooks.overview = {
      data: overview(),
      isLoading: false,
      isError: false,
    }
    hooks.notifications = {
      data: { notifications: symbols.flatMap(item => item.latest_notification ?? []) },
      isLoading: false,
      isError: false,
    }
    hooks.status = {
      data: {
        running: true,
        last_completed_at: '2026-07-29T09:35:02+08:00',
        last_success_at: '2026-07-29T09:35:02+08:00',
      },
      isLoading: false,
      isError: false,
    }
    realtimeMocks.view = {
      status: 'realtime',
      states: new Map(),
    }
    realtimeMocks.useRealtimeMarketData.mockReset()
    realtimeMocks.useRealtimeMarketData.mockImplementation(() => realtimeMocks.view)
    apiMocks.instrumentSearch.mockReset()
  })

  it('shows three exclusive markets, twenty rows, and subscribes only the current page', () => {
    render(<DowMonitor />)

    expect(screen.getByRole('button', { name: 'A股' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '港股' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: '美股' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: '全部' })).toHaveLength(1)
    expect(within(screen.getByRole('region', { name: '股票监控列表' })).getAllByRole('row'))
      .toHaveLength(21)
    expect(screen.getByText('第 1 / 3 页 · 共 45 只')).toBeInTheDocument()
    expect(realtimeMocks.useRealtimeMarketData).toHaveBeenLastCalledWith(
      Array.from({ length: 20 }, (_, index) => `${index + 1}.HK`),
      ['quote', 'depth', 'candlestick'],
      1,
    )
  })

  it('changes the WebSocket subscription with pagination', async () => {
    const user = userEvent.setup()
    render(<DowMonitor />)

    await user.click(screen.getByRole('button', { name: '下一页' }))

    expect(screen.getByText('第 2 / 3 页 · 共 45 只')).toBeInTheDocument()
    expect(realtimeMocks.useRealtimeMarketData).toHaveBeenLastCalledWith(
      Array.from({ length: 20 }, (_, index) => `${index + 21}.HK`),
      ['quote', 'depth', 'candlestick'],
      1,
    )
  })

  it('updates real-time price without changing the persisted signal', () => {
    const target = symbols[2]
    const { rerender } = render(<DowMonitor />)
    expect(screen.getAllByText('买入确认').length).toBeGreaterThan(0)

    realtimeMocks.view = {
      status: 'realtime',
      states: new Map([[
        target.symbol,
        {
          symbol: target.symbol,
          streamId: 'stream-1',
          sequence: 2,
          eventAt: '2026-07-29T09:35:30+08:00',
          publishedAt: '2026-07-29T09:35:30+08:00',
          quote: {
            lastDone: 188.88,
            prevClose: 180,
            timestamp: '2026-07-29T09:35:30+08:00',
          },
          quoteDelayed: false,
          depthDelayed: false,
          candlestickDelayed: false,
        },
      ]]),
    }
    rerender(<DowMonitor />)

    expect(screen.getByText('188.88')).toBeInTheDocument()
    expect(screen.getAllByText('买入确认').length).toBeGreaterThan(0)
  })

  it('opens the selected stock below the list without a dialog', async () => {
    const user = userEvent.setup()
    render(<DowMonitor />)

    await user.click(screen.getByRole('button', { name: '查看详情 2.HK' }))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: '2.HK 详细走势' })).toBeInTheDocument()
    expect(screen.getByTestId('inline-detail')).toBeInTheDocument()
  })

  it('resets pagination and URL scope when switching markets without mutating monitoring', async () => {
    const user = userEvent.setup()
    render(<DowMonitor />)
    await user.click(screen.getByRole('button', { name: '下一页' }))

    await user.click(screen.getByRole('button', { name: '美股' }))

    expect(window.location.search).toBe('?market=us')
    expect(screen.getByText('AAPL.US')).toBeInTheDocument()
    expect(screen.getByText('第 1 / 1 页 · 共 1 只')).toBeInTheDocument()
    expect(hooks.setEnabled).not.toHaveBeenCalled()
    expect(hooks.remove).not.toHaveBeenCalled()
  })

  it('filters by persisted signals within the selected market', async () => {
    const user = userEvent.setup()
    render(<DowMonitor />)

    await user.click(screen.getByRole('button', { name: '有信号' }))

    const bodyRows = within(screen.getByRole('region', { name: '股票监控列表' }))
      .getAllByRole('row')
    expect(bodyRows).toHaveLength(16)
    expect(screen.getByText('第 1 / 1 页 · 共 15 只')).toBeInTheDocument()
  })

  it('keeps symbol search and add behavior', async () => {
    const user = userEvent.setup()
    apiMocks.instrumentSearch.mockResolvedValue({
      results: [{ symbol: 'AAPL.US', name: '苹果', code: 'AAPL', market: 'us' }],
    })
    hooks.add.mockImplementation((_value, options) => options.onSuccess())
    render(<DowMonitor />)

    const input = screen.getByRole('textbox', { name: '股票代码' })
    await user.type(input, 'app')
    await act(async () => {
      await new Promise(resolve => window.setTimeout(resolve, 180))
    })
    await user.click(screen.getByRole('option', { name: /AAPL.US/ }))
    await user.click(screen.getByRole('button', { name: '添加' }))

    expect(hooks.add).toHaveBeenCalledWith(
      { symbol: 'AAPL.US', enabled: true },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    )
    await waitFor(() => expect(input).toHaveValue(''))
  })
})
