import { useCallback, useState } from 'react'

/**
 * 记忆"上次查看的个股"(按页面维度,localStorage 持久化)。
 *
 * 两个分析页(财务 / 个股)各自独立记忆,key 区分:
 *   - financials: 最后查看的财务分析个股
 *   - stock-analysis: 最后查看的个股分析个股
 *
 * 用法:
 *   const { last, remember } = useLastStock('stock-analysis')
 *   remember('000001.SZ', '平安银行')   // 选中股票时调用
 *   <LastStockChip stock={last} ... />  // 渲染在 PageHeader 右侧
 */

export interface StockRef { symbol: string; name: string }

const PREFIX = 'last_stock:'

export function useLastStock(scope: string) {
  const [last, setLast] = useState<StockRef | null>(() => load(scope))

  const remember = useCallback((symbol: string, name: string) => {
    const ref = { symbol, name }
    setLast(ref)
    save(scope, ref)
  }, [scope])

  const clear = useCallback(() => {
    setLast(null)
    save(scope, null)
  }, [scope])

  return { last, remember, clear }
}

function load(scope: string): StockRef | null {
  try {
    const v = localStorage.getItem(PREFIX + scope)
    if (!v) return null
    const p = JSON.parse(v)
    if (p && typeof p.symbol === 'string' && typeof p.name === 'string') return p
  } catch { /* ignore */ }
  return null
}

function save(scope: string, ref: StockRef | null) {
  try {
    if (ref) localStorage.setItem(PREFIX + scope, JSON.stringify(ref))
    else localStorage.removeItem(PREFIX + scope)
  } catch { /* ignore */ }
}
