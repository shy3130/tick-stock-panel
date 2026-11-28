// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StockIntradayChart } from './StockIntradayChart'

const fixture = vi.hoisted(() => ({ data: {
  rows: [{ datetime: '2026-11-13 09:30:00', open: 10, high: 11, low: 9, close: 10, volume: 10, amount: 10000 }],
  source: 'local_partial', actual: 1, expected: 243, asset_type: 'stock',
} }))
vi.mock('@tanstack/react-query', () => ({
  useQuery: () => ({ data: fixture.data }), useQueryClient: () => ({}), useMutation: () => ({ mutate: vi.fn(), isPending: false }),
}))
vi.mock('./charts/CustomChart', () => ({ CustomIntraday: ({ height, chartNotice, alignedLayout }: any) =>
  <div data-testid="custom-minute" data-height={height} data-aligned={alignedLayout}>{chartNotice}</div> }))
vi.mock('./charts/LightweightIntraday', () => ({ LightweightIntraday: ({ height }: any) => <output>{height}</output> }))
vi.mock('./EChartsIntraday', () => ({ EChartsIntraday: ({ height }: any) => <output>{height}</output> }))

let host: HTMLDivElement, root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); root = createRoot(host)
})
afterEach(async () => { await act(async () => root.unmount()) })

it('keeps the paired price budget intact and passes partial-data feedback into the shared header', async () => {
  await act(async () => root.render(<StockIntradayChart symbol="A" date="2026-11-13" renderer="lightweight" height={420} customIndicators alignedLayout />))
  const chart = host.querySelector('[data-testid="custom-minute"]')!
  expect(chart.getAttribute('data-height')).toBe('420')
  expect(chart.getAttribute('data-aligned')).toBe('true')
  expect(chart.textContent).toContain('1/243 根')
  expect(host.textContent?.match(/数据不完整/g)).toHaveLength(1)
  await act(async () => root.render(<StockIntradayChart symbol="A" date="2026-11-13" renderer="lightweight" height={420} />))
  expect(host.querySelector('output')?.textContent).toBe('388')
  await act(async () => root.render(<StockIntradayChart symbol="A" date="2026-11-13" renderer="echarts" height={420} customIndicators />))
  expect(host.textContent?.match(/数据不完整/g)).toHaveLength(1)
})
