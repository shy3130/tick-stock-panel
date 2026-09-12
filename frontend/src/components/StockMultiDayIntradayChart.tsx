import { useEffect, useMemo, useRef } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Download, Loader2, RefreshCw } from 'lucide-react'
import { api, type MinuteKlineSession } from '@/lib/api'
import { klineMinuteQueryOptions, klineMinuteRangeQueryOptions, minuteRefetchInterval } from '@/lib/kline'
import { toast } from '@/components/Toast'
import { EChartsMultiDayIntraday } from '@/components/EChartsMultiDayIntraday'

interface Props {
  symbol: string
  days: number
  height?: number
  refetchIntervalMs?: number
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  priceLines?: { value: number; label?: string; color?: string }[]
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '分钟数据获取失败'
}

export function StockMultiDayIntradayChart({
  symbol,
  days,
  height = 420,
  refetchIntervalMs,
  onPriceDoubleClick,
  priceLines,
}: Props) {
  const queryClient = useQueryClient()
  const history = useQuery({
    ...klineMinuteRangeQueryOptions(symbol, days),
    enabled: !!symbol,
  })
  const latest = useQuery({
    // live: 当日盘中直接实时拉取, 不被分钟增量落盘的本地分区(≥60s一轮)拖慢
    ...klineMinuteQueryOptions(symbol, undefined, true),
    enabled: !!symbol,
    refetchInterval: minuteRefetchInterval(refetchIntervalMs),
  })

  const sessions = useMemo(() => {
    const byDate = new Map<string, MinuteKlineSession>()
    for (const session of history.data?.sessions ?? []) byDate.set(session.date, session)

    const latestDate = latest.data?.date
    const latestRows = latest.data?.rows ?? []
    if (latestDate && latestRows.length > 0) {
      const existing = byDate.get(latestDate)
      byDate.set(latestDate, {
        date: latestDate,
        prev_close: latest.data?.prev_close ?? existing?.prev_close ?? null,
        rows: latestRows,
      })
    }

    return Array.from(byDate.values())
      .sort((left, right) => left.date.localeCompare(right.date))
      .slice(-days)
  }, [days, history.data?.sessions, latest.data])

  const syncMinute = useMutation({
    mutationFn: () => api.syncMinuteSingle(symbol, days),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['kline-minute-range', symbol] }),
        queryClient.invalidateQueries({ queryKey: ['kline-minute', symbol] }),
      ])
    },
    onError: (e: Error) => {
      const failKey = `${symbol}:${days}`
      autoSyncFailsRef.current.set(failKey, (autoSyncFailsRef.current.get(failKey) ?? 0) + 1)
      autoSyncRef.current = null  // 失败清标记, 允许上限内自动重试
      const msg = e.message || ''
      if (msg.includes('403') || msg.includes('Pro')) {
        toast('分钟K(批量)数据不可用', 'error')
      } else {
        toast(`补齐数据失败: ${msg}`, 'error')
      }
    },
  })

  const loading = sessions.length === 0 && (history.isLoading || latest.isLoading)
  const queryError = sessions.length === 0 ? history.error ?? latest.error : null
  const isIndex = history.data?.asset_type === 'index' || latest.data?.asset_type === 'index'
  // 缺口只按本地落库的历史 session 数计算: 当日实时 (latest, 盘中不落盘) 只合并进
  // sessions 供绘图, 若参与计数会把缺口撑成 0, 提示条与自动补齐将永不触发 (issue #305)
  const localSessionCount = history.data?.sessions?.length ?? 0
  const missingDays = Math.max(0, days - localSessionCount)
  const showCoverage = !history.isPlaceholderData && sessions.length > 0 && missingDays > 0 && !isIndex

  // 自动补齐: 本地落库天数不足且非指数时, 自动触发同步
  // 用 ref 记录已触发的 symbol:days, 避免重复; 失败后清标记, 允许上限内自动重试
  const autoSyncRef = useRef<string | null>(null)
  const autoSyncFailsRef = useRef<Map<string, number>>(new Map())
  useEffect(() => {
    if (!symbol) return
    // 后端没运行时 history 会 error, 此时 missingDays 计算无意义, 跳过
    if (history.error || history.isPlaceholderData || loading || isIndex || localSessionCount >= days) return
    if (syncMinute.isPending) return

    const key = `${symbol}:${days}`
    if (autoSyncRef.current === key) return  // 本组合已触发过
    if ((autoSyncFailsRef.current.get(key) ?? 0) >= 2) return  // 连续失败达上限, 停止自动重试(提示条保留手动重试)
    autoSyncRef.current = key
    syncMinute.mutate()
  }, [symbol, days, localSessionCount, loading, isIndex, history.error, history.isPlaceholderData, syncMinute.isPending])

  const chartHeight = Math.max(260, height - (showCoverage || syncMinute.isPending ? 32 : 0))

  if (loading) {
    return (
      <div className="flex items-center justify-center gap-2 text-xs text-muted" style={{ height }}>
        <Loader2 className="h-4 w-4 animate-spin text-accent" />
        正在加载近 {days} 日分时…
      </div>
    )
  }

  if (queryError) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 text-xs" style={{ height }}>
        <span className="text-danger">{errorMessage(queryError)}</span>
        <button
          type="button"
          onClick={() => { void history.refetch(); void latest.refetch() }}
          className="inline-flex items-center gap-1.5 rounded-btn border border-border bg-elevated px-3 py-1.5 text-secondary hover:text-foreground"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          重新加载
        </button>
      </div>
    )
  }

  if (sessions.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 text-xs" style={{ height }}>
        {syncMinute.isPending ? (
          <>
            <Loader2 className="h-5 w-5 animate-spin text-accent" />
            <span className="text-secondary">正在获取近 {days} 日分钟 K…</span>
          </>
        ) : (
          <>
            <span className="text-muted">{isIndex ? '指数暂无分钟数据' : '本地暂无可展示的分钟数据'}</span>
            {!isIndex && (
              <button
                type="button"
                onClick={() => syncMinute.mutate()}
                className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent/90"
              >
                <Download className="h-3.5 w-3.5" />
                获取近 {days} 日
              </button>
            )}
          </>
        )}
        {syncMinute.isError && <span className="max-w-md text-center text-danger">{errorMessage(syncMinute.error)}</span>}
      </div>
    )
  }

  return (
    <div style={{ height }}>
      {(showCoverage || (syncMinute.isPending && !isIndex)) && (
        <div className="flex h-8 items-center justify-between gap-3 border-b border-border/60 bg-elevated/40 px-3 text-[11px]">
          {syncMinute.isPending ? (
            <span className="truncate text-accent flex items-center gap-1.5">
              <Loader2 className="h-3 w-3 animate-spin" />
              正在补齐最近 {days} 日分时数据…
            </span>
          ) : syncMinute.isError ? (
            <span className="truncate text-muted">当前 {sessions.length} 日，目标 {days} 日 — 补齐失败</span>
          ) : (
            <span className="truncate text-muted">当前 {sessions.length} 个交易日数据，目标 {days} 日</span>
          )}
          {!syncMinute.isPending && (
            <button
              type="button"
              onClick={() => {
                autoSyncRef.current = `${symbol}:${days}`
                syncMinute.mutate()
              }}
              className="inline-flex shrink-0 items-center gap-1 text-accent hover:text-accent/80"
            >
              <Download className="h-3 w-3" />
              重试补齐
            </button>
          )}
        </div>
      )}
      <EChartsMultiDayIntraday
        sessions={sessions}
        height={chartHeight}
        onPriceDoubleClick={onPriceDoubleClick}
        priceLines={priceLines}
      />
      {syncMinute.isError && (
        <div className="px-3 pt-1 text-center text-[11px] text-danger">{errorMessage(syncMinute.error)}</div>
      )}
    </div>
  )
}
