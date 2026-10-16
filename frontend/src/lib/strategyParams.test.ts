import { describe, expect, it } from 'vitest'
import type { StrategyParamDef } from '@/lib/api'
import { groupParams, isParamVisible, paramDisplayLabel, visibleParams } from '@/lib/strategyParams'

const p = (id: string, extra: Partial<StrategyParamDef> = {}): StrategyParamDef => ({
  id,
  label: id,
  type: 'float',
  default: 0,
  ...extra,
})

describe('isParamVisible', () => {
  it('无 visible_if 恒显示', () => {
    expect(isParamVisible(p('a'), {})).toBe(true)
  })

  it('单条件: values 命中/未命中/缺省回落 default', () => {
    const def = p('use_x', { visible_if: { param: 'pool', in: ['custom'] } })
    const pool = p('pool', { type: 'select', default: 'classic4' })
    expect(isParamVisible(def, { pool: 'custom' }, [pool, def])).toBe(true)
    expect(isParamVisible(def, { pool: 'classic4' }, [pool, def])).toBe(false)
    // values 缺省 → 按 pool 的 default('classic4') 判定 → 隐藏
    expect(isParamVisible(def, {}, [pool, def])).toBe(false)
  })

  it('边界: 空 all 为空真、空 any 为 false', () => {
    expect(isParamVisible(p('a', { visible_if: { all: [] } }), {})).toBe(true)
    expect(isParamVisible(p('a', { visible_if: { any: [] } }), {})).toBe(false)
  })

  it('边界: 引用不存在的 param → 隐藏（不报错）', () => {
    const def = p('a', { visible_if: { param: 'nonexistent', in: [1] } })
    expect(isParamVisible(def, {})).toBe(false)
  })

  it('bool 容错: in 值误写字符串 "true"/"True" 也能匹配布尔值', () => {
    const def = p('use_x', { type: 'bool', default: true, visible_if: { param: 'use_x', in: ['true'] } })
    expect(isParamVisible(def, { use_x: true }, [def])).toBe(true)
    expect(isParamVisible(def, { use_x: false }, [def])).toBe(false)
    const def2 = p('use_x', { type: 'bool', default: true, visible_if: { param: 'use_x', in: ['True'] } })
    expect(isParamVisible(def2, { use_x: true }, [def2])).toBe(true)
  })

  it('递归: all / any 组合（标的池联动场景）', () => {
    // 显示条件 = pool ∈ presets OR (pool=custom AND use_gold)
    const def = p('tp_gold', {
      visible_if: {
        any: [
          { param: 'pool', in: ['classic4', 'no_hs300'] },
          { all: [{ param: 'pool', in: ['custom'] }, { param: 'use_gold', in: [true] }] },
        ],
      },
    })
    const useGold = p('use_gold', { type: 'bool', default: true })
    const all = [def, useGold]
    // 预设模式: 不看勾选状态
    expect(isParamVisible(def, { pool: 'classic4', use_gold: false }, all)).toBe(true)
    // custom 勾选 → 显示; 取消勾选 → 隐藏
    expect(isParamVisible(def, { pool: 'custom', use_gold: true }, all)).toBe(true)
    expect(isParamVisible(def, { pool: 'custom', use_gold: false }, all)).toBe(false)
  })
})

describe('visibleParams / groupParams', () => {
  it('过滤隐藏参数并按组归并（保序）', () => {
    const params = [
      p('pool', { type: 'select', default: 'classic4', group: '标的池' }),
      p('use_x', { type: 'bool', default: true, group: '标的池', visible_if: { param: 'pool', in: ['custom'] } }),
      p('m_days', { group: '动量' }),
      p('tp_x', { group: '止盈' }),
      p('lock_x', { group: '止盈' }),
    ]
    const visible = visibleParams(params, { pool: 'classic4' })
    expect(visible.map(x => x.id)).toEqual(['pool', 'm_days', 'tp_x', 'lock_x'])
    const groups = groupParams(visible)
    expect(groups.map(g => [g.name, g.items.length])).toEqual([
      ['标的池', 1],
      ['动量', 1],
      ['止盈', 2],
    ])
  })

  it('非相邻同组名不归并（保序分节）', () => {
    const groups = groupParams([
      p('a', { group: 'X' }),
      p('b', { group: 'Y' }),
      p('c', { group: 'X' }),
    ])
    expect(groups.map(g => [g.name, g.items.length])).toEqual([
      ['X', 1],
      ['Y', 1],
      ['X', 1],
    ])
  })
})

describe('paramDisplayLabel', () => {
  it('有组名加「组名 · 标签」前缀（平铺列表可区分），无组名原样', () => {
    expect(paramDisplayLabel(p('tp_gold', { label: '止盈', group: '黄金 159934.SZ' })))
      .toBe('黄金 159934.SZ · 止盈')
    expect(paramDisplayLabel(p('m_days', { label: '动量窗口(交易日)' })))
      .toBe('动量窗口(交易日)')
  })
})
