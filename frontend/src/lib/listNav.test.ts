// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { toNavItems, uniqueBy, wrapNavIndex, type NavItem } from './listNav'

describe('wrapNavIndex: 首↔尾循环的索引换算', () => {
  it('常规前进/后退不越界', () => {
    expect(wrapNavIndex(0, 1, 5)).toBe(1)
    expect(wrapNavIndex(3, -1, 5)).toBe(2)
  })

  it('末项前进回到首位, 首项后退落到末位', () => {
    expect(wrapNavIndex(4, 1, 5)).toBe(0)
    expect(wrapNavIndex(0, -1, 5)).toBe(4)
  })

  it('只有两项时前进与后退都落到另一项', () => {
    expect(wrapNavIndex(0, 1, 2)).toBe(1)
    expect(wrapNavIndex(0, -1, 2)).toBe(1)
  })
})

describe('uniqueBy: 按 key 去重保留首次出现', () => {
  it('重复 key 只保留首个 (切项不空跳、计数不虚高)', () => {
    const xs = [{ k: 'A', v: 1 }, { k: 'B', v: 2 }, { k: 'A', v: 3 }]
    expect(uniqueBy(xs, x => x.k)).toEqual([{ k: 'A', v: 1 }, { k: 'B', v: 2 }])
  })

  it('空列表返回空', () => {
    expect(uniqueBy([] as NavItem[], x => x.symbol)).toEqual([])
  })
})

describe('toNavItems: name 归一化', () => {
  it('缺字段与 null 统一为 undefined, 有值则保留', () => {
    const out = toNavItems([{ symbol: '600000', name: null }, { symbol: '000001' }, { symbol: '300750', name: '宁德时代' }])
    expect(out).toEqual([
      { symbol: '600000', name: undefined },
      { symbol: '000001', name: undefined },
      { symbol: '300750', name: '宁德时代' },
    ])
  })
})
