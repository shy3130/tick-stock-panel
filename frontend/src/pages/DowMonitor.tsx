import { useMemo, useRef, useState } from 'react'

import { MarketFilterTabs } from '@/components/MarketFilterTabs'
import { PageHeader } from '@/components/PageHeader'
import { DowMonitorCard } from '@/components/dow-monitor/DowMonitorCard'
import { DowMonitorDetailDialog } from '@/components/dow-monitor/DowMonitorDetailDialog'
import { formatServerTimestamp } from '@/components/dow-monitor/formatServerTimestamp'
import type {
  DowMonitorMarket,
  DowMonitorNotification,
  DowMonitorOverviewSymbol,
  DowTimeframe,
} from '@/components/dow-monitor/types'
import {
  useAddDowMonitorSymbol,
  useDowMonitorOverview,
  useDowMonitorStatus,
  useDowNotifications,
  useMarkDowNotificationRead,
  useRemoveDowMonitorSymbol,
  useSetDowMonitorEnabled,
} from '@/components/dow-monitor/useDowMonitor'
import { cn } from '@/lib/cn'

type SignalFilter = 'all' | 'active' | 'buy' | 'sell'

const SIGNAL_FILTERS: Array<{ value: SignalFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'active', label: '有信号' },
  { value: 'buy', label: '仅买点' },
  { value: 'sell', label: '仅卖点' },
]

function sameMarket(market: DowMonitorMarket, itemMarket: string) {
  return market === 'all' || market === itemMarket
}

function matchesSide(filter: SignalFilter, side: string | null | undefined) {
  if (filter === 'all') return true
  if (filter === 'active') return side != null
  if (filter === 'buy') return side === 'BUY'
  return side === 'SELL' || side === 'RISK'
}

function filterSymbols(
  symbols: DowMonitorOverviewSymbol[],
  market: DowMonitorMarket,
  signal: SignalFilter,
) {
  return symbols.filter(item => (
    sameMarket(market, item.market)
    && matchesSide(signal, item.latest_notification?.side)
  ))
}

function filterNotifications(
  notifications: DowMonitorNotification[],
  market: DowMonitorMarket,
  signal: SignalFilter,
) {
  return notifications.filter(notification => (
    sameMarket(market, notification.market)
    && matchesSide(signal, notification.side)
  ))
}

