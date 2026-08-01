import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { DowMonitorAiStageReport } from './DowMonitorAiStageReport'
import type { DowMonitorHalfHourAiAnalysis } from './types'


const analysis: DowMonitorHalfHourAiAnalysis = {
  analysis_id: 'hourly-1',
  market: 'us',
  symbol: 'NBIS.US',
  trade_date: '2026-07-31',
  updated_at: '2026-08-01T04:00:02Z',
  status: 'completed',
  window_end: '2026-08-01T04:00:00Z',
  data_cutoff: '2026-08-01T04:00:00Z',
  report_frequency: 'hourly',
  stage_start: '2026-08-01T03:00:00Z',
  stage_trading_minutes: 60,
  opportunity_change: 'STRENGTHENING',
  title: '尾盘V形修复，但突破未确认',
  summary: '修复力度增强',
  conclusion: '尾段形成修复，但未形成正式突破。',
  evidence: [],
  risks: [],
  scenarios: [],
  data_quality: ['分钟结构完整', '主动资金仍待确认'],
  report: {
    headline: {
      title: '尾盘V形修复，但突破未确认',
      trend_bias: 'TRANSITION',
      opportunity_change: 'STRENGTHENING',
      summary: '本小时先下探后收复，机会较上一阶段增强。',
    },
    stage_path: [
      { period: '15:00-15:25', description: '下探阶段低点', metric_keys: ['stage.low'] },
      { period: '15:25-16:00', description: '持续回升至阶段收盘', metric_keys: ['stage.close'] },
    ],
    hidden_changes: ['连续下跌后出现三段回升', '尾五分钟量能集中'],
    comparison_with_previous: '下降斜率收窄，收盘位置明显抬高。',
    day_overview: '全天仍在下降通道下沿修复，尚未收复日内关键高点。',
    channel: {
      direction: 'TRANSITION',
      maturity: 'FORMING',
      explanation: '原下降通道正在转为修复结构。',
      evidence_metric_keys: ['stage.change_pct'],
    },
    patterns: [{
      name: 'V形修复',
      status: 'CONFIRMED',
      explanation: '阶段低点后收复大部分跌幅。',
      evidence_metric_keys: ['stage.v_recovery_ratio'],
      invalidation_metric_keys: ['stage.low'],
    }],
    volume_capital_interpretation: '尾段放量推动修复，但主动资金尚未形成持续净流入。',
    holding_advice: {
      state: 'HOLD_OBSERVE',
      advice: '持仓者可继续观察前高确认，跌破阶段低点则转防守。',
      conditions: ['站稳阶段前高'],
    },
    watching_advice: {
      state: 'WAIT_CONFIRMATION',
      advice: '未参与者等待放量站稳，不追逐单段反弹。',
      conditions: ['价格与主动资金同步确认'],
    },
    next_stage_conditions: {
      strengthen: ['放量站稳阶段前高'],
      risk: ['量价背离或重新跌回VWAP下方'],
      invalidation: ['跌破阶段低点'],
    },
    confidence: 'MEDIUM',
  },
}

describe('DowMonitorAiStageReport', () => {
  it('renders the approved business-analysis sections in decision order', () => {
    const { container } = render(<DowMonitorAiStageReport analysis={analysis} />)

    expect(screen.getByText('尾盘V形修复，但突破未确认')).toBeInTheDocument()
    expect(screen.getByText(/北京时间 11:00 至 12:00/)).toBeInTheDocument()
    expect(screen.getByText(/60 个交易分钟/)).toBeInTheDocument()
    expect(screen.getByText('V形修复')).toBeInTheDocument()
    expect(screen.getByText(/持仓者可继续观察前高确认/)).toBeInTheDocument()
    expect(screen.getByText(/未参与者等待放量站稳/)).toBeInTheDocument()

    const text = container.textContent ?? ''
    const ordered = [
      '本阶段分钟路径',
      '分钟K线隐藏变化',
      '与上一阶段相比',
      '当日截至当前',
      '通道与形态',
      '量价与资金含义',
      '持仓者建议',
      '未参与者建议',
      '下一阶段条件',
      '数据质量',
    ]
    ordered.reduce((position, label) => {
      const next = text.indexOf(label)
      expect(next).toBeGreaterThan(position)
      return next
    }, -1)
  })
})
