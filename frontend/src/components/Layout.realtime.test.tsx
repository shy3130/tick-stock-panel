// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Layout } from './Layout'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'

vi.mock('@/lib/api', () => ({ api: {
  dataSources: vi.fn(async () => ({
    builtin: [], custom: [], plugins: [{ name: 'fuyao', display_name: 'fuyao' }],
  })),
  analysisMenus: vi.fn(async () => ({ items: [] })),
  indexQuotes: vi.fn(async () => ({ rows: [] })),
  watchlistGroups: vi.fn(async () => ({ groups: [] })),
  watchlistList: vi.fn(async () => ({ symbols: [] })),
  watchlistEnriched: vi.fn(async () => ({ rows: [] })),
  pipelineJobs: vi.fn(async () => ({ active_id: null, items: [] })),
  alertsList: vi.fn(async () => ({ total: 0 })),
  quoteStatus: vi.fn(),
  updateRealtimeQuotes: vi.fn(async (enabled: boolean) => ({
    realtime_quotes_enabled: enabled, realtime_allowed: true,
  })),
  miningRuns: vi.fn(async () => ({ items: [] })),
  intradayRefresh: vi.fn(async () => ({})),
} }))
vi.mock('@/lib/useSharedQueries', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/useSharedQueries')>(),
  useSettings: () => ({ data: { onboarding_completed: true } }),
  useCapabilityMatrix: () => ({ data: { capabilities: [] } }),
  usePreferences: () => ({ data: { realtime_data_provider: 'fuyao', realtime_quotes_enabled: false } }),
  useVersion: () => ({ data: { version: '0.3.20' } }),
}))
vi.mock('@/lib/useQuoteStream', () => ({
  useQuoteStream: () => {}, useQuoteStreamStatus: () => 'connected',
}))
vi.mock('@/lib/updateCheck', () => ({ useUpdateCheck: () => ({ status: 'latest' }) }))
vi.mock('@/lib/useMediaQuery', () => ({ useIsDesktop: () => true }))
vi.mock('@/extensions/registry', () => ({ getFrontendExtensionNavigation: () => [] }))
vi.mock('@/extensions/ExtensionSlot', () => ({ ExtensionSlot: () => null }))
vi.mock('@/components/Toast', () => ({ ToastContainer: () => null, toast: vi.fn() }))
vi.mock('@/components/AlertToast', () => ({ AlertToastContainer: () => null }))
vi.mock('@/components/financials/AiAnalysisHost', () => ({ AiAnalysisHost: () => null }))
vi.mock('@/components/financials/AiReportBubble', () => ({ AiReportBubble: () => null }))
vi.mock('@/components/stock-analysis/StockAnalysisHost', () => ({ StockAnalysisHost: () => null }))
vi.mock('@/components/stock-analysis/StockAnalysisBubble', () => ({ StockAnalysisBubble: () => null }))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  localStorage.clear()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: {
    retry: false, staleTime: Infinity,
  } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function toggleWithCachedPermission(cachedAllowed: boolean, currentAllowed: boolean) {
  const status = (allowed: boolean) => ({
    enabled: false, running: false, paused: false, is_trading_hours: false,
    mode: allowed ? 'full_market' : 'none', realtime_allowed: allowed,
  }) as Awaited<ReturnType<typeof api.quoteStatus>>
  client.setQueryData(QK.quoteStatus, status(cachedAllowed))
  vi.mocked(api.quoteStatus).mockResolvedValue(status(currentAllowed))
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><Layout /></QueryClientProvider></MemoryRouter>,
  ))
  const button = host.querySelector<HTMLButtonElement>('button[aria-label="开启实时行情"]')
  expect(button).not.toBeNull()
  await act(async () => button!.click())
  // Query/mutation observer notifications are batched on a timer tick.
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

it('切到 fuyao 后开启时重新校验权限, 不复用 TickFlow 的旧不可用状态', async () => {
  await toggleWithCachedPermission(false, true)
  expect(api.quoteStatus).toHaveBeenCalled()
  expect(api.updateRealtimeQuotes).toHaveBeenCalledWith(true)
  expect(toast).not.toHaveBeenCalled()
})

it('当前源已不可用时不复用旧可用状态开启', async () => {
  await toggleWithCachedPermission(true, false)
  expect(api.quoteStatus).toHaveBeenCalled()
  expect(api.updateRealtimeQuotes).not.toHaveBeenCalled()
  expect(toast).toHaveBeenCalledWith(expect.stringContaining('数据源'), 'error')
})
