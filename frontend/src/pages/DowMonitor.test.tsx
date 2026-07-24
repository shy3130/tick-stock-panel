import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  buildDowMiniChartOption,
  DowMiniChart,
} from '@/components/dow-monitor/DowMiniChart'
import type {
  DowMonitorChart,
  DowMonitorNotification,
  DowMonitorOverviewResponse,
  DowMonitorOverviewSymbol,
  DowMonitorTimeframeState,
  DowTimeframe,
} from '@/components/dow-monitor/types'
import type { RealtimeSymbolState } from '@/lib/realtimeMarketData'

import { DowMonitor } from './DowMonitor'

const hooks = vi.hoisted(() => ({
  add: vi.fn(),
  markRead: vi.fn(),
  remove: vi.fn(),
  setEnabled: vi.fn(),
  overview: {} as Record<string, unknown>,
  notifications: {} as Record<string, unknown>,
  status: {} as Record<string, unknown>,
  addState: {} as Record<string, unknown>,
  readState: {} as Record<string, unknown>,
  removeState: {} as Record<string, unknown>,
  toggleState: {} as Record<string, unknown>,
}))

const apiMocks = vi.hoisted(() => ({
  instrumentSearch: vi.fn(),
}))

const realtimeMocks = vi.hoisted(() => ({
  useRealtimeMarketData: vi.fn(),
  view: {
    status: 'fallback',
    states: new Map(),
  } as {
    status: 'connecting' | 'realtime' | 'fallback' | 'disconnected'
    states: Map<string, RealtimeSymbolState>
  },
}))

vi.mock('@/lib/api', () => ({
  api: {
    instrumentSearch: apiMocks.instrumentSearch,
  },
}))

vi.mock('@/lib/realtimeMarketData', () => ({
  useRealtimeMarketData: (...args: unknown[]) =>
    realtimeMocks.useRealtimeMarketData(...args),
}))

const chartMocks = vi.hoisted(() => ({
  disconnect: vi.fn(),
  dispose: vi.fn(),
  init: vi.fn(),
  observe: vi.fn(),
  resize: vi.fn(),
  setOption: vi.fn(),
  resizeCallback: undefined as ResizeObserverCallback | undefined,
}))

vi.mock('@/components/dow-monitor/useDowMonitor', () => ({
  useDowMonitorOverview: () => hooks.overview,
  useDowMonitorStatus: () => hooks.status,
  useDowNotifications: () => hooks.notifications,
  useAddDowMonitorSymbol: () => ({ mutate: hooks.add, ...hooks.addState }),
  useMarkDowNotificationRead: () => ({
    mutate: hooks.markRead,
    mutateAsync: hooks.markRead,
    ...hooks.readState,
  }),
  useRemoveDowMonitorSymbol: () => ({
    mutate: hooks.remove,
    mutateAsync: hooks.remove,
    ...hooks.removeState,
  }),
  useSetDowMonitorEnabled: () => ({
    mutate: hooks.setEnabled,
    mutateAsync: hooks.setEnabled,
    ...hooks.toggleState,
  }),
}))

vi.mock('echarts', () => ({
  init: chartMocks.init,
}))

const TIMEFRAMES: DowTimeframe[] = ['5m', '15m', '30m', '60m', 'day']

const bars = [
  {
    index: 0,
    timestamp: '2026-07-23T01:00:00Z',
    open: 10,
    high: 10.6,
    low: 9.8,
    close: 10.4,
    volume: 100,
  },
  {
    index: 1,
    timestamp: '2026-07-23T01:05:00Z',
    open: 10.4,
    high: 11.2,
    low: 10.2,
    close: 11,
    volume: 120,
  },
]

const authoritativeChart: DowMonitorChart = {
  bars,
  lines: [
    {
      id: 'main-support',
      side: 'SUPPORT',
      role: 'MAIN',
      generation: 1,
      anchorIndexes: [0, 1],
      anchorTimes: [bars[0].timestamp, bars[1].timestamp],
      anchorPrices: [9.8, 10.2],
      createdIndex: 1,
      invalidatedIndex: null,
      controlsSignals: true,
    },
    {
      id: 'acceleration-resistance',
      side: 'RESISTANCE',
      role: 'ACCELERATION',
      generation: 1,
      anchorIndexes: [0, 1],
      anchorTimes: [bars[0].timestamp, bars[1].timestamp],
      anchorPrices: [10.6, 11.2],
      createdIndex: 1,
      invalidatedIndex: null,
      controlsSignals: false,
    },
  ],
  signals: [
    {
      side: 'BUY',
      barIndex: 0,
      barTime: bars[0].timestamp,
      price: 10.4,
      reason: 'backend buy',
      confidence: 'HIGH',
      lineId: 'main-support',
      firstCrossIndex: null,
      firstCrossTime: null,
      volumeRatio: null,
      pattern: '突破',
      evidence: [],
    },
    {
      side: 'RISK',
      barIndex: 1,
      barTime: bars[1].timestamp,
      price: 11,
      reason: 'backend risk',
      confidence: 'HIGH',
      lineId: 'acceleration-resistance',
      firstCrossIndex: null,
      firstCrossTime: null,
      volumeRatio: null,
      pattern: '风险退出',
      evidence: [],
    },
  ],
  longTerm: {
    first_anchor_time: bars[0].timestamp,
    first_anchor_price: 9.6,
    second_anchor_time: bars[1].timestamp,
    second_anchor_price: 10,
  },
}

