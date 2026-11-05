// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it } from 'vitest'
import { StockInfoBar } from './StockInfoBar'

let cleanup = async () => {}
afterEach(async () => cleanup())

it('详情报价及开高低按资产精度显示, 涨跌幅仍为两位', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  const props = { symbol: '159558.SZ', onFieldsChange: () => {},
    rows: [{ date: '2026-11-04', open: 0.750, high: 0.755, low: 0.747, close: 0.752 },
      { date: '2026-11-05', open: 0.751, high: 0.828, low: 0.747, close: 0.827 }],
    fields: ['open', 'high', 'low'].map(key => ({ id: `builtin:${key}`, source: { type: 'builtin' as const, key }, label: key, visible: true })),
  }
  await act(async () => root.render(<StockInfoBar {...props} assetType="etf" />))
  for (const value of ['0.827', '+0.075', '+9.97%', '0.751', '0.828', '0.747']) expect(host.textContent).toContain(value)
  await act(async () => root.render(<StockInfoBar {...props} assetType="stock" />))
  expect(host.textContent).toContain('0.83')
  expect(host.textContent).not.toContain('0.827')
})
