// @vitest-environment jsdom
// 回归: 预热/同步/修复期间 (stats_pending / indicators_ready=false) 统计为 null 是未知态,
// 不得误判"缺少指标数据"禁用运行回测按钮 (同 Dashboard 的 warmup 误判一族);
// 真实统计就绪且确无数据时仍要禁用 + 提示。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StrategyBacktest } from './StrategyBacktest'
import type { DataStatus } from '@/lib/api'

vi.mock('@/lib/api', () => ({
  api: {
  dataStatus: vi.fn(),
  screenerStrategies: vi.fn(async () => ({ presets: [] })),
  strategyGet: vi.fn(async () => ({})),
  watchlistList: vi.fn(async () => ({ items: [] })),
  positionPolicyList: vi.fn(async () => ({ policies: [] })),
  factorColumns: vi.fn(async () => ({ columns: [] })),
  factorLibrary: vi.fn(async () => ({ factors: [] })),
  instrumentSearch: vi.fn(async () => ({ results: [] })),
  customSignalsList: vi.fn(async () => ({ signals: [] })),
  customSignalsOptions: vi.fn(async () => ({ fields: [], operators: [] })),
  capabilities: vi.fn(async () => ({ capabilities: {} })),
  settings: vi.fn(async () => ({ onboarding_completed: true, mode: 'api_key', has_tickflow_key: true })),
  preferences: vi.fn(async () => ({})),
  dataSources: vi.fn(async () => []),
  regimeRecompute: vi.fn(async () => ({ ok: true, computed: 0 })),
  strategySaveConfig: vi.fn(async () => ({ ok: true })),
  researchCandidateCreate: vi.fn(async () => ({})),
  },
  REGIME_STATE_LABELS: {} as Record<string, string>,
  REGIME_STATE_COLORS: {} as Record<string, string>,
}))
vi.mock('@/lib/backtestTask', () => ({
  startBacktest: vi.fn(),
  stopBacktest: vi.fn(async () => {}),
  tryReconnect: vi.fn(() => false),
  clearBacktest: vi.fn(),
  useBacktestTask: () => null,
}))

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

const busy = {
  indicators_ready: false, stats_pending: true,
  daily: null, enriched: null, etf_enriched: null,
} as unknown as DataStatus
const readyNoData = {
  indicators_ready: true, stats_pending: false,
  daily: null, enriched: null, etf_enriched: null,
} as unknown as DataStatus

async function renderPage() {
  const errors: unknown[] = []
  const spy = vi.spyOn(console, 'error').mockImplementation((...args) => { errors.push(args) })
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><StrategyBacktest /></QueryClientProvider></MemoryRouter>,
  ))
  await act(async () => { await new Promise(r => setTimeout(r, 80)) })
  spy.mockRestore()
  return errors
}

function runButton(): HTMLButtonElement | undefined {
  return [...host.querySelectorAll('button')].find(b => b.textContent?.includes('运行回测'))
}

it('预热/同步期间统计为 null: 不提示缺数据, 回测按钮可用', async () => {
  const { api } = await import('@/lib/api')
  vi.mocked(api.dataStatus).mockResolvedValue(busy)

  const errors = await renderPage()
  // 核心误判: busy 未知态不得显示"缺少指标数据"横幅 (backtestDataUnavailable=false,
  // 按钮的 disabled 另受"未选策略"影响, 不在此断言)
  expect(host.textContent).not.toContain('缺少股票指标数据')
  // 渲染期 TypeError 防线 (与数据页崩溃修复同族守护)
  expect(errors.some(e => String(e).includes('TypeError'))).toBe(false)
})

it('真实统计就绪且确无数据: 提示缺数据并禁用回测', async () => {
  const { api } = await import('@/lib/api')
  vi.mocked(api.dataStatus).mockResolvedValue(readyNoData)

  await renderPage()
  expect(host.textContent).toContain('缺少股票指标数据')
  expect(runButton()!.disabled).toBe(true)
})
