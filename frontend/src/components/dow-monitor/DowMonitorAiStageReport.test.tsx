import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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
  it('shows the decision summary first and keeps complete evidence closed by default', async () => {
    const user = userEvent.setup()
    render(<DowMonitorAiStageReport analysis={analysis} />)

    expect(screen.getByRole('heading', { name: analysis.report!.headline.title })).toBeVisible()
    expect(screen.getByText(/北京时间 11:00 至 12:00/)).toBeVisible()
    expect(screen.getByText(/60 个交易分钟/)).toBeVisible()
    const holderAdvice = screen.getByText(analysis.report!.holding_advice.advice)
    const watcherAdvice = screen.getByText(analysis.report!.watching_advice.advice)
    expect(holderAdvice).toBeVisible()
    expect(watcherAdvice).toBeVisible()
    expect(holderAdvice.closest('section')).toHaveClass('min-w-0')
    expect(watcherAdvice.closest('section')).toHaveClass('min-w-0')
    expect(screen.getByTestId('hourly-ai-stage-report')).toHaveClass('whitespace-normal')
    expect(screen.getByRole('heading', { name: '增强确认' })).toBeVisible()
    expect(screen.getByRole('heading', { name: '风险出现' })).toBeVisible()
    expect(screen.getByRole('heading', { name: '判断失效' })).toBeVisible()

    const disclosure = screen
      .getByText('展开完整分析（分钟路径、形态、量价、数据质量）')
      .closest('details')
    expect(disclosure).not.toHaveAttribute('open')

    await user.click(screen.getByText('展开完整分析（分钟路径、形态、量价、数据质量）'))
    expect(disclosure).toHaveAttribute('open')
    expect(screen.getByRole('heading', { name: '本小时发生了什么' })).toBeVisible()
    expect(screen.getByRole('heading', { name: '当日整体结构与量价资金' })).toBeVisible()
    expect(screen.getByRole('heading', { name: '分析依据与数据质量' })).toBeVisible()

    await user.click(screen.getByText('收起完整分析'))
    expect(disclosure).not.toHaveAttribute('open')
  })

  it('stacks available next-stage groups and omits empty optional blocks', () => {
    const sparse = {
      ...analysis,
      report: {
        ...analysis.report!,
        watching_advice: {
          state: 'WAIT_CONFIRMATION' as const,
          advice: '',
          conditions: [],
        },
        next_stage_conditions: {
          strengthen: ['站稳阶段高点'],
          risk: [],
          invalidation: ['跌破阶段低点'],
        },
      },
    }
    render(<DowMonitorAiStageReport analysis={sparse} />)

    const conditions = screen.getByTestId('next-stage-conditions')
    expect(conditions).toHaveClass('grid-cols-1')
    expect(screen.getByRole('heading', { name: '增强确认' })).toBeVisible()
    expect(screen.queryByRole('heading', { name: '风险出现' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '判断失效' })).toBeVisible()
    expect(screen.queryByRole('heading', { name: '未参与者建议' })).not.toBeInTheDocument()
  })
})
