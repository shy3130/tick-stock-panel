import { useSyncExternalStore } from 'react'
import type { FactorBacktestResult } from './api'
import { storage } from './storage'
import type { RunConnectionState } from './runStatus'

export interface FactorBacktestPayload {
  factor_name: string
  symbols?: string[] | null
  start?: string | null
  end?: string | null
  n_groups?: number
  rebalance?: 'daily' | 'weekly' | 'monthly'
  weight?: 'equal' | 'factor_weight'
  fees_pct?: number
  slippage_bps?: number
  risk_free_rate?: number
}

export interface FactorBacktestProgress {
  stage: string
  label: string
  completed: number
  total: number
  elapsed_ms?: number
}

export interface FactorBacktestTask {
  id: number
  isPending: boolean
  payload: FactorBacktestPayload
  result: FactorBacktestResult | null
  progress: FactorBacktestProgress | null
  error: string | null
  /** SSE 连接状态, 供运行状态条显示断线提示 */
  connectionState: RunConnectionState
}

const RECONNECT_KEY = 'factor-backtest-reconnect'
const DEFAULT_RANGE_DAYS = 180

let taskSeq = 0
let current: FactorBacktestTask | null = restoreCompletedTask()
let eventSource: EventSource | null = null
const listeners = new Set<() => void>()

function dayString(value: Date): string {
  return value.toISOString().slice(0, 10)
}

function defaultStart(end: string): string {
  const value = new Date(`${end}T00:00:00Z`)
  value.setUTCDate(value.getUTCDate() - DEFAULT_RANGE_DAYS)
  return dayString(value)
}

/**
 * 把隐式后端默认值冻结到 query 中。取消和刷新重连据此重建同一 job key，
 * 避免任务跨过本地/服务端的午夜时因默认日期漂移而失联。
 */
function normalizePayload(payload: FactorBacktestPayload): FactorBacktestPayload {
  const end = payload.end || dayString(new Date())
  const symbols = (payload.symbols ?? [])
    .map(symbol => symbol.trim())
    .filter(Boolean)
  return {
    factor_name: payload.factor_name,
    symbols: symbols.length > 0 ? symbols : null,
    start: payload.start === null ? null : payload.start || defaultStart(end),
    end,
    n_groups: payload.n_groups ?? 5,
    rebalance: payload.rebalance ?? 'monthly',
    weight: payload.weight ?? 'equal',
    fees_pct: payload.fees_pct ?? 0.0002,
    slippage_bps: payload.slippage_bps ?? 5,
    risk_free_rate: payload.risk_free_rate ?? 0,
  }
}

function buildQuery(payload: FactorBacktestPayload): string {
  const query = new URLSearchParams()
  query.set('factor_name', payload.factor_name)
  if (payload.symbols?.length) query.set('symbols', payload.symbols.join(','))
  // 空 start 是后端约定的“全部历史”；必须保留 key，不能在序列化时丢失。
  if (payload.start === null) query.set('start', '')
  else if (payload.start) query.set('start', payload.start)
  if (payload.end) query.set('end', payload.end)
  if (payload.n_groups != null) query.set('n_groups', String(payload.n_groups))
  if (payload.rebalance) query.set('rebalance', payload.rebalance)
  if (payload.weight) query.set('weight', payload.weight)
  if (payload.fees_pct != null) query.set('fees_pct', String(payload.fees_pct))
  if (payload.slippage_bps != null) query.set('slippage_bps', String(payload.slippage_bps))
  if (payload.risk_free_rate != null) query.set('risk_free_rate', String(payload.risk_free_rate))
  return query.toString()
}

function payloadFromQuery(query: string): FactorBacktestPayload | null {
  const params = new URLSearchParams(query)
  const factorName = params.get('factor_name')?.trim()
  if (!factorName) return null
  const numberOrUndefined = (name: string): number | undefined => {
    const raw = params.get(name)
    if (raw == null || raw === '') return undefined
    const value = Number(raw)
    return Number.isFinite(value) ? value : undefined
  }
  const rebalance = params.get('rebalance')
  const weight = params.get('weight')
  return normalizePayload({
    factor_name: factorName,
    symbols: params.get('symbols')?.split(',').map(symbol => symbol.trim()).filter(Boolean) ?? null,
    start: params.has('start') ? params.get('start') || null : undefined,
    end: params.get('end') || undefined,
    n_groups: numberOrUndefined('n_groups'),
    rebalance: rebalance === 'daily' || rebalance === 'weekly' || rebalance === 'monthly'
      ? rebalance
      : undefined,
    weight: weight === 'equal' || weight === 'factor_weight' ? weight : undefined,
    fees_pct: numberOrUndefined('fees_pct'),
    slippage_bps: numberOrUndefined('slippage_bps'),
    risk_free_rate: numberOrUndefined('risk_free_rate'),
  })
}

