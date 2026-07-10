import { useSyncExternalStore } from 'react'

/**
 * 参数优化任务管理 (SSE 模式 + 重连)。镜像 backtestTask, 结果为排名 dict。
 */

export interface OptimizeProgress {
  type: string
  done: number
  total: number
  best_score: number | null
}

export interface OptimizeResultRow {
  params: Record<string, any>
  objective_raw: number | null
  stats?: Record<string, any>
  rank: number
  error?: string
}

export interface OptimizeResult {
  objective: string
  direction: string
  n_combinations: number
  n_completed: number
  best_params: Record<string, any> | null
  best_score: number | null
  results: OptimizeResultRow[]
  elapsed_ms: number
}

export interface OptimizerTask {
  id: number
  isPending: boolean
  result: OptimizeResult | null
  progress: OptimizeProgress | null
  error: string | null
}

export interface StartOptimizeParams {
  strategy_id: string
  param_grid: Record<string, any>
  objective: string
  direction?: string
  max_workers?: number
  params?: Record<string, any> | null       // 未扫描参数固定为用户当前值
  overrides?: Record<string, any> | null     // 策略当前的 basic_filter/信号/风控覆盖
  symbols?: string[] | null
  start?: string | null
  end?: string | null
  matching?: string
  fees_pct?: number
  commission_pct?: number
  stamp_tax_pct?: number
  slippage_bps?: number
  max_positions?: number
  max_exposure_pct?: number
  initial_capital?: number
  position_sizing?: string
  mode?: 'position' | 'full'
  holding_days?: number
}

let current: OptimizerTask | null = null
const listeners = new Set<() => void>()
let taskSeq = 0
let eventSource: EventSource | null = null
let currentJobKey: string | null = null
let cancelRequested = false      // stop 在拿到 job_key 前被点 -> 收到 job 事件立即补发 cancel
let reconnectAttempts = 0        // 无 data 断线的连续重连计数, 超上限放弃
const MAX_RECONNECT = 5

const RECONNECT_KEY = 'optimizer_reconnect'
const JOB_KEY_KEY = 'optimizer_job_key'

function emit() {
  listeners.forEach(fn => fn())
}

function subscribe(fn: () => void) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

function buildQuery(params: Record<string, string | number | boolean | undefined | null>): string {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v != null && v !== '') sp.set(k, String(v))
  }
  return sp.toString()
}

function connectSSE(url: string): void {
  const id = current?.id ?? ++taskSeq

  if (eventSource) {
    eventSource.close()
    eventSource = null
  }

  const es = new EventSource(url)
  eventSource = es

  // 首事件: 后端回吐 job_key, 存下供 cancel 直接引用 (无需前端重算)
  es.addEventListener('job', (e: MessageEvent) => {
    reconnectAttempts = 0
    try {
      const key = JSON.parse(e.data)?.key
      if (key) {
        currentJobKey = key
        localStorage.setItem(JOB_KEY_KEY, key)
        // 竞态修复: stop 在拿到 key 前被点过 -> 现在补发 cancel 真正停后端任务, 再收尾关闭。
        if (cancelRequested) {
          postCancel(key)
          es.close()
          eventSource = null
          currentJobKey = null
          localStorage.removeItem(RECONNECT_KEY)
          localStorage.removeItem(JOB_KEY_KEY)
        }
      }
    } catch { /* ignore */ }
  })

  es.addEventListener('progress', (e: MessageEvent) => {
    if (current?.id !== id) return
    reconnectAttempts = 0
    try {
      const prog = JSON.parse(e.data) as OptimizeProgress
      current = { ...current, progress: prog }
      emit()
    } catch { /* ignore */ }
  })

  es.addEventListener('done', (e: MessageEvent) => {
    if (current?.id !== id) return
    try {
      const result = JSON.parse(e.data) as OptimizeResult
      current = { ...current, isPending: false, result, error: null }
      emit()
    } catch {
      current = { ...current, isPending: false, error: '结果解析失败' }
      emit()
    }
    es.close()
    eventSource = null
    currentJobKey = null
    localStorage.removeItem(RECONNECT_KEY)
    localStorage.removeItem(JOB_KEY_KEY)
  })

  es.addEventListener('error', (e: MessageEvent) => {
    if (current?.id !== id) return
    if (e.data) {
      try {
        const msg = JSON.parse(e.data)?.message ?? '优化出错'
        current = { ...current, isPending: false, error: msg }
        emit()
      } catch {
        current = { ...current, isPending: false, error: '优化出错' }
        emit()
      }
      es.close()
      eventSource = null
      currentJobKey = null
      localStorage.removeItem(RECONNECT_KEY)
      localStorage.removeItem(JOB_KEY_KEY)
      return
    }
    // 无 data: 连接异常断开。EventSource 会自动重连, 但设上限避免网络长断时无限 pending。
    if (current?.id === id) {
      reconnectAttempts += 1
      if (reconnectAttempts > MAX_RECONNECT) {
        es.close()
        eventSource = null
        current = { ...current, isPending: false, error: '连接中断, 重连多次失败' }
        emit()
      }
    }
  })
}

