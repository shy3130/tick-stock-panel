// @vitest-environment jsdom
import { act, type ComponentProps } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StockPanel } from './StockPanel'

const data = vi.hoisted(() => ({ rows: [{ date: '2026-11-05', close: 10, open: 10, high: 10, low: 10 }] }))
vi.mock('@tanstack/react-query', () => ({ useQuery: () => ({ data, isSuccess: true }), useQueryClient: () => ({}) }))
vi.mock('@/lib/useQuoteSubscription', () => ({ useQuoteSubscription: () => {} }))
vi.mock('@/lib/useSharedQueries', () => ({ useCapabilities: () => ({}) }))
vi.mock('@/lib/useFinancials', () => ({ useFinancialMetrics: () => ({}), financialMetricsQueryOptions: () => ({}) }))
vi.mock('./StockInfoBar', () => ({ StockInfoBar: () => null }))
vi.mock('./StockDailyKChart', () => ({
  getDefaultRange: () => ({ start: '2026-05-06', end: '2026-11-06' }),
  toOHLC: (rows: unknown) => rows,
  StockDailyKChart: ({ onDateClick }: any) => <button data-history onClick={() => onDateClick('2026-11-05')}>查看历史</button>,
}))
vi.mock('./StockIntradayChart', () => ({ StockIntradayChart: ({ date, prevClose }: { date: string; prevClose?: number }) => <output data-prev-close={prevClose}>{date}</output> }))
let root: Root
let host: HTMLDivElement
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  data.rows = [{ date: '2026-11-05', close: 10, open: 10, high: 10, low: 10 }]
  host = document.createElement('div'); root = createRoot(host)
})
afterEach(async () => { await act(async () => root.unmount()) })
const render = (symbol = '000985.SH', props: Partial<ComponentProps<typeof StockPanel>> = {}) => act(async () => root.render(<StockPanel symbol={symbol} {...props} />))

it('an automatically selected yesterday follows the newly published today', async () => {
  await render()
  expect(host.querySelector('output')?.textContent).toBe('2026-11-05')
  data.rows = [...data.rows, { ...data.rows[0], date: '2026-11-06' }]
  await render()
  expect(host.querySelector('output')?.textContent).toBe('2026-11-06')
})

it('explicit history selection survives a live update, then resets on symbol change', async () => {
  await render()
  await act(async () => host.querySelector<HTMLButtonElement>('[data-history]')!.click())
  data.rows = [...data.rows, { ...data.rows[0], date: '2026-11-06' }]
  await render()
  expect(host.querySelector('output')?.textContent).toBe('2026-11-05')
  await render('159558.SZ')
  expect(host.querySelector('output')?.textContent).toBe('2026-11-06')
})

it('keeps a replay-selected date on live updates and never uses a later close as its previous close', async () => {
  data.rows = [...data.rows, { ...data.rows[0], date: '2026-11-06', close: 12 }]
  await render('600000.SH', { selectedDate: '2026-11-05' })
  expect(host.querySelector('output')?.textContent).toBe('2026-11-05')
  expect(host.querySelector('output')?.getAttribute('data-prev-close')).toBeNull()
  await render('600000.SH', { selectedDate: '2026-11-06' })
  expect(host.querySelector('output')?.getAttribute('data-prev-close')).toBe('10')
  data.rows = [...data.rows, { ...data.rows[0], date: '2026-11-09', close: 13 }]
  await render('600000.SH', { selectedDate: '2026-11-06' })
  expect(host.querySelector('output')?.textContent).toBe('2026-11-06')
})

it('a controlled intraday close leaves visibility to the caller so the pane can reopen', async () => {
  const onIntradayClose = vi.fn()
  await render('600000.SH', { selectedDate: '2026-11-05', onIntradayClose })
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="收起分时图"]')!.click())
  expect(onIntradayClose).toHaveBeenCalledOnce()
  await render('600000.SH', { selectedDate: '2026-11-05', onIntradayClose, showIntraday: false })
  expect(host.querySelector('output')).toBeNull()
  await render('600000.SH', { selectedDate: '2026-11-05', onIntradayClose, showIntraday: true })
  expect(host.querySelector('output')?.textContent).toBe('2026-11-05')
})
