import { useSyncExternalStore } from 'react'
import { api, type FactorBacktestResult } from './api'
import { storage } from './storage'

export type FactorBacktestPayload = Parameters<typeof api.factorRun>[0]

export interface FactorBacktestTask {
  id: number
  isPending: boolean
  payload: FactorBacktestPayload
  result: FactorBacktestResult | null
  error: string | null
}

let taskSeq = 0
let current: FactorBacktestTask | null = restoreCompletedTask()
const listeners = new Set<() => void>()

function restoreCompletedTask(): FactorBacktestTask | null {
  const saved = storage.factorBacktestLast.get(null)
  if (!saved?.result) return null
  return {
    id: ++taskSeq,
    isPending: false,
    payload: saved.payload,
    result: saved.result,
    error: null,
  }
}

function emit(): void {
  listeners.forEach(listener => listener())
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function getSnapshot(): FactorBacktestTask | null {
  return current
}

function getServerSnapshot(): null {
  return null
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : '因子回测失败'
}

/**
 * 启动因子回测。状态属于模块级任务，而非页面组件：路由切换后请求和结果仍可见。
 * 因子端点没有可重连的服务端 job id；刷新前完成的最后结果按 best-effort 写入
 * localStorage，刷新中的请求不伪装成可恢复任务。
 */
export async function startFactorBacktest(payload: FactorBacktestPayload): Promise<void> {
  const id = ++taskSeq
  const previousResult = current?.result ?? storage.factorBacktestLast.get(null)?.result ?? null
  current = { id, isPending: true, payload, result: previousResult, error: null }
  emit()

  try {
    const result = await api.factorRun(payload)
    if (current?.id !== id) return
    current = { ...current, isPending: false, result, error: null }
    storage.factorBacktestLast.set({ payload, result })
  } catch (cause) {
    if (current?.id !== id) return
    current = { ...current, isPending: false, error: errorMessage(cause) }
  }
  emit()
}

export function clearFactorBacktestTask(): void {
  current = null
  storage.factorBacktestLast.set(null)
  emit()
}

export function getFactorBacktestTask(): FactorBacktestTask | null {
  return current
}

export function useFactorBacktestTask(): FactorBacktestTask | null {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)
}
