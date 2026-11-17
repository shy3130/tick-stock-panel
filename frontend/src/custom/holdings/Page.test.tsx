// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Holdings from './Page'
import { api, type HoldingsState } from '@/lib/api'

vi.mock('@/lib/api', () => ({ api: {holdings: vi.fn(), holdingTrade: vi.fn(), holdingChips: vi.fn(), holdingsRestore: vi.fn(), instrumentSearch: vi.fn(), klineDaily: vi.fn()} }))
vi.mock('@/extensions/stockPreview', () => ({openStockPreview: vi.fn()}))
vi.mock('@/components/PageHeader', () => ({PageHeader: ({title}: {title: string}) => <h1>{title}</h1>}))
let root: Root
let host: HTMLDivElement
let qc: QueryClient
const state: HoldingsState = {version: 1, revision: 'rev1', market_error: null,
  positions: [{id: 'a', symbol: '600000.SH', name: '测试股票', qty: 100, cost: 1000, avg_cost: 10, realized: 0, bought: 1000, sold: 0, opened: '2026-11-01', holding_days: 16, last_price: 12, quote_date: '2026-11-17', market_value: 1200, unrealized: 200}],
  trades: [{id: 'a', symbol: '600000.SH', traded_at: '2026-11-01T10:00:00', side: 'buy', qty: 100, price: '10', fee: '0', note: '', order: 0, voided: false}], transactions: []}
state.transactions = [{...state.trades[0], cycle_id: 'a', balance: 100, avg_cost: 10, realized: 0}]
beforeEach(() => {
  Object.assign(globalThis, {IS_REACT_ACT_ENVIRONMENT: true})
  vi.mocked(api.holdings).mockResolvedValue(structuredClone(state))
  vi.mocked(api.instrumentSearch).mockResolvedValue({results: []})
  vi.mocked(api.klineDaily).mockResolvedValue({symbol: "600000.SH", rows: []})
  vi.mocked(api.holdingTrade).mockResolvedValue({ok: true})
  vi.mocked(api.holdingChips).mockResolvedValue({bins: [{price: 10, weight: 1}], message: '市场筹码估算'})
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  qc = new QueryClient({defaultOptions: {queries: {retry: false}, mutations: {retry: false}}})
})
afterEach(async () => {await act(async () => root.unmount()); qc.clear(); host.remove(); vi.clearAllMocks()})
async function flush() {await act(async () => {await new Promise(r => setTimeout(r, 30))})}
async function render() {await act(async () => root.render(<QueryClientProvider client={qc}><Holdings /></QueryClientProvider>)); await flush()}
async function click(text: string) {const b = [...document.querySelectorAll('button')].find(b => b.textContent === text)!; expect(b).toBeTruthy(); await act(async () => b.click()); await flush()}
it('shows empty closed positions and market chips in selected cycle', async () => {
  await render(); expect(host.textContent).toContain('测试股票')
  await click('已清仓（0）'); expect(host.textContent).toContain('暂无清仓记录')
  await click('当前持仓（1）'); await click('详情')
  expect(host.textContent).toContain('市场筹码估算')
  expect(host.textContent).toContain('交易后成本')
  expect(api.holdingChips).toHaveBeenCalledWith('600000.SH')
})
it('edits only authoritative trade fields and preserves reviewed revision', async () => {
  await render(); await click('详情'); await click('修改')
  const form = document.querySelector('form')!
  await act(async () => form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})))
  await flush()
  expect(api.holdingTrade).toHaveBeenCalledWith('rev1', {...state.trades[0], traded_at: '2026-11-01T00:00:00'})
  expect(document.querySelector('form')).toBeNull()
})
it('keeps save errors visible and does not close form', async () => {
  vi.mocked(api.holdingTrade).mockRejectedValue(new Error('卖出超过当时持仓'))
  await render(); await click('详情'); await click('修改')
  await act(async () => document.querySelector('form')!.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})))
  await flush(); expect(document.querySelector('form')?.textContent).toContain('卖出超过当时持仓')
})
