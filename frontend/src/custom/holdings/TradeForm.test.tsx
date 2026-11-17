// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { TradeForm } from './TradeForm'
import { api, type HoldingTrade } from '@/lib/api'

vi.mock('@/lib/api', () => ({ api: {klineDaily: vi.fn(), instrumentSearch: vi.fn()} }))
let root: Root
let host: HTMLDivElement
let qc: QueryClient
let save: ReturnType<typeof vi.fn>
const initial: HoldingTrade = {id: 'new', symbol: '', traded_at: '2026-11-01', side: 'buy', qty: 100, price: '', fee: '5', note: '', order: 0, voided: false}
beforeEach(() => {
  Object.assign(globalThis, {IS_REACT_ACT_ENVIRONMENT: true})
  vi.mocked(api.instrumentSearch).mockResolvedValue({results: [{symbol: '600000.SH', name: '浦发银行', code: '600000'}]})
  vi.mocked(api.klineDaily).mockResolvedValue({symbol: '600000.SH', rows: [{date: '2026-11-01', open: 6, high: 6, low: 6, close: 6, raw_close: 12}]})
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  qc = new QueryClient({defaultOptions: {queries: {retry: false}}}); save = vi.fn()
})
afterEach(async () => {await act(async () => root.unmount()); qc.clear(); host.remove(); vi.clearAllMocks()})
async function flush() {await act(async () => {await new Promise(r => setTimeout(r, 40))})}
async function render(value = initial, existing = false) {await act(async () => root.render(<QueryClientProvider client={qc}><TradeForm initial={value} existing={existing} pending={false} error="" onSave={save} onCancel={() => {}} /></QueryClientProvider>)); await flush()}
async function fill(el: HTMLInputElement, value: string) {
  await act(async () => {Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(el, value); el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true}))}); await flush()
}
async function click(text: string) {await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent === text)!.click()); await flush()}
const field = (label: string) => [...host.querySelectorAll('label')].find(l => l.textContent?.startsWith(label))!.querySelector('input')!
it('searches and selects stock, fills raw close, and submits date, radio, fee and quick notes', async () => {
  await render()
  expect(field('成交日期').type).toBe('date')
  expect(field('数量').step).toBe('100')
  expect(field('总费用').value).toBe('5')
  expect(host.textContent).not.toContain('同一时刻成交顺序')
  await fill(host.querySelector('input[placeholder]')!, '浦发')
  expect(api.instrumentSearch).toHaveBeenCalledWith('浦发', 20, 'stock')
  await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent?.includes('浦发银行'))!.click()); await flush()
  expect(api.klineDaily).toHaveBeenCalledWith('600000.SH', 120, {start: '2026-11-01', end: '2026-11-01'})
  expect(field('成交价格').value).toBe('12')
  await click('定投'); await click('定投'); await click('抄底')
  expect(field('备注').value).toBe('定投，抄底')
  await act(async () => host.querySelector<HTMLInputElement>('input[type="radio"][value="sell"]')!.click())
  await act(async () => host.querySelector('form')!.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})))
  expect(save).toHaveBeenCalledWith(expect.objectContaining({symbol: '600000.SH', traded_at: '2026-11-01T00:00:00', price: '12', side: 'sell', fee: '5', note: '定投，抄底'}))
})
it('preserves recorded price on edit and clears it on date change with no close', async () => {
  await render({...initial, symbol: '600000.SH', price: '10'}, true)
  expect(api.klineDaily).not.toHaveBeenCalled()
  expect(field('成交价格').value).toBe('10')
  await fill(field('成交日期'), '2026-11-02')
  expect(field('成交价格').value).toBe('')
  expect(host.textContent).toContain('所选日期无可用收盘价')
})
it('does not overwrite manually entered price when a slow close arrives', async () => {
  let resolve!: (v: Awaited<ReturnType<typeof api.klineDaily>>) => void
  vi.mocked(api.klineDaily).mockImplementation(() => new Promise(r => {resolve = r}))
  await render({...initial, symbol: '600000.SH'})
  await fill(field('成交价格'), '9.5')
  await act(async () => resolve({symbol: '600000.SH', rows: [{date: '2026-11-01', open: 6, high: 6, low: 6, close: 6, raw_close: 12}]})); await flush()
  expect(field('成交价格').value).toBe('9.5')
})
