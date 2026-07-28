import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { MinuteDecisionPanel } from './MinuteDecisionPanel'
import type { DowMinuteDecision } from './types'

const decision: DowMinuteDecision = {
  symbol: '01347.HK',
  market: 'hk',
  decision_minute: '2026-07-27T10:26:00+08:00',
  direction: 'BULLISH',
  direction_label: '偏涨',
  action: 'WATCH_BUY',
  action_label: '买入观察',
  confidence: 72,
  dominant_timeframe: '15m',
  confirmation_timeframes: ['30m', '5m'],
  supporting_reasons: ['15分钟趋势向上', '30分钟结构同步确认'],
  contrary_risks: ['5分钟量能仍需确认'],
  invalidation_conditions: ['跌破 31.20 后取消买入观察'],
  data_status: 'COMPLETE',
  status_label: '分钟决策已完成',
  source_timestamp: '2026-07-27T10:25:58+08:00',
}

describe('MinuteDecisionPanel', () => {
  it('shows the complete server-authored minute decision', () => {
    render(<MinuteDecisionPanel decision={decision} />)

    expect(screen.getByTestId('minute-decision-panel')).toHaveTextContent('偏涨')
    expect(screen.getByText('买入观察')).toBeInTheDocument()
    expect(screen.getByText('72%')).toBeInTheDocument()
    expect(screen.getByText('15分钟主导')).toBeInTheDocument()
    expect(screen.getByText('30分钟确认')).toBeInTheDocument()
    expect(screen.getByText('15分钟趋势向上')).toBeInTheDocument()
    expect(screen.getByText('5分钟量能仍需确认')).toBeInTheDocument()
    expect(screen.getByText('跌破 31.20 后取消买入观察')).toBeInTheDocument()
    expect(screen.getByText('分钟决策已完成')).toBeInTheDocument()
  })

  it('keeps a safe waiting state before the first complete minute', () => {
    render(<MinuteDecisionPanel decision={null} />)

    expect(screen.getByTestId('minute-decision-panel')).toHaveTextContent('等待分钟决策')
    expect(screen.getByText('继续观察')).toBeInTheDocument()
    expect(screen.queryByText('买入观察')).not.toBeInTheDocument()
  })

  it('uses a responsive, wrapping layout without a fixed card width', () => {
    render(<MinuteDecisionPanel decision={decision} />)

    const panel = screen.getByTestId('minute-decision-panel')
    const evidence = screen.getByTestId('minute-decision-evidence')
    expect(panel).toHaveClass('min-w-0')
    expect(panel.className).not.toMatch(/\bw-\[[^\]]+\]/)
    expect(evidence).toHaveClass('grid-cols-1', 'sm:grid-cols-2')
    expect(evidence).toHaveClass('break-words')
  })

  it('shows compact quantitative drivers and the active early-risk warning', () => {
    render(
      <MinuteDecisionPanel
        decision={{
          ...decision,
          summary_text: '高点回落1.44%，主动卖出占优27%，资金流1分钟恶化1500万',
          key_drivers: [
            {
              driver_code: 'HIGH_DRAWDOWN',
              direction: 'BEARISH',
              contribution: -3,
              current_value: 1.44,
              previous_value: null,
              change_value: null,
              unit: '%',
              horizons: ['1m'],
              confirmation: 'CONFIRMED',
              text: '高点回落1.44%',
            },
            {
              driver_code: 'TRADE_SELL_IMBALANCE',
              direction: 'BEARISH',
              contribution: -2,
              current_value: -27,
              previous_value: null,
              change_value: null,
              unit: '%',
              horizons: ['1m'],
              confirmation: 'CONFIRMED',
              text: '主动卖出占优27%',
            },
          ],
          turn_stronger_condition: '重新站上VWAP并连续2分钟主动买入占优',
          turn_weaker_condition: '跌破开盘低点且资金继续流出',
          risk_warning: {
            family: 'OPENING_SURGE_REVERSAL',
            stage: 'WARNING',
            title: '冲高回落风险',
            message: '高点回落1.44%；主动卖出占优27%，资金流1分钟恶化1500万。',
          },
        }}
      />,
    )

    expect(screen.getByText('冲高回落风险')).toBeInTheDocument()
    expect(screen.getAllByText(/高点回落1.44%/).length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('主动卖出占优27%')).toBeInTheDocument()
    expect(screen.getByTestId('minute-decision-panel')).toHaveTextContent(
      '转强：重新站上VWAP',
    )
    expect(screen.getByTestId('minute-decision-panel')).toHaveTextContent(
      '转弱：跌破开盘低点',
    )
  })
})