function state(
  symbol: string,
  market: 'cn' | 'hk' | 'us',
  timeframe: DowTimeframe,
  actionCode?: string,
  freshness: DowMonitorTimeframeState['freshness_state'] = 'LIVE',
  chart: DowMonitorChart = { bars, lines: [], signals: [] },
): DowMonitorTimeframeState {
  return {
    symbol,
    market,
    timeframe,
    freshness_state: freshness,
    source_timestamp: '2026-07-23T01:05:00Z',
    snapshot: actionCode ? { action_code: actionCode } : {},
    chart,
    updated_at: '2026-07-23T01:05:01Z',
  }
}

function notification(
  symbol: string,
  market: 'cn' | 'hk' | 'us',
  side: 'BUY' | 'SELL' | 'RISK',
  action: string,
): DowMonitorNotification {
  return {
    notification_id: `${symbol}-${side}`,
    event_key: `${symbol}-${side}-1`,
    symbol,
    market,
    timeframe: '5m',
    side,
    action_name: action,
    shape_name: side === 'BUY' ? '向上突破' : '加速线失守',
    triggered_at: '2026-07-23T01:05:00Z',
    trigger_price: 11,
    snapshot_payload: {},
    read_at: null,
  }
}

const hkNotification = notification('01347.HK', 'hk', 'BUY', '买入')
const usNotification = notification('INTC.US', 'us', 'SELL', '卖出')

function symbolFixture(
  symbol: string,
  market: 'cn' | 'hk' | 'us',
  enabled: boolean,
  latest: DowMonitorNotification | null,
  states: DowMonitorOverviewSymbol['states'],
): DowMonitorOverviewSymbol {
  return {
    symbol,
    name: symbol === '01347.HK' ? '华丰科技' : null,
    last_price: symbol === '01347.HK' ? 13.47 : 21.5,
    change_pct: symbol === '01347.HK' ? 0.0125 : -0.02,
    quote_timestamp: 1_774_752_700_000,
    market,
    enabled,
    created_at: '2026-07-23T00:00:00Z',
    updated_at: '2026-07-23T01:05:01Z',
    states,
    latest_notification: latest,
    last_success_at: '2026-07-23T01:05:01Z',
    last_error: null,
  } as DowMonitorOverviewSymbol
}

const overview: DowMonitorOverviewResponse = {
  symbols: [
    symbolFixture('01347.HK', 'hk', true, hkNotification, {
      '5m': state('01347.HK', 'hk', '5m', 'OPEN_LONG', 'LIVE', authoritativeChart),
      '15m': state('01347.HK', 'hk', '15m', 'CLOSE_LONG'),
      '30m': state('01347.HK', 'hk', '30m', 'WATCH'),
      '60m': state('01347.HK', 'hk', '60m', 'OPEN_LONG', 'STALE_DATA'),
      day: state('01347.HK', 'hk', 'day'),
    }),
    symbolFixture('INTC.US', 'us', true, usNotification, Object.fromEntries(
      TIMEFRAMES.map(timeframe => [
        timeframe,
        state('INTC.US', 'us', timeframe, 'CLOSE_LONG'),
      ]),
    )),
    symbolFixture('600000.SH', 'cn', true, null, {
      '5m': state('600000.SH', 'cn', '5m', 'OPEN_LONG', 'ANALYSIS_PAUSED'),
    }),
    symbolFixture('600519.SH', 'cn', false, null, {
      '5m': state('600519.SH', 'cn', '5m', 'OPEN_LONG', 'LIVE', authoritativeChart),
    }),
    symbolFixture('000001.SZ', 'cn', true, null, {
      '5m': state('000001.SZ', 'cn', '5m'),
    }),
  ],
  source: 'webstock',
  source_timestamp: '2026-07-23T01:05:00Z',
}

const notifications = [hkNotification, usNotification]

function deferred<T = unknown>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve
    reject = promiseReject
  })
  return { promise, reject, resolve }
}

