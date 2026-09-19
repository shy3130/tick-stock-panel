/**
 * 列表导航的纯函数部分 (与 React 无关, 便于单测)。
 * 交互侧 (键盘/循环提示/邻项预取) 见 useListNav。
 */

/** 列表导航项 (股票等可按 key 定位的候选项) */
export interface NavItem { symbol: string; name?: string }

/** 把 symbol+name 的列表转成导航列表项 (统一 name 归一化为 undefined, 免去各处重复 map + as 断言) */
export function toNavItems<T extends { symbol: string; name?: string | null }>(xs: T[]): NavItem[] {
  return xs.map(x => ({ symbol: x.symbol, name: x.name ?? undefined }))
}

/** 股票列表的导航 key (模块级函数, 保证引用稳定) */
export const navItemKey = (item: NavItem): string => item.symbol

/**
 * 按 key 去重, 保留首次出现。
 * 榜单里同一标的可能多次出现 (多概念/行业 leader、监控重复触发、同一笔交易跨买卖两侧),
 * 不去重会让切项空跳或计数虚高。
 */
export function uniqueBy<T>(xs: T[], keyOf: (item: T) => string): T[] {
  const seen = new Set<string>()
  const out: T[] = []
  for (const x of xs) {
    const k = keyOf(x)
    if (seen.has(k)) continue
    seen.add(k)
    out.push(x)
  }
  return out
}

/** 首↔尾循环的索引换算 (切项与邻近预取共用同一换行规则) */
export function wrapNavIndex(navIdx: number, delta: number, navTotal: number): number {
  return (navIdx + delta + navTotal) % navTotal
}
