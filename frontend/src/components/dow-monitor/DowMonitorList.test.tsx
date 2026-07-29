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

describe('DowMonitorList', () => {
  it('renders the nine grouped indicator column headers', () => {
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
      '趋势 / 位置',
      '动量 / 涨速',
      '量价 / 资金',
      '突破 / 风险',
      '买卖信号',
      '操作',
    ]) {
      expect(screen.getByRole('columnheader', { name: new RegExp(heading) })).toBeInTheDocument()
    }
    expect(screen.getAllByRole('columnheader')).toHaveLength(9)
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
    const sparkline = screen.getByRole('img', { name: '700.HK 当日趋势' })
    expect(within(sparkline).getByTestId('sparkline-line')).toBeInTheDocument()
    expect(sparkline.querySelectorAll('polyline')).toHaveLength(1)
    expect(sparkline.querySelector('rect')).toBeNull()
    expect(screen.getByText('成本 +0.19%')).toBeInTheDocument()
    expect(screen.getByText('1m +1.00%')).toBeInTheDocument()
    expect(screen.getByText('确认 2/2')).toBeInTheDocument()
    expect(screen.getByText('高 2.00%')).toBeInTheDocument()
    expect(screen.getByText('低 2.00%')).toBeInTheDocument()
    for (const group of ['trend-position', 'momentum-speed', 'volume-funds', 'breakout-risk']) {
      const cell = screen.getByTestId(`${group}-700.HK`)
      expect(Array.from(cell.children).filter(child => child.tagName === 'DIV')).toHaveLength(2)
    }
    const volumeFundsCell = screen.getByTestId('volume-funds-700.HK')
    expect(within(volumeFundsCell).getByTestId('relative-volume-stable-badge-700.HK').nextElementSibling)
      .toHaveTextContent('量比 1.50×')
    expect(within(volumeFundsCell).getByTestId('active-funds-stable-badge-700.HK').nextElementSibling)
      .toHaveTextContent('主买 60%')
    expect(within(volumeFundsCell).getByTestId('volume-speed-live-badge-700.HK').nextElementSibling)
      .toHaveTextContent('量速 --')
    expect(within(volumeFundsCell).getByTestId('depth-pressure-live-badge-700.HK').nextElementSibling)
      .toHaveTextContent('五档 --')
    expect(screen.getByText('买入确认')).toBeInTheDocument()
    expect(screen.getByText('09:34')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '查看详情 700.HK' })).toHaveTextContent('查看详情')
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
    expect(screen.getByText('成本 --')).toBeInTheDocument()
    expect(screen.getByText('1m --')).toBeInTheDocument()
    expect(screen.getByText('5m --')).toBeInTheDocument()
    expect(screen.getByText('15m --')).toBeInTheDocument()
    expect(screen.getByText('量比 --')).toBeInTheDocument()
    expect(screen.getByText('量速 --')).toBeInTheDocument()
    expect(screen.getByText('主买 未确认')).toBeInTheDocument()
    expect(screen.getByText('五档 --')).toBeInTheDocument()
    expect(screen.getByText('高 --')).toBeInTheDocument()
    expect(screen.getByText('低 --')).toBeInTheDocument()
    expect(screen.getByText('ATR14 --')).toBeInTheDocument()
    for (const group of ['trend-position', 'momentum-speed', 'volume-funds', 'breakout-risk']) {
      expect(screen.getByTestId(`${group}-700.HK`)).not.toHaveTextContent(/(?:\+|-)?0(?:\.0+)?[%×]/)
    }
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

    await user.click(screen.getByRole('row', { name: /腾讯控股/ }))
    await user.click(screen.getByRole('button', { name: '查看详情 700.HK' }))
    await user.click(screen.getByRole('button', { name: '暂停监控 700.HK' }))
    await user.click(screen.getByRole('button', { name: '移除 700.HK' }))

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
