// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { getSortValue } from './stock-table'
import type { ColumnConfig } from './list-columns'

function builtinCol(key: string): ColumnConfig {
  return { id: `builtin:${key}`, source: { type: 'builtin', key }, label: key, visible: false, align: 'center' }
}

describe('getSortValue: 自选加入信息列', () => {
  it('added_at 返回北京时间完整串 (与展示同口径, 字典序即时间序)', () => {
    expect(getSortValue({ added_at: '2026-09-18T20:30:00' }, builtinCol('added_at')))
      .toBe('2026-09-19 04:30:00')
  })

  it('added_at 缺失返回 null 而非空串', () => {
    // useTableSort 会先试 Number('') === 0, 返回空串会让空值排到最前
    for (const v of ['', null, undefined]) {
      expect(getSortValue({ added_at: v }, builtinCol('added_at'))).toBe(null)
    }
  })

  it('pct_since_added 透传数值 (含 0 与负值)', () => {
    expect(getSortValue({ pct_since_added: 0.1 }, builtinCol('pct_since_added'))).toBe(0.1)
    expect(getSortValue({ pct_since_added: -0.05 }, builtinCol('pct_since_added'))).toBe(-0.05)
    expect(getSortValue({ pct_since_added: 0 }, builtinCol('pct_since_added'))).toBe(0)
    expect(getSortValue({}, builtinCol('pct_since_added'))).toBe(undefined)
  })
})
