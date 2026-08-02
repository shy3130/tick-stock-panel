import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/lib/api'

import { DowMonitorHalfHourAiButton } from './DowMonitorHalfHourAiButton'
import type { DowMonitorHalfHourAiSummary } from './types'


vi.mock('@/lib/api', () => ({
  api: {
    dowMonitorAiHistory: vi.fn(),
    dowMonitorAiDetail: vi.fn(),
    dowMonitorAiRerunStatus: vi.fn(),
    rerunDowMonitorAi: vi.fn(),
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
const queuedRequest = {
  request_id: 'rerun-1',
  analysis_id: 'analysis-2',
  market: 'us' as const,
  symbol: 'RNG.US',
  trade_date: '2026-07-31',
  window_end: '2026-07-31T14:00:00Z',
  data_cutoff: '2026-07-31T14:00:00Z',
  status: 'queued' as const,
  requested_at: '2026-08-02T02:00:00Z',
  started_at: null,
  completed_at: null,
  updated_at: '2026-08-02T02:00:00Z',
  error_code: null,
  error_message: null,
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
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.dowMonitorAiRerunStatus).mockResolvedValue({ request: null })
    vi.mocked(api.rerunDowMonitorAi).mockResolvedValue({
      request: queuedRequest,
      deduplicated: false,
    })
  })

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

  it('reruns the selected hourly report and disables duplicate submission', async () => {
    const second = {
      ...historyItem,
      analysis_id: 'analysis-2',
      window_end: '2026-07-31T14:00:00Z',
      title: '第二阶段',
    }
    vi.mocked(api.dowMonitorAiHistory).mockResolvedValue({
      analyses: [historyItem, second],
    })
    vi.mocked(api.dowMonitorAiDetail).mockImplementation(
      async (_symbol, analysisId) => ({
        ...(analysisId === 'analysis-2' ? second : historyItem),
        data_cutoff: analysisId === 'analysis-2'
          ? '2026-07-31T14:00:00Z'
          : '2026-07-31T15:00:00Z',
        conclusion: '当前报告继续显示。',
        evidence: [],
        risks: [],
        scenarios: [],
        data_quality: [],
        report: null,
      }),
    )
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderButton()

    fireEvent.click(screen.getByRole('button', { name: '查看 RNG.US 盘中AI分析' }))
    fireEvent.click(await screen.findByRole('button', { name: '22:00' }))
    await waitFor(() => {
      expect(api.dowMonitorAiDetail).toHaveBeenCalledWith('RNG.US', 'analysis-2')
    })
    await screen.findByText('第二阶段')

    fireEvent.click(screen.getByRole('button', { name: '重跑AI分析' }))

    expect(confirm).toHaveBeenCalledWith(
      '将重新分析当前时间点。新报告成功后会替换当前报告，是否继续？',
    )
    await waitFor(() => {
      expect(api.rerunDowMonitorAi).toHaveBeenCalledWith('RNG.US', 'analysis-2')
    })
    expect(await screen.findByRole('button', { name: '排队中' })).toBeDisabled()
    expect(screen.getByText('当前报告继续显示。')).toBeInTheDocument()
  })

  it('keeps legacy half-hour reports read-only', async () => {
    const legacy = {
      ...historyItem,
      report_frequency: 'half_hour' as const,
    }
    vi.mocked(api.dowMonitorAiHistory).mockResolvedValue({ analyses: [legacy] })
    vi.mocked(api.dowMonitorAiDetail).mockResolvedValue({
      ...legacy,
      data_cutoff: '2026-07-31T15:00:00Z',
      conclusion: '历史报告保持可读。',
      evidence: [],
      risks: [],
      scenarios: [],
      data_quality: [],
      report: null,
    })
    renderButton()

    fireEvent.click(screen.getByRole('button', { name: '查看 RNG.US 盘中AI分析' }))

    expect(await screen.findByText('历史报告保持可读。')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '重跑AI分析' })).not.toBeInTheDocument()
    expect(api.dowMonitorAiRerunStatus).not.toHaveBeenCalled()
  })

  it('preserves a failed report and exposes a retry action', async () => {
    vi.mocked(api.dowMonitorAiHistory).mockResolvedValue({ analyses: [historyItem] })
    vi.mocked(api.dowMonitorAiDetail).mockResolvedValue({
      ...historyItem,
      data_cutoff: '2026-07-31T15:00:00Z',
      conclusion: '失败后仍显示原报告。',
      evidence: [],
      risks: [],
      scenarios: [],
      data_quality: [],
      report: null,
    })
    vi.mocked(api.dowMonitorAiRerunStatus).mockResolvedValue({
      request: {
        ...queuedRequest,
        analysis_id: 'analysis-1',
        status: 'failed',
        error_code: 'provider_error',
        error_message: '模型暂时不可用',
      },
    })
    renderButton()

    fireEvent.click(screen.getByRole('button', { name: '查看 RNG.US 盘中AI分析' }))

    expect(await screen.findByText('失败后仍显示原报告。')).toBeInTheDocument()
    expect(await screen.findByText(
      /重跑失败，可再次尝试：模型暂时不可用/,
    )).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重跑AI分析' })).toBeEnabled()
  })
})
