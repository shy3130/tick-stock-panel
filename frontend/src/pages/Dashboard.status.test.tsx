// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Dashboard } from './Dashboard'
import { api, type DataStatus } from '@/lib/api'

vi.mock('@/lib/api', () => ({ api: {
  dataStatus: vi.fn(),
  overviewMarket: vi.fn(async () => ({ emotion: { score: 50, label: '中性' } })),
  dataSources: vi.fn(async () => ({})),
  settings: vi.fn(async () => ({ onboarding_completed: true })),
  preferences: vi.fn(async () => ({})),
  capabilities: vi.fn(async () => ({ capabilities: {} })),
  pipelineJobs: vi.fn(async () => ({ active_id: null })),
  pipelineJob: vi.fn(),
  pipelineRun: vi.fn(),
} }))
vi.mock('@/components/dashboard/useDashboardLayout', () => ({
  useDashboardLayout: () => ({ items: [], setItems: () => {} }),
}))
vi.mock('@/components/dashboard/DashboardGrid', () => ({ DashboardGrid: () => null }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
vi.mock('@/components/DimensionMembersDialog', () => ({ DimensionMembersDialog: () => null }))
vi.mock('@/components/AdjFactorSyncGate', () => ({
  useAdjFactorSyncGate: () => ({ guard: (run: () => void) => run(), dialog: null }),
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  vi.clearAllMocks()
  sessionStorage.clear()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.useRealTimers()
})

const busy = { indicators_ready: true, stats_pending: true, daily: null, enriched: null } as DataStatus

async function render() {
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><Dashboard /></QueryClientProvider></MemoryRouter>,
  ))
  await tick(1)
}

async function tick(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

it('同步/修复期不弹获取数据, 完成后自动重查并停止轮询', async () => {
  vi.mocked(api.dataStatus).mockResolvedValueOnce(busy).mockResolvedValue({
    ...busy, stats_pending: false, daily: { trading_days: 240 },
  } as DataStatus)
  await render()
  expect(host.textContent).not.toContain('立即获取数据')
  expect(sessionStorage.getItem('tf_welcome_shown')).toBeNull()

  await tick(5_001)
  expect(api.dataStatus).toHaveBeenCalledTimes(2)
  expect(host.textContent).not.toContain('立即获取数据')
  expect(sessionStorage.getItem('tf_welcome_shown')).toBeNull()
  await tick(10_000)
  expect(api.dataStatus).toHaveBeenCalledTimes(2)
})

it('等统计可用后才为真正无数据的新用户弹出引导', async () => {
  vi.mocked(api.dataStatus).mockResolvedValueOnce(busy).mockResolvedValue({
    ...busy, stats_pending: false,
  })
  await render()
  expect(sessionStorage.getItem('tf_welcome_shown')).toBeNull()
  await tick(5_001)
  expect(host.textContent).toContain('立即获取数据')
  expect(host.textContent).toContain('欢迎首次使用')
  expect(sessionStorage.getItem('tf_welcome_shown')).toBe('1')
})