beforeEach(() => {
  realtimeMocks.view = {
    status: 'fallback',
    states: new Map(),
  }
  realtimeMocks.useRealtimeMarketData.mockReset()
  realtimeMocks.useRealtimeMarketData.mockImplementation(() => realtimeMocks.view)
  apiMocks.instrumentSearch.mockReset()
  apiMocks.instrumentSearch.mockResolvedValue({ results: [] })
  hooks.add.mockReset()
  hooks.markRead.mockReset()
  hooks.remove.mockReset()
  hooks.setEnabled.mockReset()
  hooks.add.mockImplementation((_variables, options) => options?.onSuccess?.())
  hooks.markRead.mockResolvedValue(undefined)
  hooks.remove.mockResolvedValue(undefined)
  hooks.setEnabled.mockResolvedValue(undefined)
  chartMocks.disconnect.mockReset()
  chartMocks.dispose.mockReset()
  chartMocks.init.mockReset()
  chartMocks.observe.mockReset()
  chartMocks.resize.mockReset()
  chartMocks.setOption.mockReset()
  chartMocks.resizeCallback = undefined
  chartMocks.init.mockReturnValue({
    dispose: chartMocks.dispose,
    resize: chartMocks.resize,
    setOption: chartMocks.setOption,
  })
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: ResizeObserverCallback) {
      chartMocks.resizeCallback = callback
    }
    observe = chartMocks.observe
    disconnect = chartMocks.disconnect
  })
  hooks.overview = {
    data: overview,
    isError: false,
    isLoading: false,
  }
  hooks.notifications = {
    data: { notifications },
    isError: false,
    isLoading: false,
  }
  hooks.status = {
    data: {
      running: true,
      poll_seconds: 15,
      source: 'webstock',
      last_started_at: '2026-07-23T01:05:00Z',
      last_completed_at: '2026-07-23T01:05:00Z',
      last_success_at: '2026-07-23T01:05:00Z',
      last_error: null,
      errors: {},
    },
    isError: false,
    isLoading: false,
  }
  hooks.addState = { isError: false, isPending: false }
  hooks.readState = { isError: false, isPending: false }
  hooks.removeState = { isError: false, isPending: false }
  hooks.toggleState = { isError: false, isPending: false }
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Dow monitor page', () => {
  it('shows a wide four-column grid and keeps notifications inside their market card', async () => {
    const user = userEvent.setup()
    render(<DowMonitor />)

    expect(screen.queryByTestId('dow-monitor-signal-rail')).not.toBeInTheDocument()
    expect(screen.getByTestId('dow-monitor-grid')).toHaveClass(
      'grid-cols-1',
      'md:grid-cols-2',
      'xl:grid-cols-3',
      '2xl:grid-cols-4',
    )
    await user.click(screen.getByRole('button', { name: '港股' }))

    expect(screen.getByTestId('card-01347.HK')).toBeInTheDocument()
    expect(screen.queryByTestId('card-INTC.US')).not.toBeInTheDocument()
    expect(screen.getByTestId('card-message-01347.HK-BUY')).toBeInTheDocument()
    expect(screen.queryByTestId('card-message-INTC.US-SELL')).not.toBeInTheDocument()
  })

  it('shows multiple stock notifications as plain text rows in a taller message box', () => {
    hooks.notifications = {
      data: {
        notifications: [
          hkNotification,
          {
            ...hkNotification,
            notification_id: '01347.HK-BUY-30m',
            timeframe: '30m',
            shape_name: '第二次突破',
          },
          usNotification,
        ],
      },
      isError: false,
      isLoading: false,
    }

    render(<DowMonitor />)

    const hongKongCard = screen.getByTestId('card-01347.HK')
    const messageBox = within(hongKongCard).getByRole('log', { name: '01347.HK 消息通知' })
    expect(messageBox).toHaveClass('h-32', 'overflow-y-auto')
    const firstMessage = within(messageBox).getByTestId('card-message-01347.HK-BUY')
    expect(firstMessage).toHaveClass('border-b', 'border-l-2', 'border-l-accent')
    expect(firstMessage).not.toHaveClass('rounded', 'border', 'bg-elevated/50')
    expect(within(firstMessage).getByText('最新')).toBeInTheDocument()
    const secondMessage = within(messageBox).getByTestId('card-message-01347.HK-BUY-30m')
    expect(secondMessage).not.toHaveClass('border-l-2', 'border-l-accent')
    expect(within(secondMessage).queryByText('最新')).not.toBeInTheDocument()
    expect(within(messageBox).queryByTestId('card-message-INTC.US-SELL')).not.toBeInTheDocument()
    expect(screen.queryByTestId('dow-monitor-signal-rail')).not.toBeInTheDocument()
  })

  it('uses authoritative quote header fields and only falls back to the symbol for a missing name', () => {
    render(<DowMonitor />)

    const named = screen.getByTestId('card-01347.HK')
    expect(within(named).getByText('华丰科技')).toBeInTheDocument()
    expect(within(named).getByText('13.47')).toBeInTheDocument()
    expect(within(named).getByText('+1.25%')).toBeInTheDocument()
    expect(within(named).queryByText('+5.77%')).not.toBeInTheDocument()
    expect(within(named).getByText('行情 2026-03-29 10:51')).toBeVisible()
    expect(within(named).getByText('成功 2026-07-23 09:05')).toBeVisible()
    expect(screen.getByText('数据源 webstock · 源 2026-07-23 09:05')).toBeVisible()

    const unnamed = screen.getByTestId('card-INTC.US')
    expect(within(unnamed).getAllByText('INTC.US')).toHaveLength(1)
  })

  it('uses the shared realtime state for price, best bid/ask, and the live badge', () => {
    realtimeMocks.view = {
      status: 'realtime',
      states: new Map([[
        '01347.HK',
        {
          symbol: '01347.HK',
          streamId: 'stream-1',
          sequence: 4,
          eventAt: '2026-07-24T10:00:00+08:00',
          publishedAt: '2026-07-24T10:00:00.100+08:00',
          quote: {
            lastDone: 14,
            prevClose: 13.5,
            timestamp: '2026-07-24T10:00:00+08:00',
          },
          depth: {
            bids: [{ position: 1, price: 13.99 }],
            asks: [{ position: 1, price: 14.01 }],
          },
          candlestick: {
            period: 'min_1',
            timestamp: '2026-07-24T10:00:00+08:00',
            open: 13.8,
            high: 14.1,
            low: 13.7,
            close: 14,
            volume: 100,
            turnover: 1400,
          },
          quoteDelayed: false,
          depthDelayed: false,
          candlestickDelayed: false,
        },
      ]]),
    }

    render(<DowMonitor />)

    const card = screen.getByTestId('card-01347.HK')
    expect(within(card).getByText('14.00')).toBeInTheDocument()
    expect(within(card).getByText('+3.70%')).toBeInTheDocument()
    expect(within(card).getByText('买一 13.99 · 卖一 14.01')).toBeInTheDocument()
    expect(within(card).getByText('实时')).toBeInTheDocument()
    expect(realtimeMocks.useRealtimeMarketData).toHaveBeenCalledWith(
      ['01347.HK', 'INTC.US', '600000.SH', '000001.SZ'],
      ['quote', 'depth', 'candlestick'],
      1,
    )
  })

  it('uses red for rising prices and green for falling prices', () => {
    render(<DowMonitor />)

    const risingPrice = within(screen.getByTestId('card-01347.HK')).getByText('13.47')
    expect(risingPrice).toHaveClass('text-[16px]', 'text-bull')
    expect(risingPrice).not.toHaveClass('text-foreground', 'text-base')

    const fallingPrice = within(screen.getByTestId('card-INTC.US')).getByText('21.50')
    expect(fallingPrice).toHaveClass('text-[16px]', 'text-bear')
    expect(fallingPrice).not.toHaveClass('text-foreground', 'text-base')
  })

  it('keeps a compact two-row summary and gives the mini K-line 180 pixels', () => {
    render(<DowMonitor />)

    const card = screen.getByTestId('card-01347.HK')
    expect(within(card).getByTestId('card-summary-01347.HK')).toHaveAttribute(
      'data-layout',
      'compact-two-row',
    )
    expect(within(card).getByText('行情 2026-03-29 10:51')).toBeInTheDocument()
    expect(within(card).getByText('成功 2026-07-23 09:05')).toBeInTheDocument()
    expect(within(card).getByTestId('mini-chart-01347.HK-5m')).toHaveStyle({
      height: '180px',
    })
  })

  it('shows the latest signal trigger time and trigger price on the card', () => {
    hooks.notifications = {
      data: {
        notifications: [{ ...hkNotification, timeframe: 'day' }, usNotification],
      },
      isError: false,
      isLoading: false,
    }

    render(<DowMonitor />)

    const card = screen.getByTestId('card-01347.HK')
    expect(within(card).getByText('周期 日K')).toBeVisible()
    expect(within(card).getByText('触发 2026-07-23 09:05')).toBeVisible()
    expect(within(card).getByText('@11.00')).toBeVisible()
    expect(within(card).getByText('向上突破')).toBeVisible()
  })

  it('filters both cards and notifications by active, buy, and sell signal states', async () => {
    const user = userEvent.setup()
    render(<DowMonitor />)

    await user.click(screen.getByRole('button', { name: '有信号' }))
    expect(screen.queryByTestId('card-600000.SH')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '仅买点' }))
    expect(screen.getByTestId('card-01347.HK')).toBeInTheDocument()
    expect(screen.queryByTestId('card-INTC.US')).not.toBeInTheDocument()
    expect(screen.getByTestId('card-message-01347.HK-BUY')).toBeInTheDocument()
    expect(screen.queryByTestId('card-message-INTC.US-SELL')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '仅卖点' }))
    expect(screen.queryByTestId('card-01347.HK')).not.toBeInTheDocument()
    expect(screen.getByTestId('card-INTC.US')).toBeInTheDocument()
    expect(screen.queryByTestId('card-message-01347.HK-BUY')).not.toBeInTheDocument()
    expect(screen.getByTestId('card-message-INTC.US-SELL')).toBeInTheDocument()
  })

  it('never mutates monitoring when switching market tabs', async () => {
    const user = userEvent.setup()
    render(<DowMonitor />)

    await user.click(screen.getByRole('button', { name: '美股' }))

    expect(hooks.setEnabled).not.toHaveBeenCalled()
  })

  it('keeps switches independent and routes add/remove through Task 8 mutations', async () => {
    const user = userEvent.setup()
    render(<DowMonitor />)

    await user.click(screen.getByRole('switch', { name: '01347.HK 监控开关' }))
    expect(hooks.setEnabled).toHaveBeenCalledWith({ symbol: '01347.HK', enabled: false })
    expect(hooks.setEnabled).not.toHaveBeenCalledWith(
      expect.objectContaining({ symbol: 'INTC.US' }),
    )

    await user.type(screen.getByRole('textbox', { name: '股票代码' }), '  aapl.us ')
    await user.click(screen.getByRole('button', { name: '添加' }))
    expect(hooks.add).toHaveBeenCalledWith(
      { symbol: 'AAPL.US', enabled: true },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    )
    expect(screen.getByRole('textbox', { name: '股票代码' })).toHaveValue('')

    await user.click(screen.getByRole('button', { name: '移除 INTC.US' }))
    expect(hooks.remove).toHaveBeenCalledWith('INTC.US')
  })

  it('shows stock suggestions and fills the canonical symbol before explicit add', async () => {
    apiMocks.instrumentSearch.mockResolvedValue({
      results: [
        {
          symbol: '0700.HK',
          name: '腾讯控股',
          code: '00700',
          market: 'hk',
          asset_type: 'stock',
        },
      ],
    })
    const user = userEvent.setup()
    render(<DowMonitor />)

    const input = screen.getByRole('textbox', { name: '股票代码' })
    await user.type(input, '腾讯')

    const option = await screen.findByRole('option', { name: /0700\.HK.*腾讯控股/ })
    const listbox = screen.getByRole('listbox', { name: '股票候选' })
    expect(input).toHaveClass('w-52')
    expect(listbox).toHaveClass('right-0', 'w-80')
    expect(listbox).not.toHaveClass('left-0')
    expect(apiMocks.instrumentSearch).toHaveBeenCalledWith('腾讯', 8, 'stock', 'all')
    await user.click(option)

    expect(input).toHaveValue('0700.HK')
    expect(hooks.add).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '添加' }))
    expect(hooks.add).toHaveBeenCalledWith(
      { symbol: '0700.HK', enabled: true },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    )
  })

  it('shows all five timeframe badges and changes only the selected card mini chart', async () => {
    const user = userEvent.setup()
    const onOpen = vi.fn()
    render(<DowMonitor onOpen={onOpen} />)

    const hongKongCard = screen.getByTestId('card-01347.HK')
    const unitedStatesCard = screen.getByTestId('card-INTC.US')
    for (const label of ['5分', '15分', '30分', '60分', '日K']) {
      expect(within(hongKongCard).getByRole('button', { name: label })).toBeInTheDocument()
    }

    expect(within(hongKongCard).getByTestId('mini-chart-01347.HK-5m')).toBeInTheDocument()
    expect(within(unitedStatesCard).getByTestId('mini-chart-INTC.US-5m')).toBeInTheDocument()
    await user.click(within(hongKongCard).getByRole('button', { name: '15分' }))
    expect(within(hongKongCard).getByTestId('mini-chart-01347.HK-15m')).toBeInTheDocument()
    expect(within(unitedStatesCard).getByTestId('mini-chart-INTC.US-5m')).toBeInTheDocument()

    await user.click(within(hongKongCard).getByRole('button', { name: '打开 01347.HK 完整K线' }))
    expect(onOpen).toHaveBeenCalledWith('01347.HK', '15m')
  })

  it('uses green buy, red sell/risk, yellow watch, gray none, and blocked stale states', () => {
    render(<DowMonitor />)
    const card = screen.getByTestId('card-01347.HK')

    expect(within(card).getByRole('button', { name: '5分' })).toHaveClass('text-emerald-400')
    expect(within(card).getByRole('button', { name: '15分' })).toHaveClass('text-red-400')
    expect(within(card).getByRole('button', { name: '30分' })).toHaveClass('text-amber-400')
    expect(within(card).getByRole('button', { name: '日K' })).toHaveClass('text-muted')
    expect(within(card).getByRole('button', { name: '60分' })).toHaveAttribute(
      'data-tradable',
      'false',
    )
    expect(within(card).getByText('买入')).toHaveClass('text-emerald-400')

    expect(
      within(screen.getByTestId('card-message-INTC.US-SELL')).getByText('卖出'),
    ).toHaveClass('text-red-400')
    expect(screen.getByTestId('card-600000.SH')).toHaveAttribute('data-tradable', 'false')
    expect(within(screen.getByTestId('card-600000.SH')).getByText('分析暂停')).toBeInTheDocument()

    const disabled = screen.getByTestId('card-600519.SH')
    expect(disabled).toHaveAttribute('data-tradable', 'false')
    expect(within(disabled).getByText('监控已暂停')).toBeInTheDocument()
    for (const label of ['5分', '15分', '30分', '60分', '日K']) {
      expect(within(disabled).getByRole('button', { name: label })).toHaveClass('text-muted')
      expect(within(disabled).getByRole('button', { name: label })).not.toHaveClass(
        'text-emerald-400',
        'text-red-400',
      )
    }
  })

  it('keeps the current WATCH badge yellow when the chart contains historical signals', () => {
    const historical = structuredClone(overview)
    historical.symbols[0].states['30m']!.chart = authoritativeChart
    hooks.overview = { data: historical, isError: false, isLoading: false }

    render(<DowMonitor />)

    const badge = within(screen.getByTestId('card-01347.HK')).getByRole(
      'button',
      { name: '30分' },
    )
    expect(badge).toHaveClass('text-amber-400')
    expect(badge).not.toHaveClass('text-emerald-400', 'text-red-400')
  })

  it('keeps current short actions red when the chart contains a historical buy', () => {
    const shortActions = structuredClone(overview)
    const historicalBuy = {
      ...authoritativeChart,
      signals: [authoritativeChart.signals![0]],
    }
    shortActions.symbols[0].states['30m'] = state(
      '01347.HK',
      'hk',
      '30m',
      'OPEN_SHORT',
      'LIVE',
      historicalBuy,
    )
    shortActions.symbols[0].states.day = state(
      '01347.HK',
      'hk',
      'day',
      'CLOSE_SHORT',
      'LIVE',
      historicalBuy,
    )
    hooks.overview = { data: shortActions, isError: false, isLoading: false }

    render(<DowMonitor />)

    const card = screen.getByTestId('card-01347.HK')
    expect(within(card).getByRole('button', { name: '30分' })).toHaveClass('text-red-400')
    expect(within(card).getByRole('button', { name: '日K' })).toHaveClass('text-red-400')
  })

  it('shows the compact no-signal state without inventing a notification', async () => {
    const user = userEvent.setup()
    render(<DowMonitor />)

    await user.click(screen.getByRole('button', { name: 'A股' }))

    expect(screen.getByRole('log', { name: '600000.SH 消息通知' })).toHaveTextContent(
      '暂无消息通知',
    )
    expect(screen.queryByTestId('dow-monitor-signal-rail')).not.toBeInTheDocument()
  })

  it('retains prior layout but blocks every card when queries disconnect', () => {
    hooks.overview = { data: overview, isError: true, isLoading: false }
    hooks.notifications = {
      data: { notifications },
      isError: true,
      isLoading: false,
    }

    render(<DowMonitor />)

    expect(screen.getByRole('alert')).toHaveTextContent('监控状态连接失败')
    expect(screen.getByRole('alert')).toHaveTextContent('通知连接失败')
    expect(screen.getByTestId('card-01347.HK')).toHaveAttribute('data-tradable', 'false')
    for (const label of ['5分', '15分', '30分', '60分', '日K']) {
      expect(
        within(screen.getByTestId('card-01347.HK')).getByRole('button', { name: label }),
      ).toHaveClass('text-muted')
    }
    expect(screen.getByTestId('card-message-01347.HK-BUY')).toBeInTheDocument()
  })

  it('blocks retained data when backend status is stopped and exposes loading states', () => {
    hooks.status = {
      data: { ...(hooks.status.data as object), running: false },
      isError: false,
      isLoading: false,
    }
    const { rerender } = render(<DowMonitor />)

    expect(screen.getByRole('alert')).toHaveTextContent('后台监控未运行')
    expect(screen.getByTestId('card-01347.HK')).toHaveAttribute('data-tradable', 'false')
    expect(screen.queryByText('后台持续运行')).not.toBeInTheDocument()

    hooks.status = { data: undefined, isError: false, isLoading: true }
    hooks.notifications = { data: undefined, isError: false, isLoading: true }
    rerender(<DowMonitor />)
    expect(screen.getByRole('alert')).toHaveTextContent('正在连接监控服务')
    expect(screen.getByRole('log', { name: '01347.HK 消息通知' })).toHaveTextContent(
      '正在加载通知',
    )
    expect(screen.getByText('数据源不可用')).toBeInTheDocument()
  })

  it('blocks retained LIVE quotes until the restarted backend completes one successful cycle', () => {
    hooks.status = {
      data: {
        ...(hooks.status.data as object),
        running: true,
        last_completed_at: null,
        last_success_at: null,
      },
      isError: false,
      isLoading: false,
    }
    const { rerender } = render(<DowMonitor />)
    const card = screen.getByTestId('card-01347.HK')

    expect(screen.getByRole('alert')).toHaveTextContent('等待后台首轮监控结果')
    expect(screen.getByText('5 只 · 后台准备中')).toBeInTheDocument()
    expect(card).toHaveAttribute('data-tradable', 'false')
    expect(within(card).queryByText('13.47')).not.toBeInTheDocument()
    expect(within(card).getByText('—')).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: '5分' })).toHaveClass('text-muted')

    hooks.status = {
      data: {
        ...(hooks.status.data as object),
        running: true,
        last_completed_at: '2026-07-23T01:05:00Z',
        last_success_at: '2026-07-23T01:05:00Z',
        last_error: 'an older isolated symbol failure',
      },
      isError: false,
      isLoading: false,
    }
    rerender(<DowMonitor />)

    expect(screen.queryByText('等待后台首轮监控结果')).not.toBeInTheDocument()
    expect(screen.getByText('5 只 · 后台运行中')).toBeInTheDocument()
    expect(card).toHaveAttribute('data-tradable', 'true')
    expect(within(card).getByText('13.47')).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: '5分' })).toHaveClass(
      'text-emerald-400',
    )
  })

  it('uses unknown or connection-failed status labels without a running contradiction', () => {
    hooks.status = {
      data: hooks.status.data,
      isError: true,
      isLoading: false,
    }
    const { rerender } = render(<DowMonitor />)

    expect(screen.getByText('5 只 · 后台连接失败')).toBeInTheDocument()
    expect(screen.queryByText('5 只 · 后台运行中')).not.toBeInTheDocument()
    expect(screen.getByTestId('card-01347.HK')).toHaveAttribute('data-tradable', 'false')

    hooks.status = { data: undefined, isError: false, isLoading: false }
    rerender(<DowMonitor />)
    expect(screen.getByText('5 只 · 后台状态未知')).toBeInTheDocument()
    expect(screen.queryByText('5 只 · 后台未运行')).not.toBeInTheDocument()
  })

  it('keeps failed mutations visible and retryable, clearing add input only on success', async () => {
    const user = userEvent.setup()
    hooks.addState = { isError: true, isPending: false, error: new Error('add failed') }
    hooks.add.mockImplementation(() => undefined)
    hooks.setEnabled.mockRejectedValueOnce(new Error('toggle failed'))
    hooks.remove.mockRejectedValueOnce(new Error('remove failed'))
    hooks.markRead.mockRejectedValueOnce(new Error('read failed'))
    const { rerender } = render(<DowMonitor />)

    expect(screen.getByRole('alert')).toHaveTextContent('添加失败，请重试')

    const input = screen.getByRole('textbox', { name: '股票代码' })
    await user.type(input, 'aapl.us')
    await user.click(screen.getByRole('button', { name: '添加' }))
    expect(input).toHaveValue('aapl.us')

    await user.click(screen.getByRole('switch', { name: '01347.HK 监控开关' }))
    await user.click(screen.getByRole('button', { name: '移除 INTC.US' }))
    await user.click(screen.getByRole('button', { name: '标记 01347.HK 已读' }))

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(
        '01347.HK 监控开关更新失败，请重试',
      )
      expect(screen.getByRole('alert')).toHaveTextContent('移除 INTC.US 失败，请重试')
      expect(screen.getByRole('alert')).toHaveTextContent(
        '标记 01347.HK 已读失败，请重试',
      )
    })
    expect(screen.getByRole('button', { name: '添加' })).not.toBeDisabled()
    expect(screen.getByRole('switch', { name: '01347.HK 监控开关' })).not.toBeDisabled()
    expect(screen.getByRole('button', { name: '移除 INTC.US' })).not.toBeDisabled()
    expect(screen.getByRole('button', { name: '标记 01347.HK 已读' })).not.toBeDisabled()

    await user.click(screen.getByRole('switch', { name: '01347.HK 监控开关' }))
    await user.click(screen.getByRole('button', { name: '移除 INTC.US' }))
    await user.click(screen.getByRole('button', { name: '标记 01347.HK 已读' }))
    await waitFor(() => {
      expect(hooks.setEnabled).toHaveBeenCalledTimes(2)
      expect(hooks.remove).toHaveBeenCalledTimes(2)
      expect(hooks.markRead).toHaveBeenCalledTimes(2)
    })

    hooks.addState = { isError: false, isPending: false }
    hooks.add.mockImplementation((_variables, options) => options?.onSuccess?.())
    rerender(<DowMonitor />)
    await user.click(screen.getByRole('button', { name: '添加' }))
    expect(input).toHaveValue('')
  })

  it('keeps add pending explicit without serializing stock controls', () => {
    hooks.addState = { isError: false, isPending: true }

    render(<DowMonitor />)

    expect(screen.getByRole('button', { name: '添加中' })).toBeDisabled()
    expect(screen.getByRole('switch', { name: '01347.HK 监控开关' })).not.toBeDisabled()
    expect(screen.getByRole('switch', { name: 'INTC.US 监控开关' })).not.toBeDisabled()
    expect(screen.getByRole('button', { name: '移除 INTC.US' })).not.toBeDisabled()
    expect(screen.getByRole('button', { name: '标记 01347.HK 已读' })).not.toBeDisabled()
  })

  it('tracks concurrent toggle pending and errors per symbol in reverse settlement order', async () => {
    const user = userEvent.setup()
    const first = deferred()
    const second = deferred()
    hooks.setEnabled
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)
    render(<DowMonitor />)
    const hk = screen.getByRole('switch', { name: '01347.HK 监控开关' })
    const us = screen.getByRole('switch', { name: 'INTC.US 监控开关' })

    await user.click(hk)
    await user.click(us)
    expect(hk).toBeDisabled()
    expect(us).toBeDisabled()

    act(() => second.reject(new Error('US failed')))
    await waitFor(() => expect(us).not.toBeDisabled())
    expect(hk).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent(
      'INTC.US 监控开关更新失败，请重试',
    )

    act(() => first.resolve(undefined))
    await waitFor(() => expect(hk).not.toBeDisabled())
    expect(us).not.toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent(
      'INTC.US 监控开关更新失败，请重试',
    )
  })

  it('tracks concurrent removals per symbol when the second settles first', async () => {
    const user = userEvent.setup()
    const first = deferred()
    const second = deferred()
    hooks.remove
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)
    render(<DowMonitor />)
    const hk = screen.getByRole('button', { name: '移除 01347.HK' })
    const us = screen.getByRole('button', { name: '移除 INTC.US' })

    await user.click(hk)
    await user.click(us)
    expect(hk).toBeDisabled()
    expect(us).toBeDisabled()

    act(() => second.resolve(undefined))
    await waitFor(() => expect(us).not.toBeDisabled())
    expect(hk).toBeDisabled()

    act(() => first.reject(new Error('HK failed')))
    await waitFor(() => expect(hk).not.toBeDisabled())
    expect(us).not.toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent(
      '移除 01347.HK 失败，请重试',
    )
  })

  it('tracks concurrent notification reads by id inside independent card message boxes', async () => {
    const user = userEvent.setup()
    const first = deferred()
    const second = deferred()
    hooks.markRead
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)
    render(<DowMonitor />)
    const hk = screen.getByRole('button', { name: '标记 01347.HK 已读' })
    const us = screen.getByRole('button', { name: '标记 INTC.US 已读' })

    await user.click(hk)
    await user.click(us)
    expect(hk).toBeDisabled()
    expect(us).toBeDisabled()

    act(() => second.reject(new Error('US failed')))
    await waitFor(() => expect(us).not.toBeDisabled())
    expect(hk).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent(
      '标记 INTC.US 已读失败，请重试',
    )

    act(() => first.resolve(undefined))
    await waitFor(() => expect(hk).not.toBeDisabled())
    expect(us).not.toBeDisabled()
  })

  it('uses a dedicated detail control and never opens from nested keyboard actions', async () => {
    const user = userEvent.setup()
    const onOpen = vi.fn()
    render(<DowMonitor onOpen={onOpen} />)
    const card = screen.getByTestId('card-01347.HK')
    expect(card).not.toHaveAttribute('role', 'button')

    for (const control of [
      within(card).getByRole('switch', { name: '01347.HK 监控开关' }),
      within(card).getByRole('button', { name: '移除 01347.HK' }),
      within(card).getByRole('button', { name: '15分' }),
    ]) {
      control.focus()
      await user.keyboard('{Enter}')
      await user.keyboard(' ')
      expect(onOpen).not.toHaveBeenCalled()
    }

    const open = within(card).getByRole('button', { name: '打开 01347.HK 完整K线' })
    open.focus()
    await user.keyboard('{Enter}')
    expect(onOpen).toHaveBeenCalledWith('01347.HK', '15m')
  })

  it('survives legacy malformed chart payloads without rendering invented semantics', () => {
    const legacy = structuredClone(overview)
    legacy.symbols[0].states['5m']!.chart = {
      bars: [null, { timestamp: 'not-a-time', open: 'bad' }],
      lines: [{ role: 'FUTURE_ROLE', side: 'UNKNOWN' }],
      signals: [{ side: 'HOLD', price: 'bad' }],
      longTerm: { first_anchor_time: 'not-a-time', first_anchor_price: 1 },
    } as unknown as DowMonitorChart
    hooks.overview = { data: legacy, isError: false, isLoading: false }

    expect(() => render(<DowMonitor />)).not.toThrow()
    expect(screen.getByTestId('card-01347.HK')).toBeInTheDocument()
  })
})