function restoreCompletedTask(): FactorBacktestTask | null {
  const saved = storage.factorBacktestLast.get(null)
  if (!saved?.result) return null
  return {
    id: ++taskSeq,
    isPending: false,
    payload: normalizePayload(saved.payload),
    result: saved.result,
    progress: null,
    error: null,
    connectionState: 'closed',
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

function closeEventSource(): void {
  if (!eventSource) return
  eventSource.close()
  eventSource = null
}

function completeWithError(id: number, message: string, source: EventSource): void {
  if (current?.id !== id) return
  current = { ...current, isPending: false, error: message, connectionState: 'closed' }
  emit()
  source.close()
  if (eventSource === source) eventSource = null
  localStorage.removeItem(RECONNECT_KEY)
}

function connectSSE(query: string, id: number): void {
  closeEventSource()
  const source = new EventSource(`/api/backtest/factor/stream?${query}`)
  eventSource = source

  // SSE 连接状态跟踪: onopen 置 open; 断线错误按 readyState 区分自动重连/彻底断开
  source.onopen = () => {
    if (current?.id !== id || current.connectionState === 'open') return
    current = { ...current, connectionState: 'open' }
    emit()
  }

  source.addEventListener('progress', event => {
    if (current?.id !== id) return
    try {
      const progress = JSON.parse((event as MessageEvent).data) as FactorBacktestProgress
      current = { ...current, progress }
      emit()
    } catch {
      // 单条进度包损坏不影响服务端任务或后续结果。
    }
  })

  source.addEventListener('done', event => {
    if (current?.id !== id) return
    try {
      const result = JSON.parse((event as MessageEvent).data) as FactorBacktestResult
      current = { ...current, isPending: false, result, progress: null, error: null, connectionState: 'closed' }
      storage.factorBacktestLast.set({ payload: current.payload, result })
      emit()
    } catch {
      completeWithError(id, '因子回测结果解析失败', source)
      return
    }
    source.close()
    if (eventSource === source) eventSource = null
    localStorage.removeItem(RECONNECT_KEY)
  })

  source.addEventListener('error', event => {
    if (current?.id !== id) return
    // 无 data 是浏览器网络层断线，EventSource 会自动重连；不可把仍运行的任务误报失败。
    const data = (event as MessageEvent).data
    if (!data) {
      // CONNECTING=浏览器自动重连中, CLOSED=彻底断开 (字面量避免 mock 缺静态属性)
      const connection = source.readyState === 0
        ? 'reconnecting'
        : source.readyState === 2 ? 'closed' : null
      if (connection && current.connectionState !== connection) {
        current = { ...current, connectionState: connection }
        emit()
      }
      return
    }
    try {
      completeWithError(id, JSON.parse(data)?.message ?? '因子回测失败', source)
    } catch {
      completeWithError(id, '因子回测失败', source)
    }
  })
}

/** 启动/订阅服务端因子任务；相同 query 可由刷新后的页面重新连接。 */
export function startFactorBacktest(payload: FactorBacktestPayload): void {
  const normalized = normalizePayload(payload)
  const query = buildQuery(normalized)
  const id = ++taskSeq
  const previousResult = current?.result ?? storage.factorBacktestLast.get(null)?.result ?? null
  current = {
    id,
    isPending: true,
    payload: normalized,
    result: previousResult,
    progress: null,
    error: null,
    connectionState: 'connecting',
  }
  emit()
  localStorage.setItem(RECONNECT_KEY, query)
  connectSSE(query, id)
}

/** 显式取消服务端任务；关闭当前订阅不会终止后台计算，故必须调用 cancel 端点。 */
export async function stopFactorBacktest(): Promise<void> {
  const query = localStorage.getItem(RECONNECT_KEY)
  if (query) {
    try {
      await fetch('/api/backtest/factor/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ qs: query }),
      })
    } catch {
      // 本地状态仍应退出“分析中”；后端任务若仍在运行可由下一次连接继续观察。
    }
  }
  closeEventSource()
  if (current?.isPending) {
    current = { ...current, isPending: false, error: '已取消', progress: null, connectionState: 'closed' }
    emit()
  }
  localStorage.removeItem(RECONNECT_KEY)
}

/** 刷新/路由重入时重新订阅尚未完成的同一服务端任务。 */
export function tryReconnectFactorBacktest(): boolean {
  if (current?.isPending) return true
  const query = localStorage.getItem(RECONNECT_KEY)
  if (!query) return false
  const payload = payloadFromQuery(query)
  if (!payload) {
    localStorage.removeItem(RECONNECT_KEY)
    return false
  }
  const id = ++taskSeq
  current = {
    id,
    isPending: true,
    payload,
    result: current?.result ?? storage.factorBacktestLast.get(null)?.result ?? null,
    progress: null,
    error: null,
    connectionState: 'connecting',
  }
  emit()
  connectSSE(query, id)
  return true
}

export function clearFactorBacktestTask(): void {
  closeEventSource()
  current = null
  storage.factorBacktestLast.set(null)
  localStorage.removeItem(RECONNECT_KEY)
  emit()
}

export function getFactorBacktestTask(): FactorBacktestTask | null {
  return current
}

export function useFactorBacktestTask(): FactorBacktestTask | null {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)
}
