import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { api } from '@/lib/api'

import { DowMonitorHalfHourAiButton } from './DowMonitorHalfHourAiButton'
import type { DowMonitorHalfHourAiSummary } from './types'


vi.mock('@/lib/api', () => ({
  api: {
    dowMonitorAiHistory: vi.fn(),
    dowMonitorAiDetail: vi.fn(),
  },
}))

const summary: DowMonitorHalfHourAiSummary = {
  analysis_id: 'analysis-1',
  status: 'completed',
  window_end: '2026-07-31T15:00:00',
  report_frequency: 'hourly',
  stage_start: '2026-07-31T14:00:00',
  stage_trading_minutes: 60,
  opportunity_change: 'STRENGTHENING',
  title: '量价仍待确认',
  summary: '价格回升但资金持续性不足',
}
const historyItem = {
  ...summary,
  market: 'us' as const,
  symbol: 'RNG.US',
  trade_date: '2026-07-31',
  updated_at: '2026-07-31T15:00:02',
}

function renderButton() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <DowMonitorHalfHourAiButton symbol="RNG.US" latest={summary} />
    </QueryClientProvider>,
  )
}

describe('DowMonitorHalfHourAiButton', () => {
  it('keeps the overview light and loads long content only after opening', async () => {
    vi.mocked(api.dowMonitorAiHistory).mockResolvedValue({ analyses: [historyItem] })
    vi.mocked(api.dowMonitorAiDetail).mockResolvedValue({
      ...historyItem,
      data_cutoff: '2026-07-31T15:00:00',
      conclusion: '价格回升，但资金证据尚未同步。',
      evidence: [],
      risks: ['样本有限'],
      scenarios: [],
      data_quality: ['数据完整'],
      report: null,
    })
    renderButton()

    expect(screen.getByText('量价仍待确认')).toBeInTheDocument()
    expect(screen.getByText(/北京时间 23:00/)).toBeInTheDocument()
    expect(api.dowMonitorAiHistory).not.toHaveBeenCalled()
    expect(api.dowMonitorAiDetail).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '查看 RNG.US 盘中AI分析' }))

    await waitFor(() => {
      expect(api.dowMonitorAiHistory).toHaveBeenCalledWith(
        'RNG.US',
        '2026-07-31',
      )
    })
    await waitFor(() => expect(api.dowMonitorAiDetail).toHaveBeenCalled())
    expect(screen.getByRole('dialog', { name: 'RNG.US 盘中AI阶段分析' }))
      .toBeInTheDocument()
    expect(screen.getByText('价格回升，但资金证据尚未同步。'))
      .toBeInTheDocument()
    expect(screen.getAllByText('23:00').length).toBeGreaterThan(0)
    expect(screen.getByText(/截止 2026-07-31 23:00/)).toBeInTheDocument()
  })
})
