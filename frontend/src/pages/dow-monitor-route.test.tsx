import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { Layout } from '@/components/Layout'
import { MarketScopeProvider } from '@/lib/market-scope'
import { router } from '@/router'

vi.mock('@/lib/useSharedQueries', () => ({
  useCapabilities: () => ({ data: { label: 'Pro' } }),
  useSettings: () => ({ data: { mode: 'local' } }),
  usePreferences: () => ({ data: {} }),
  useQuoteStatus: () => ({ data: {} }),
  useVersion: () => ({ data: { version: 'test' } }),
}))

vi.mock('@/lib/useSharedMutations', () => ({
  useToggleRealtimeQuotes: () => ({
    isPending: false,
    mutateAsync: vi.fn(),
  }),
}))

vi.mock('@/lib/useQuoteStream', () => ({
  useQuoteStream: vi.fn(),
  useQuoteStreamStatus: () => 'connected',
}))

vi.mock('@/lib/api', () => ({
  api: {
    alertsList: vi.fn().mockResolvedValue({ total: 0 }),
    analysisMenus: vi.fn().mockResolvedValue({ items: [] }),
    dataSources: vi.fn().mockResolvedValue({ custom: [] }),
    indexQuotes: vi.fn().mockResolvedValue({ rows: [] }),
    pipelineJobs: vi.fn().mockResolvedValue({ active_id: null }),
  },
}))

function routePaths() {
  const root = router.routes.find(route => route.path === '/')
  return root?.children?.map(route => route.path).filter(Boolean) ?? []
}

describe('Dow monitor route', () => {
  it('exposes trend monitoring in desktop and mobile navigation', () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/dow-monitor']}>
          <MarketScopeProvider>
            <Layout />
          </MarketScopeProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect(screen.getByRole('link', { name: '趋势监控' })).toHaveAttribute('href', '/dow-monitor')
    expect(within(screen.getByRole('banner')).getByText('趋势监控')).toBeInTheDocument()
    expect(routePaths()).toContain('dow-monitor')
  })

  it('exposes collection monitoring in the route and desktop/mobile navigation', async () => {
    const user = userEvent.setup()
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/collection-monitor']}>
          <MarketScopeProvider>
            <Layout />
          </MarketScopeProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect(screen.getByRole('link', { name: '采集监控' })).toHaveAttribute('href', '/collection-monitor')
    expect(within(screen.getByRole('banner')).getByText('采集监控')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '打开导航菜单' }))
    expect(within(screen.getByRole('dialog', { name: '主导航' })).getByRole('link', { name: '采集监控' })).toBeInTheDocument()
    expect(routePaths()).toContain('collection-monitor')
  })
})
