import { useSyncExternalStore } from 'react'
import { fetchRunningTask, reportTaskFinished, reportTaskRunning } from './taskRegistry'

/**
 * 稳健优化任务管理 (SSE + 重连 + 跨设备接回)。镜像 optimizerTask, 端点为
 * /optimize-robust/stream, 结果为 K 段稳健分排名 (设计: docs/robust-optimizer-plan.md)。
 */

export interface RobustProgress {
  type: string   // robust_optimizer_prepare | robust_optimizer_progress | robust_optimizer_stage2
  done: number
  total: number
  best_score?: number | null
  prepare_ms?: number
}

export interface RobustSegmentStat {
  start: string
  end: string
  objective_raw: number | null
  n_trades: number | null
  total_return?: number | null
  sharpe?: number | null
  max_drawdown?: number | null
}

export interface RobustResultRow {
  params: Record<string, any>
  rank: number
  score: number | null
  median: number | null
  worst: number | null
  spread: number | null
  oos_values: (number | null)[]
  segments: RobustSegmentStat[]
  trades_total: number | null
  disqualified?: boolean
  disqualify_reason?: string | null
  error?: string | null
  /** 阶段二全窗 IS 细化 (仅 Top-N 行有) */
  is_full?: { objective_raw?: number | null; score_norm?: number | null; n_trades?: number | null; total_return?: number | null; error?: string }
  degradation?: number
  degradation_pct?: number | null
}

export interface RobustConvergence {
  label: 'unanimous' | 'majority' | 'scattered' | 'no_data'
  champions: { segment: number; params: Record<string, any>; objective_raw: number }[]
  max_count: number
  k: number
}

export interface RobustPlateau {
  label: 'plateau' | 'slope' | 'peak' | 'isolated'
  ratio: number | null
  tolerance: number | null
  n_valid: number
  neighbors: { params: Record<string, any>; score: number | null; decay?: number; disqualified?: boolean; error?: string | null }[]
}

export interface RobustResult {
  objective: string
  direction: string
  k_segments: number
  windows: { start: string; end: string }[]
  n_combinations: number
  n_completed: number
  min_trades_total: number
  best_params: Record<string, any> | null
  best_score: number | null
  convergence: RobustConvergence
  plateau: RobustPlateau | null
  stage2: { top_n: number; evaluated: number }
  cancelled: boolean
  elapsed_ms: number
  results: RobustResultRow[]
}

export interface RobustTask {
  id: number
  isPending: boolean
  result: RobustResult | null
  progress: RobustProgress | null
  error: string | null
}

export interface StartRobustParams {
  strategy_id: string
  param_grid: Record<string, any>
  objective: string
  direction?: string
  k_segments?: number
  min_trades_total?: number
  stage2_top_n?: number
  max_workers?: number
  matrix_cache_max_mb?: number
  params?: Record<string, any> | null
  overrides?: Record<string, any> | null
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

let current: RobustTask | null = null
const listeners = new Set<() => void>()
let taskSeq = 0
let eventSource: EventSource | null = null
let currentJobKey: string | null = null
let cancelRequested = false
let reconnectAttempts = 0
const MAX_RECONNECT = 5

const RECONNECT_KEY = 'robust_optimizer_reconnect'
const JOB_KEY_KEY = 'robust_optimizer_job_key'

const TASK_KIND = 'robust_optimizer' as const

function finishTaskRegistry() {
  const qs = localStorage.getItem(RECONNECT_KEY)
  if (qs) reportTaskFinished(TASK_KIND, { qs })
  localStorage.removeItem(RECONNECT_KEY)
  localStorage.removeItem(JOB_KEY_KEY)
}

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

  es.addEventListener('job', (e: MessageEvent) => {
    reconnectAttempts = 0
    try {
      const key = JSON.parse(e.data)?.key
      if (key) {
        currentJobKey = key
        localStorage.setItem(JOB_KEY_KEY, key)
        if (cancelRequested) {
          postCancel(key)
          es.close()
          eventSource = null
          currentJobKey = null
          finishTaskRegistry()
        }
      }
    } catch { /* ignore */ }
  })

