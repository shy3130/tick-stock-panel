// @vitest-environment jsdom
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { DEFAULT_LAYOUT, widgetDef, type WidgetCtx } from './registry'
import type { WidgetType } from './layout'

const first = { symbol: '600000.SH', name: '浦发银行' }
const second = { symbol: '000001.SZ', name: '平安银行' }
const unnamed = { symbol: '600519.SH', name: '' }
const stocks = [first, second, unnamed, { symbol: '601318.SH', name: '中国平安' }]
const rank = { name: '银行', source_field: 'ext_banks.所属行业', count: 3, avg_pct: 0.02, up_count: 3, down_count: 0, amount: 1000 }

let host: HTMLDivElement
let root: Root
let client: QueryClient
let ctx: WidgetCtx

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient()
  ctx = {
    data: {
      as_of: '2026-11-18',
      quote_status: {},
      indices: [],
      limit: { limit_up: 10, broken: 0, failed: 0, limit_down: 2, max_boards: 4, tiers: [{ boards: 4, count: 6, stocks }] },
      breadth: { total: 10, up: 6, flat: 1, down: 3, up_pct: 60, down_pct: 30 },
      amount: { total: 10000, avg: 1000 },
      boards: [],
      distribution: [],
      trend: { above_ma5: 0, above_ma20: 0, above_ma60: 0, above_ma5_pct: 0, above_ma20_pct: 0, above_ma60_pct: 0, new_high: 0, new_low: 0 },
      activity: { avg_turnover: 2, vol_ratio: 1.5, high_turnover: 1, high_vol_ratio: 2 },
      radar: [],
      emotion: { score: 50, label: '中性' },
      top_gainers: [first, second],
      top_losers: [],
      turnover_leaders: [],
      active_leaders: [],
      industry_rank: { leading: [{ ...rank, leader: unnamed }], lagging: [] },
      concept_rank: { leading: [{ ...rank, name: '无扩展字段', source_field: null, leader: first }], lagging: [] },
    },
    score: 50,
    hasDepth: false,
    sealedReady: false,
    isSealedDegrade: true,
    hasExtensionRoute: path => path === '/limit-ladder' || path === '/industry-analysis',
    activeSymbol: () => undefined,
    openStock: vi.fn(),
    openAlert: vi.fn(),
    openDimension: vi.fn(),
  }
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function render(node: ReactNode) {
  await act(async () => root.render(<MemoryRouter><QueryClientProvider client={client}>{node}</QueryClientProvider></MemoryRouter>))
}

function widget(type: WidgetType) {
  return widgetDef(type)!.render(ctx, DEFAULT_LAYOUT.find(item => item.t === type)!)
}

async function click(label: string) {
  const button = host.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)
  expect(button).not.toBeNull()
  await act(async () => button!.click())
}

it('opens highest-streak and ladder stocks with the other displayed securities available to navigate', async () => {
  await render(<>{widget('kpi')}</>)
  await click('查看 浦发银行 行情')
  expect(ctx.openStock).toHaveBeenCalledWith('highest-streak', first.symbol, first.name, [
    first, second, unnamed,
  ])
  await render(<>{widget('ladder')}</>)
  await click('查看 600519.SH 行情')
  expect(ctx.openStock).toHaveBeenLastCalledWith('ladder', unnamed.symbol, undefined, [
    first, second, unnamed, stocks[3],
  ])
  expect(host.querySelector('a[href="/limit-ladder?direction=up&date=2026-11-18"]')).not.toBeNull()
})

it('uses a native button across each stock ranking row', async () => {
  await render(widget('gainers'))
  const priceRow = host.querySelector<HTMLButtonElement>('button[aria-label="查看 平安银行 行情"]')
  expect(priceRow?.textContent).toContain('000001.SZ')
  await click('查看 平安银行 行情')
  expect(ctx.openStock).toHaveBeenCalledWith('gain', second.symbol, second.name, [first, second])
})

it('opens a leader separately from the sector constituents and falls back to the stock code', async () => {
  await render(<>{widget('industry-rank')}{widget('concept-rank')}</>)
  await click('查看 600519.SH 行情')
  expect(ctx.openStock).toHaveBeenCalledWith('industry', unnamed.symbol, undefined, [
    unnamed,
  ])
  expect(ctx.openDimension).not.toHaveBeenCalled()
  await click('查看银行成分股')
  expect(ctx.openDimension).toHaveBeenCalledWith({ kind: 'industry', value: '银行', sourceField: 'ext_banks.所属行业' })
  expect(host.querySelector('button[aria-label="查看无扩展字段成分股"]')).toBeNull()
  expect(host.querySelector('a[href="/concept-analysis"]')).toBeNull()
  expect(host.querySelector('a[href="/industry-analysis"]')).not.toBeNull()
})
