import { useSyncExternalStore } from 'react'

/** Walk-forward 任务管理 (SSE + job_key 回吐 + 重连)。镜像 optimizerTask。 */

export interface WFProgress {
  type: string
  done: number
  total: number
  fold: number
}

export interface WFFold {
  index: number
  train_start: string
  train_end: string
  test_start: string
  test_end: string
  best_params: Record<string, any> | null
  is_score: number | null
  oos_objective: number | null
  oos_degraded: boolean | null
  oos_stats: Record<string, any>
}

export interface WFSummary {
  n_folds: number
  compounded_oos_return: number
  avg_is_objective: number | null
  avg_oos_objective: number | null
  degradation: number | null
  consistency: number
  oos_equity_curve: { fold: number; date: string; value: number }[]
}

export interface WalkForwardResult {
  objective: string
  direction: string
  n_folds: number
  n_skipped: number
  n_planned_folds: number
  folds: WFFold[]
  skipped: { index: number; test_start: string; test_end: string; reason: string }[]
  summary: WFSummary
  elapsed_ms: number
}

export interface WalkForwardTask {
  id: number
  isPending: boolean
  result: WalkForwardResult | null
  progress: WFProgress | null
  error: string | null
}

export interface StartWalkForwardParams {
  strategy_id: string
  param_grid: Record<string, any>
  objective: string
  train_days: number
  test_days: number
  step_days: number
  params?: Record<string, any> | null       // 未扫描参数固定为用户当前值
  overrides?: Record<string, any> | null     // 策略当前的 basic_filter/信号/风控覆盖
  symbols?: string[] | null
  start?: string | null
  end?: string | null
  mode?: 'position' | 'full'
}

let current: WalkForwardTask | null = null
const listeners = new Set<() => void>()
let taskSeq = 0
let eventSource: EventSource | null = null
let currentJobKey: string | null = null
let cancelRequested = false
let reconnectAttempts = 0
const MAX_RECONNECT = 5

const RECONNECT_KEY = 'walkforward_reconnect'
const JOB_KEY_KEY = 'walkforward_job_key'

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
        // 竞态: stop 在拿到 key 前被点过 -> 补发 cancel 真正停后端任务, 再收尾关闭。
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
      const prog = JSON.parse(e.data) as WFProgress
      current = { ...current, progress: prog }
      emit()
    } catch { /* ignore */ }
  })

  es.addEventListener('done', (e: MessageEvent) => {
    if (current?.id !== id) return
    try {
      const result = JSON.parse(e.data) as WalkForwardResult
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
        const msg = JSON.parse(e.data)?.message ?? 'walk-forward 出错'
        current = { ...current, isPending: false, error: msg }
        emit()
      } catch {
        current = { ...current, isPending: false, error: 'walk-forward 出错' }
        emit()
      }
      es.close()
      eventSource = null
      currentJobKey = null
      localStorage.removeItem(RECONNECT_KEY)
      localStorage.removeItem(JOB_KEY_KEY)
      return
    }
    // 无 data: 连接异常断开。EventSource 自动重连, 设上限避免网络长断时无限 pending。
    if (current?.id === id) {
      reconnectAttempts += 1
      if (reconnectAttempts > MAX_RECONNECT) {
        es.close()
        eventSource = null
        // 清 localStorage: 否则刷新页面 tryReconnect 会重连到这个已放弃的任务。
        localStorage.removeItem(RECONNECT_KEY)
        localStorage.removeItem(JOB_KEY_KEY)
        current = { ...current, isPending: false, error: '连接中断, 重连多次失败' }
        emit()
      }
    }
  })
}

/** 调后端 cancel (按回吐的 job_key)。 */
function postCancel(jobKey: string): void {
  fetch('/api/backtest/walkforward/cancel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ job_key: jobKey }),
  }).catch(() => {})
}

export function startWalkForward(params: StartWalkForwardParams): void {
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
    train_days: params.train_days,
    test_days: params.test_days,
    step_days: params.step_days,
    params: params.params ? JSON.stringify(params.params) : undefined,
    overrides: params.overrides ? JSON.stringify(params.overrides) : undefined,
    symbols: params.symbols?.join(','),
    start: params.start ?? undefined,
    end: params.end ?? undefined,
    mode: params.mode,
  })

  localStorage.setItem(RECONNECT_KEY, qs)
  connectSSE(`/api/backtest/walkforward/stream?${qs}`)
}

export function stopWalkForward(): void {
  // 竞态: job_key 未到手时保持 SSE 打开, 等 job 事件补发 cancel (关 SSE 不停后端 daemon 线程)。
  cancelRequested = true
  const jobKey = currentJobKey ?? localStorage.getItem(JOB_KEY_KEY)
  if (jobKey) {
    postCancel(jobKey)
    if (eventSource) { eventSource.close(); eventSource = null }
    currentJobKey = null
    localStorage.removeItem(RECONNECT_KEY)
    localStorage.removeItem(JOB_KEY_KEY)
  } else if (eventSource) {
    const es = eventSource
    // job_key 始终没到手(job 事件未达): 5 秒后放弃并清 localStorage, 避免刷新重连到未取消任务。
    // (若期间 job 到达, job handler 已 postCancel+清storage 并置 eventSource=null, 下面条件不成立跳过)
    setTimeout(() => {
      if (es === eventSource) {
        es.close(); eventSource = null
        localStorage.removeItem(RECONNECT_KEY)
        localStorage.removeItem(JOB_KEY_KEY)
      }
    }, 5000)
  }
  if (current?.isPending) {
    current = { ...current, isPending: false, error: '已取消' }
    emit()
  }
}

export function clearWalkForward(): void {
  current = null
  emit()
}

export function tryReconnectWalkForward(): boolean {
  const qs = localStorage.getItem(RECONNECT_KEY)
  if (!qs) return false
  const id = ++taskSeq
  current = { id, isPending: true, result: null, progress: null, error: null }
  emit()
  connectSSE(`/api/backtest/walkforward/stream?${qs}`)
  return true
}

export function useWalkForwardTask(): WalkForwardTask | null {
  return useSyncExternalStore(subscribe, () => current, () => null)
}
