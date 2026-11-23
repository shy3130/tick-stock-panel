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
import type { DataStatus, StrategyBacktestResult, StrategyDetail } from '@/lib/api'
import { startBacktest, useBacktestTask } from '@/lib/backtestTask'
import { storage } from '@/lib/storage'
import { QK } from '@/lib/queryKeys'
import { PRO_DYNAMIC_PRICE_PROVIDER } from '@/lib/engineJobTypes'

vi.mock('@/lib/api', () => ({
  api: {
  dataStatus: vi.fn(),
  enginePriceViews: vi.fn(),
  dataIntegrity: vi.fn(async () => ({ report: null, blocking_assets: [], data_revisions: {} })),
  screenerStrategies: vi.fn(async () => ({ presets: [] })),
  strategyGet: vi.fn(async () => ({})),
  watchlistList: vi.fn(async () => ({ items: [] })),
  watchlistGroups: vi.fn(async () => ({ groups: [] })),
  positionPolicyList: vi.fn(async () => ({ policies: [] })),
  factorColumns: vi.fn(async () => ({ columns: [] })),
  factorLibrary: vi.fn(async () => ({ factors: [] })),
  instrumentSearch: vi.fn(async () => ({ results: [] })),
  customSignalsList: vi.fn(async () => ({ signals: [] })),
  customSignalsOptions: vi.fn(async () => ({ fields: [], operators: [] })),
  extDataList: vi.fn(async () => ({ items: [] })),
  capabilities: vi.fn(async () => ({ capabilities: {} })),
  settings: vi.fn(async () => ({ onboarding_completed: true, mode: 'api_key', has_tickflow_key: true })),
  preferences: vi.fn(async () => ({})),
  dataSources: vi.fn(async () => []),
  regimeRecompute: vi.fn(async () => ({ ok: true, computed: 0 })),
  strategySaveConfig: vi.fn(async () => ({ ok: true })),
  researchCandidateCreate: vi.fn(async () => ({})),
  },
  REGIME_STATE_LABELS: { strong: '强势' } as Record<string, string>,
  REGIME_STATE_COLORS: {} as Record<string, string>,
}))
vi.mock('@/lib/backtestTask', () => ({
  startBacktest: vi.fn(),
  stopBacktest: vi.fn(async () => {}),
  tryReconnect: vi.fn(() => false),
  clearBacktest: vi.fn(),
  useBacktestTask: vi.fn(),
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  sessionStorage.clear()
  localStorage.clear()
  const { api } = await import('@/lib/api')
  vi.mocked(api.enginePriceViews).mockResolvedValue([])
  vi.mocked(api.screenerStrategies).mockResolvedValue({ presets: [], load_errors: [], legacy_migration: undefined })
  vi.mocked(useBacktestTask).mockReturnValue(null)
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

it('叠加条件为空时隐藏回测过滤摘要', async () => {
  const { api } = await import('@/lib/api')
  vi.mocked(api.dataStatus).mockResolvedValue(busy)
  vi.mocked(useBacktestTask).mockReturnValue({
    id: 1, isPending: false, progress: null, error: null, reconnecting: false,
    result: {
      run_id: 'empty-overlay',
      config: { overrides: { overlay_filter: [] } },
      strategy_info: { id: 'grouped', name: '观察组策略' } as StrategyBacktestResult['strategy_info'],
      stats: {}, equity_curve: [], drawdown_curve: [], trades: [], per_symbol_stats: [],
      elapsed_ms: 1, error: null,
    },
  })
  await renderPage()
  const filter = [...host.querySelectorAll('button')]
    .find(button => button.textContent?.startsWith('回测过滤'))!.parentElement!
  const unlabelledText = [...filter.childNodes]
    .filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.textContent?.trim())
  expect(unlabelledText).not.toContain('0')
  expect(filter.textContent).not.toContain('本次回测过滤')
})

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

const libraryStrategies = [
  { id: 'grouped', name: '观察组策略', source: 'custom', library: { group: 'g', group_name: '观察组', archived: false } },
  { id: 'ungrouped', name: '未分组策略', source: 'custom' },
  { id: 'archived', name: '归档策略', source: 'ai', library: { group: 'g', group_name: '观察组', archived: true } },
] as unknown as StrategyDetail[]

it('回测过滤可从模拟模式下方直接配置', async () => {
  const { api } = await import('@/lib/api')
  vi.mocked(api.dataStatus).mockResolvedValue(busy)
  vi.mocked(api.screenerStrategies).mockResolvedValue({ presets: libraryStrategies, load_errors: [], legacy_migration: undefined })
  vi.mocked(api.strategyGet).mockResolvedValue({
    ...libraryStrategies[0], params: [], params_defaults: {}, basic_filter: {}, scoring: {},
    scoring_directions: {}, entry_signals: [], exit_signals: [], asset_types: ['stock'], timeframes: ['1d'],
  } as StrategyDetail)
  storage.strategyBacktestLast.set({ selectedStrategy: 'grouped' } as Parameters<typeof storage.strategyBacktestLast.set>[0])
  await renderPage()

  const buttons = () => [...host.querySelectorAll<HTMLButtonElement>('button')]
  const trigger = buttons().find(button => button.textContent?.startsWith('回测过滤'))!
  const mode = buttons().find(button => button.textContent === '仓位模拟')!
  expect(mode.closest('section')?.contains(trigger)).toBe(true)
  expect(trigger.getAttribute('aria-expanded')).toBe('false')
  await act(async () => trigger.click())
  expect(trigger.getAttribute('aria-expanded')).toBe('true')
  expect(host.querySelector('[role="dialog"]')).toBeNull()

  const source = [...host.querySelectorAll<HTMLSelectElement>('select')]
    .find(control => control.labels?.[0]?.textContent?.includes('市场来源'))!
  await act(async () => {
    source.value = 'builtin'
    source.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await act(async () => buttons().find(button => button.textContent === '强势')!.click())
  await act(async () => trigger.click())
  expect(trigger.textContent).toContain('内置市场环境')
  expect(trigger.getAttribute('aria-expanded')).toBe('false')

  await act(async () => buttons().find(button => button.textContent?.startsWith('回测配置'))!.click())
  const settings = host.querySelector('[role="dialog"]')!
  expect(settings.querySelector('nav')?.textContent).not.toContain('回测过滤')
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="关闭回测配置"]')!.click())
  await act(async () => runButton()!.click())
  expect(startBacktest).toHaveBeenLastCalledWith(expect.objectContaining({ regime_filter: { states: ['strong'] } }))
})

it('策略回测隐藏新选择中的归档策略并支持自定义分组', async () => {
  const { api } = await import('@/lib/api')
  vi.mocked(api.dataStatus).mockResolvedValue(busy)
  vi.mocked(api.screenerStrategies).mockResolvedValue({ presets: libraryStrategies, load_errors: [], legacy_migration: undefined })
  await renderPage()
  expect(host.textContent).toContain('观察组策略')
  expect(host.textContent).toContain('未分组策略')
  expect(host.textContent).not.toContain('归档策略')
  const filter = host.querySelector<HTMLSelectElement>('select[aria-label="策略分组"]')!
  await act(async () => { filter.value = 'g'; filter.dispatchEvent(new Event('change', { bubbles: true })) })
  expect(host.textContent).not.toContain('未分组策略')
  expect(host.textContent).toContain('观察组策略')
})

it('已保存的归档策略仍能载入, 恢复后缓存刷新可重新选择', async () => {
  const { api } = await import('@/lib/api')
  vi.mocked(api.dataStatus).mockResolvedValue(busy)
  vi.mocked(api.screenerStrategies).mockResolvedValue({ presets: libraryStrategies, load_errors: [], legacy_migration: undefined })
  vi.mocked(api.strategyGet).mockResolvedValue({
    ...libraryStrategies[2], params: [], params_defaults: {}, basic_filter: {}, scoring: {},
    scoring_directions: {}, entry_signals: [], exit_signals: [], asset_types: ['stock'], timeframes: ['1d'],
  } as StrategyDetail)
  storage.strategyBacktestLast.set({ selectedStrategy: 'archived' } as Parameters<typeof storage.strategyBacktestLast.set>[0])
  await renderPage()
  expect(host.textContent).toContain('归档策略（已归档）')
  expect(api.strategyGet).toHaveBeenCalledWith('archived')
  await act(async () => client.setQueryData(QK.screenerStrategies('stock', 'all'), {
    presets: libraryStrategies.map(item => item.id === 'archived' ? { ...item, library: { ...item.library, archived: false } } : item),
  }))
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  expect(host.textContent).not.toContain('归档策略（已归档）')
})


it('普通回测默认前复权，可直接选择后复权运行', async () => {
  const { api } = await import('@/lib/api')
  vi.mocked(api.dataStatus).mockResolvedValue(busy)
  vi.mocked(api.screenerStrategies).mockResolvedValue({ presets: libraryStrategies, load_errors: [], legacy_migration: undefined })
  vi.mocked(api.strategyGet).mockResolvedValue({
    ...libraryStrategies[0], params: [], params_defaults: {}, basic_filter: {}, scoring: {},
    scoring_directions: {}, entry_signals: [], exit_signals: [], asset_types: ['stock'], timeframes: ['1d'],
  } as StrategyDetail)
  storage.strategyBacktestLast.set({ selectedStrategy: 'grouped' } as Parameters<typeof storage.strategyBacktestLast.set>[0])
  await renderPage()
  const select = [...host.querySelectorAll<HTMLSelectElement>('select')]
    .find(control => control.closest('label')?.textContent?.includes('复权方式'))!
  expect([...select.options].map(option => option.textContent)).toEqual(['前复权', '后复权'])
  expect(select.value).toBe('qfq')
  await act(async () => runButton()!.click())
  expect(startBacktest).toHaveBeenLastCalledWith(expect.objectContaining({ price_basis: 'qfq' }))

  await act(async () => { select.value = 'hfq'; select.dispatchEvent(new Event('change', { bubbles: true })) })
  await act(async () => runButton()!.click())
  expect(startBacktest).toHaveBeenLastCalledWith(expect.objectContaining({ price_basis: 'hfq' }))
})

it('runs authorized dynamic adjustment in the existing strategy form', async () => {
  const { api } = await import('@/lib/api')
  vi.mocked(api.dataStatus).mockResolvedValue(busy)
  vi.mocked(api.screenerStrategies).mockResolvedValue({ presets: libraryStrategies, load_errors: [], legacy_migration: undefined })
  vi.mocked(api.strategyGet).mockResolvedValue({
    ...libraryStrategies[0], params: [], params_defaults: {}, basic_filter: {}, scoring: {},
    scoring_directions: {}, entry_signals: [], exit_signals: [], asset_types: ['stock'], timeframes: ['1d'],
  } as StrategyDetail)
  vi.mocked(startBacktest).mockClear()
  storage.strategyBacktestLast.set({ selectedStrategy: 'grouped', symbols: '600000.SH',
    start: '2024-01-02', end: '2024-01-12' } as Parameters<typeof storage.strategyBacktestLast.set>[0])
  vi.mocked(api.enginePriceViews).mockResolvedValue([
    { provider_id: PRO_DYNAMIC_PRICE_PROVIDER, mode: 'extension', state: 'needs_license' },
  ])
  const priceControl = () => [...host.querySelectorAll<HTMLSelectElement>('select')]
    .find(control => control.closest('label')?.textContent?.includes('复权方式'))!
  const changePrice = (value: string) => act(async () => {
    priceControl().value = value
    priceControl().dispatchEvent(new Event('change', { bubbles: true }))
  })
  await renderPage()
  expect([...priceControl().options].map(option => option.value)).toEqual(['qfq', 'hfq'])
  await act(async () => {
    client.setQueryData(QK.enginePriceViews, [
      { provider_id: PRO_DYNAMIC_PRICE_PROVIDER, mode: 'extension', state: 'available', ordinary_backtest: false },
    ])
    await new Promise(resolve => setTimeout(resolve, 0))
  })
  expect([...priceControl().options].map(option => option.value)).toEqual(['qfq', 'hfq'])
  await act(async () => {
    client.setQueryData(QK.enginePriceViews, [
      { provider_id: PRO_DYNAMIC_PRICE_PROVIDER, mode: 'extension', state: 'available', ordinary_backtest: true },
    ])
    await new Promise(resolve => setTimeout(resolve, 0))
  })
  expect([...priceControl().options].map(option => option.value)).toEqual(['qfq', 'hfq', 'dynamic'])
  await changePrice('dynamic')
  expect(host.textContent).toContain('按原始日 K 撮合')
  expect(host.textContent).not.toContain('选择回测归档')
  await act(async () => runButton()!.click())
  expect(startBacktest).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
    strategy_id: 'grouped', price_basis: 'dynamic', symbols: ['600000.SH'],
    start: '2024-01-02', end: '2024-01-12', minute_fill: false,
  }))
  await changePrice('hfq')
  expect(runButton()).toBeDefined()
})
