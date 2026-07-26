import { useState, useCallback, useEffect } from 'react'
import { storage } from '@/lib/storage'

const DEFAULT_POOL = ['next_day_direction']

export function useStrategyPool() {
  const [pool, setPool] = useState<string[]>(() => {
    const saved = storage.strategyPool.get(DEFAULT_POOL)
    return [...new Set([...DEFAULT_POOL, ...saved])]
  })

  // 同步写入 localStorage
  useEffect(() => { storage.strategyPool.set(pool) }, [pool])

  const addToPool = useCallback((id: string) => {
    setPool(prev => prev.includes(id) ? prev : [...prev, id])
  }, [])

  const removeFromPool = useCallback((id: string) => {
    setPool(prev => prev.filter(x => x !== id))
  }, [])

  const reorderPool = useCallback((newOrder: string[]) => {
    setPool(newOrder)
  }, [])

  // 清除池中不存在于 validIds 的失效策略(如本地开发残留的自定义策略)。
  // 仅当确实有失效项时才更新,避免无谓重渲染。
  const prune = useCallback((validIds: Iterable<string>) => {
    const validSet = validIds instanceof Set ? validIds : new Set(validIds)
    setPool(prev => {
      if (prev.length === 0) return prev
      const next = prev.filter(id => validSet.has(id))
      return next.length === prev.length ? prev : next
    })
  }, [])

  const isInPool = useCallback((id: string) => pool.includes(id), [pool])

  return { pool, addToPool, removeFromPool, reorderPool, prune, isInPool }
}
