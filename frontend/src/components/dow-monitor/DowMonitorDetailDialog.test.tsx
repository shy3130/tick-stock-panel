import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  DowHeadShouldersPayload,
  DowMonitorDetailResponse,
  DowMonitorOverviewResponse,
  DowTimeframe,
} from './types'
import { DowMonitorDetailDialog } from './DowMonitorDetailDialog'
import {
  toChartMarkers,
  toHeadShouldersOverlays,
  toPriceLines,
  toSignalPriceLines,
} from './chartMappings'
import { DowMonitor } from '@/pages/DowMonitor'
import { buildHeadShouldersSeries } from '@/components/EChartsCandlestick'

const testState = vi.hoisted(() => ({
  detail: {} as Record<string, unknown>,
  detailCalls: [] as Array<[string, DowTimeframe]>,
  chart: {
    dispose: vi.fn(),
    resize: vi.fn(),
    setOption: vi.fn(),
  },
}))

vi.mock('./useDowMonitor', () => ({
  useDowMonitorDetail: (symbol: string, timeframe: DowTimeframe) => {
    testState.detailCalls.push([symbol, timeframe])
    const data = testState.detail.data as DowMonitorDetailResponse | undefined
    return data
      ? { ...testState.detail, data: { ...data, symbol, timeframe } }
      : testState.detail
  },
  useDowMonitorOverview: () => ({
    data: overview,
    isLoading: false,
    isError: false,
  }),
  useDowMonitorStatus: () => ({
    data: {
      running: true,
      last_completed_at: '2026-07-23T01:10:00Z',
      last_success_at: '2026-07-23T01:10:00Z',
    },
    isLoading: false,
    isError: false,
  }),
  useDowNotifications: () => ({
    data: { notifications: [] },
    isLoading: false,
    isError: false,
  }),
  useAddDowMonitorSymbol: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useRemoveDowMonitorSymbol: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useSetDowMonitorEnabled: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useMarkDowNotificationRead: () => ({ mutateAsync: vi.fn(), isPending: false }),
}))

vi.mock('echarts', () => ({
  init: () => testState.chart,
}))

vi.mock('@/components/EChartsCandlestick', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/components/EChartsCandlestick')>()
  return {
    ...actual,
    EChartsCandlestick: (props: Record<string, unknown>) => (
      <div
        data-testid="intraday-candlestick"
        data-markers={JSON.stringify(props.markers)}
        data-price-lines={JSON.stringify(props.priceLines)}
        data-head-shoulders-overlays={JSON.stringify(props.headShouldersOverlays)}
      />
    ),
  }
})

vi.mock('@/components/StockDailyKChart', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/components/StockDailyKChart')>()
  return {
    ...actual,
    StockDailyKChart: (props: Record<string, unknown>) => (
      <div data-testid="daily-candlestick" data-price-lines={JSON.stringify(props.priceLines)} />
    ),
  }
})

const bars = [
  {
    index: 0,
    timestamp: '2026-07-23T09:30:00+08:00',
    open: 9.8,
    high: 10.4,
    low: 9.6,
    close: 10.2,
    volume: 100,
  },
  {
    index: 1,
    timestamp: '2026-07-23T09:35:00+08:00',
    open: 10.2,
    high: 10.8,
    low: 10,
    close: 10.6,
    volume: 120,
  },
  {
    index: 2,
    timestamp: '2026-07-23T09:40:00+08:00',
    open: 10.6,
    high: 10.7,
    low: 9.1,
    close: 9.4,
    volume: 130,
  },
  {
    index: 3,
    timestamp: '2026-07-23T09:45:00+08:00',
    open: 9.4,
    high: 10,
    low: 9.3,
    close: 9.8,
    volume: 115,
  },
  {
    index: 4,
    timestamp: '2026-07-23T09:50:00+08:00',
    open: 9.8,
    high: 9.9,
    low: 9.35,
    close: 9.6,
    volume: 110,
  },
  {
    index: 5,
    timestamp: '2026-07-23T09:55:00+08:00',
    open: 9.6,
    high: 10.4,
    low: 9.55,
    close: 10.3,
    volume: 180,
  },
]

