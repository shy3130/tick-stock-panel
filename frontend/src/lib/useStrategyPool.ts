import { useState, useCallback, useEffect, useRef } from 'react'
import { storage } from '@/lib/storage'
import { hydrateUiPrefs, setUiPref } from '@/lib/uiPrefs'

// 旧版按周期隔离的双池 ('strategy-pool' 日线 + 'strategy-pool-1m' 分钟) 已合并为
// 统一池 ('strategy-pool'): 策略自带周期声明, 执行时按各自 timeframes 路由,
// 池不再按周期隔离。此处做一次性迁移: 日线在前、分钟在后、按 ID 去重,
// 完成后移除旧分钟 key 保证幂等 (StrictMode 双调用 / HMR 重放均安全)。
function loadUnifiedPool(): string[] {
  const savedMinute = storage.strategyPoolMinute.get([])
  const savedDaily = storage.strategyPool.get([])
  const minute = Array.isArray(savedMinute) ? savedMinute.filter(id => typeof id === 'string') : []
  const daily = Array.isArray(savedDaily) ? savedDaily.filter(id => typeof id === 'string') : []
  if (minute.length === 0) {
    storage.strategyPoolMinute.remove()
    return daily
  }
  const merged = [...daily]
  for (const id of minute) {
    if (!merged.includes(id)) merged.push(id)
  }
  storage.strategyPool.set(merged)
  storage.strategyPoolMinute.remove()
  return merged
}

/**
 * 策略池 — 日线与分钟策略共用的统一池。
 * 卡片列表可按周期筛选显示, 但池本身只有一份;
 * addToPool 不再区分周期 (构建器/叠加策略创建的日线策略与分钟策略同池)。
 */
export function useStrategyPool() {
  const [pool, setPool] = useState<string[]>(loadUnifiedPool)
  const poolRef = useRef(pool)
  const edited = useRef(false)
  const hydrated = useRef(false)
  const validIdsRef = useRef<Set<string> | null>(null)

  useEffect(() => {
    let active = true
    void hydrateUiPrefs().then(() => {
      if (!active) return
      const restored = edited.current ? poolRef.current : loadUnifiedPool()
      const next = validIdsRef.current ? restored.filter(id => validIdsRef.current!.has(id)) : restored
      poolRef.current = next
      hydrated.current = true
      setPool(next)
      if (next.length !== restored.length) setUiPref('strategy-pool', JSON.stringify(next))
    })
    return () => { active = false }
  }, [])

  const updatePool = useCallback((update: (prev: string[]) => string[]) => {
    const next = update(poolRef.current)
    if (next === poolRef.current) return
    edited.current = true
    poolRef.current = next
    setPool(next)
    setUiPref('strategy-pool', JSON.stringify(next))
  }, [])

  const addToPool = useCallback((id: string) => {
    updatePool(prev => (prev.includes(id) ? prev : [...prev, id]))
  }, [updatePool])

  const removeFromPool = useCallback((id: string) => {
    updatePool(prev => prev.filter(x => x !== id))
  }, [updatePool])

  const reorderPool = useCallback((newOrder: string[]) => {
    updatePool(() => newOrder)
  }, [updatePool])

  // 清除池中不存在于 validIds 的失效策略(如本地开发残留的自定义策略)。
  // 调用方传入"全周期合并的策略列表" ID, 池内日线/分钟策略一并校验。
  // 仅当确实有失效项时才更新,避免无谓重渲染。
  const prune = useCallback((validIds: Iterable<string>) => {
    const validSet = validIds instanceof Set ? validIds : new Set(validIds)
    validIdsRef.current = validSet
    if (!hydrated.current) return
    updatePool(prev => {
      if (prev.length === 0) return prev
      const next = prev.filter(id => validSet.has(id))
      return next.length === prev.length ? prev : next
    })
  }, [updatePool])

  const isInPool = useCallback((id: string) => pool.includes(id), [pool])

  return { pool, addToPool, removeFromPool, reorderPool, prune, isInPool }
}