export function DowMonitor({
  onOpen,
}: {
  onOpen?: (symbol: string, timeframe: DowTimeframe) => void
}) {
  const [market, setMarket] = useState<DowMonitorMarket>('all')
  const [signal, setSignal] = useState<SignalFilter>('all')
  const [symbolInput, setSymbolInput] = useState('')
  const [pendingToggles, setPendingToggles] = useState<Set<string>>(() => new Set())
  const [pendingRemovals, setPendingRemovals] = useState<Set<string>>(() => new Set())
  const [pendingReads, setPendingReads] = useState<Set<string>>(() => new Set())
  const [toggleErrors, setToggleErrors] = useState<Set<string>>(() => new Set())
  const [removeErrors, setRemoveErrors] = useState<Set<string>>(() => new Set())
  const [readErrors, setReadErrors] = useState<Map<string, string>>(() => new Map())
  const [detail, setDetail] = useState<{ symbol: string; timeframe: DowTimeframe } | null>(null)
  const detailScrollPosition = useRef(0)
  const overview = useDowMonitorOverview(market)
  const notificationQuery = useDowNotifications(market)
  const status = useDowMonitorStatus()
  const addSymbol = useAddDowMonitorSymbol()
  const removeSymbol = useRemoveDowMonitorSymbol()
  const setEnabled = useSetDowMonitorEnabled()
  const markRead = useMarkDowNotificationRead()

  const symbols = overview.data?.symbols ?? []
  const notifications = notificationQuery.data?.notifications ?? []
  const filteredSymbols = useMemo(
    () => filterSymbols(symbols, market, signal),
    [market, signal, symbols],
  )
  const filteredNotifications = useMemo(
    () => filterNotifications(notifications, market, signal),
    [market, notifications, signal],
  )

  const backendReady = Boolean(
    !status.isLoading
    && !status.isError
    && status.data?.running
    && status.data.last_completed_at
    && status.data.last_success_at,
  )
  const connectivityIssues: string[] = []
  if (status.isLoading) connectivityIssues.push('正在连接监控服务')
  else if (status.isError) connectivityIssues.push('监控服务连接失败')
  else if (!status.data) connectivityIssues.push('监控服务状态不可用')
  else if (!status.data.running) connectivityIssues.push('后台监控未运行')
  else if (!status.data.last_completed_at || !status.data.last_success_at) {
    connectivityIssues.push('等待后台首轮监控结果')
  }
  if (overview.isLoading) connectivityIssues.push('监控状态加载中')
  if (overview.isError) connectivityIssues.push('监控状态连接失败')
  if (notificationQuery.isLoading) connectivityIssues.push('通知加载中')
  if (notificationQuery.isError) connectivityIssues.push('通知连接失败')

  const mutationIssues: string[] = []
  if (addSymbol.isError) mutationIssues.push('添加失败，请重试')
  for (const symbol of toggleErrors) {
    mutationIssues.push(`${symbol} 监控开关更新失败，请重试`)
  }
  for (const symbol of removeErrors) {
    mutationIssues.push(`移除 ${symbol} 失败，请重试`)
  }
  for (const symbol of readErrors.values()) {
    mutationIssues.push(`标记 ${symbol} 已读失败，请重试`)
  }
  const visibleIssues = [...connectivityIssues, ...mutationIssues]
  const forceBlocked = connectivityIssues.length > 0

  const submitSymbol = () => {
    const symbol = symbolInput.trim().toUpperCase()
    if (!symbol) return
    addSymbol.mutate(
      { symbol, enabled: true },
      { onSuccess: () => setSymbolInput('') },
    )
  }

  let statusLabel = '后台状态未知'
  if (status.isLoading) statusLabel = '后台状态加载中'
  else if (status.isError) statusLabel = '后台连接失败'
  else if (!status.data) statusLabel = '后台状态未知'
  else if (!status.data.running) statusLabel = '后台未运行'
  else if (!backendReady) statusLabel = '后台准备中'
  else statusLabel = '后台运行中'
  const sourceTime = formatServerTimestamp(overview.data?.source_timestamp)
  const sourceLabel = (
    !backendReady
    || !overview.data?.source
  )
    ? '数据源不可用'
    : `数据源 ${overview.data.source}${sourceTime ? ` · 源 ${sourceTime}` : ''}`

  const beginToggle = async (symbol: string, enabled: boolean) => {
    setPendingToggles(current => new Set(current).add(symbol))
    setToggleErrors(current => {
      const next = new Set(current)
      next.delete(symbol)
      return next
    })
    try {
      await setEnabled.mutateAsync({ symbol, enabled })
    } catch {
      setToggleErrors(current => new Set(current).add(symbol))
    } finally {
      setPendingToggles(current => {
        const next = new Set(current)
        next.delete(symbol)
        return next
      })
    }
  }

  const beginRemove = async (symbol: string) => {
    setPendingRemovals(current => new Set(current).add(symbol))
    setRemoveErrors(current => {
      const next = new Set(current)
      next.delete(symbol)
      return next
    })
    try {
      await removeSymbol.mutateAsync(symbol)
    } catch {
      setRemoveErrors(current => new Set(current).add(symbol))
    } finally {
      setPendingRemovals(current => {
        const next = new Set(current)
        next.delete(symbol)
        return next
      })
    }
  }

  const beginRead = async (notificationId: string) => {
    const notification = notifications.find(item => item.notification_id === notificationId)
    const symbol = notification?.symbol ?? notificationId
    setPendingReads(current => new Set(current).add(notificationId))
    setReadErrors(current => {
      const next = new Map(current)
      next.delete(notificationId)
      return next
    })
    try {
      await markRead.mutateAsync(notificationId)
    } catch {
      setReadErrors(current => new Map(current).set(notificationId, symbol))
    } finally {
      setPendingReads(current => {
        const next = new Set(current)
        next.delete(notificationId)
        return next
      })
    }
  }

  const openDetail = (symbol: string, timeframe: DowTimeframe) => {
    if (onOpen) {
      onOpen(symbol, timeframe)
      return
    }
    detailScrollPosition.current = window.scrollY
    setDetail({ symbol, timeframe })
  }

  const closeDetail = () => {
    setDetail(null)
    window.scrollTo({ top: detailScrollPosition.current, behavior: 'auto' })
  }

  return (
    <div className="min-h-full bg-base">
      <PageHeader
        title="趋势监控"
        subtitle={`${symbols.length} 只 · ${statusLabel}`}
        right={(
          <form
            className="flex items-center gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              submitSymbol()
            }}
          >
            <input
              value={symbolInput}
              onChange={event => setSymbolInput(event.target.value)}
              aria-label="股票代码"
              placeholder="股票代码"
              className="h-8 w-36 rounded-btn border border-border bg-elevated px-2.5 font-mono text-xs uppercase outline-none transition-colors placeholder:font-sans placeholder:normal-case focus:border-accent/50"
            />
            <button
              type="submit"
              aria-label={addSymbol.isPending ? '添加中' : '添加'}
              disabled={addSymbol.isPending}
              className="h-8 rounded-btn bg-accent px-3 text-xs font-medium text-white transition-opacity disabled:cursor-wait disabled:opacity-50"
            >
              {addSymbol.isPending ? '添加中' : '添加'}
            </button>
          </form>
        )}
      />

      {visibleIssues.length > 0 && (
        <div
          role="alert"
          className="flex flex-wrap gap-x-4 gap-y-1 border-b border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger sm:px-5"
        >
          {visibleIssues.map(message => <span key={message}>{message}</span>)}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2 sm:px-5">
        <MarketFilterTabs value={market} onChange={setMarket} />
        <div className="flex h-8 items-center overflow-hidden rounded-btn border border-border bg-surface">
          {SIGNAL_FILTERS.map(option => (
            <button
              key={option.value}
              type="button"
              aria-pressed={signal === option.value}
              onClick={() => setSignal(option.value)}
              className={cn(
                'h-full px-2.5 text-xs font-medium transition-colors',
                signal === option.value
                  ? 'bg-accent/15 text-accent'
                  : 'text-muted hover:bg-elevated hover:text-secondary',
              )}
            >
              {option.label}
            </button>
          ))}
        </div>
        <span className="ml-auto text-[10px] text-muted">{sourceLabel}</span>
      </div>

      <main className="p-3 sm:px-5">
        {overview.isLoading && symbols.length === 0 ? (
          <div className="py-10 text-center text-sm text-muted">加载监控状态…</div>
        ) : filteredSymbols.length === 0 ? (
          <div className="rounded-card border border-dashed border-border py-10 text-center text-sm text-muted">
            当前筛选暂无监控股票
          </div>
        ) : (
          <div
            data-testid="dow-monitor-grid"
            className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4"
          >
            {filteredSymbols.map(item => (
              <DowMonitorCard
                key={item.symbol}
                item={item}
                notifications={filteredNotifications.filter(
                  notification => notification.symbol === item.symbol,
                )}
                notificationLoading={notificationQuery.isLoading}
                notificationError={notificationQuery.isError}
                forceBlocked={forceBlocked}
                blockedReason={connectivityIssues[0]}
                quoteReady={backendReady}
                togglePending={pendingToggles.has(item.symbol)}
                removePending={pendingRemovals.has(item.symbol)}
                onOpen={openDetail}
                onToggle={beginToggle}
                onRemove={beginRemove}
                onRead={beginRead}
                readPendingIds={pendingReads}
              />
            ))}
          </div>
        )}
      </main>
      {detail && (
        <DowMonitorDetailDialog
          symbol={detail.symbol}
          timeframe={detail.timeframe}
          open
          onClose={closeDetail}
        />
      )}
    </div>
  )
}
