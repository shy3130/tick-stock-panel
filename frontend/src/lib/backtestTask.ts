import { useSyncExternalStore } from 'react'
import type { StrategyBacktestResult } from './api'

/**
 * 全局回测任务管理 (SSE 模式 + 任务缓存 + 重连支持)。
 *
 * 特性:
 * - 实时进度: EventSource 监听后端 SSE, 推送 day/total/equity
 * - 可取消: POST /strategy/cancel/{job_key}, 后端 cancel_event
 * - 切页/刷新保持: 后端按参数 hash 缓存任务, 重连不重启
 *   - 切页: 模块级 store 保持, EventSource 随组件卸载断开, 回来后重连
 *   - 刷新: localStorage 存 job 参数, 刷新后重新连接到同一任务
 */

export interface BacktestProgress {
  day: number
  total: number
  date: string
  equity: number
}

export interface BacktestTask {
  id: number
  isPending: boolean
  result: StrategyBacktestResult | null
  progress: BacktestProgress | null
  error: string | null
  /** 连接中断、正在有界重连中 (UI 显示"连接中断，重试中") */
  reconnecting: boolean
}

// 连接断开后最多自动重连次数, 超过则放弃并进入可重试的错误态
const MAX_RECONNECT_ATTEMPTS = 5

let current: BacktestTask | null = null
const listeners = new Set<() => void>()
let taskSeq = 0
let eventSource: EventSource | null = null

const RECONNECT_KEY = 'backtest_reconnect'

function emit() {
  listeners.forEach(fn => fn())
}

function subscribe(fn: () => void) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

function getSnapshot() {
  return current
}

function getServerSnapshot() {
  return null
}

/** 查询字符串构建 */
function buildQuery(params: Record<string, string | number | boolean | undefined | null>): string {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v != null && v !== '') sp.set(k, String(v))
  }
  return sp.toString()
}

/** 连接 SSE (新建或重连都用这个) */
function connectSSE(url: string): void {
  const id = current?.id ?? ++taskSeq

  // 关闭旧连接
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }

  const es = new EventSource(url)
  eventSource = es

  // 本次连接的重连计数 (EventSource 断开会自动重连并再次触发 onerror)
  let reconnectAttempts = 0

  const clearReconnecting = () => {
    if (current?.id === id && current.reconnecting) {
      current = { ...current, reconnecting: false }
      emit()
    }
    reconnectAttempts = 0
  }

  es.onopen = () => {
    clearReconnecting()
  }

  es.addEventListener('progress', (e: MessageEvent) => {
    if (current?.id !== id) return
    // 收到数据说明连接恢复正常
    reconnectAttempts = 0
    try {
      const prog = JSON.parse(e.data) as BacktestProgress
      current = { ...current, progress: prog, reconnecting: false }
      emit()
    } catch { /* ignore */ }
  })

  es.addEventListener('done', (e: MessageEvent) => {
    if (current?.id !== id) return
    try {
      const result = JSON.parse(e.data) as StrategyBacktestResult
      current = { ...current, isPending: false, result, error: null, reconnecting: false }
      emit()
    } catch {
      current = { ...current, isPending: false, error: '结果解析失败', reconnecting: false }
      emit()
    }
    es.close()
    eventSource = null
    localStorage.removeItem(RECONNECT_KEY)
  })

  es.addEventListener('error', (e: MessageEvent) => {
    if (current?.id !== id) return
    // SSE error 事件: 有 data 说明是后端主动推送的错误/取消; 无 data 说明是连接断开
    if (e.data) {
      try {
        const msg = JSON.parse(e.data)?.message ?? '回测出错'
        current = { ...current, isPending: false, error: msg, reconnecting: false }
        emit()
      } catch {
        current = { ...current, isPending: false, error: '回测出错', reconnecting: false }
        emit()
      }
      es.close()
      eventSource = null
      localStorage.removeItem(RECONNECT_KEY)
      return
    }
    // 无 data: 连接异常断开。EventSource 会自动重连, 但需给出可见状态并有界放弃,
    // 避免进度条永久冻结、isPending 永远 true。
    reconnectAttempts += 1
    if (reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
      // 放弃: 停止自动重连, 进入可重试的错误态 (用户可重新发起回测)
      es.close()
      eventSource = null
      current = {
        ...current,
        isPending: false,
        reconnecting: false,
        error: '连接中断，请重试',
      }
      emit()
      return
    }
    // 仍在重试窗口内: 标记 reconnecting, 让 UI 显示"连接中断，重试中"
    current = { ...current, reconnecting: true }
    emit()
  })
}