  es.addEventListener('progress', (e: MessageEvent) => {
    if (current?.id !== id) return
    reconnectAttempts = 0
    try {
      const prog = JSON.parse(e.data) as RobustProgress
      current = { ...current, progress: prog }
      emit()
    } catch { /* ignore */ }
  })

  es.addEventListener('done', (e: MessageEvent) => {
    if (current?.id !== id) return
    try {
      const result = JSON.parse(e.data) as RobustResult
      current = { ...current, isPending: false, result, error: null }
      emit()
    } catch {
      current = { ...current, isPending: false, error: '结果解析失败' }
      emit()
    }
    es.close()
    eventSource = null
    currentJobKey = null
    finishTaskRegistry()
  })

  es.addEventListener('error', (e: MessageEvent) => {
    if (current?.id !== id) return
    if (e.data) {
      try {
        const msg = JSON.parse(e.data)?.message ?? '稳健优化出错'
        current = { ...current, isPending: false, error: msg }
        emit()
      } catch {
        current = { ...current, isPending: false, error: '稳健优化出错' }
        emit()
      }
      es.close()
      eventSource = null
      currentJobKey = null
      finishTaskRegistry()
      return
    }
    if (current?.id === id) {
      reconnectAttempts += 1
      if (reconnectAttempts > MAX_RECONNECT) {
        es.close()
        eventSource = null
        finishTaskRegistry()
        current = { ...current, isPending: false, error: '连接中断, 重连多次失败' }
        emit()
      }
    }
  })
}

function postCancel(jobKey: string): void {
  fetch('/api/backtest/optimize-robust/cancel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ job_key: jobKey }),
  }).catch(() => {})
}

export function startRobust(params: StartRobustParams): void {
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
    k_segments: params.k_segments,
    min_trades_total: params.min_trades_total,
    stage2_top_n: params.stage2_top_n,
    max_workers: params.max_workers,
    matrix_cache_max_mb: params.matrix_cache_max_mb,
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
  reportTaskRunning(TASK_KIND, { qs }, params.strategy_id)
  connectSSE(`/api/backtest/optimize-robust/stream?${qs}`)
}

export function stopRobust(): void {
  cancelRequested = true
  const jobKey = currentJobKey ?? localStorage.getItem(JOB_KEY_KEY)
  if (jobKey) {
    postCancel(jobKey)
    if (eventSource) { eventSource.close(); eventSource = null }
    currentJobKey = null
    finishTaskRegistry()
  } else if (eventSource) {
    const es = eventSource
    setTimeout(() => {
      if (es === eventSource) {
        es.close(); eventSource = null
        finishTaskRegistry()
      }
    }, 5000)
  }
  if (current?.isPending) {
    current = { ...current, isPending: false, error: '已取消' }
    emit()
  }
}

export function clearRobust(): void {
  current = null
  emit()
}

async function adoptFromServer() {
  if (current && !current.error && !current.result) return
  const task = await fetchRunningTask(TASK_KIND)
  const qs = typeof task?.payload?.qs === 'string' ? task.payload.qs : ''
  if (!qs || localStorage.getItem(RECONNECT_KEY)) return
  localStorage.setItem(RECONNECT_KEY, qs)
  tryReconnectRobust()
}

export function tryReconnectRobust(): boolean {
  const qs = localStorage.getItem(RECONNECT_KEY)
  if (!qs) {
    void adoptFromServer()
    return false
  }
  const id = ++taskSeq
  current = { id, isPending: true, result: null, progress: null, error: null }
  emit()
  connectSSE(`/api/backtest/optimize-robust/stream?${qs}`)
  return true
}

export function useRobustTask(): RobustTask | null {
  return useSyncExternalStore(subscribe, () => current, () => null)
}