/** 调后端 cancel (按回吐的 job_key)。 */
function postCancel(jobKey: string): void {
  fetch('/api/backtest/optimize/cancel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ job_key: jobKey }),
  }).catch(() => {})
}

export function startOptimize(params: StartOptimizeParams): void {
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }

  cancelRequested = false
  currentJobKey = null
  reconnectAttempts = 0
  const id = ++taskSeq
  current = { id, isPending: true, result: null, progress: null, error: null }
  emit()

  const qs = buildQuery({
    strategy_id: params.strategy_id,
    param_grid: JSON.stringify(params.param_grid),
    objective: params.objective,
    direction: params.direction,
    max_workers: params.max_workers,
    params: params.params ? JSON.stringify(params.params) : undefined,
    overrides: params.overrides ? JSON.stringify(params.overrides) : undefined,
    symbols: params.symbols?.join(','),
    start: params.start ?? undefined,
    end: params.end ?? undefined,
    matching: params.matching,
    fees_pct: params.fees_pct,
    commission_pct: params.commission_pct,
    stamp_tax_pct: params.stamp_tax_pct,
    slippage_bps: params.slippage_bps,
    max_positions: params.max_positions,
    max_exposure_pct: params.max_exposure_pct,
    initial_capital: params.initial_capital,
    position_sizing: params.position_sizing,
    mode: params.mode,
    holding_days: params.holding_days,
  })

  localStorage.setItem(RECONNECT_KEY, qs)
  connectSSE(`/api/backtest/optimize/stream?${qs}`)
}

export function stopOptimize(): void {
  // 竞态: 若刚点开始还没收到 job 事件, job_key 尚未到手。标记 cancelRequested ——
  // 有 key 则立即取消并关闭; 无 key 则保持 SSE 打开, 等 job 事件到达时补发 cancel 再关
  // (关闭 SSE 不会停后端 daemon 线程, 必须真正 POST cancel)。5s 兜底防 job 事件永不来。
  cancelRequested = true
  const jobKey = currentJobKey ?? localStorage.getItem(JOB_KEY_KEY)
  if (jobKey) {
    postCancel(jobKey)
    if (eventSource) { eventSource.close(); eventSource = null }
    currentJobKey = null
    localStorage.removeItem(RECONNECT_KEY)
    localStorage.removeItem(JOB_KEY_KEY)
  } else if (eventSource) {
    // 保持连接等 job 事件; 兜底: 5s 后仍没 key 就强关
    const es = eventSource
    setTimeout(() => { if (es === eventSource) { es.close(); eventSource = null } }, 5000)
  }
  if (current?.isPending) {
    current = { ...current, isPending: false, error: '已取消' }
    emit()
  }
}

export function clearOptimize(): void {
  current = null
  emit()
}

export function tryReconnectOptimize(): boolean {
  const qs = localStorage.getItem(RECONNECT_KEY)
  if (!qs) return false
  const id = ++taskSeq
  current = { id, isPending: true, result: null, progress: null, error: null }
  emit()
  connectSSE(`/api/backtest/optimize/stream?${qs}`)
  return true
}

export function useOptimizerTask(): OptimizerTask | null {
  return useSyncExternalStore(subscribe, () => current, () => null)
}