describe('Dow mini chart semantics', () => {
  it('uses solid blue/magenta main lines, dashed acceleration, and backend signal colors', () => {
    const option = buildDowMiniChartOption(authoritativeChart)
    const series = option.series as Array<Record<string, any>>
    const main = series.find(item => item.id === 'main-support')
    const acceleration = series.find(item => item.id === 'acceleration-resistance')
    const candle = series.find(item => item.id === 'candles')

    expect(main?.lineStyle).toMatchObject({ color: '#3B82F6', type: 'solid' })
    expect(acceleration?.lineStyle).toMatchObject({ color: '#D946EF', type: 'dashed' })
    expect(candle?.markPoint.data).toEqual(expect.arrayContaining([
      expect.objectContaining({ itemStyle: { color: '#22C55E' } }),
      expect.objectContaining({ itemStyle: { color: '#EF4444' } }),
    ]))
    expect(option.xAxis).toMatchObject({ axisLabel: { show: false } })
    expect(option.yAxis).toMatchObject({ axisLabel: { show: false } })
    expect(option.legend).toBeUndefined()
  })

  it('draws the amber long-term line only when both persisted anchors are complete', () => {
    const complete = buildDowMiniChartOption(authoritativeChart).series as Array<Record<string, any>>
    const incomplete = buildDowMiniChartOption({
      ...authoritativeChart,
      longTerm: {
        first_anchor_time: bars[0].timestamp,
        first_anchor_price: 9.6,
        second_anchor_time: bars[1].timestamp,
      },
    }).series as Array<Record<string, any>>

    expect(complete.find(item => item.id === 'long-term')?.lineStyle).toMatchObject({
      color: '#F59E0B',
    })
    expect(incomplete.some(item => item.id === 'long-term')).toBe(false)
  })

  it('does not infer lines or signals when the backend returns none', () => {
    const option = buildDowMiniChartOption({ bars, lines: [], signals: [] })
    const series = option.series as Array<Record<string, any>>
    const candle = series.find(item => item.id === 'candles')

    expect(series.filter(item => item.type === 'line')).toHaveLength(0)
    expect(candle?.markPoint.data).toEqual([])
  })

  it('omits malformed and unknown legacy bars, lines, signals, and anchors', () => {
    const option = buildDowMiniChartOption({
      bars: [
        bars[0],
        null,
        { ...bars[1], timestamp: 'not-a-time' },
        { ...bars[1], close: Number.NaN },
      ],
      lines: [
        authoritativeChart.lines![0],
        { ...authoritativeChart.lines![1], role: 'FUTURE_ROLE' },
        { ...authoritativeChart.lines![1], side: 'UNKNOWN' },
        { ...authoritativeChart.lines![1], anchorPrices: [10, Number.NaN] },
      ],
      signals: [
        authoritativeChart.signals![0],
        { ...authoritativeChart.signals![1], side: 'HOLD' },
        { ...authoritativeChart.signals![1], price: Number.NaN },
        { ...authoritativeChart.signals![1], barTime: '2020-01-01T00:00:00Z' },
      ],
      longTerm: {
        first_anchor_time: 'not-a-time',
        first_anchor_price: 9.6,
        second_anchor_time: bars[1].timestamp,
        second_anchor_price: 10,
      },
    } as unknown as DowMonitorChart)
    const series = option.series as Array<Record<string, any>>
    const candle = series.find(item => item.id === 'candles')

    expect(candle).toBeDefined()
    expect(candle!.data).toHaveLength(1)
    expect(series.filter(item => item.type === 'line').map(item => item.id)).toEqual([
      'main-support',
    ])
    expect(candle!.markPoint.data).toHaveLength(1)
    expect(candle!.markPoint.data[0].name).toBe('BUY')
    expect(series.some(item => item.id === 'long-term')).toBe(false)
  })

  it('reuses one chart instance, observes resize, and disposes on unmount', () => {
    const { rerender, unmount } = render(
      <DowMiniChart chart={{ bars, lines: [], signals: [] }} />,
    )

    expect(chartMocks.init).toHaveBeenCalledTimes(1)
    expect(chartMocks.observe).toHaveBeenCalledTimes(1)
    expect(chartMocks.setOption).toHaveBeenCalledTimes(1)

    rerender(<DowMiniChart chart={authoritativeChart} />)
    expect(chartMocks.init).toHaveBeenCalledTimes(1)
    expect(chartMocks.setOption).toHaveBeenCalledTimes(2)

    act(() => chartMocks.resizeCallback?.([], {} as ResizeObserver))
    expect(chartMocks.resize).toHaveBeenCalledTimes(1)
    unmount()
    expect(chartMocks.disconnect).toHaveBeenCalledTimes(1)
    expect(chartMocks.dispose).toHaveBeenCalledTimes(1)
  })
})
