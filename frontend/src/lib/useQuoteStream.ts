import { useEffect, useRef, useCallback, useSyncExternalStore } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { SSE_INVALIDATE_PREFIXES, QK } from './queryKeys'
import { getQueryConfig } from './useQueryConfig'
import { toast } from '@/components/Toast'
import { pushAlertToasts } from '@/components/AlertToast'
import { feedReviewEvent } from './reviewStore'
import type { StrategyAlertEvent } from './api'

// ===== 全局 SSE 连接状态 (模块级 store, 仿 AlertToast.tsx 模式) =====
// 实时行情 SSE 断开时 UI 无感知 → 会漏掉策略告警。这里暴露连接状态,
// 供 Layout 顶部渲染徽标、连续失败 N 次后弹一次 toast。
export type QuoteStreamStatus = 'connected' | 'reconnecting' | 'disconnected'

let _streamStatus: QuoteStreamStatus = 'disconnected'
const _statusListeners = new Set<() => void>()

// 连续失败到达该阈值后弹一次 toast (只弹一次, 恢复后重置)
const FAILS_BEFORE_TOAST = 3
// 指数退避上限
const BACKOFF_CAP_MS = 60_000

function _emitStatus() {
  _statusListeners.forEach((fn) => fn())
}

function _setStatus(s: QuoteStreamStatus) {
  if (_streamStatus === s) return
  _streamStatus = s
  _emitStatus()
}

function _subscribeStatus(fn: () => void) {
  _statusListeners.add(fn)
  return () => {
    _statusListeners.delete(fn)
  }
}

function _getStatus() {
  return _streamStatus
}

/** React hook: 读取全局实时行情 SSE 连接状态 (供 Layout 徽标使用) */
export function useQuoteStreamStatus(): QuoteStreamStatus {
  return useSyncExternalStore(_subscribeStatus, _getStatus, () => 'disconnected' as const)
}

// ===== 焦点股票注册表 (个股对话框用) =====
// 个股对话框打开时注册当前 symbol, SSE quotes_updated 推送时精准 invalidate
// 该 symbol 的日K查询 (['kline', symbol]), 让日K最后一根蜡烛随实时价变化。
// 不加进 SSE_INVALIDATE_PREFIXES 全局列表 —— 避免回测弹窗等也每秒重拉。
let _focusSymbol: string | null = null

/** 注册当前焦点股票 (个股对话框打开时调用)。 */
export function setFocusSymbol(symbol: string): void {
  _focusSymbol = symbol
}

/** 清除焦点股票 (个股对话框关闭时调用)。 */
export function clearFocusSymbol(): void {
  _focusSymbol = null
}

/**
 * 全局 SSE hook: 监听后端行情更新推送 + 策略监控通知。
 *
 * - 行情更新 (quotes_updated): 根据 sseRefreshPages 配置过滤 invalidation
 * - 策略监控通知 (strategy_alert): 通过 onAlert 回调弹 toast
 *
 * 应在顶层 Layout 中调用一次。
 */
