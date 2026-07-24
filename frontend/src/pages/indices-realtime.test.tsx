import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { MarketScopeProvider } from '@/lib/market-scope'
import { Indices } from './Indices'

describe('global index realtime refresh', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.restoreAllMocks()
    vi.spyOn(api, 'capabilities').mockResolvedValue({ label: 'None', capabilities: {} })
    vi.spyOn(api, 'indexList').mockResolvedValue({
      results: [
        { symbol: 'HSI.HK', name: '恒生指数', asset_type: 'index', market: 'hk' },
        { symbol: 'HSTECH.HK', name: '恒生科技指数', asset_type: 'index', market: 'hk' },
        { symbol: 'HSCEI.HK', name: '恒生中国企业指数', asset_type: 'index', market: 'hk' },
      ],
      count: 3,
    })
    vi.spyOn(api, 'indexQuotes').mockResolvedValue({ rows: [], count: 0 })
    vi.spyOn(api, 'indexDaily').mockResolvedValue({ symbol: 'HSI.HK', rows: [] })
    vi.spyOn(api, 'indexMinute').mockResolvedValue({ symbol: 'HSI.HK', date: null, rows: [] })
  })

  it('polls visible HK index quotes at least every ten seconds', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter
          initialEntries={['/indices?market=hk']}
          future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        >
          <MarketScopeProvider><Indices /></MarketScopeProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    )

    await waitFor(() => expect(api.indexQuotes).toHaveBeenCalled())
    const quoteQuery = client.getQueryCache().findAll().find(query => query.queryKey[0] === 'index-quotes')
    const options = quoteQuery?.options as { refetchInterval?: number } | undefined
    expect(options?.refetchInterval).toEqual(expect.any(Number))
    expect(Number(options?.refetchInterval)).toBeLessThanOrEqual(10_000)
  })
})
