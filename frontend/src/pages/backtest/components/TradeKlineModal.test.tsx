// @vitest-environment jsdom
import { act, useState, type ComponentProps } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { StrategyBacktestTrade } from '@/lib/api'
import type { TradeNavItem } from '../tradeNav'
import { TradeKlineModal } from './TradeKlineModal'

let panel: Record<string, any>
vi.mock('@/components/StockPanel', () => ({ StockPanel: (props: Record<string, any>) => {
  panel = props
  return <div>
    <button onClick={() => props.onSelectDate('2026-10-12')}>选择历史日K</button>
    {props.showIntraday && <button onClick={() => props.onIntradayClose?.()}>收起分时图</button>}
  </div>
} }))
vi.mock('framer-motion', () => ({
  AnimatePresence: ({ children }: any) => children,
  motion: { div: ({ children, initial: _initial, animate: _animate, exit: _exit, transition: _transition, ...props }: any) => <div {...props}>{children}</div> },
}))

const first: StrategyBacktestTrade = {
  symbol: '600000.SH', name: '浦发银行', entry_date: '2026-09-01', exit_date: '2026-10-29',
  entry_price: 9.88, exit_price: 10.23, pnl_pct: 0.0354, pnl_amount: 350, duration: 38, exit_reason: 'signal',
}
let root: Root
let host: HTMLDivElement
let close: ReturnType<typeof vi.fn>
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  close = vi.fn()
})
afterEach(async () => { await act(async () => root.unmount()); host.remove() })
const render = (props: Partial<ComponentProps<typeof TradeKlineModal>> = {}) => act(async () => {
  root.render(<TradeKlineModal trade={first} onClose={close} {...props} />)
})
async function click(label: string) {
  const button = [...host.querySelectorAll('button')].find(b => (b.getAttribute('aria-label') ?? b.textContent?.trim()) === label)
  expect(button).toBeTruthy()
  await act(async () => button!.click())
}

it('uses Lightweight Charts and fits the entire trade with explicit buy/sell and holding annotations', async () => {
  await render()
  expect(panel.renderer).toBe('lightweight')
  expect(panel.visibleBars).toBe('all')
  expect(panel.dateRange).toEqual({ start: '2026-07-18', end: '2026-11-18' })
  expect(panel.ranges).toEqual([expect.objectContaining({ start: '2026-09-01', end: '2026-10-29' })])
  expect(panel.markers).toEqual([
    expect.objectContaining({ date: '2026-09-01', kind: 'buy', label: expect.stringContaining('9.88') }),
    expect.objectContaining({ date: '2026-10-29', kind: 'sell', label: expect.stringContaining('10.23') }),
  ])
  expect(panel.priceLines.map((line: any) => line.value)).toEqual([9.88, 10.23])
  expect(panel.showLimitMarkers).toBe(false)
  expect(panel.selectedDate).toBe('2026-09-01')
  expect(host.querySelector('[role="dialog"]')?.getAttribute('aria-modal')).toBe('true')
  expect(host.querySelector('[role="dialog"] button[aria-label="关闭交易回放"]')).not.toBeNull()
})

it('preserves ETF three-decimal fill prices in the summary, markers and price lines', async () => {
  await render({ assetType: 'etf', trade: { ...first, symbol: '159558.SZ', entry_price: 0.817, exit_price: 0.823 } })
  expect(host.textContent).toContain('0.817')
  expect(host.textContent).toContain('0.823')
  expect(panel.priceLines.map((line: any) => line.label)).toEqual(['买入价 0.817', '卖出价 0.823'])
  expect(panel.markers[0].label).toContain('0.817')
  expect(panel.markers[1].label).toContain('0.823')
})

it('resolves price precision from metadata for older results without an asset type and keeps the callback stable', async () => {
  const trade = { ...first, symbol: '159558.SZ', entry_price: 0.817, exit_price: 0.823 }
  await render({ trade })
  const onAssetType = panel.onAssetType
  await act(async () => onAssetType('etf'))
  expect(panel.onAssetType).toBe(onAssetType)
  expect(panel.priceLines[0].label).toBe('买入价 0.817')
  await act(async () => onAssetType('etf'))
  expect(panel.onAssetType).toBe(onAssetType)
  await render({ trade: first })
  expect(panel.priceLines[0].label).toBe('买入价 9.88')
})

it('selects entry/exit days, preserves clicked history and can reopen a dismissed intraday pane', async () => {
  await render()
  await click('分时')
  expect(panel.showIntraday).toBe(true)
  expect(panel.selectedDate).toBe('2026-09-01')
  await click('查看卖出日')
  expect(panel.selectedDate).toBe('2026-10-29')
  expect(panel.showIntraday).toBe(true)
  await click('选择历史日K')
  expect(panel.selectedDate).toBe('2026-10-12')
  await click('收起分时图')
  expect(panel.showIntraday).toBe(false)
  await click('分时')
  expect(panel.showIntraday).toBe(true)
  expect(panel.selectedDate).toBe('2026-10-12')
  await click('查看买入日')
  expect(panel.selectedDate).toBe('2026-09-01')
})

it('resets the selected date when navigating another trade of the same symbol while retaining intraday mode', async () => {
  const second = { ...first, entry_date: '2026-10-15', exit_date: '2026-11-05' }
  const items: TradeNavItem[] = [first, second].map((trade, i) => ({ key: String(i), rowIdx: i, trade }))
  function Harness() {
    const [current, setCurrent] = useState(items[0])
    return <TradeKlineModal trade={current.trade} navItems={items} currentKey={current.key}
      onNavigate={setCurrent} onClose={close} />
  }
  await act(async () => root.render(<Harness />))
  await click('选择历史日K')
  await act(async () => host.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })))
  expect(panel.selectedDate).toBe('2026-10-15')
  expect(panel.showIntraday).toBe(true)
  expect(panel.markers.map((marker: any) => marker.date)).toEqual(['2026-10-15', '2026-11-05'])
  await act(async () => host.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true })))
  expect(panel.selectedDate).toBe('2026-09-01')
})

it('keeps both directions on a same-day trade and merges coincident price lines', async () => {
  await render({ assetType: 'etf', trade: {
    ...first, symbol: '512890.SH', entry_date: '2026-11-05 10:03', exit_date: '2026-11-05 14:20',
    entry_price: 1.237, exit_price: 1.237, duration: 0,
  } })
  expect(host.textContent).toContain('10:03')
  expect(host.textContent).toContain('14:20')
  expect(panel.markers.map((marker: any) => marker.kind)).toEqual(['buy', 'sell'])
  expect(panel.priceLines).toHaveLength(1)
  expect(panel.priceLines[0].label).toBe('买卖价 1.237')
})

it('closes with Escape or its labeled button, and starts a reopened trade at the entry day', async () => {
  await render()
  await click('选择历史日K')
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(close).toHaveBeenCalledOnce()
  await click('关闭交易回放')
  expect(close).toHaveBeenCalledTimes(2)
  await render({ trade: null })
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  await render()
  expect(panel.selectedDate).toBe('2026-09-01')
  expect(panel.showIntraday).toBe(false)
})
