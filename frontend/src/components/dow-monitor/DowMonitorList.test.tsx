import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { RealtimeSymbolState } from '@/lib/realtimeMarketData'

import type { DowMonitorOverviewSymbol } from './types'
import { DowMonitorList } from './DowMonitorList'

function item(
  symbol = '700.HK',
  overrides: Partial<DowMonitorOverviewSymbol> = {},
): DowMonitorOverviewSymbol {
  return {
    symbol,
    market: 'hk',
    enabled: true,
    created_at: '2026-07-29T09:00:00+08:00',
    updated_at: '2026-07-29T09:35:02+08:00',
    name: symbol === '700.HK' ? '腾讯控股' : '测试股票',
    last_price: 500,
    change_pct: 0.0125,
    quote_timestamp: '2026-07-29T09:35:00+08:00',
    analysis_status: 'READY',
    intraday_capital: {
      total_in: 60,
      total_out: 40,
      quality: 'COMPLETE',
    },
    minute_decision: null,
    states: {
      '5m': {
        symbol,
        market: 'hk',
        timeframe: '5m',
        freshness_state: 'LIVE',
        source_timestamp: '2026-07-29T09:35:00+08:00',
        snapshot: {
          bar_time: '2026-07-29T09:35:00+08:00',
          bar_completion: 'FINAL',
          price_to_line_pct: 0.6,
          line_role: 'SUPPORT',
          volume_ratio_20: 1.5,
        },
        chart: {
          bars: [
            {
              index: 0,
              timestamp: '2026-07-29T09:30:00+08:00',
              open: 499,
              high: 500,
              low: 498,
              close: 499,
              volume: 100,
            },
            {
              index: 1,
              timestamp: '2026-07-29T09:35:00+08:00',
              open: 499,
              high: 501,
              low: 499,
              close: 500,
              volume: 120,
            },
          ],
        },
        updated_at: '2026-07-29T09:35:02+08:00',
      },
    },
    latest_notification: {
      notification_id: 'n1',
      event_key: 'e1',
      symbol,
      market: 'hk',
      timeframe: '15m',
      side: 'BUY',
      action_name: '买入确认',
      shape_name: '双重突破',
      triggered_at: '2026-07-29T09:34:00+08:00',
      trigger_price: 499,
      snapshot_payload: {},
      read_at: null,
    },
    last_success_at: '2026-07-29T09:35:02+08:00',
    last_error: null,
    ...overrides,
  }
}

function anomalyItem(): DowMonitorOverviewSymbol {
  const base = item()
  const fiveMinute = base.states['5m']!
  return {
    ...base,
    last_price: 100,
    states: {
      ...base.states,
      '5m': {
        ...fiveMinute,
        chart: {
          bars: Array.from({ length: 12 }, (_, index) => ({
            index,
            timestamp: `2026-07-29T${String(8 + Math.floor((40 + index * 5) / 60)).padStart(2, '0')}:${String((40 + index * 5) % 60).padStart(2, '0')}:00+08:00`,
            open: 100,
            high: 101,
            low: 99,
            close: 100,
            volume: 500,
          })),
        },
      },
    },
  }
}

function anomalyRealtime({
  lastDone = 100,
  high = 102,
  low = 98,
  candleClose = 100,
  candleVolume = 50,
  bidVolume = 100,
  askVolume = 100,
  quoteDelayed = false,
  candlestickDelayed = false,
  depthDelayed = false,
}: {
  lastDone?: number
  high?: number
  low?: number
  candleClose?: number
  candleVolume?: number
  bidVolume?: number
  askVolume?: number
  quoteDelayed?: boolean
  candlestickDelayed?: boolean
  depthDelayed?: boolean
} = {}): RealtimeSymbolState {
  return {
    symbol: '700.HK',
    streamId: 'anomaly-stream',
    sequence: 1,
    eventAt: '2026-07-29T09:35:30+08:00',
    publishedAt: '2026-07-29T09:35:30+08:00',
    quote: {
      lastDone,
      prevClose: 100,
      high,
      low,
      timestamp: '2026-07-29T09:35:30+08:00',
    },
    depth: {
      bids: [{ position: 1, price: 99.9, volume: bidVolume }],
      asks: [{ position: 1, price: 100.1, volume: askVolume }],
      timestamp: '2026-07-29T09:35:30+08:00',
    },
    candlestick: {
      period: 'min_1',
      timestamp: '2026-07-29T09:35:00+08:00',
      open: 100,
      close: candleClose,
      volume: candleVolume,
    },
    quoteDelayed,
    depthDelayed,
    candlestickDelayed,
  }
}

