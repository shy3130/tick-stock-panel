// @vitest-environment node
import { describe, expect, it } from 'vitest'
import type { StrategyBacktestTrade } from '@/lib/api'
import { uniqueBy } from '@/lib/listNav'
import { buildDailyNavItems, buildTradeKeyMap, buildTradesNavItems } from './tradeNav'

function trade(over: Partial<StrategyBacktestTrade> & Pick<StrategyBacktestTrade, 'symbol' | 'entry_date' | 'exit_date'>): StrategyBacktestTrade {
  return {
    entry_price: 10,
    exit_price: 11,
    pnl_pct: 10,
    duration: 1,
    exit_reason: 'signal',
    ...over,
  }
}

describe('buildTradeKeyMap: 拆单交易不会撞 key', () => {
  it('同一标的同日同价的拆单各拿一个 key', () => {
    const trades = [
      trade({ symbol: '600000', entry_date: '2024-01-02', exit_date: '2024-01-05' }),
      trade({ symbol: '600000', entry_date: '2024-01-02', exit_date: '2024-01-05' }),
    ]
    const map = buildTradeKeyMap(trades)
    expect(map.get(trades[0])).toBe('0')
    expect(map.get(trades[1])).toBe('1')
    expect(new Set(map.values()).size).toBe(2)
  })
})

describe('buildTradesNavItems: 与表格同序', () => {
  it('rowIdx 即表格行号, key 唯一', () => {
    const a = trade({ symbol: 'A', entry_date: '2024-01-02', exit_date: '2024-01-05' })
    const b = trade({ symbol: 'B', entry_date: '2024-01-03', exit_date: '2024-01-08' })
    const keyMap = buildTradeKeyMap([a, b])
    // 表格按 exit_date 降序: B(01-08) 在前
    const items = buildTradesNavItems([b, a], keyMap)
    expect(items.map(i => [i.trade.symbol, i.key, i.rowIdx])).toEqual([
      ['B', '1', 0],
      ['A', '0', 1],
    ])
  })

  it('空结果返回空链', () => {
    expect(buildTradesNavItems([], buildTradeKeyMap([]))).toEqual([])
  })
})

describe('buildDailyNavItems: chips 展开与去重锚点', () => {
  it('同一天先买后卖展开, rowIdx 为该天所在行', () => {
    const b1 = trade({ symbol: 'A', entry_date: '2024-01-10', exit_date: '2024-01-12' })
    const s1 = trade({ symbol: 'B', entry_date: '2024-01-08', exit_date: '2024-01-10' })
    const keyMap = buildTradeKeyMap([b1, s1])
    // 表格按日期倒序: 01-12 行在前, 01-10 行在其后
    const rows = [
      { buys: [] as StrategyBacktestTrade[], sells: [b1] },
      { buys: [b1], sells: [s1] },
    ]
    const items = buildDailyNavItems(rows, keyMap)
    expect(items.map(i => [i.trade, i.key, i.rowIdx])).toEqual([
      [b1, '0', 0],
      [b1, '0', 1],
      [s1, '1', 1],
    ])
  })

  it('跨买卖两侧的交易经 useListNav 去重后只占一个位置, 锚在显示序更靠前的卖出日', () => {
    const t = trade({ symbol: 'A', entry_date: '2024-01-08', exit_date: '2024-01-12' })
    const keyMap = buildTradeKeyMap([t])
    // 日期倒序: 卖出日 01-12 在第 0 行, 买入日 01-08 在第 2 行
    const rows = [
      { buys: [], sells: [t] },
      { buys: [], sells: [] },
      { buys: [t], sells: [] },
    ]
    const chain = uniqueBy(buildDailyNavItems(rows, keyMap), i => i.key)
    expect(chain).toHaveLength(1)
    expect(chain[0].rowIdx).toBe(0)
  })

  it('不在回测交易里的对象不入链 (不产生无 key 的导航位)', () => {
    const stray = trade({ symbol: 'X', entry_date: '2024-01-02', exit_date: '2024-01-03' })
    const items = buildDailyNavItems([{ buys: [stray], sells: [] }], buildTradeKeyMap([]))
    expect(items).toEqual([])
  })
})
