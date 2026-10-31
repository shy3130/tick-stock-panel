// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StrategyExecutionPanel } from './StrategyExecutionPanel'
import { executionApi, sourceFromReport, type ExecutionPreview } from './executionClient'

vi.mock('@/lib/api', () => ({ api: {
  screenerStrategies: vi.fn(async () => ({ presets: [{ id: 'demo', name: '示例策略' }] })),
  backtestCandidates: vi.fn(async () => ({ items: [{ id: 'candidate', name: '回测候选', kind: 'strategy' }] })),
} }))
vi.mock('@/lib/backtestTask', () => ({ useBacktestTask: () => null }))
vi.mock('./executionClient', async importOriginal => ({ ...await importOriginal<typeof import('./executionClient')>(), executionApi: {
  state: vi.fn(), preview: vi.fn(), bind: vi.fn(), enabled: vi.fn(),
} }))

const preview: ExecutionPreview = { revision: 'a'.repeat(64), differences: ['佣金最低 5 元，与回测存在差异'], snapshot: {
  strategy_id: 'demo', name: '示例策略', source: '回测候选', params: { threshold: 9 },
  overrides: { entry_signals: ['entry'], exit_signals: ['exit'] }, symbols: null, max_positions: 10,
  max_exposure_pct: .8, position_sizing: 'equal', initial_capital: 100000,
  fees: { commission_pct: .00025, stamp_tax_pct: .001, slippage_bps: 5 },
} }
let host: HTMLDivElement
let root: Root
let qc: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.mocked(executionApi.state).mockResolvedValue({ binding: null })
  vi.mocked(executionApi.preview).mockResolvedValue(preview)
  vi.mocked(executionApi.bind).mockResolvedValue({ binding: { ...preview, enabled: true, status: 'waiting', message: '等待当日收盘数据确认', last_bar: null, events: [] } })
  host = document.createElement('div'); document.body.append(host)
  root = createRoot(host)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})
afterEach(async () => { await act(async () => root.unmount()); qc.clear(); host.remove(); vi.clearAllMocks() })
async function flush() { await act(async () => { await new Promise(r => setTimeout(r, 20)) }) }
async function render(account = 'account-a') {
  await act(async () => root.render(<QueryClientProvider client={qc}><StrategyExecutionPanel key={account} account={account} /></QueryClientProvider>))
  await flush()
}
function button(text: string) { return [...document.querySelectorAll('button')].find(b => b.textContent === text)! }
async function click(text: string) { await act(async () => button(text).click()); await flush() }
async function select(label: string, value: string) {
  const el = document.querySelector(`select[aria-label="${label}"]`) as HTMLSelectElement
  await act(async () => { el.value = value; el.dispatchEvent(new Event('change', { bubbles: true })) }); await flush()
}

it('previews the selected candidate and binds exactly the reviewed revision', async () => {
  await render()
  expect(host.textContent).toContain('绑定参数快照')
  await click('绑定参数快照')
  await select('回测候选', 'candidate')
  await click('预览并核对参数')
  expect(executionApi.preview).toHaveBeenCalledWith('account-a', { candidate_id: 'candidate' })
  expect(document.body.textContent).toContain('threshold')
  expect(document.body.textContent).toContain('佣金最低 5 元')
  await click('确认参数并启用跟单')
  expect(executionApi.bind).toHaveBeenCalledWith('account-a', { candidate_id: 'candidate' }, preview.revision, false)
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(host.textContent).toContain('示例策略 · 已启用')
})

it('clears a reviewed snapshot when source changes and resets the dialog when reopened', async () => {
  await render(); await click('绑定参数快照'); await select('回测候选', 'candidate'); await click('预览并核对参数')
  await select('参数来源', 'strategy')
  expect(button('确认参数并启用跟单')).toBeUndefined()
  await act(async () => (document.querySelector('button[aria-label="关闭"]') as HTMLButtonElement).click())
  await click('绑定参数快照')
  expect((document.querySelector('select[aria-label="回测候选"]') as HTMLSelectElement).value).toBe('')
  expect(button('预览并核对参数').disabled).toBe(true)
})

it('isolates accounts and reports loading failures without showing another account binding', async () => {
  await render('account-a')
  vi.mocked(executionApi.state).mockRejectedValueOnce(new Error('扩展未就绪'))
  await render('account-b')
  expect(executionApi.state).toHaveBeenCalledWith('account-b')
  expect(host.textContent).toContain('扩展未就绪')
  expect(button('绑定参数快照').disabled).toBe(true)
  await click('重试')
  expect(button('绑定参数快照').disabled).toBe(false)
})

it('keeps binding errors visible and allows re-preview after dependencies change', async () => {
  await render(); await click('绑定参数快照'); await select('回测候选', 'candidate'); await click('预览并核对参数')
  vi.mocked(executionApi.bind).mockRejectedValueOnce(new Error('策略在预览后发生变化'))
  await click('确认参数并启用跟单')
  expect(document.querySelector('[role="alert"]')?.textContent).toContain('预览后发生变化')
  expect(document.querySelector('[role="dialog"]')).not.toBeNull()
})

it('reads only config and effective strategy info from a full report', () => {
  expect(sourceFromReport({ config: { strategy_id: 'demo', params: { n: 8 } }, strategy_info: { id: 'demo' }, trades: [{ private: true }] })).toEqual({ config: { strategy_id: 'demo', params: { n: 8 } }, strategy_info: { id: 'demo' }, label: '导入的回测配置' })
  expect(() => sourceFromReport({ config: { factor_name: 'close' } })).toThrow('策略 ID')
  expect(() => sourceFromReport({ error: 'failed', config: { strategy_id: 'demo' } })).toThrow('失败')
})

it('refreshes original orders after recovery even when the decision journal lacks order ids', async () => {
  const orderKey = ['paper', 'orders', 'account-a'] as const
  qc.setQueryData(orderKey, { orders: [] })
  vi.mocked(executionApi.state).mockResolvedValueOnce({ binding: { ...preview, enabled: true, status: 'running', message: '当日信号已处理', last_bar: '2026-10-26', events: [] } })
  await render()
  expect(qc.getQueryState(orderKey)?.isInvalidated).toBe(true)
})