const headShouldersPayload = {
  patterns: [{
    id: 'hs-bottom-confirmed',
    type: 'BOTTOM',
    stage: 'CONFIRMED',
    side: 'BUY',
    signal: {
      family: 'HEAD_SHOULDERS',
      patternId: 'hs-bottom-confirmed',
      side: 'BUY',
      stage: 'CONFIRMED',
      barIndex: 5,
      barTime: bars[5].timestamp,
      price: 10.3,
    },
    points: {
      leftShoulder: { role: 'A', barIndex: 0, barTime: bars[0].timestamp, price: 9.6 },
      neckline1: { role: 'N1', barIndex: 1, barTime: bars[1].timestamp, price: 10.8 },
      head: { role: 'B', barIndex: 2, barTime: bars[2].timestamp, price: 9.1 },
      neckline2: { role: 'N2', barIndex: 3, barTime: bars[3].timestamp, price: 10 },
      rightShoulder: { role: 'C', barIndex: 4, barTime: bars[4].timestamp, price: 9.35 },
      breakout: { role: 'BREAKOUT', barIndex: 5, barTime: bars[5].timestamp, price: 10.3 },
    },
    neckline: {
      anchorIndexes: [1, 3],
      anchorTimes: [bars[1].timestamp, bars[3].timestamp],
      anchorPrices: [10.8, 10],
      triggerIndex: 5,
      triggerTime: bars[5].timestamp,
      triggerValue: 9.2,
    },
    volume: {
      ratio: 1.64,
      requiredRatio: 1.2,
      baseline: 109.8,
      triggerIndex: 5,
      triggerTime: bars[5].timestamp,
    },
    invalidation: { price: 9.05 },
    geometryScore: 82,
    volumeScore: 71,
    contextScore: 63,
    qualityScore: 216,
    evidence: ['HEAD_DEPTH_CONFIRMED', 'VOLUME_CONFIRMED'],
  }],
  signals: [{
    family: 'HEAD_SHOULDERS',
    patternId: 'hs-bottom-confirmed',
    side: 'BUY',
    stage: 'CONFIRMED',
    barIndex: 5,
    barTime: bars[5].timestamp,
    price: 10.3,
  }],
} satisfies DowHeadShouldersPayload

const detail: DowMonitorDetailResponse = {
  symbol: '01347.HK',
  market: 'hk',
  timeframe: '5m',
  freshness_state: 'LIVE',
  source_timestamp: '2026-07-23T01:05:00Z',
  snapshot: {
    action: '买入',
    action_code: 'OPEN_LONG',
    phase: '首次突破趋势线',
    candle_pattern: null,
    bar_completion: 'FORMING',
  },
  chart: {
    bars,
    lines: [{
      id: 'main-support',
      side: 'SUPPORT',
      role: 'MAIN',
      generation: 1,
      anchorIndexes: [0, 1],
      anchorTimes: [bars[0].timestamp, bars[1].timestamp],
      anchorPrices: [9.6, 10],
      createdIndex: 1,
      invalidatedIndex: null,
      controlsSignals: true,
    }],
    signals: [{
      side: 'BUY',
      barIndex: 1,
      barTime: bars[1].timestamp,
      price: 10.6,
      reason: 'engine output',
      confidence: 'HIGH',
      lineId: 'main-support',
      firstCrossIndex: 1,
      firstCrossTime: bars[1].timestamp,
      volumeRatio: 1.2,
      pattern: '向上突破',
      evidence: [],
    }],
    turning: {
      signals: [{
        side: 'BUY',
        stage: 'TRIGGER',
        actionableTime: bars[1].timestamp,
        actionableIndex: 1,
        detectedTime: bars[1].timestamp,
        detectedIndex: 1,
        price: 10.6,
        trendStateBefore: 'DOWN',
        trendStateAfter: 'UP',
        lineId: 'main-support',
        lineRole: 'MAIN',
        lineGeneration: 1,
        parentLineId: null,
        lineValue: 10.2,
        lineAnchorTimes: [bars[0].timestamp, bars[1].timestamp],
        lineAnchorPrices: [9.6, 10],
        breakDistanceNormalized: 0.4,
        structurePivotId: 'LOCAL-HIGH-1',
        structurePivotPrice: 10.5,
        structurePivotTime: bars[1].timestamp,
        triggerPath: 'DIRECT_STRUCTURE',
        reasonCodes: ['LINE_AND_NEAREST_LEVEL_BROKEN'],
      }],
    },
    headShoulders: headShouldersPayload,
    longTerm: {
      first_anchor_time: bars[0].timestamp,
      first_anchor_price: 9.4,
      second_anchor_time: bars[1].timestamp,
      second_anchor_price: 9.8,
    },
  },
  updated_at: '2026-07-23T01:05:01Z',
  last_success_at: '2026-07-23T01:05:01Z',
  last_error: null,
}

