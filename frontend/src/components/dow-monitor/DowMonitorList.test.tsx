import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

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
    change_pct: 1.25,
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
  it('renders the decision columns and one background-free intraday line', () => {
    render(
      <DowMonitorList
        items={[item()]}
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
      '价格/涨跌',
      '日内走势',
      '通道',
      '控制线',
      '动量 5m/15m',
      '量比',
      '主动资金',
      '买卖信号',
      '操作',
    ]) {
      expect(screen.getByRole('columnheader', { name: heading })).toBeInTheDocument()
    }
    const sparkline = screen.getByRole('img', { name: '700.HK 当日趋势' })
    expect(within(sparkline).getByTestId('sparkline-line')).toBeInTheDocument()
    expect(sparkline.querySelectorAll('polyline')).toHaveLength(1)
    expect(sparkline.querySelector('rect')).toBeNull()
    expect(screen.getByText('买入确认')).toBeInTheDocument()
    expect(screen.getByText('09:34')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '查看详情 700.HK' })).toHaveTextContent('查看详情')
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
