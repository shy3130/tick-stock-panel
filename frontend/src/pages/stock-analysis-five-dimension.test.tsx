import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MarketScopeProvider } from '@/lib/market-scope'
import { StockAnalysis } from './StockAnalysis'

const startAnalysis = vi.fn()

vi.mock('@/lib/stockAnalysisStore', () => ({
  startAnalysis: (...args: unknown[]) => startAnalysis(...args),
  findTodayReport: vi.fn().mockResolvedValue(null),
  useHistoryReports: () => ({ reports: [], loaded: true }),
  deleteReport: vi.fn(),
  openHistoryReport: vi.fn(),
  loadHistory: vi.fn(),
}))

vi.mock('@/components/financials/StockFinancialSearch', () => ({
  StockFinancialSearch: ({ onSelect }: { onSelect: (symbol: string, name: string) => void }) => (
    <button onClick={() => onSelect('700.HK', '腾讯控股')}>选择港股</button>
  ),
}))

vi.mock('@/components/stock-analysis/AnalysisKChart', () => ({ AnalysisKChart: () => null }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
vi.mock('@/components/LastStockChip', () => ({ LastStockChip: () => null }))

describe('AI 个股五维分析', () => {
  beforeEach(() => {
    window.localStorage.clear()
    startAnalysis.mockReset()
    startAnalysis.mockResolvedValue({ id: 'task-1' })
  })

  it('展示五维文案并把当前港股市场传给分析任务', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/stock-analysis?market=hk']}>
          <MarketScopeProvider><StockAnalysis /></MarketScopeProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect(screen.getByText(/AI 五维分析\(技术 \/ 资金 \/ 基本面 \/ 财务 \/ 消息面\)/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '选择港股' }))
    fireEvent.click(screen.getByRole('button', { name: /AI 个股分析/ }))

    await waitFor(() => expect(startAnalysis).toHaveBeenCalledWith('700.HK', '腾讯控股', '', 'hk'))
  })
})
