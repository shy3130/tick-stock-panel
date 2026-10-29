// @vitest-environment jsdom
// 回归: fuyao 龙虎榜对退市整理期股/B股返回 thscode=null (ticker 有值),
// 复盘页龙虎榜卡片渲染/点击该行不得崩溃 (用户反馈: 展开龙虎榜即崩)。
// 真实数据样本 (2026-10-29 榜单): 萃华退 002731 / 宁通信B 200468 / 动力B股 900920。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { DragonTigerCard } from './extension'
import type { DragonTigerStockItem } from '@/lib/api'

const _items: DragonTigerStockItem[] = [
  { thscode: null, ticker: '002731', name: '萃华退', change: -0.107143, net_value: -525558.3, net_rate: -0.0131, buy_value: 4489000, sell_value: 5014558.3, range_days: 1 },
  { thscode: null, ticker: '200468', name: '宁通信B', change: -0.08011, net_value: -54180.43, net_rate: -0.0102, buy_value: 2617000, sell_value: 2671180.43, range_days: 1 },
  { thscode: null, ticker: '900920', name: '动力B股', change: 0.099602, net_value: 447119.44, net_rate: 0.0521, buy_value: 12030000, sell_value: 11582880.56, range_days: 1 },
  { thscode: '600519.SH', ticker: '600519', name: '贵州茅台', change: 0.012, net_value: 93000000, net_rate: 0.04, buy_value: 200000000, sell_value: 107000000, range_days: 1 },
]

const _payload = {
  state: 'ok' as const,
  trade_date: '2026-10-29',
  all: { trade_date: '2026-10-29', stock_count: _items.length, count: _items.length, stock_items: _items },
  org: { trade_date: '2026-10-29', stock_count: 1, count: 1, stock_items: [_items[3]] },
  hot_money: { trade_date: '2026-10-29', count: 1, hot_money_items: [
    { name: 'T王', buying: 5000000, rows: [_items[0], _items[1]] },
  ] },
}

vi.mock('@/lib/api', () => ({
  api: { dragonTiger: vi.fn(async () => _payload) },
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
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

async function renderCard(onOpenStock = vi.fn()) {
  const errors: unknown[] = []
  const spy = vi.spyOn(console, 'error').mockImplementation((...args) => { errors.push(args) })
  await act(async () => root.render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <DragonTigerCard date="2026-10-29" onOpenStock={onOpenStock} />
      </QueryClientProvider>
    </MemoryRouter>,
  ))
  await act(async () => { await new Promise(r => setTimeout(r, 80)) })
  spy.mockRestore()
  return { errors, onOpenStock }
}

async function expand() {
  const btn = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('查看三榜明细'))
  expect(btn).toBeDefined()
  await act(async () => { btn!.click() })
  await act(async () => { await new Promise(r => setTimeout(r, 60)) })
}

it('含 thscode=null 的标的: 展开三榜明细不崩溃, 以名称/ticker 兜底展示', async () => {
  const { errors } = await renderCard()
  expect(errors.some(e => String(e).includes('TypeError'))).toBe(false)

  await expand()
  expect(errors.some(e => String(e).includes('TypeError'))).toBe(false)
  // 三条 null thscode 标的都以名称 + ticker 展示
  expect(host.textContent).toContain('萃华退')
  expect(host.textContent).toContain('宁通信B')
  expect(host.textContent).toContain('动力B股')
  expect(host.textContent).toContain('002731')
})

it('点击 thscode=null 的行用 ticker 兜底回调, 不崩溃', async () => {
  const { errors, onOpenStock } = await renderCard()
  await expand()

  const row = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('萃华退'))
  expect(row).toBeDefined()
  await act(async () => { row!.click() })

  expect(errors.some(e => String(e).includes('TypeError'))).toBe(false)
  expect(onOpenStock).toHaveBeenCalledWith('002731')
})