const overview: DowMonitorOverviewResponse = {
  source: 'webstock',
  source_timestamp: '2026-07-23T01:05:00Z',
  symbols: [{
    symbol: '01347.HK',
    market: 'hk',
    enabled: true,
    created_at: '2026-07-23T00:00:00Z',
    updated_at: '2026-07-23T01:05:01Z',
    name: '华丰科技',
    last_price: 10.6,
    change_pct: 0.02,
    quote_timestamp: '2026-07-23T01:05:00Z',
    states: {
      '5m': detail,
      '15m': { ...detail, timeframe: '15m' },
    },
    latest_notification: {
      notification_id: '01347.HK-BUY',
      event_key: '01347.HK-5m-BUY-1',
      symbol: '01347.HK',
      market: 'hk',
      timeframe: '5m',
      side: 'BUY',
      action_name: '买入',
      shape_name: '向上突破',
      triggered_at: '2026-07-23T01:05:00Z',
      trigger_price: 10.6,
      snapshot_payload: {},
      read_at: null,
    },
    last_success_at: '2026-07-23T01:05:01Z',
    last_error: null,
  }],
}

describe('Dow chart mappings', () => {
  it('maps complete causal head-and-shoulders geometry and projected neckline', () => {
    const overlays = toHeadShouldersOverlays(headShouldersPayload, bars)

    expect(overlays).toHaveLength(1)
    expect(overlays[0]).toEqual(expect.objectContaining({
      id: 'hs-bottom-confirmed',
      type: 'BOTTOM',
      stage: 'CONFIRMED',
      points: [
        expect.objectContaining({ role: 'A', date: bars[0].timestamp, price: 9.6 }),
        expect.objectContaining({ role: 'N1', date: bars[1].timestamp, price: 10.8 }),
        expect.objectContaining({ role: 'B', date: bars[2].timestamp, price: 9.1 }),
        expect.objectContaining({ role: 'N2', date: bars[3].timestamp, price: 10 }),
        expect.objectContaining({ role: 'C', date: bars[4].timestamp, price: 9.35 }),
        expect.objectContaining({ role: 'D', date: bars[5].timestamp, price: 10.3 }),
      ],
      neckline: {
        start: bars[1].timestamp,
        anchor2: bars[3].timestamp,
        end: bars[5].timestamp,
        startValue: 10.8,
        anchor2Value: 10,
        endValue: 9.2,
      },
    }))

    const series = buildHeadShouldersSeries(overlays)
    expect(series).toEqual(expect.arrayContaining([
      expect.objectContaining({
        name: '头肩形态 hs-bottom-confirmed',
        type: 'line',
      }),
      expect.objectContaining({
        name: '头肩颈线 hs-bottom-confirmed',
        type: 'line',
      }),
    ]))
  })

  it('renders only confirmed independent markers and keeps false breaks as warnings', () => {
    const top = {
      ...headShouldersPayload.patterns[0],
      id: 'hs-top-retest',
      type: 'TOP',
      stage: 'RETEST_CONFIRMED',
      side: 'SELL',
      signal: {
        ...headShouldersPayload.patterns[0].signal,
        patternId: 'hs-top-retest',
        side: 'SELL',
        stage: 'RETEST_CONFIRMED',
      },
    }
    const weak = {
      ...headShouldersPayload.patterns[0],
      id: 'hs-weak',
      stage: 'NECKLINE_BREAK_WEAK',
      signal: null,
    }
    const failed = {
      ...headShouldersPayload.patterns[0],
      id: 'hs-false',
      stage: 'FALSE_BREAKOUT',
      signal: null,
    }

    const overlays = toHeadShouldersOverlays({
      patterns: [headShouldersPayload.patterns[0], top, weak, failed],
      signals: [
        headShouldersPayload.signals[0],
        top.signal,
      ],
    }, bars)

    expect(overlays.map(item => item.marker)).toEqual([
      expect.objectContaining({ kind: 'buy', label: 'B', color: '#EF4444' }),
      expect.objectContaining({ kind: 'sell', label: 'S', color: '#22C55E' }),
      undefined,
      undefined,
    ])
    expect(overlays[2]).toEqual(expect.objectContaining({
      color: '#94A3B8',
      warning: false,
    }))
    expect(overlays[3]).toEqual(expect.objectContaining({
      color: '#F59E0B',
      warning: true,
    }))
  })

  it('omits incomplete patterns and presents Chinese evidence without internal codes', () => {
    const incomplete = {
      ...headShouldersPayload.patterns[0],
      id: 'hs-incomplete',
      points: {
        ...headShouldersPayload.patterns[0].points,
        breakout: null,
      },
    }
    const overlays = toHeadShouldersOverlays({
      patterns: [incomplete, headShouldersPayload.patterns[0]],
      signals: headShouldersPayload.signals,
    }, bars)

    expect(overlays).toHaveLength(1)
    expect(overlays[0].tooltipHtml).toContain('左肩')
    expect(overlays[0].tooltipHtml).toContain('头部')
    expect(overlays[0].tooltipHtml).toContain('右肩')
    expect(overlays[0].tooltipHtml).toContain('颈线锚点一')
    expect(overlays[0].tooltipHtml).toContain('触发时颈线')
    expect(overlays[0].tooltipHtml).toContain('突破量比')
    expect(overlays[0].tooltipHtml).toContain('已确认')
    expect(overlays[0].tooltipHtml).toContain('失效价')
    expect(overlays[0].tooltipHtml).toContain('结构评分')
    expect(overlays[0].tooltipHtml).toContain('量能评分')
    expect(overlays[0].tooltipHtml).toContain('背景评分')
    expect(overlays[0].tooltipHtml).toContain('综合评分')
    expect(overlays[0].tooltipHtml).toContain('头部深度成立')
    expect(overlays[0].tooltipHtml).not.toMatch(/CONFIRMED|HEAD_DEPTH|VOLUME_CONFIRMED|BREAKOUT/)
  })

  it('maps only authoritative signal sides and omits malformed legacy entries', () => {
    expect(toChartMarkers([
      detail.chart.signals![0],
      { side: 'RISK', barTime: bars[0].timestamp, price: 9.8 },
      { side: 'HOLD', barTime: bars[0].timestamp, price: 9.8 },
      { side: 'SELL', barTime: 'not-a-time', price: 9.8 },
      null,
    ] as unknown[])).toEqual([
      expect.objectContaining({
        date: bars[1].timestamp,
        kind: 'buy',
        above: true,
        color: '#EF4444',
        label: 'B',
        price: 10.6,
      }),
      expect.objectContaining({
        date: bars[0].timestamp,
        kind: 'sell',
        above: true,
        color: '#22C55E',
        label: 'R',
        price: 9.8,
      }),
    ])
  })

  it('maps exact backend anchors including longTerm without selecting or recomputing them', () => {
    const mapped = toPriceLines(
      [
        detail.chart.lines![0],
        { ...detail.chart.lines![0], id: 'future', role: 'FUTURE_ROLE' },
        { ...detail.chart.lines![0], id: 'bad', anchorPrices: [9.6, Number.NaN] },
      ] as unknown[],
      bars,
      detail.chart.longTerm,
    )

    expect(mapped).toEqual([
      expect.objectContaining({
        id: 'main-support',
        start: bars[0].timestamp,
        end: bars[1].timestamp,
        value: 9.6,
        endValue: 10,
        label: '主支撑',
        lineType: 'solid',
      }),
      expect.objectContaining({
        id: 'long-term',
        start: bars[0].timestamp,
        end: bars[1].timestamp,
        value: 9.4,
        endValue: 9.8,
        label: '长期趋势',
      }),
    ])
  })

  it('uses only strict turning double-break signals for chart markers', () => {
    const markers = toChartMarkers([
      {
        side: 'BUY',
        stage: 'TRIGGER',
        actionableTime: bars[1].timestamp,
        actionableIndex: 1,
        detectedTime: bars[0].timestamp,
        detectedIndex: 0,
        price: 10.8,
        lineId: 'down-line',
        lineValue: 10.2,
        structurePivotId: 'LEVEL:HIGH',
        structurePivotPrice: 10.6,
        triggerPath: 'DIRECT_STRUCTURE',
        reasonCodes: ['LINE_AND_NEAREST_LEVEL_BROKEN'],
      },
      {
        side: 'BUY',
        stage: 'TRIGGER',
        actionableTime: bars[1].timestamp,
        actionableIndex: 1,
        detectedTime: bars[0].timestamp,
        detectedIndex: 0,
        price: 10.7,
        lineId: 'down-line',
        lineValue: 10.2,
        structurePivotId: 'LOCAL-HIGH',
        structurePivotPrice: 10.6,
        triggerPath: 'TWO_BAR_RETEST',
        reasonCodes: ['SECOND_CLOSE_ABOVE'],
      },
    ], bars)

    expect(markers).toHaveLength(1)
    expect(markers[0]).toEqual(expect.objectContaining({
      kind: 'buy',
      price: 10.8,
      lineValue: 10.2,
      structurePivotPrice: 10.6,
      reasonCodes: ['LINE_AND_NEAREST_LEVEL_BROKEN'],
    }))
  })

  it('presents a replay-failed strict signal as a false breakout', () => {
    const markers = toChartMarkers([{
      side: 'BUY',
      stage: 'TRIGGER',
      actionableTime: bars[1].timestamp,
      actionableIndex: 1,
      detectedTime: bars[0].timestamp,
      detectedIndex: 0,
      price: 10.8,
      lineId: 'down-line',
      lineValue: 10.2,
      structurePivotId: 'LEVEL:HIGH',
      structurePivotPrice: 10.6,
      triggerPath: 'DIRECT_STRUCTURE',
      reasonCodes: ['LINE_AND_NEAREST_LEVEL_BROKEN'],
      signalQuality: {
        entryQuality: 'WEAK',
        replayOutcome: 'FAILED',
        score: 31,
        summary: 'buy signal failed',
        reasonCodes: ['FOLLOW_THROUGH_FAILED', 'FELL_BACK_UNDER_STRUCTURE'],
      },
    }], bars)

    expect(markers).toHaveLength(1)
    expect(markers[0]).toEqual(expect.objectContaining({
      kind: 'neutral',
      label: 'F',
      above: true,
      title: '\u5047\u7a81\u7834\uff08\u539f\u4e70\u70b9\uff09',
      confidence: '\u56de\u653e\u786e\u8ba4\u5931\u8d25',
      price: 10.8,
    }))
  })

  it('keeps cross-session two-bar retest confirmations with stable marker labels', () => {
    const markers = toChartMarkers([
      {
        side: 'BUY',
        stage: 'TRIGGER',
        actionableTime: '2026-07-21T09:45:00-04:00',
        detectedTime: '2026-07-20T15:45:00-04:00',
        price: 194.8,
        lineId: 'cross-session-down-line',
        lineValue: 193.9,
        structurePivotId: 'FIRST-ACCEPTANCE-HIGH',
        structurePivotPrice: 194.3,
        triggerPath: 'TWO_BAR_RETEST',
        reasonCodes: [
          'SECOND_CLOSE_ABOVE',
          'HIGHER_SECOND_CLOSE',
          'FIRST_ACCEPTANCE_HIGH_BROKEN',
        ],
      },
      {
        side: 'SELL',
        stage: 'CONFIRMED',
        actionableTime: '2026-07-23T09:40:00-04:00',
        detectedTime: '2026-07-22T15:55:00-04:00',
        price: 223.7,
        lineId: 'cross-session-up-line',
        lineValue: 224.1,
        structurePivotId: 'FIRST-ACCEPTANCE-LOW',
        structurePivotPrice: 224.0,
        triggerPath: 'TWO_BAR_RETEST',
        reasonCodes: [
          'SECOND_CLOSE_BELOW',
          'LOWER_SECOND_CLOSE',
          'FIRST_ACCEPTANCE_LOW_BROKEN',
        ],
      },
      {
        side: 'BUY',
        stage: 'TRIGGER',
        actionableTime: '2026-07-21T09:35:00-04:00',
        detectedTime: '2026-07-20T15:45:00-04:00',
        price: 194.1,
        lineId: 'cross-session-down-line',
        lineValue: 193.9,
        structurePivotId: 'FIRST-ACCEPTANCE-HIGH',
        structurePivotPrice: 194.3,
        triggerPath: 'TWO_BAR_RETEST',
        reasonCodes: ['SECOND_CLOSE_ABOVE', 'HIGHER_SECOND_CLOSE'],
      },
    ])

    expect(markers).toHaveLength(2)
    expect(markers[0]).toEqual(expect.objectContaining({
      date: '2026-07-21T09:45:00-04:00',
      kind: 'buy',
      label: 'B',
      title: '买点',
      structurePivotPrice: 194.3,
    }))
    expect(markers[1]).toEqual(expect.objectContaining({
      date: '2026-07-23T09:40:00-04:00',
      kind: 'sell',
      label: 'S',
      title: '卖点',
      structurePivotPrice: 224.0,
    }))
  })

  it('maps signal-local causal trend lines and ignores future anchors', () => {
    const mapped = toSignalPriceLines([
      {
        date: bars[1].timestamp,
        kind: 'buy',
        price: 10.6,
        lineValue: 10.2,
        lineRole: 'ACCELERATION',
        lineAnchorTimes: [bars[0].timestamp, bars[1].timestamp],
        lineAnchorPrices: [9.6, 10],
        structurePivotTime: bars[0].timestamp,
        structurePivotPrice: 10.5,
      },
      {
        date: bars[0].timestamp,
        kind: 'sell',
        price: 9.8,
        lineValue: 9.7,
        lineAnchorTimes: [bars[0].timestamp, bars[1].timestamp],
        lineAnchorPrices: [10.2, 10],
      },
    ])

    expect(mapped).toEqual([
      expect.objectContaining({
        start: bars[0].timestamp,
        end: bars[1].timestamp,
        value: 9.6,
        endValue: 10.2,
        label: '买点趋势线',
        lineType: 'dashed',
      }),
      expect.objectContaining({
        start: bars[0].timestamp,
        end: bars[1].timestamp,
        value: 10.5,
        endValue: 10.5,
        label: '前高/压力位',
        lineType: 'dotted',
      }),
    ])
  })
})

