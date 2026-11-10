import { useEffect } from 'react'
import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import type { StrategyLibraryInfo } from './api'
import { QK } from './queryKeys'

export interface StrategyChoice {
  id: string
  name: string
  library?: StrategyLibraryInfo | null
}

/** 新选择隐藏归档; 已保存的选择保留, 不改动运行配置或历史结果。 */
export function selectableStrategies<T extends StrategyChoice>(items: readonly T[], selected: readonly string[] = []): T[] {
  const keep = new Set(selected)
  return items.filter(item => !item.library?.archived || keep.has(item.id))
}

export function strategyOptionLabel(item: StrategyChoice): string {
  return `${item.name || item.id}${item.library?.archived ? '（已归档）' : ''}`
}

export function strategyChoiceGroups<T extends StrategyChoice>(items: readonly T[]) {
  const groups = new Map<string, { id: string; name: string; items: T[] }>()
  for (const item of items) {
    const id = item.library?.group ?? ''
    if (!groups.has(id)) groups.set(id, { id, name: item.library?.group_name || '未分组', items: [] })
    groups.get(id)!.items.push(item)
  }
  return [...groups.values()]
}

export const STRATEGY_LIBRARY_QUERY_KEYS = [
  QK.strategyPool, QK.strategyList().slice(0, 1), QK.screenerStrategies().slice(0, 1),
  QK.strategyLinkOptions(), QK.strategyDetail('').slice(0, 1), QK.resourceCatalog,
]
const CHANGE_KEY = 'tsp-strategy-library-changed'

export function notifyStrategyLibraryChanged() {
  try { localStorage.setItem(CHANGE_KEY, `${Date.now()}:${Math.random()}`) } catch { /* 隐私模式下仍可手动刷新 */ }
}

export async function invalidateStrategyLibrary(qc: QueryClient, includePool = true) {
  await Promise.allSettled(STRATEGY_LIBRARY_QUERY_KEYS
    .filter(key => includePool || key !== QK.strategyPool)
    .map(queryKey => qc.invalidateQueries({ queryKey })))
}

/** 宿主只挂一次, 其他窗口即使停在回测/模拟盘也会更新选择器。 */
export function useStrategyLibrarySync() {
  const qc = useQueryClient()
  useEffect(() => {
    const refresh = () => {
      void invalidateStrategyLibrary(qc)
      for (const queryKey of [QK.definitions, QK.customSignals, QK.customSignalsOptions, QK.factorColumns, QK.factorLibrary('all').slice(0, 1)]) {
        void qc.invalidateQueries({ queryKey })
      }
    }
    const onStorage = (event: StorageEvent) => {
      if (event.key === CHANGE_KEY) refresh()
    }
    // 桌面端口/浏览器 origin 不同的窗口没有 storage 事件, 回到窗口时校准库状态。
    const onVisible = () => { if (document.visibilityState === 'visible') refresh() }
    window.addEventListener('storage', onStorage)
    window.addEventListener('focus', refresh)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.removeEventListener('storage', onStorage)
      window.removeEventListener('focus', refresh)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [qc])
}
