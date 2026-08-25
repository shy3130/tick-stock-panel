import { useSyncExternalStore } from 'react'
import type { StrategyBacktestResult } from './api'
import type { RunConnectionState } from './runStatus'

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
  equity?: number
  stage?: string
  label?: string
  elapsed_ms?: number
}

export interface BacktestTask {
  id: number
  isPending: boolean
  result: StrategyBacktestResult | null
  progress: BacktestProgress | null
  error: string | null
  /** SSE 连接状态, 供运行状态条显示断线提示 */
  connectionState: RunConnectionState
}

let current: BacktestTask | null = null
const listeners = new Set<() => void>()
let taskSeq = 0
let eventSource: EventSource | null = null
// EventSource.readyState 常量 (0=CONNECTING, 2=CLOSED); 用字面量避免 mock 环境缺静态属性
const ES_CONNECTING = 0
const ES_CLOSED = 2

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

  // SSE 连接状态跟踪: onopen 置 open; 断线错误按 readyState 区分自动重连/彻底断开
  es.onopen = () => {
    if (current?.id !== id || current.connectionState === 'open') return
    current = { ...current, connectionState: 'open' }
    emit()
  }

  es.addEventListener('progress', (e: MessageEvent) => {
    if (current?.id !== id) return
    try {
      const prog = JSON.parse(e.data) as BacktestProgress
      current = { ...current, progress: prog }
      emit()
    } catch { /* ignore */ }
  })

  // 服务重启后后端自动整单重跑：提示即可，勿当 error，勿清 reconnect key
  es.addEventListener('resumed', (e: MessageEvent) => {
    if (current?.id !== id) return
    let message = '服务已重启，策略回测无法从中途续跑，正在整单重跑'
    if (e.data) {
      try {
        const parsed = JSON.parse(e.data) as { message?: string }
        if (parsed?.message) message = parsed.message
      } catch { /* 用默认文案 */ }
    }
    const prev = current.progress
    current = {
      ...current,
      progress: prev
        ? { ...prev, label: message, stage: prev.stage ?? 'resumed' }
        : { day: 0, total: 0, date: '', label: message, stage: 'resumed' },
      error: null,
    }
    emit()
  })

  es.addEventListener('done', (e: MessageEvent) => {
    if (current?.id !== id) return
    try {
      const result = JSON.parse(e.data) as StrategyBacktestResult
      current = { ...current, isPending: false, result, error: null, connectionState: 'closed' }
      emit()
    } catch {
      current = { ...current, isPending: false, error: '结果解析失败', connectionState: 'closed' }
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
        current = { ...current, isPending: false, error: msg, connectionState: 'closed' }
        emit()
      } catch {
        current = { ...current, isPending: false, error: '回测出错', connectionState: 'closed' }
        emit()
      }
      es.close()
      eventSource = null
      localStorage.removeItem(RECONNECT_KEY)
      return
    }
    // 无 data: 连接异常断开; CONNECTING=浏览器自动重连中, CLOSED=彻底断开
    const connection = es.readyState === ES_CONNECTING
      ? 'reconnecting'
      : es.readyState === ES_CLOSED ? 'closed' : null
    if (connection && current.connectionState !== connection) {
      current = { ...current, connectionState: connection }
      emit()
    }
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
  slippage_bps?: number
  /** 印花税率 (仅卖出单边, 0.0005 = 万分之五); 缺省由后端默认 */
  stamp_tax_pct?: number
  max_positions?: number
  max_exposure_pct?: number
  initial_capital?: number
  position_sizing?: string
  params?: Record<string, any> | null
  overrides?: Record<string, any> | null
  mode?: 'position' | 'full'
  holding_days?: number
  regime_filter?: { states?: string[]; min_score?: number } | null
  benchmark_symbol?: string
  /** F9 历史 Run 净值基准 (run_id); 设置时后端忽略 benchmark_symbol (互斥) */
  benchmark_run_id?: string | null
  risk_free_rate?: number
  /** A1 量能约束: 单笔最大参与率 (0-1 小数); null/缺省 = 关闭 (不进 query) */
  max_participation_pct?: number | null
  /** 参与率均量窗口 (交易日数) */
  participation_volume_window?: number
  /** 上市天数门控 (天, 0 = 关闭) */
  min_listed_days?: number
  /** F14 成交精度: daily = 日 K 收盘/开盘口径; minute = 分钟 VWAP 撮合 + 盘中风控 */
  bar_precision?: 'daily' | 'minute'
}): void {
  // 取消之前的任务状态
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }

  const id = ++taskSeq
  current = { id, isPending: true, result: null, progress: null, error: null, connectionState: 'connecting' }
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
    regime_filter: params.regime_filter ? JSON.stringify(params.regime_filter) : undefined,
    benchmark_symbol: params.benchmark_run_id ? undefined : params.benchmark_symbol,
    // F9: run 基准与 symbol 基准互斥 — 设置 run_id 时不发送 symbol
    benchmark_run_id: params.benchmark_run_id ?? undefined,
    risk_free_rate: params.risk_free_rate,
    // A1/B6 撮合约束: 仅非默认时附加, 保持与旧行为的 job_key 稳定
    max_participation_pct: params.max_participation_pct ?? undefined,
    participation_volume_window: params.participation_volume_window != null && params.participation_volume_window !== 5
      ? params.participation_volume_window
      : undefined,
    min_listed_days: params.min_listed_days != null && params.min_listed_days > 0
      ? params.min_listed_days
      : undefined,
    // F14 成交精度: 仅 minute 时附加, 保持 daily 的 job_key 与旧行为稳定
    bar_precision: params.bar_precision === 'minute' ? 'minute' : undefined,
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
    current = { ...current, isPending: false, error: '已取消', connectionState: 'closed' }
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
  current = { id, isPending: true, result: null, progress: null, error: null, connectionState: 'connecting' }
  emit()
  connectSSE(`/api/backtest/strategy/stream?${qs}`)
  return true
}

/** React hook: 读取当前全局回测任务状态 */
export function useBacktestTask(): BacktestTask | null {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)
}
