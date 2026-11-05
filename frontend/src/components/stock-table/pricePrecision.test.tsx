// @vitest-environment node
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import { renderBuiltinDataCell } from './primitives'
import type { ColumnConfig } from '@/lib/list-columns'

const cell = (key: string, row: Record<string, unknown>, assetType?: string) => {
  const column: ColumnConfig = { id: `builtin:${key}`, label: key, visible: true, source: { type: 'builtin', key }, align: 'right' }
  return renderToStaticMarkup(<table><tbody><tr>{renderBuiltinDataCell(row, column, assetType)}</tr></tbody></table>)
}

it('混合自选行的所有价格列按 asset_type 显示, 策略表可传入明确资产类型', () => {
  const fields = ['price', 'open', 'high', 'low', 'prev_close', 'change_amount', 'ma5', 'ma10', 'ma20', 'ma60', 'high_60d', 'low_60d', 'boll_upper', 'boll_lower', 'atr14', 'limit_up_price', 'limit_down_price']
  const values = Object.fromEntries(['close', 'open', 'high', 'low', 'prev_close', 'change_amount', 'ma5', 'ma10', 'ma20', 'ma60', 'high_60d', 'low_60d', 'boll_upper', 'boll_lower', 'atr_14', 'limit_up_price', 'limit_down_price'].map(key => [key, 0.827]))
  for (const field of fields) {
    expect(cell(field, { ...values, asset_type: 'etf' })).toContain('0.827')
    expect(cell(field, { ...values, asset_type: 'stock' })).toContain('0.83')
    expect(cell(field, values, 'etf')).toContain('0.827')
  }
  expect(cell('pct', { asset_type: 'etf', change_pct: 0.01234 })).toContain('+1.23%')
  expect(cell('vol_ratio', { asset_type: 'etf', vol_ratio_5d: 1.234 })).toContain('1.23')
})
