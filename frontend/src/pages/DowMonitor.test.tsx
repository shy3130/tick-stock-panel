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
    next_day_direction: {
      symbol,
      as_of: '2026-07-23',
      score: symbol === '01347.HK' ? 86 : 62,
      probability: symbol === '01347.HK' ? 0.86 : 0.62,
      direction_label: symbol === '01347.HK' ? '强势偏多' : '中性震荡',
      realtime_signal: symbol === '01347.HK' ? 'BUY_WATCH' : 'OBSERVE',
      realtime_label: symbol === '01347.HK' ? '强势跟踪' : '观察',
      realtime_reason: symbol === '01347.HK'
        ? '日线评分强，实时价守在支撑上方'
        : '次日方向优势不足',
      key_levels: { support: 12.8, resistance: 14.2, stop: 12.42, recent_low: 12.1 },
      evidence: ['趋势站上MA20且MA20不弱于MA60'],
    },
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
  window.history.replaceState(null, '', '/')
  vi.unstubAllGlobals()
})

describe('Dow monitor page', () => {
  it('uses the market query parameter as the initial market scope', () => {
    window.history.replaceState(null, '', '/dow-monitor?market=hk')

    render(<DowMonitor />)

    expect(screen.getByTestId('card-01347.HK')).toBeInTheDocument()
    expect(screen.getByTestId('realtime-state-01347.HK')).toHaveTextContent('方向：')
    expect(within(screen.getByTestId('card-01347.HK')).queryByText(/历史信息（/))
      .not.toBeInTheDocument()
    expect(screen.queryByTestId('card-INTC.US')).not.toBeInTheDocument()
    expect(realtimeMocks.useRealtimeMarketData).toHaveBeenCalledWith(
      ['01347.HK'],
      ['quote', 'depth', 'candlestick'],
      1,
    )
  })

  it('shows realtime state as a direct direction conclusion with compact evidence', () => {
    window.history.replaceState(null, '', '/dow-monitor?market=hk')
    realtimeMocks.view = {
      status: 'realtime',
      states: new Map([
        [
          '01347.HK',
          {
            symbol: '01347.HK',
            streamId: 'stream-1',
            sequence: 1,
            eventAt: '2026-07-23T01:05:30Z',
            publishedAt: '2026-07-23T01:05:31Z',
            quote: { lastDone: 13.8, prevClose: 13.2, timestamp: '2026-07-23T01:05:30Z' },
            depth: {
              bids: [{ price: 13.8, volume: 100_000 }, { price: 13.78, volume: 50_000 }],
              asks: [{ price: 13.82, volume: 20_000 }, { price: 13.84, volume: 10_000 }],
              timestamp: '2026-07-23T01:05:30Z',
            },
            candlestick: {
              period: 'min_1',
              timestamp: '2026-07-23T01:05:00Z',
              open: 13.5,
              high: 13.85,
              low: 13.48,
              close: 13.8,
              volume: 240,
              turnover: 3_300_000,
            },
            quoteDelayed: false,
            depthDelayed: false,
            candlestickDelayed: false,
          },
        ],
      ]),
    }

    render(<DowMonitor />)

    const state = screen.getByTestId('realtime-state-01347.HK')
    expect(state).toHaveTextContent('方向：偏涨')
    expect(state).toHaveTextContent('上涨概率')
    expect(state).toHaveTextContent('%')
    expect(state).toHaveTextContent('主因：页面累计净流入，买盘明显强于卖盘，量能放大')
    expect(state).toHaveTextContent('证据：累计净流入 +165万；累计买卖比 4.99x；累计均比 2.40x；最新切片净流入 +165万')
    expect(state).not.toHaveTextContent('买207万/卖41万')
    expect(state).not.toHaveTextContent('最大买138万/卖28万')
    const chart = screen.getByTestId('mini-chart-01347.HK-5m')
    expect(
      Boolean(chart.compareDocumentPosition(state) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true)
  })

  it('keeps realtime state summary stable inside the same minute and refreshes on the next minute', () => {
    window.history.replaceState(null, '', '/dow-monitor?market=hk')
    const realtimeState: RealtimeSymbolState = {
      symbol: '01347.HK',
      streamId: 'stream-1',
      sequence: 1,
      eventAt: '2026-07-23T01:05:20Z',
      publishedAt: '2026-07-23T01:05:21Z',
      quote: { lastDone: 13.8, prevClose: 13.2, timestamp: '2026-07-23T01:05:20Z' },
      depth: {
        bids: [{ price: 13.8, volume: 100_000 }],
        asks: [{ price: 13.82, volume: 20_000 }],
        timestamp: '2026-07-23T01:05:20Z',
      },
      candlestick: {
        period: 'min_1',
        timestamp: '2026-07-23T01:05:00Z',
        open: 13.5,
        high: 13.85,
        low: 13.48,
        close: 13.8,
        volume: 240,
        turnover: 3_300_000,
      },
      quoteDelayed: false,
      depthDelayed: false,
      candlestickDelayed: false,
    }
    realtimeMocks.view = {
      status: 'realtime',
      states: new Map([['01347.HK', realtimeState]]),
    }

    const { rerender } = render(<DowMonitor />)
    expect(screen.getByTestId('realtime-state-01347.HK')).toHaveTextContent('累计净流入 +110万')
    expect(screen.getByTestId('realtime-state-01347.HK')).toHaveTextContent('累计买卖比 4.99x')
    expect(screen.getByTestId('realtime-state-01347.HK')).toHaveTextContent('最新切片净流入 +110万')
    expect(screen.getByTestId('realtime-state-01347.HK')).toHaveTextContent('方向：偏涨')

    realtimeMocks.view = {
      status: 'realtime',
      states: new Map([
        [
          '01347.HK',
          {
            ...realtimeState,
            sequence: 2,
            eventAt: '2026-07-23T01:05:45Z',
            depth: {
              bids: [{ price: 13.8, volume: 5_000 }],
              asks: [{ price: 13.82, volume: 200_000 }],
              timestamp: '2026-07-23T01:05:45Z',
            },
          },
        ],
      ]),
    }
    rerender(<DowMonitor />)
    expect(screen.getByTestId('realtime-state-01347.HK')).toHaveTextContent('累计净流入 +110万')
    expect(screen.getByTestId('realtime-state-01347.HK')).toHaveTextContent('累计买卖比 4.99x')
    expect(screen.getByTestId('realtime-state-01347.HK')).toHaveTextContent('最新切片净流入 +110万')
    expect(screen.getByTestId('realtime-state-01347.HK')).toHaveTextContent('方向：偏涨')

    realtimeMocks.view = {
      status: 'realtime',
      states: new Map([
        [
          '01347.HK',
          {
            ...realtimeState,
            sequence: 3,
            eventAt: '2026-07-23T01:06:03Z',
            depth: {
              bids: [{ price: 13.8, volume: 5_000 }],
              asks: [{ price: 13.82, volume: 200_000 }],
              timestamp: '2026-07-23T01:06:03Z',
            },
            candlestick: {
              ...realtimeState.candlestick!,
              timestamp: '2026-07-23T01:06:00Z',
              close: 13.4,
              turnover: 800_000,
            },
          },
        ],
      ]),
    }
    rerender(<DowMonitor />)
    expect(screen.getByTestId('realtime-state-01347.HK')).toHaveTextContent('累计净流出 -159万')
    expect(screen.getByTestId('realtime-state-01347.HK')).toHaveTextContent('累计买卖比 0.48x')
    expect(screen.getByTestId('realtime-state-01347.HK')).toHaveTextContent('最新切片净流出 -270万')
    expect(screen.getByTestId('realtime-state-01347.HK')).toHaveTextContent('方向：偏跌')
  })

  it('uses trading-day capital for direction even when the latest minute slice is negative', () => {
    window.history.replaceState(null, '', '/dow-monitor?market=hk')
    const withCapital = structuredClone(overview)
    withCapital.symbols[0].intraday_capital = {
      capital_minute: '2026-07-23T01:06:00Z',
      total_net: 186,
      large_net: 92,
      total_in: 560,
      total_out: 374,
      large_net_ratio: 0.19,
      flow_15m: -8,
      flow_30m: 22,
      flow_today: 186,
      last_flow_time: '2026-07-23T01:06:00Z',
      flow_points: 88,
      windows: [
        {
          label: '近30分钟',
          minutes: 30,
          start_time: '2026-07-23 14:30:00.000',
          end_time: '2026-07-23 15:00:00.000',
          start_price: 13.1,
          end_price: 13.47,
          price_change_pct: 2.82,
          start_total_net: 100,
          end_total_net: 186,
          total_net_delta: 86,
          start_large_net: 48,
          end_large_net: 92,
          large_net_delta: 44,
        },
        {
          label: '近45分钟',
          minutes: 45,
          start_time: '2026-07-23 14:15:00.000',
          end_time: '2026-07-23 15:00:00.000',
          start_price: 12.98,
          end_price: 13.47,
          price_change_pct: 3.78,
          start_total_net: 80,
          end_total_net: 186,
          total_net_delta: 106,
          start_large_net: 28,
          end_large_net: 92,
          large_net_delta: 64,
        },
        {
          label: '近60分钟',
          minutes: 60,
          start_time: '2026-07-23 14:00:00.000',
          end_time: '2026-07-23 15:00:00.000',
          start_price: 12.9,
          end_price: 13.47,
          price_change_pct: 4.42,
          start_total_net: 70,
          end_total_net: 186,
          total_net_delta: 116,
          start_large_net: 18,
          end_large_net: 92,
          large_net_delta: 74,
        },
      ],
      source: 'trading_day',
    }
    hooks.overview = {
      data: withCapital,
      isError: false,
      isLoading: false,
    }
    realtimeMocks.view = {
      status: 'realtime',
      states: new Map([
        [
          '01347.HK',
          {
            symbol: '01347.HK',
            streamId: 'stream-1',
            sequence: 3,
            eventAt: '2026-07-23T01:06:03Z',
            publishedAt: '2026-07-23T01:06:04Z',
            quote: { lastDone: 13.8, prevClose: 13.2, timestamp: '2026-07-23T01:06:03Z' },
            depth: {
              bids: [{ price: 13.8, volume: 5_000 }],
              asks: [{ price: 13.82, volume: 200_000 }],
              timestamp: '2026-07-23T01:06:03Z',
            },
            candlestick: {
              period: 'min_1',
              timestamp: '2026-07-23T01:06:00Z',
              open: 13.7,
              high: 13.8,
              low: 13.35,
              close: 13.4,
              volume: 240,
              turnover: 800_000,
            },
            quoteDelayed: false,
            depthDelayed: false,
            candlestickDelayed: false,
          },
        ],
      ]),
    }

    render(<DowMonitor />)

    const state = screen.getByTestId('realtime-state-01347.HK')
    expect(state).toHaveTextContent('方向：偏涨')
    expect(state).toHaveTextContent('当日资金净流入 +186万')
    expect(state).toHaveTextContent('大单 +92万')
    expect(state).toHaveTextContent('30分钟 +22万')
    expect(state).toHaveTextContent('最新切片净流出 -270万')
    expect(state).toHaveTextContent('分析结论：偏涨')
    expect(state).toHaveTextContent('上涨条件占优')
    expect(state).toHaveTextContent('时间切片改善')
    expect(state).toHaveTextContent('14:30-15:00，价格 +2.8%')
    expect(state).toHaveTextContent('总资金从 +100万 到 +186万，改善 +86万')
    expect(state).toHaveTextContent('大单资金从 +48万 到 +92万，改善 +44万')
    expect(state).toHaveTextContent('连续性观察')
    expect(state).toHaveTextContent('30分 总+86万 / 大单+44万；45分 总+106万 / 大单+64万；60分 总+116万 / 大单+74万')
    expect(state).not.toHaveTextContent('大单压力未解')
  })

  it('treats flat capital windows after close as no fresh capital update', () => {
    window.history.replaceState(null, '', '/dow-monitor?market=us')
    const withCapital = structuredClone(overview)
    withCapital.symbols[1].intraday_capital = {
      capital_minute: '2026-07-24T22:19:00Z',
      total_net: -5399,
      large_net: -2087,
      total_in: 14300,
      total_out: 19700,
      large_net_ratio: -0.11,
      flow_15m: 0,
      flow_30m: 0,
      flow_today: -308112902,
      last_flow_time: '2026-07-24T22:19:00Z',
      flow_points: 98,
      windows: [
        {
          label: '近30分钟',
          minutes: 30,
          start_time: '2026-07-25 05:49:00.000',
          end_time: '2026-07-25 06:19:00.000',
          start_price: 315.5,
          end_price: 313.59,
          price_change_pct: -0.6,
          start_total_net: -5399,
          end_total_net: -5399,
          total_net_delta: 0,
          start_large_net: -2087,
          end_large_net: -2087,
          large_net_delta: 0,
        },
        {
          label: '近45分钟',
          minutes: 45,
          start_time: '2026-07-25 05:34:00.000',
          end_time: '2026-07-25 06:19:00.000',
          start_price: 316,
          end_price: 313.59,
          price_change_pct: -0.76,
          start_total_net: -5399,
          end_total_net: -5399,
          total_net_delta: 0,
          start_large_net: -2087,
          end_large_net: -2087,
          large_net_delta: 0,
        },
        {
          label: '近60分钟',
          minutes: 60,
          start_time: '2026-07-25 05:19:00.000',
          end_time: '2026-07-25 06:19:00.000',
          start_price: 317,
          end_price: 313.59,
          price_change_pct: -1.08,
          start_total_net: -5399,
          end_total_net: -5399,
          total_net_delta: 0,
          start_large_net: -2087,
          end_large_net: -2087,
          large_net_delta: 0,
        },
      ],
      source: 'trading_day',
    }
    hooks.overview = { data: withCapital, isError: false, isLoading: false }
    realtimeMocks.view = {
      status: 'realtime',
      states: new Map([
        [
          'INTC.US',
          {
            symbol: 'INTC.US',
            streamId: 'stream-flat',
            sequence: 1,
            eventAt: '2026-07-24T22:19:00Z',
            publishedAt: '2026-07-24T22:19:01Z',
            quote: { lastDone: 313.59, prevClose: 335.96, timestamp: '2026-07-24T22:19:00Z' },
            depth: {
              bids: [{ price: 313.56, volume: 80_000 }],
              asks: [{ price: 313.62, volume: 82_000 }],
              timestamp: '2026-07-24T22:19:00Z',
            },
            candlestick: {
              period: 'min_1',
              timestamp: '2026-07-24T22:19:00Z',
              open: 313.7,
              high: 313.8,
              low: 313.45,
              close: 313.59,
              volume: 10,
              turnover: 3_135_900,
            },
            quoteDelayed: false,
            depthDelayed: false,
            candlestickDelayed: false,
          },
        ],
      ]),
    }

    render(<DowMonitor />)

    const stateBox = screen.getByTestId('realtime-state-INTC.US')
    expect(stateBox).toHaveTextContent('分析结论：偏跌')
    expect(stateBox).toHaveTextContent('下跌风险占优')
    expect(stateBox).toHaveTextContent('资金未更新')
    expect(stateBox).toHaveTextContent('05:49-06:19，价格 -0.6%')
    expect(stateBox).toHaveTextContent('总资金 -5399万，大单资金 -2087万在该窗口未变化')
    expect(stateBox).toHaveTextContent('连续性观察')
    expect(stateBox).toHaveTextContent('30分 总0万 / 大单0万；45分 总0万 / 大单0万；60分 总0万 / 大单0万')
    expect(stateBox).toHaveTextContent('当日资金净流出 -5399万')
    expect(stateBox).not.toHaveTextContent('-308112902万')
    expect(stateBox).not.toHaveTextContent('恶化 0万')
    expect(stateBox).not.toHaveTextContent('大单压力未解')
  })

  it('explains why a deeply negative stock is being pulled up from the intraday low', () => {
    window.history.replaceState(null, '', '/dow-monitor?market=us')
    const withCapital = structuredClone(overview)
    withCapital.symbols[1].change_pct = -0.069
    withCapital.symbols[1].intraday_capital = {
      capital_minute: '2026-07-24T16:15:00Z',
      total_net: -1814,
      large_net: -448,
      total_in: 24028,
      total_out: 25842,
      large_net_ratio: -0.018,
      flow_15m: 420,
      flow_30m: 1063,
      flow_today: -1814,
      last_flow_time: '2026-07-24T16:15:00Z',
      flow_points: 88,
      windows: [
        {
          label: '近30分钟',
          minutes: 30,
          start_time: '2026-07-24 23:45:00.000',
          end_time: '2026-07-25 00:15:00.000',
          start_price: 202.044,
          end_price: 205.665,
          price_change_pct: 1.79,
          start_total_net: -2876.57,
          end_total_net: -1813.69,
          total_net_delta: 1062.88,
          start_large_net: -976.54,
          end_large_net: -448.31,
          large_net_delta: 528.23,
        },
      ],
      source: 'trading_day',
    }
    withCapital.symbols[1].states['5m'] = state(
      'INTC.US',
      'us',
      '5m',
      'WATCH',
      'LIVE',
      {
        bars: [
          { index: 0, timestamp: '2026-07-24T14:45:00Z', open: 202, high: 203, low: 194.04, close: 194.25, volume: 100 },
          { index: 1, timestamp: '2026-07-24T15:15:00Z', open: 194.25, high: 206, low: 194.1, close: 205.67, volume: 240 },
        ],
        lines: [],
        signals: [],
      },
    )
    hooks.overview = { data: withCapital, isError: false, isLoading: false }
    realtimeMocks.view = {
      status: 'realtime',
      states: new Map([
        [
          'INTC.US',
          {
            symbol: 'INTC.US',
            streamId: 'stream-1',
            sequence: 1,
            eventAt: '2026-07-24T16:15:00Z',
            publishedAt: '2026-07-24T16:15:01Z',
            quote: { lastDone: 205.67, prevClose: 220.97, timestamp: '2026-07-24T16:15:00Z' },
            depth: {
              bids: [{ price: 205.6, volume: 80_000 }],
              asks: [{ price: 205.8, volume: 35_000 }],
              timestamp: '2026-07-24T16:15:00Z',
            },
            candlestick: {
              period: 'min_1',
              timestamp: '2026-07-24T16:15:00Z',
              open: 204,
              high: 206,
              low: 203.8,
              close: 205.67,
              volume: 360,
              turnover: 7_400_000,
            },
            quoteDelayed: false,
            depthDelayed: false,
            candlestickDelayed: false,
          },
        ],
      ]),
    }

    render(<DowMonitor />)

    const stateBox = screen.getByTestId('realtime-state-INTC.US')
    expect(stateBox).toHaveTextContent('下跌后卖压衰减')
    expect(stateBox).toHaveTextContent('大单资金净流仍为 -448万')
    expect(stateBox).toHaveTextContent('23:45-00:15 从 -977万 修复到 -448万')
    expect(stateBox).toHaveTextContent('净改善 +528万')
    expect(stateBox).toHaveTextContent('总资金开始修复')
    expect(stateBox).toHaveTextContent('23:45-00:15，总资金从 -2877万 修复到 -1814万')
    expect(stateBox).toHaveTextContent('净改善 +1063万')
    expect(stateBox).toHaveTextContent('价格和资金开始同步改善')
    expect(stateBox).toHaveTextContent('23:45-00:15，价格 +1.8%')
    expect(stateBox).toHaveTextContent('从低点 194.04 拉到 205.67')
    expect(stateBox).toHaveTextContent('低位承接出现')
    expect(stateBox).toHaveTextContent('但还不是强势反转')
    expect(stateBox).toHaveTextContent('下跌承接 / 资金修复 / 卖压衰减 / 弱转强观察')
  })

  it('does not render a duplicate minute decision panel while realtime quotes update', () => {
    window.history.replaceState(null, '', '/dow-monitor?market=hk')
    const withDecision = structuredClone(overview)
    withDecision.symbols[0].minute_decision = {
      symbol: '01347.HK',
      market: 'hk',
      decision_minute: '2026-07-27T10:26:00+08:00',
      direction: 'BULLISH',
      direction_label: '偏涨',
      action: 'WATCH_BUY',
      action_label: '买入观察',
      confidence: 72,
      dominant_timeframe: '15m',
      confirmation_timeframes: ['30m'],
      supporting_reasons: ['15分钟趋势向上'],
      contrary_risks: ['5分钟量能仍需确认'],
      invalidation_conditions: ['跌破 31.20 后取消买入观察'],
      data_status: 'COMPLETE',
      status_label: '分钟决策已完成',
      source_timestamp: '2026-07-27T10:25:58+08:00',
    }
    hooks.overview = { data: withDecision, isError: false, isLoading: false }

    const { rerender } = render(<DowMonitor />)
    const card = screen.getByTestId('card-01347.HK')
    expect(within(card).queryByTestId('minute-decision-panel')).not.toBeInTheDocument()
    expect(within(card).getByTestId('latest-card-message')).toHaveTextContent('买入')

    realtimeMocks.view = {
      status: 'realtime',
      states: new Map([
        [
          '01347.HK',
          {
            symbol: '01347.HK',
            streamId: 'stream-decision-stability',
            sequence: 9,
            eventAt: '2026-07-27T02:26:45Z',
            publishedAt: '2026-07-27T02:26:46Z',
            quote: {
              lastDone: 138.8,
              prevClose: 133.2,
              timestamp: '2026-07-27T02:26:45Z',
            },
            quoteDelayed: false,
            depthDelayed: false,
            candlestickDelayed: false,
          },
        ],
      ]),
    }
    rerender(<DowMonitor />)

    expect(within(card).queryByTestId('minute-decision-panel')).not.toBeInTheDocument()
    expect(within(card).getByTestId('latest-card-message')).toHaveTextContent('买入')
    expect(within(card).getByText('138.80')).toBeVisible()
  })

  it('keeps the market query parameter in sync when switching tabs', async () => {
    const user = userEvent.setup()
    window.history.replaceState(null, '', '/dow-monitor?market=hk')

    render(<DowMonitor />)
    await user.click(screen.getByRole('button', { name: '美股' }))

    expect(window.location.search).toBe('?market=us')
    expect(screen.getByTestId('card-INTC.US')).toBeInTheDocument()
  })

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

  it('shows only the latest message and folds every older message newest first', async () => {
    const user = userEvent.setup()
    hooks.notifications = {
      data: {
        notifications: [
          {
            ...hkNotification,
            notification_id: '01347.HK-BUY-30m',
            timeframe: '30m',
            shape_name: '第二次突破',
            triggered_at: '2026-07-23T01:06:00Z',
            category: 'SELL_POINT',
            available_at: '2026-07-23T01:06:00Z',
            evidence_text: '周期30分，跌破趋势线13.20，结构位12.90',
            prompt_text: '卖出触发｜第二次突破',
          },
          {
            ...hkNotification,
            notification_id: '01347.HK-RISK',
            side: 'RISK',
            timeframe: '1m',
            triggered_at: '2026-07-23T01:05:30Z',
            category: 'EARLY_RISK',
            available_at: '2026-07-23T01:05:30Z',
            evidence_text: '高点回落1.44%；主动卖出占优27%，盘口卖压64%，资金流1分钟恶化1500万。',
            prompt_text: '首次冲高回落预警',
          },
          {
            ...hkNotification,
            category: 'BUY_POINT',
            available_at: '2026-07-23T01:05:00Z',
            evidence_text: '周期5分，向上突破，触发价11.00',
            prompt_text: '买入触发｜向上突破',
          },
          usNotification,
        ],
      },
      isError: false,
      isLoading: false,
    }

    render(<DowMonitor />)

    const hongKongCard = screen.getByTestId('card-01347.HK')
    expect(hongKongCard).toHaveClass('dow-card-container')
    expect(within(hongKongCard).queryByLabelText('分钟决策分析中心'))
      .not.toBeInTheDocument()
    const messageBox = within(hongKongCard).getByRole('log', { name: '01347.HK 当日决策消息' })
    expect(messageBox).not.toHaveTextContent('可获知时间')
    expect(messageBox).not.toHaveTextContent('完成后')
    expect(within(messageBox).queryByRole('button', { name: '标记 01347.HK 已读' }))
      .not.toBeInTheDocument()
    expect(messageBox).toHaveTextContent('首次冲高回落预警')
    expect(messageBox).toHaveTextContent('资金流1分钟恶化1500万')
    const latestMessage = within(messageBox).getByTestId('latest-card-message')
    expect(within(latestMessage).getByTestId('card-message-01347.HK-BUY-30m'))
      .toBeInTheDocument()
    expect(within(latestMessage).queryByTestId('card-message-01347.HK-RISK'))
      .not.toBeInTheDocument()

    const historySummary = within(messageBox).getByText('历史信息（2条）')
    const historyDetails = historySummary.closest('details')
    expect(historyDetails).not.toBeNull()
    expect(historyDetails).not.toHaveAttribute('open')
    const historyMessages = within(messageBox).getByTestId('history-card-messages')
    const messageRows = Array.from(
      historyMessages.querySelectorAll<HTMLElement>('[data-testid^="card-message-"]'),
    ).filter(element => !element.dataset.testid?.includes('headline')
      && !element.dataset.testid?.includes('evidence'))
    expect(messageRows.map(element => element.dataset.testid)).toEqual([
      'card-message-01347.HK-RISK',
      'card-message-01347.HK-BUY',
    ])
    expect(within(historyMessages).queryByTestId('card-message-01347.HK-BUY-30m'))
      .not.toBeInTheDocument()
    await user.click(historySummary)
    expect(historyDetails).toHaveAttribute('open')

    const newestMessage = within(latestMessage).getByTestId('card-message-01347.HK-BUY-30m')
    expect(newestMessage).toHaveClass('border-b', 'dow-timeline-row')
    expect(newestMessage).not.toHaveClass('rounded', 'bg-elevated/50')
    const newestHeadline = within(newestMessage).getByTestId(
      'card-message-headline-01347.HK-BUY-30m',
    )
    expect(within(newestHeadline).getByText('提示：卖出触发｜第二次突破'))
      .toHaveClass('font-semibold')
    expect(within(newestHeadline).getByText('2026-07-23 09:06')).toBeVisible()
    expect(within(newestMessage).getByTestId(
      'card-message-evidence-01347.HK-BUY-30m',
    )).toHaveTextContent(
      '内部变化：周期30分，跌破趋势线13.20，结构位12.90',
    )
    expect(within(messageBox).queryByTestId('card-message-INTC.US-SELL')).not.toBeInTheDocument()
    const rawDetails = within(hongKongCard)
      .getByText('分钟行情原始信息（辅助）')
      .closest('details')
    expect(rawDetails).not.toBeNull()
    expect(rawDetails).not.toHaveAttribute('open')
    expect(
      Boolean(historyDetails!.compareDocumentPosition(rawDetails!) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true)
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

  it('shows legacy signal fields through the causal message fallback', () => {
    hooks.notifications = {
      data: {
        notifications: [{ ...hkNotification, timeframe: 'day' }, usNotification],
      },
      isError: false,
      isLoading: false,
    }

    render(<DowMonitor />)

    const card = screen.getByTestId('card-01347.HK')
    expect(within(card).getByTestId('card-message-headline-01347.HK-BUY'))
      .toHaveTextContent('提示：买入2026-07-23 09:05')
    expect(within(card).getByTestId('card-message-evidence-01347.HK-BUY'))
      .toHaveTextContent('内部变化：日K 向上突破，触发价 11.00')
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
    expect(within(card).getByText('提示：买入')).toHaveClass('text-emerald-400')

    expect(
      within(screen.getByTestId('card-message-INTC.US-SELL')).getByText('提示：卖出'),
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

    expect(screen.getByRole('log', { name: '600000.SH 当日决策消息' })).toHaveTextContent(
      '暂无当日决策消息',
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
    expect(screen.getByRole('log', { name: '01347.HK 当日决策消息' })).toHaveTextContent(
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
    const { rerender } = render(<DowMonitor />)

    expect(screen.getByRole('alert')).toHaveTextContent('添加失败，请重试')

    const input = screen.getByRole('textbox', { name: '股票代码' })
    await user.type(input, 'aapl.us')
    await user.click(screen.getByRole('button', { name: '添加' }))
    expect(input).toHaveValue('aapl.us')

    await user.click(screen.getByRole('switch', { name: '01347.HK 监控开关' }))
    await user.click(screen.getByRole('button', { name: '移除 INTC.US' }))

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(
        '01347.HK 监控开关更新失败，请重试',
      )
      expect(screen.getByRole('alert')).toHaveTextContent('移除 INTC.US 失败，请重试')
    })
    expect(screen.getByRole('button', { name: '添加' })).not.toBeDisabled()
    expect(screen.getByRole('switch', { name: '01347.HK 监控开关' })).not.toBeDisabled()
    expect(screen.getByRole('button', { name: '移除 INTC.US' })).not.toBeDisabled()

    await user.click(screen.getByRole('switch', { name: '01347.HK 监控开关' }))
    await user.click(screen.getByRole('button', { name: '移除 INTC.US' }))
    await waitFor(() => {
      expect(hooks.setEnabled).toHaveBeenCalledTimes(2)
      expect(hooks.remove).toHaveBeenCalledTimes(2)
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
  it('uses solid blue/magenta main lines, dashed acceleration, and red-buy green-sell signal colors', () => {
    const option = buildDowMiniChartOption(authoritativeChart)
    const series = option.series as Array<Record<string, any>>
    const main = series.find(item => item.id === 'main-support')
    const acceleration = series.find(item => item.id === 'acceleration-resistance')
    const candle = series.find(item => item.id === 'candles')

    expect(main?.lineStyle).toMatchObject({ color: '#3B82F6', type: 'solid' })
    expect(acceleration?.lineStyle).toMatchObject({ color: '#D946EF', type: 'dashed' })
    expect(candle?.markPoint.data).toEqual(expect.arrayContaining([
      expect.objectContaining({ name: 'BUY', itemStyle: { color: '#EF4444' } }),
      expect.objectContaining({ name: 'RISK', itemStyle: { color: '#22C55E' } }),
    ]))
    expect(option.tooltip).toMatchObject({ show: true, trigger: 'item', confine: true })
    expect(option.xAxis).toMatchObject({ axisLabel: { show: false } })
    expect(option.yAxis).toMatchObject({ axisLabel: { show: false } })
    expect(option.legend).toBeUndefined()
  })

  it('shows signal context in the mini chart hover tooltip', () => {
    const option = buildDowMiniChartOption(authoritativeChart)
    const series = option.series as Array<Record<string, any>>
    const candle = series.find(item => item.id === 'candles')
    const buyPoint = candle?.markPoint.data.find((item: Record<string, any>) => item.name === 'BUY')
    const formatter = (option.tooltip as Record<string, any>).formatter

    expect(buyPoint).toMatchObject({
      value: 10.4,
      signal: expect.objectContaining({
        side: 'BUY',
        reason: 'backend buy',
        confidence: 'HIGH',
      }),
    })
    expect(formatter({ data: buyPoint })).toContain('买点')
    expect(formatter({ data: buyPoint })).toContain('10.400')
    expect(formatter({ data: buyPoint })).toContain('backend buy')
    expect(formatter({ data: buyPoint })).toContain('HIGH')
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

  it('can hide trend lines for compact monitor cards while keeping signal markers', () => {
    const option = buildDowMiniChartOption(authoritativeChart, undefined, { showLines: false })
    const series = option.series as Array<Record<string, any>>
    const candle = series.find(item => item.id === 'candles')

    expect(series.filter(item => item.type === 'line')).toHaveLength(0)
    expect(candle?.markPoint.data).toEqual(expect.arrayContaining([
      expect.objectContaining({ name: 'BUY', itemStyle: { color: '#EF4444' } }),
      expect.objectContaining({ name: 'RISK', itemStyle: { color: '#22C55E' } }),
    ]))
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