export function useQuoteStream(
  enabled: boolean,
  sseRefreshPages: Record<string, boolean> | undefined,
  onAlert?: (alerts: StrategyAlertEvent[]) => void,
) {
  const qc = useQueryClient()
  const esRef = useRef<EventSource | null>(null)
  const retryRef = useRef<ReturnType<typeof setTimeout>>()
  const pagesRef = useRef(sseRefreshPages)
  pagesRef.current = sseRefreshPages

  const handleAlerts = useCallback((alerts: StrategyAlertEvent[]) => {
    // depth 系统接管通知: 单独处理, 不走 strategy 回调
    const depthAlerts = alerts.filter(a => a.source === 'depth')
    const strategyAlerts = alerts.filter(a => a.source !== 'depth')

    // depth 通知直接 toast(防刷屏: 后端已在状态切换时才推)
    for (const a of depthAlerts.slice(0, 1)) {
      toast(a.message, 'success')
    }

    // 监控告警: 用专用 AlertToast (整批只响一声, 每条都弹, 受 maxVisible 上限保护)
    if (strategyAlerts.length > 0) {
      // 有 onAlert 回调时走回调, 否则弹 AlertToast
      if (onAlert) {
        onAlert(strategyAlerts)
      }
      // 批量弹通知 (去掉了 slice(0,2) 截断, 让每只新命中都弹 toast; 声音整批只响一次)
      pushAlertToasts(strategyAlerts as any)
    }
  }, [onAlert])

  const enabledRef = useRef(enabled)
  enabledRef.current = enabled

  useEffect(() => {
    // SSE 始终连接 — 监控告警不依赖实时行情开关
    // (quotes_updated 行情刷新受 enabled 控制, strategy_alert 始终处理)

    // 连续失败计数 (用于指数退避 + 到阈值弹一次 toast)
    let failCount = 0
    let toastFired = false

    const connect = () => {
      _setStatus(failCount > 0 ? 'reconnecting' : _streamStatus)
      const es = new EventSource('/api/intraday/stream')
      esRef.current = es

      es.onopen = () => {
        // 连接成功: 重置退避与 toast 标记
        failCount = 0
        toastFired = false
        _setStatus('connected')
      }

      // sse-starlette ping 心跳走 SSE comment，不会到达这里

      es.addEventListener('quotes_updated', () => {
        // 实时行情未开启时不处理行情刷新
        if (!enabledRef.current) return
        // 根据用户配置过滤 invalidation
        const pages = pagesRef.current
        if (pages) {
          // 只 invalidate 开启的页面对应的 prefix
          const activePrefixes = SSE_INVALIDATE_PREFIXES.filter((p) => {
            // 'quote-status' 始终刷新 (全局状态)
            if (p === 'quote-status') return true
            return pages[p] !== false
          })
          qc.invalidateQueries({
            predicate: (query) =>
              activePrefixes.some(
                (prefix) => String(query.queryKey[0]).startsWith(prefix),
              ),
          })
        } else {
          // 无配置时全部刷新 (向后兼容)
          qc.invalidateQueries({
            predicate: (query) =>
              SSE_INVALIDATE_PREFIXES.some(
                (prefix) => String(query.queryKey[0]).startsWith(prefix),
              ),
          })
        }
        // 焦点股票日K精准刷新: 个股对话框打开时, 日K最后一根蜡烛随实时价变化。
        // 后端 _maybe_inject_live_candle 只读内存缓存, 不调 TickFlow, 秒级重拉零额外成本。
        if (_focusSymbol) {
          qc.invalidateQueries({ queryKey: ['kline', _focusSymbol] })
        }
      })

      es.addEventListener('strategy_results_updated', () => {
        // 策略监控完成后只刷新策略结果缓存，不扩散到其他行情页面。
        qc.invalidateQueries({ queryKey: ['screener-cached'] })
      })

      es.addEventListener('depth_updated', () => {
        // 五档修正完成: 刷新连板梯队 + 看板封单数据。
        // 不受实时行情开关限制 — 修正轮询独立于行情轮询, 用户开了修正就想看实时封单。
        qc.invalidateQueries({ queryKey: ['limit-ladder'] })
        qc.invalidateQueries({ queryKey: ['overview-market'] })
      })

      es.addEventListener('strategy_alert', (e: MessageEvent) => {
        try {
          const data = JSON.parse(e.data)
            const alerts: StrategyAlertEvent[] = data.alerts || []
            if (alerts.length > 0) {
              handleAlerts(alerts)
              // 实时刷新触发记录列表 + 监控中心徽标
              qc.invalidateQueries({ queryKey: ['alerts'] })
              qc.invalidateQueries({ queryKey: ['alerts-total'] })
            }
        } catch {
          // 忽略解析错误
        }
      })

      // 定时复盘流式进度: 后端到点生成时把 meta/delta/done 推来, 喂进 reviewStore
      // 开着复盘页可看到「边生成边显示」, 切走再回来也能看到生成中/已生成
      es.addEventListener('review_progress', (e: MessageEvent) => {
        try {
          const evt = JSON.parse(e.data)
          feedReviewEvent(evt)
          // done(后端已归档) → 刷新历史列表, 让新报告出现并可查看
          if (evt.type === 'done') {
            qc.invalidateQueries({ queryKey: QK.reviewReports })
          }
        } catch {
          // 忽略解析错误
        }
      })

      es.onerror = () => {
        es.close()
        esRef.current = null
        failCount += 1
        _setStatus('reconnecting')
        // 连续失败到阈值 → 弹一次 toast (漏行情=可能漏策略告警, 需明确告知)
        if (failCount >= FAILS_BEFORE_TOAST && !toastFired) {
          toastFired = true
          toast('实时连接已断开，正在重连…', 'error')
        }
        // 指数退避 (base * 2^(n-1), 上限 60s), 替代原来固定 5s
        const base = getQueryConfig().sse.reconnectDelay
        const delay = Math.min(base * 2 ** (failCount - 1), BACKOFF_CAP_MS)
        retryRef.current = setTimeout(connect, delay)
      }
    }

    connect()

    return () => {
      clearTimeout(retryRef.current)
      if (esRef.current) {
        esRef.current.close()
        esRef.current = null
      }
      _setStatus('disconnected')
    }
  }, [qc, handleAlerts])
}
