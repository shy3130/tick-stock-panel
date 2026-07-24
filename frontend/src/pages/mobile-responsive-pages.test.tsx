import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { MarketScopeProvider } from '@/lib/market-scope'
import { Branding } from './Branding'
import { Indices } from './Indices'
import { StockAnalysis } from './StockAnalysis'

function renderMarketPage(element: React.ReactNode, path: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter
        initialEntries={[path]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <MarketScopeProvider>{element}</MarketScopeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('remaining page-level mobile layouts', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.restoreAllMocks()
    vi.spyOn(api, 'capabilities').mockResolvedValue({ label: 'None', capabilities: {} })
    vi.spyOn(api, 'indexList').mockResolvedValue({ results: [], count: 0 })
    vi.spyOn(api, 'indexQuotes').mockResolvedValue({ rows: [], count: 0 })
    vi.spyOn(api, 'indexDaily').mockResolvedValue({ symbol: '000001.SH', rows: [] })
    vi.spyOn(api, 'indexMinute').mockResolvedValue({ symbol: '000001.SH', date: null, rows: [] })
    vi.spyOn(api, 'stockAnalysisReportsList').mockResolvedValue({ reports: [] })
    vi.spyOn(api, 'klineDaily').mockResolvedValue({ symbol: '002842.SZ', rows: [] })
    vi.spyOn(api, 'stockAnalysisLevels').mockResolvedValue({
      symbol: '002842.SZ',
      close: null,
      summary: '',
      levels: {
        sr: [],
        pivot: [],
        extreme: [],
        boll: [],
        keltner_s: [],
        keltner_m: [],
        keltner_l: [],
        atr_stop: [],
        gap: [],
        fib: [],
        round: [],
      },
    })
  })

  it('stacks stock analysis controls and history before the desktop breakpoint', () => {
    window.localStorage.setItem(
      'last_stock:stock-analysis',
      JSON.stringify({ symbol: '002842.SZ', name: '翔鹭钨业' }),
    )
    renderMarketPage(<StockAnalysis />, '/stock-analysis?market=cn')

    expect(screen.getByRole('region', { name: '个股分析工具栏' })).toHaveClass('flex-col', 'sm:flex-row')
    expect(screen.getByRole('region', { name: '个股分析内容' })).toHaveClass(
      'grid-cols-1',
      'lg:grid-cols-[minmax(0,1fr)_288px]',
    )
  })

  it('stacks the index list and chart workspace on phones', () => {
    renderMarketPage(<Indices />, '/indices?market=cn')

    expect(screen.getByRole('region', { name: '指数行情内容' })).toHaveClass(
      'grid-cols-1',
      'lg:grid-cols-[15rem_minmax(0,1fr)]',
    )
    expect(screen.getByRole('region', { name: '指数图表组合' })).toHaveClass('flex-col', 'xl:flex-row')
  })

  it('stacks every branding preview instead of clipping its description', () => {
    render(<Branding />)

    for (const sample of screen.getAllByRole('article')) {
      expect(sample).toHaveClass('flex-col', 'xl:flex-row', 'min-w-0')
    }
  })
})