describe('DowMonitorList', () => {
  it('renders the four approved mobile fields separately from the desktop table', () => {
    render(
      <DowMonitorList
        items={[item()]}
        notifications={[]}
        realtimeStates={new Map()}
        selectedSymbol={null}
        page={1}
        pageCount={1}
        total={1}
        onPageChange={vi.fn()}
        onSelect={vi.fn()}
        onToggle={vi.fn()}
        onRemove={vi.fn()}
      />,
    )

    const mobile = screen.getByTestId('dow-monitor-mobile-700.HK')
    expect(mobile).toHaveTextContent('腾讯控股')
    expect(mobile).toHaveTextContent('700.HK')
    expect(mobile).toHaveTextContent('500.00')
    expect(within(mobile).getByLabelText('700.HK 当日趋势')).toBeInTheDocument()
    expect(within(mobile).getByTestId('key-interpretation-mobile')).toBeInTheDocument()
    expect(screen.getByTestId('dow-monitor-mobile-list')).toHaveClass('md:hidden')
    expect(screen.getByTestId('dow-monitor-table-scroll')).toHaveClass(
      'hidden',
      'md:block',
    )
  })

  it('renders the ten grouped columns with interpretation after the intraday line', () => {
    render(
      <DowMonitorList
        items={[]}
        notifications={[]}
        realtimeStates={new Map()}
        selectedSymbol="700.HK"
        page={1}
        pageCount={1}
        total={1}
        nowMs={Date.parse('2026-07-29T09:35:30+08:00')}
        onPageChange={vi.fn()}
        onSelect={vi.fn()}
        onToggle={vi.fn()}
        onRemove={vi.fn()}
      />,
    )

    for (const heading of [
      '股票',
      '价格 / 涨跌',
      '日内走势',
      '重点解读',
      '趋势 / 位置',
      '动量 / 涨速',
      '量价 / 资金',
      '突破 / 风险',
      '买卖信号',
      '半小时分析',
      '操作',
    ]) {
      expect(screen.getByRole('columnheader', { name: new RegExp(heading) })).toBeInTheDocument()
    }
    const headers = screen.getAllByRole('columnheader')
    expect(headers).toHaveLength(11)
    const texts = headers.map(header => header.textContent ?? '')
    expect(texts.indexOf('日内走势')).toBeLessThan(
      texts.findIndex(text => text.includes('重点解读')),
    )
    expect(texts.findIndex(text => text.includes('重点解读'))).toBeLessThan(
      texts.findIndex(text => text.includes('趋势 / 位置')),
    )
    expect(screen.queryByRole('columnheader', { name: '通道' })).not.toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: '主动资金' })).not.toBeInTheDocument()
  })

  it('renders the grouped fields and one background-free intraday line', () => {
    const baseState = item().states['5m']!
    const groupedItem = item('700.HK', {
      states: {
        ...item().states,
        '15m': {
          ...baseState,
          timeframe: '15m',
          snapshot: {
            ...baseState.snapshot,
            price_to_line_pct: 0.42,
          },
          chart: {
            bars: baseState.chart.bars?.map((bar, index) => ({
              ...bar,
              index,
              ma5: bar.close - 0.5,
              ma10: bar.close - 1,
              ma20: bar.close - 2,
            })),
          },
        },
        '30m': {
          ...baseState,
          timeframe: '30m',
          chart: {
            bars: baseState.chart.bars?.map((bar, index) => ({
              ...bar,
              index,
              ma5: bar.close - 0.5,
              ma10: bar.close - 1,
              ma20: bar.close - 2,
            })),
          },
        },
      },
      minute_decision: {
        symbol: '700.HK',
        market: 'hk',
        decision_minute: '2026-07-29T09:35:00+08:00',
        direction: 'BULLISH',
        direction_label: '偏涨',
        action: 'HOLD',
        action_label: '持有',
        confidence: 0.8,
        dominant_timeframe: '15m',
        confirmation_timeframes: ['30m'],
        supporting_reasons: [],
        contrary_risks: [],
        invalidation_conditions: [],
        data_status: 'COMPLETE',
        status_label: '完整',
        source_timestamp: '2026-07-29T09:35:00+08:00',
        daily_summary: {
          as_of_minute: '2026-07-29T09:35:00+08:00',
          direction: 'BULLISH',
          direction_label: '偏涨',
          action: 'HOLD',
          action_label: '持有',
          confidence: 0.8,
          phase_path: [],
          summary_text: '',
          key_evidence: [],
          reversal_condition: '',
          data_status: 'COMPLETE',
          status_label: '完整',
          input_event_ids: [],
          vwap_price: 498.6,
          vwap_distance_pct: 0.19,
        },
        risk_warning: {
          family: 'OPENING_SURGE_REVERSAL',
          stage: 'WARNING',
          title: '高位风险',
          message: '',
        },
      },
    })
    const realtime: RealtimeSymbolState = {
      symbol: '700.HK',
      streamId: 'stream',
      sequence: 1,
      eventAt: '2026-07-29T09:35:30+08:00',
      publishedAt: '2026-07-29T09:35:30+08:00',
      quote: {
        lastDone: 500,
        prevClose: 495,
        high: 510,
        low: 490,
        timestamp: '2026-07-29T09:35:30+08:00',
      },
      depth: {
        bids: [{ position: 1, price: 499, volume: 100 }],
        asks: [{ position: 1, price: 501, volume: 80 }],
        timestamp: '2026-07-29T09:35:25+08:00',
      },
      candlestick: {
        period: 'min_1',
        timestamp: '2026-07-29T09:35:00+08:00',
        open: 500,
        close: 505,
      },
      quoteDelayed: false,
      depthDelayed: false,
      candlestickDelayed: false,
    }
    render(
      <DowMonitorList
        items={[groupedItem]}
        notifications={[]}
        realtimeStates={new Map([['700.HK', realtime]])}
        selectedSymbol="700.HK"
        page={1}
        pageCount={1}
        total={1}
        nowMs={Date.parse('2026-07-29T09:35:30+08:00')}
        onPageChange={vi.fn()}
        onSelect={vi.fn()}
        onToggle={vi.fn()}
        onRemove={vi.fn()}
      />,
    )
    const desktop = screen.getByTestId('dow-monitor-table-scroll')
    const sparkline = within(desktop).getByRole('img', { name: '700.HK 当日趋势' })
    expect(within(sparkline).getByTestId('sparkline-line')).toBeInTheDocument()
    expect(sparkline.querySelectorAll('polyline')).toHaveLength(1)
    expect(sparkline.querySelector('rect')).toBeNull()
    expect(screen.getByText('VWAP 498.60 / +0.19%')).toBeInTheDocument()
    expect(screen.getByText('1m +1.00%')).toBeInTheDocument()
    expect(screen.getByText('周期 2/2')).toBeInTheDocument()
    expect(screen.getByText('15m✓')).toBeInTheDocument()
    expect(screen.getByText('30m✓')).toBeInTheDocument()
    expect(screen.getByText('高 2.00%')).toBeInTheDocument()
    expect(screen.getByText('低 2.00%')).toBeInTheDocument()
    expect(screen.getByText('位置 50')).toBeInTheDocument()
    expect(screen.getByTestId('freshness-700.HK'))
      .toHaveAccessibleName('数据时效，行情0s，盘口5s，1m K线30s，分析30s')
    for (const group of ['trend-position', 'momentum-speed', 'volume-funds', 'breakout-risk']) {
      const cell = screen.getByTestId(`${group}-700.HK`)
      const primaryRow = within(cell).getByTestId(`${group}-primary-row-700.HK`)
      const secondaryRow = within(cell).getByTestId(`${group}-secondary-row-700.HK`)
      expect(cell.children).toHaveLength(2)
      expect(cell.children[0]).toBe(primaryRow)
      expect(cell.children[1]).toBe(secondaryRow)
    }
    const volumeFundsCell = screen.getByTestId('volume-funds-700.HK')
    const relativeVolumeBadge = within(volumeFundsCell).getByTestId('relative-volume-stable-badge-700.HK')
    const capitalInflowBadge = within(volumeFundsCell).getByTestId('capital-inflow-stable-badge-700.HK')
    const volumeSpeedBadge = within(volumeFundsCell).getByTestId('volume-speed-live-badge-700.HK')
    const depthPressureBadge = within(volumeFundsCell).getByTestId('depth-pressure-live-badge-700.HK')
    const momentumBadge = screen.getByText('1m +1.00%').previousElementSibling
    const rangeBadges = screen.getByTestId('breakout-risk-700.HK').querySelectorAll('.text-cyan-300')
    expect(momentumBadge).not.toBeNull()
    if (!momentumBadge) throw new Error('Missing live badge before 1m momentum')
    expect(momentumBadge).toHaveClass('text-cyan-300')
    expect(momentumBadge).toHaveTextContent('实时')
    expect(momentumBadge.nextElementSibling).toHaveTextContent('1m +1.00%')
    expect(rangeBadges).toHaveLength(2)
    expect(rangeBadges[0]).toHaveTextContent('实时')
    expect(rangeBadges[1]).toHaveTextContent('实时')
    expect(rangeBadges[0].nextElementSibling).toHaveTextContent(/高.*2.00%/)
    expect(rangeBadges[1].nextElementSibling).toHaveTextContent(/低.*2.00%/)
    expect(relativeVolumeBadge).toHaveTextContent('稳')
    expect(capitalInflowBadge).toHaveTextContent('稳')
    expect(volumeSpeedBadge).toHaveTextContent('实时')
    expect(depthPressureBadge).toHaveTextContent('实时')
    expect(relativeVolumeBadge.nextElementSibling)
      .toHaveTextContent('量比 1.50×')
    expect(capitalInflowBadge.nextElementSibling)
      .toHaveTextContent('资金流入 60%')
    expect(volumeSpeedBadge.nextElementSibling)
      .toHaveTextContent('量速 --')
    expect(depthPressureBadge.nextElementSibling)
      .toHaveTextContent('五档 +11.11%')
    expect(screen.getByText('买入确认')).toBeInTheDocument()
    expect(screen.getByText('北京时间 09:34')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '查看详情 700.HK' })).toHaveTextContent('查看详情')
    const interpretation = screen.getByTestId('key-interpretation')
    expect(interpretation.querySelectorAll('[data-interpretation-line]')).toHaveLength(3)
    expect(interpretation).toHaveClass('min-w-[320px]')
    expect(within(interpretation).queryByText(/量比|量速|五档/)).not.toBeInTheDocument()
  })

  it('keeps missing grouped values explicit instead of rendering zeroes', () => {
    render(
      <DowMonitorList
        items={[item('700.HK', { states: {}, intraday_capital: null, minute_decision: null })]}
        notifications={[]}
        realtimeStates={new Map()}
        selectedSymbol={null}
        page={1}
        pageCount={1}
        total={1}
        nowMs={Date.parse('2026-07-29T09:35:30+08:00')}
        onPageChange={vi.fn()}
        onSelect={vi.fn()}
        onToggle={vi.fn()}
        onRemove={vi.fn()}
      />,
    )

    expect(screen.getByText('控制 --')).toBeInTheDocument()
    expect(screen.getByText('VWAP --')).toBeInTheDocument()
    expect(screen.getByText('1m --')).toBeInTheDocument()
    expect(screen.getByText('5m --')).toBeInTheDocument()
    expect(screen.getByText('15m --')).toBeInTheDocument()
    expect(screen.getByText('量比 --')).toBeInTheDocument()
    expect(screen.getByText('量速 --')).toBeInTheDocument()
    expect(screen.getByText('资金流入 未确认')).toBeInTheDocument()
    expect(screen.getByText('五档 --')).toBeInTheDocument()
    expect(screen.getByText('高 --')).toBeInTheDocument()
    expect(screen.getByText('低 --')).toBeInTheDocument()
    expect(screen.getByText('ATR14 --')).toBeInTheDocument()
    expect(screen.getByText('振幅/ATR --')).toBeInTheDocument()
    expect(screen.getByText('位置 --')).toBeInTheDocument()
    expect(screen.getByTestId('freshness-700.HK'))
      .toHaveAccessibleName('数据时效，行情30s，盘口--，1m K线--，分析28s')
    for (const group of ['trend-position', 'momentum-speed', 'volume-funds', 'breakout-risk']) {
      expect(screen.getByTestId(`${group}-700.HK`)).not.toHaveTextContent(/(?:\+|-)?0(?:\.0+)?[%×]/)
    }
  })

  it('highlights only six suddenly changed metric values and keeps the formal signal', () => {
    const monitorItem = anomalyItem()
    const initialRealtime = anomalyRealtime()
    const changedRealtime = anomalyRealtime({
      lastDone: 100.5,
      high: 102,
      low: 97.5,
      candleClose: 100.4,
      candleVolume: 100,
      bidVolume: 140,
      askVolume: 60,
    })
    const props = {
      items: [monitorItem],
      notifications: [],
      selectedSymbol: null,
      page: 1,
      pageCount: 1,
      total: 1,
      nowMs: Date.parse('2026-07-29T09:35:30+08:00'),
      onPageChange: vi.fn(),
      onSelect: vi.fn(),
      onToggle: vi.fn(),
      onRemove: vi.fn(),
    }
    const { rerender } = render(
      <DowMonitorList
        {...props}
        realtimeStates={new Map([['700.HK', initialRealtime]])}
      />,
    )

    expect(screen.queryByText('异动')).not.toBeInTheDocument()

    rerender(
      <DowMonitorList
        {...props}
        realtimeStates={new Map([['700.HK', changedRealtime]])}
      />,
    )

    for (const [metric, label] of [
      ['changePct', '涨跌幅'],
      ['momentum1m', '1m 涨速'],
      ['volumeSpeed', '1m 量速'],
      ['depthPressurePct', '五档盘口'],
      ['toDayHighPct', '距日高'],
      ['fromDayLowPct', '距日低'],
    ]) {
      const highlight = screen.getByTestId(`anomaly-${metric}-700.HK`)
      expect(highlight).toHaveClass('border-danger', 'bg-danger/10', 'text-danger')
      expect(highlight).toHaveAccessibleName(new RegExp(`${label}.*突发异动`))
    }
    const desktop = screen.getByTestId('dow-monitor-table-scroll')
    expect(within(desktop).getAllByText('异动')).toHaveLength(7)
    expect(screen.getByTestId('key-interpretation')).toHaveTextContent('待确认')
    expect(screen.getByRole('row', { name: /腾讯控股/ })).not.toHaveClass('bg-danger/10')
    for (const group of ['momentum-speed', 'volume-funds', 'breakout-risk']) {
      expect(screen.getByTestId(`${group}-700.HK`)).not.toHaveClass('bg-danger/10')
    }
    expect(screen.getByText('买入确认')).toBeInTheDocument()
    expect(screen.getByText('北京时间 09:34')).toBeInTheDocument()
  })

  it('does not highlight delayed changes or the first recovered values', () => {
    const monitorItem = anomalyItem()
    const props = {
      items: [monitorItem],
      notifications: [],
      selectedSymbol: null,
      page: 1,
      pageCount: 1,
      total: 1,
      nowMs: Date.parse('2026-07-29T09:35:30+08:00'),
      onPageChange: vi.fn(),
      onSelect: vi.fn(),
      onToggle: vi.fn(),
      onRemove: vi.fn(),
    }
    const changed = {
      lastDone: 101,
      high: 102,
      low: 97,
      candleClose: 101,
      candleVolume: 150,
      bidVolume: 180,
      askVolume: 20,
    }
    const { rerender } = render(
      <DowMonitorList
        {...props}
        realtimeStates={new Map([['700.HK', anomalyRealtime()]])}
      />,
    )

    rerender(
      <DowMonitorList
        {...props}
        realtimeStates={new Map([['700.HK', anomalyRealtime({
          ...changed,
          quoteDelayed: true,
          candlestickDelayed: true,
          depthDelayed: true,
        })]])}
      />,
    )
    expect(screen.queryByText('异动')).not.toBeInTheDocument()

    rerender(
      <DowMonitorList
        {...props}
        realtimeStates={new Map([['700.HK', anomalyRealtime(changed)]])}
      />,
    )
    expect(screen.queryByText('异动')).not.toBeInTheDocument()
    expect(screen.getByText('买入确认')).toBeInTheDocument()
  })

  it('renders every signal occurrence in Beijing time', () => {
    render(
      <DowMonitorList
        items={[item('NBIS.US', {
          market: 'us',
          latest_notification: {
            ...item().latest_notification!,
            symbol: 'NBIS.US',
            market: 'us',
            side: 'SELL',
            action_name: '卖出确认',
            triggered_at: '2026-07-29T16:15:00.313318Z',
          },
        })]}
        notifications={[]}
        realtimeStates={new Map()}
        selectedSymbol={null}
        page={1}
        pageCount={1}
        total={1}
        nowMs={Date.parse('2026-07-30T00:16:00+08:00')}
        onPageChange={vi.fn()}
        onSelect={vi.fn()}
        onToggle={vi.fn()}
        onRemove={vi.fn()}
      />,
    )

    expect(screen.getByText('北京时间 00:15')).toBeInTheDocument()
    expect(screen.queryByText('16:15')).not.toBeInTheDocument()
  })

  it('selects from the row or detail action and keeps management controls outside the action column', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    const onToggle = vi.fn()
    const onRemove = vi.fn()
    render(
      <DowMonitorList
        items={[item()]}
        notifications={[]}
        realtimeStates={new Map()}
        selectedSymbol={null}
        page={1}
        pageCount={1}
        total={1}
        nowMs={Date.parse('2026-07-29T09:35:30+08:00')}
        onPageChange={vi.fn()}
        onSelect={onSelect}
        onToggle={onToggle}
        onRemove={onRemove}
      />,
    )

    const desktop = screen.getByTestId('dow-monitor-table-scroll')
    await user.click(within(desktop).getByRole('row', { name: /腾讯控股/ }))
    await user.click(within(desktop).getByRole('button', { name: '查看详情 700.HK' }))
    await user.click(within(desktop).getByRole('button', { name: '暂停监控 700.HK' }))
    await user.click(within(desktop).getByRole('button', { name: '移除 700.HK' }))

    expect(onSelect).toHaveBeenCalledTimes(2)
    expect(onSelect).toHaveBeenLastCalledWith('700.HK')
    expect(onToggle).toHaveBeenCalledWith('700.HK', false)
    expect(onRemove).toHaveBeenCalledWith('700.HK')
  })

  it('shows delayed state and changes pages through the pager', async () => {
    const user = userEvent.setup()
    const onPageChange = vi.fn()
    render(
      <DowMonitorList
        items={[item('1.HK')]}
        notifications={[]}
        realtimeStates={new Map()}
        selectedSymbol={null}
        page={2}
        pageCount={3}
        total={45}
        forceDelayed
        nowMs={Date.parse('2026-07-29T09:40:00+08:00')}
        onPageChange={onPageChange}
        onSelect={vi.fn()}
        onToggle={vi.fn()}
        onRemove={vi.fn()}
      />,
    )

    expect(screen.getByText('数据延迟')).toBeInTheDocument()
    expect(screen.getByText('第 2 / 3 页 · 共 45 只')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '下一页' }))
    expect(onPageChange).toHaveBeenCalledWith(3)
  })
})
