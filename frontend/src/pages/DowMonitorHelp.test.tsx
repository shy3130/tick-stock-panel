import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { DowMonitorHelp } from './DowMonitorHelp'

function renderHelp(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <DowMonitorHelp />
    </MemoryRouter>,
  )
}

describe('DowMonitorHelp', () => {
  it('explains the grouped indicators in a structured reading order', () => {
    renderHelp('/dow-monitor/help?market=cn')

    expect(screen.getByRole('heading', { level: 1, name: '趋势监控指标说明' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '返回趋势监控' }))
      .toHaveAttribute('href', '/dow-monitor?market=cn')

    for (const heading of [
      '快速决策路径',
      '趋势 / 位置',
      '动能 / 涨速',
      '量价 / 资金',
      '突破 / 风险',
      '典型组合场景',
      '指标速查表',
    ]) {
      expect(screen.getByRole('heading', { name: heading })).toBeInTheDocument()
    }

    expect(screen.getAllByText('实时').length).toBeGreaterThanOrEqual(5)
    expect(screen.getAllByText('稳').length).toBeGreaterThanOrEqual(8)
    expect(screen.getByText(/周期确认.*0\/2、1\/2、2\/2/)).toBeInTheDocument()
    expect(screen.getByText(/ATR14 越大表示短线波动风险越高/)).toBeInTheDocument()
    expect(screen.getByText(/五档是当前挂单结构/)).toBeInTheDocument()
    expect(screen.getByText(/VWAP.*不是用户持仓成本/)).toBeInTheDocument()
    expect(screen.getByText(/资金流入占比.*不是逐笔主动买入占比/)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '日内位置' })).toBeInTheDocument()
    expect(screen.getByText(/限制在0到100/)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '数据时效' })).toBeInTheDocument()
    expect(screen.getByText(/行情、盘口、1m K线和分析分别显示自己的数据年龄/)).toBeInTheDocument()
    expect(screen.getByText(/实时指标不能生成、翻转或升级正式信号/)).toBeInTheDocument()
  })

  it('falls back invalid markets to hk and exposes keyboard-friendly section links', () => {
    renderHelp('/dow-monitor/help?market=invalid')

    expect(screen.getByRole('link', { name: '返回趋势监控' }))
      .toHaveAttribute('href', '/dow-monitor?market=hk')

    const navigation = screen.getByRole('navigation', { name: '指标说明目录' })
    expect(within(navigation).getByRole('link', { name: '趋势 / 位置' }))
      .toHaveAttribute('href', '#trend-position')
    expect(within(navigation).getByRole('link', { name: '突破 / 风险' }))
      .toHaveAttribute('href', '#breakout-risk')
    expect(screen.getByTestId('indicator-reference-scroll')).toHaveClass('overflow-x-auto')
    expect(screen.getByTestId('dow-monitor-help-page')).toHaveClass('overflow-x-clip')
  })
})