describe('Dow monitor detail dialog', () => {
  beforeEach(() => {
    testState.detail = { data: detail, isLoading: false, isError: false, isFetching: false }
    testState.detailCalls = []
    vi.stubGlobal('scrollTo', vi.fn())
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 420 })
  })

  it('opens from a compact card and exposes the shared indicator controls', async () => {
    const user = userEvent.setup()
    render(<DowMonitor />)

    await user.click(screen.getByRole('button', { name: /打开 01347\.HK/ }))

    const dialog = screen.getByRole('dialog', { name: '01347.HK 完整K线' })
    for (const label of ['成交量', 'MACD', 'RSI', 'KDJ', 'BOLL']) {
      expect(within(dialog).getByRole('button', { name: label })).toBeInTheDocument()
    }
    expect(within(dialog).getByText('量比')).toBeInTheDocument()
    expect(within(dialog).getByLabelText('量能对比周期')).toHaveDisplayValue('前1日均量')
    expect(within(dialog).getByText('买入')).toBeInTheDocument()
    expect(within(dialog).getByText('首次突破趋势线')).toBeInTheDocument()
    expect(within(dialog).getByText(/源 2026/)).toBeInTheDocument()
    expect(screen.getByTestId('intraday-candlestick')).toHaveAttribute(
      'data-price-lines',
      expect.not.stringContaining('"id":"long-term"'),
    )
    expect(screen.getByTestId('intraday-candlestick')).toHaveAttribute(
      'data-price-lines',
      expect.stringContaining('"label":"买点趋势线"'),
    )
    expect(screen.getByTestId('intraday-candlestick')).toHaveAttribute(
      'data-markers',
      expect.stringContaining('"price":10.6'),
    )
    await user.click(within(dialog).getByRole('button', { name: '全屏查看' }))
    expect(within(dialog).getByRole('button', { name: '退出全屏' })).toBeInTheDocument()
  })

  it('uses one trend-line switch for intraday and daily charts', async () => {
    const user = userEvent.setup()
    render(
      <DowMonitorDetailDialog
        symbol="01347.HK"
        timeframe="5m"
        open
        onClose={vi.fn()}
      />,
    )

    const lineSwitch = screen.getByRole('switch', { name: '显示趋势线和压力线' })
    expect(lineSwitch).toBeChecked()
    expect(screen.getByTestId('intraday-candlestick')).toHaveAttribute(
      'data-price-lines',
      expect.not.stringMatching(/^\[\]$/),
    )

    await user.click(lineSwitch)
    expect(lineSwitch).not.toBeChecked()
    expect(screen.getByTestId('intraday-candlestick')).toHaveAttribute('data-price-lines', '[]')

    await user.click(screen.getByRole('button', { name: '日K' }))
    expect(screen.getByTestId('daily-candlestick')).toHaveAttribute('data-price-lines', '[]')
  })

  it('isolates the head-and-shoulders switch from Dow markers and trend lines', async () => {
    const user = userEvent.setup()
    render(
      <DowMonitorDetailDialog
        symbol="01347.HK"
        timeframe="5m"
        open
        onClose={vi.fn()}
      />,
    )

    const shapeSwitch = screen.getByRole('switch', { name: '头肩形态' })
    const chart = screen.getByTestId('intraday-candlestick')
    const dowMarkers = chart.getAttribute('data-markers')
    const dowLines = chart.getAttribute('data-price-lines')

    expect(shapeSwitch).toBeChecked()
    expect(chart).toHaveAttribute(
      'data-head-shoulders-overlays',
      expect.stringContaining('"id":"hs-bottom-confirmed"'),
    )

    await user.click(shapeSwitch)

    expect(shapeSwitch).not.toBeChecked()
    expect(chart).toHaveAttribute('data-head-shoulders-overlays', '[]')
    expect(chart).toHaveAttribute('data-markers', dowMarkers)
    expect(chart).toHaveAttribute('data-price-lines', dowLines)
  })

  it('queries only the selected detail timeframe and uses the daily chart framework for day', async () => {
    const user = userEvent.setup()
    render(
      <DowMonitorDetailDialog
        symbol="01347.HK"
        timeframe="5m"
        open
        onClose={vi.fn()}
      />,
    )

    expect(testState.detailCalls.at(-1)).toEqual(['01347.HK', '5m'])
    await user.click(screen.getByRole('button', { name: '15分' }))
    expect(testState.detailCalls.at(-1)).toEqual(['01347.HK', '15m'])
    await user.click(screen.getByRole('button', { name: '日K' }))
    expect(testState.detailCalls.at(-1)).toEqual(['01347.HK', 'day'])
    expect(screen.getByTestId('daily-candlestick')).toBeInTheDocument()
    expect(screen.queryByTestId('intraday-candlestick')).not.toBeInTheDocument()
  })

  it('closes with Escape, retains page filters, restores scroll, and returns focus', async () => {
    const user = userEvent.setup()
    render(<DowMonitor />)

    await user.click(screen.getByRole('button', { name: '港股' }))
    await user.click(screen.getByRole('button', { name: '仅买点' }))
    const opener = screen.getByRole('button', { name: /打开 01347\.HK/ })
    await user.click(opener)
    await user.keyboard('{Escape}')

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '港股' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: '仅买点' })).toHaveAttribute('aria-pressed', 'true')
    expect(window.scrollTo).toHaveBeenCalledWith({ top: 420, behavior: 'auto' })
    expect(opener).toHaveFocus()
  })

  it('shows loading, error, stale, and paused non-tradable states without promoting old data', () => {
    testState.detail = { data: undefined, isLoading: true, isError: false, isFetching: true }
    const { rerender } = render(
      <DowMonitorDetailDialog
        symbol="01347.HK"
        timeframe="5m"
        open
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByText('正在加载 5分 K线…')).toBeInTheDocument()

    testState.detail = { data: detail, isLoading: false, isError: true, isFetching: false }
    rerender(
      <DowMonitorDetailDialog
        symbol="01347.HK"
        timeframe="5m"
        open
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('详情连接失败')
    expect(screen.getByTestId('dow-detail-state')).toHaveAttribute('data-tradable', 'false')

    testState.detail = {
      data: { ...detail, freshness_state: 'STALE_DATA' },
      isLoading: false,
      isError: false,
      isFetching: false,
    }
    rerender(
      <DowMonitorDetailDialog
        symbol="01347.HK"
        timeframe="5m"
        open
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByTestId('dow-detail-state')).toHaveAttribute('data-tradable', 'false')
    expect(screen.getByText('数据延迟 · 不可交易')).toBeInTheDocument()

    testState.detail = {
      data: { ...detail, freshness_state: 'ANALYSIS_PAUSED' },
      isLoading: false,
      isError: false,
      isFetching: false,
    }
    rerender(
      <DowMonitorDetailDialog
        symbol="01347.HK"
        timeframe="5m"
        open
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByTestId('dow-detail-state')).toHaveAttribute('data-tradable', 'false')
    expect(screen.getByText('分析暂停 · 不可交易')).toBeInTheDocument()
  })
})
