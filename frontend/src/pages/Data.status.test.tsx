// @vitest-environment jsdom
// 回归: busy 降级期间 storage 为 null 时数据页整页崩溃
// (用户反馈桌面客户端 "Unexpected Application Error! Cannot read properties of
//  null (reading 'total_size_mb')" — 后端 /api/data/status 在预热/同步/修复期间
//  返回 storage=null, 前端 s.storage.xxx 未判空直接抛 TypeError)。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Data } from './Data'
import type { DataStatus } from '@/lib/api'

vi.mock('@/lib/api', () => ({ api: {
  dataStatus: vi.fn(),
  regimeCoverage: vi.fn(async () => ({ rows: 0, earliest_date: null, latest_date: null })),
  pipelineJobs: vi.fn(async () => ({ active_id: null, jobs: [] })),
  pipelineJob: vi.fn(async () => null),
  extDataList: vi.fn(async () => ({ items: [] })),
  dataSources: vi.fn(async () => []),
  settings: vi.fn(async () => ({ onboarding_completed: true, mode: 'api_key', has_tickflow_key: true })),
  preferences: vi.fn(async () => ({})),
  capabilities: vi.fn(async () => ({ capabilities: {} })),
} }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
vi.mock('@/components/EndpointTestDialog', () => ({ EndpointTestDialog: () => null }))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
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
  vi.restoreAllMocks()
})

// busy 降级形态: status 非空但各统计与 storage 均为 null (冷启动缓存未热)
const busyStatus = {
  indicators_ready: false,
  stats_pending: true,
  daily: null,
  enriched: null,
  index_daily: null,
  index_enriched: null,
  etf_daily: null,
  etf_enriched: null,
  minute: null,
  adj_factor: null,
  instruments: null,
  financials: null,
  storage: null,
  next_pipeline_run: null,
  next_instruments_run: null,
  last_pipeline_run: null,
  last_instruments_run: null,
  checked_at: new Date().toISOString(),
} as unknown as DataStatus

async function renderPage() {
  const errors: unknown[] = []
  const spy = vi.spyOn(console, 'error').mockImplementation((...args) => { errors.push(args) })
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><Data /></QueryClientProvider></MemoryRouter>,
  ))
  await act(async () => { await new Promise(r => setTimeout(r, 60)) })
  spy.mockRestore()
  return errors
}

it('storage 为 null 时不崩, 存储卡片降级为占位', async () => {
  const { api } = await import('@/lib/api')
  vi.mocked(api.dataStatus).mockResolvedValue(busyStatus)

  const errors = await renderPage()
  // 渲染期 TypeError 会被 React 记为 console.error("The above error occurred ...")
  const crashed = errors.some(e => String(e).includes('total_size_mb'))
  expect(crashed).toBe(false)
  expect(host.textContent).toContain('存储')
  expect(host.textContent).toContain('—')  // 总大小降级占位
  // 各 storage 数值区降级显示, 不出现 NaN/undefined
  expect(host.textContent).not.toContain('undefined')
})
