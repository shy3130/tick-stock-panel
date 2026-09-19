import type { StrategyBacktestTrade } from '@/lib/api'

/**
 * 单笔回放的切交易链构建 (纯函数, 见 tradeNav.test.ts)。
 * 链里的 key 决定切项顺序与去重, rowIdx 决定切项时翻到哪一页, 由调用方交给 useListNav。
 */

/** 交易回放导航项: 同一笔交易在链中只占一个位置 */
export interface TradeNavItem {
  key: string
  trade: StrategyBacktestTrade
  /** 在所属表格中的行序号 (交易明细按行, 每日交易按天), 供调用方同步翻页 */
  rowIdx: number
}

/** 交易导航 key (模块级函数, 保证引用稳定) */
export const tradeNavKey = (item: TradeNavItem): string => item.key

/** 每日交易的一行, 与表格一致 (同一天先买后卖) */
export interface DailyNavRow {
  buys: StrategyBacktestTrade[]
  sells: StrategyBacktestTrade[]
}

/**
 * 按回测原始交易序号建 key。
 * 不用 symbol+日期+价格: 同一标的同日同价的多笔拆单会撞 key, 切交易时少一笔。
 */
export function buildTradeKeyMap(trades: StrategyBacktestTrade[]): Map<StrategyBacktestTrade, string> {
  const map = new Map<StrategyBacktestTrade, string>()
  trades.forEach((t, i) => map.set(t, String(i)))
  return map
}

/** 交易明细链: 与表格同序 (exit_date 降序) */
export function buildTradesNavItems(
  sortedTrades: StrategyBacktestTrade[],
  keyMap: Map<StrategyBacktestTrade, string>,
): TradeNavItem[] {
  const out: TradeNavItem[] = []
  sortedTrades.forEach((t, rowIdx) => {
    const key = keyMap.get(t)
    if (key != null) out.push({ key, trade: t, rowIdx })
  })
  return out
}

/**
 * 每日交易链: 按表格显示序展开 chips。
 * 同一笔交易在买入日与卖出日各有一个 chip, 这里允许重复入链 (useListNav 按 key 去重),
 * 每笔交易锚定在显示序里更靠前的那个 chip。表格按日期倒序, 所以锚点是卖出日那一行。
 */
export function buildDailyNavItems(
  rows: DailyNavRow[],
  keyMap: Map<StrategyBacktestTrade, string>,
): TradeNavItem[] {
  const out: TradeNavItem[] = []
  rows.forEach((row, rowIdx) => {
    for (const t of [...row.buys, ...row.sells]) {
      const key = keyMap.get(t)
      if (key != null) out.push({ key, trade: t, rowIdx })
    }
  })
  return out
}