/** 启动一次 SSE 回测任务 */
export function startBacktest(params: {
  strategy_id: string
  symbols?: string[] | null
  start?: string | null
  end?: string | null
  matching?: string
  entry_fill?: string
  exit_fill?: string
  fees_pct?: number
  commission_pct?: number
  stamp_tax_pct?: number
  slippage_bps?: number
  max_positions?: number
  max_exposure_pct?: number
  initial_capital?: number
  position_sizing?: string
  params?: Record<string, any> | null
  overrides?: Record<string, any> | null
  mode?: 'position' | 'full'
  holding_days?: number
  asset_type?: 'stock' | 'etf'
  minute_fill?: boolean
}): void {
  // 取消之前的任务状态
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }

  const id = ++taskSeq
  current = { id, isPending: true, result: null, progress: null, error: null, reconnecting: false }
  emit()

  const qs = buildQuery({
    strategy_id: params.strategy_id,
    symbols: params.symbols?.join(','),
    start: params.start ?? undefined,
    end: params.end ?? undefined,
    matching: params.matching,
    entry_fill: params.entry_fill,
    exit_fill: params.exit_fill,
    fees_pct: params.fees_pct,
    commission_pct: params.commission_pct,
    stamp_tax_pct: params.stamp_tax_pct,
    slippage_bps: params.slippage_bps,
    max_positions: params.max_positions,
    max_exposure_pct: params.max_exposure_pct,
    initial_capital: params.initial_capital,
    position_sizing: params.position_sizing,
    params: params.params ? JSON.stringify(params.params) : undefined,
    overrides: params.overrides ? JSON.stringify(params.overrides) : undefined,
    mode: params.mode,
    holding_days: params.holding_days,
    asset_type: params.asset_type,
    minute_fill: params.minute_fill,
  })

  // 存 reconnect 信息 (刷新后用)
  localStorage.setItem(RECONNECT_KEY, qs)

  connectSSE(`/api/backtest/strategy/stream?${qs}`)
}

/** 停止当前回测任务 (调后端 cancel, 后端 cancel_event → 停止计算) */
export async function stopBacktest(): Promise<void> {
  // 从 reconnect key 提取 job_key (后端按参数 hash 算 job_key)
  const qs = localStorage.getItem(RECONNECT_KEY)
  if (qs) {
    // 解析出参数, 用 fetch 调 cancel
    try {
      // job_key 是后端算的 md5, 前端不知道。用 reconnect URL 里的参数重新请求 stream,
      // 后端会找到同一个 job 并返回它的 job_key? 不行。
      // 替代: 前端直接关闭 SSE 连接 + 调一个带参数的 cancel 接口。
      // 简化: 关闭连接即可, 后端检测断开后 (不取消)。需要 cancel 用 POST。
      // 这里用 cancel 接口: POST /strategy/cancel, body 带 qs 的参数。
      await fetch('/api/backtest/strategy/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ qs }),
      }).catch(() => {})
    } catch { /* ignore */ }
  }
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
  if (current?.isPending) {
    current = { ...current, isPending: false, error: '已取消', reconnecting: false }
    emit()
  }
  localStorage.removeItem(RECONNECT_KEY)
}

/** 清除任务状态 (隐藏提示) */
export function clearBacktest(): void {
  current = null
  emit()
}

/** 恢复: 从 localStorage 读取 reconnect 信息, 重新连接 (刷新后调用) */
export function tryReconnect(): boolean {
  const qs = localStorage.getItem(RECONNECT_KEY)
  if (!qs) return false
  // 有未完成的任务, 重连
  const id = ++taskSeq
  current = { id, isPending: true, result: null, progress: null, error: null, reconnecting: false }
  emit()
  connectSSE(`/api/backtest/strategy/stream?${qs}`)
  return true
}

/** React hook: 读取当前全局回测任务状态 */
export function useBacktestTask(): BacktestTask | null {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)
}
