// @vitest-environment jsdom
import { act, lazy } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QK } from '@/lib/queryKeys'
import type { FrontendSlotContextMap, FrontendSlotRegistration } from '@/extensions/types'
import type { Props } from './StockPreviewDialogContent'

vi.mock('./StockPanel', () => ({
  getDefaultRange: () => ({ start: '2025-01-01', end: '2025-06-30' }),
  StockPanel: ({ symbol, dateRange, infoBarOnly }: {
    symbol: string; dateRange: { start: string }; infoBarOnly?: boolean
  }) => infoBarOnly ? <span>行情</span> : <span>日线 {symbol} {dateRange.start}</span>,
}))
vi.mock('./StockMultiDayIntradayChart', () => ({
  StockMultiDayIntradayChart: ({ symbol, days }: { symbol: string; days: number }) =>
    <span>分钟线 {symbol} {days}日</span>,
}))
vi.mock('./monitor/RuleEditor', () => ({ RuleEditor: () => null }))

const first = { symbol: '600000.SH', name: '浦发银行' }
const second = { symbol: '000001.SZ' }
let host: HTMLDivElement, root: Root, client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  localStorage.clear()
  client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })
  client.setQueryData(QK.watchlist, { symbols: [] })
  client.setQueryData(QK.monitorRules, { rules: [] })
  client.setQueryData(QK.customSignals, { signals: [] })
  client.setQueryData(QK.preferences, {})
})

afterEach(() => {
  act(() => root.unmount())
  client.clear()
  host.remove()
  vi.restoreAllMocks()
  vi.resetModules()
})

async function setup(slots: FrontendSlotRegistration[] = []) {
  const registry = await import('@/extensions/registry')
  if (slots.length) {
    await registry.loadFrontendExtensions({
      example: async () => ({ default: { id: 'example', apiVersion: 1, slots } }),
    })
  }
  registry.finalizeFrontendExtensions(new Set())
  expect(registry.getFrontendExtensionLoadErrors()).toEqual([])
  const { StockPreviewDialogContent } = await import('./StockPreviewDialogContent')
  const render = (stock: Pick<Props, 'symbol' | 'name'>) => {
    root.render(<QueryClientProvider client={client}>
      <StockPreviewDialogContent {...stock} onClose={() => {}} navList={[first, second]}
        onNavigate={(symbol, name) => render({ symbol, name })} />
    </QueryClientProvider>)
  }
  await act(async () => render(first))
  return (stock: Pick<Props, 'symbol' | 'name'>) => act(async () => render(stock))
}

function button(label: string) {
  const match = [...host.querySelectorAll('button')].find(el => el.textContent === label || el.title === label)
  if (!match) throw new Error(`找不到按钮 ${label}`)
  return match
}

function click(label: string) {
  act(() => button(label).click())
}

function tabs() {
  return [...host.querySelectorAll('[role=tab]')].map(el => el.textContent)
}

function panel() {
  return host.querySelector<HTMLElement>('[role=tabpanel]')!
}

it('没有扩展时，日 K 和分时正常切换，保留各自区间并刷新对应行情', async () => {
  await setup()
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  expect(tabs()).toEqual(['日 K', '分时'])
  expect(button('日 K').getAttribute('aria-selected')).toBe('true')
  click('1年')
  const daily = panel().textContent
  expect(daily).toContain('日线 600000.SH')
  click('刷新')
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kline', first.symbol] })
  click('分时')
  click('5日')
  expect(panel().textContent).toContain('分钟线 600000.SH 5日')
  click('刷新')
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kline-minute-range', first.symbol] })
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kline-minute', first.symbol] })
  click('日 K')
  expect(panel().textContent).toBe(daily)
  click('分时')
  expect(button('5日').getAttribute('aria-pressed')).toBe('true')
})

it('扩展按 order 和 id 追加，只有选中面板挂载，切股和重开遵循详情视图生命周期', async () => {
  const Research = vi.fn(({ symbol, name }: FrontendSlotContextMap['stock-preview.tabs']) =>
    <label>{symbol} {name ?? '无简称'}<input aria-label="草稿" /></label>)
  const Other = vi.fn(() => <div>其他面板</div>)
  const render = await setup([
    { name: 'stock-preview.tabs', id: 'daily', label: '研究', component: Research },
    { name: 'stock-preview.tabs', id: 'z', label: '附录', component: Other },
    { name: 'stock-preview.tabs', id: 'first', label: '概览', order: 1, component: Other },
    { name: 'stock-preview.footer', id: 'status', component: ({ view }) => <footer>{view}</footer> },
  ])
  expect(tabs()).toEqual(['日 K', '分时', '概览', '研究', '附录'])
  expect(Research).not.toHaveBeenCalled()
  expect(host.querySelector('footer')?.textContent).toBe('daily')
  click('研究')
  expect(panel().getAttribute('aria-labelledby')).toBe(button('研究').id)
  expect(panel().textContent).toBe('600000.SH 浦发银行')
  expect(host.querySelector('footer, [aria-label="分时周期"], [title="刷新"]')).toBeNull()
  expect(host.textContent).not.toContain('半年')
  panel().querySelector('input')!.value = '仅限第一只股票'
  await act(async () => {
    button('研究').dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
  })
  expect(button('研究').getAttribute('aria-selected')).toBe('true')
  expect(panel().textContent).toBe('000001.SZ 无简称')
  expect(panel().querySelector('input')!.value).toBe('')
  expect(Other).not.toHaveBeenCalled()
  click('分时')
  expect(panel().textContent).toContain('分钟线 000001.SZ')
  expect(host.querySelector('footer')?.textContent).toBe('intraday')
  click('日 K')
  expect(panel().textContent).toContain('日线 000001.SZ')
  click('附录')
  expect(panel().textContent).toBe('其他面板')
  await render({ symbol: null })
  await render(first)
  expect(button('日 K').getAttribute('aria-selected')).toBe('true')
})

it('单个懒加载扩展的加载和渲染失败局限于面板，仍能切回内置图表', async () => {
  let fail!: (error: Error) => void
  const load = vi.fn(() => new Promise<{ default: () => null }>((_, reject) => { fail = reject }))
  await setup([{ name: 'stock-preview.tabs', id: 'broken', label: '研究', component: lazy(load) }])
  expect(load).not.toHaveBeenCalled()
  click('研究')
  expect(panel().querySelector('[role=status]')).not.toBeNull()
  const log = vi.spyOn(console, 'error').mockImplementation(() => {})
  await act(async () => fail(new Error('测试加载失败')))
  expect(panel().textContent).toContain('扩展 example 暂时不可用')
  expect(log).toHaveBeenCalled()
  click('分时')
  expect(panel().textContent).toContain('分钟线 600000.SH')
})
