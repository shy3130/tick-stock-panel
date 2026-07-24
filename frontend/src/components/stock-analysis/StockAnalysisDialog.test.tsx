import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ActiveTask, HistoryReport } from '@/lib/stockAnalysisStore'
import { StockAnalysisDialog } from './StockAnalysisDialog'

const copyTextMock = vi.fn()

vi.mock('@/lib/copyText', () => ({
  copyText: (text: string) => copyTextMock(text),
}))

vi.mock('@/lib/stockAnalysisStore', async importOriginal => {
  const actual = await importOriginal<typeof import('@/lib/stockAnalysisStore')>()
  return {
    ...actual,
    closeDialog: vi.fn(),
    minimizeDialog: vi.fn(),
    startAnalysis: vi.fn(),
  }
})

const completedTask: ActiveTask = {
  id: 'task-1',
  symbol: '600519.SH',
  name: '贵州茅台',
  focus: '',
  phase: 'done',
  content: '# 完整报告\n\n**资金状态**：净流入',
  error: '',
  meta: { summary: '测试摘要', close: 1400 },
  createdAt: Date.now(),
}

const streamingTask: ActiveTask = {
  ...completedTask,
  id: 'task-2',
  phase: 'streaming',
}

const historyReport: HistoryReport = {
  id: 'report-1',
  symbol: '600519.SH',
  name: '贵州茅台',
  focus: '',
  content: '# 历史完整报告',
  summary: '历史摘要',
  close: 1398,
  created_at: '2026-07-21T09:30:00+08:00',
}

describe('StockAnalysisDialog report copy action', () => {
  beforeEach(() => {
    copyTextMock.mockReset()
  })

  it('copies completed Markdown and shows success feedback', async () => {
    copyTextMock.mockResolvedValue(true)
    render(<StockAnalysisDialog task={completedTask} mode="active" minimized={false} />)

    await userEvent.click(screen.getByRole('button', { name: '复制报告' }))

    expect(copyTextMock).toHaveBeenCalledWith(completedTask.content)
    expect(screen.getByRole('button', { name: '已复制' })).toBeInTheDocument()
  })

  it('copies a historical report from the same labeled title action', async () => {
    copyTextMock.mockResolvedValue(true)
    render(<StockAnalysisDialog task={historyReport} mode="history" minimized={false} />)

    await userEvent.click(screen.getByRole('button', { name: '复制报告' }))

    expect(copyTextMock).toHaveBeenCalledWith(historyReport.content)
  })

  it('hides the copy action while the report is streaming', () => {
    render(<StockAnalysisDialog task={streamingTask} mode="active" minimized={false} />)

    expect(screen.queryByRole('button', { name: '复制报告' })).not.toBeInTheDocument()
  })

  it('shows failure feedback when all clipboard methods fail', async () => {
    copyTextMock.mockResolvedValue(false)
    render(<StockAnalysisDialog task={completedTask} mode="active" minimized={false} />)

    await userEvent.click(screen.getByRole('button', { name: '复制报告' }))

    expect(screen.getByRole('button', { name: '复制失败' })).toBeInTheDocument()
  })
})
