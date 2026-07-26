import { useEffect, useMemo, useRef, useState } from 'react'

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
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { useRealtimeMarketData } from '@/lib/realtimeMarketData'

type SignalFilter = 'all' | 'active' | 'buy' | 'sell'
type InstrumentSuggestion = {
  symbol: string
  name: string
  code: string
  market: 'cn' | 'hk' | 'us'
}

const SIGNAL_FILTERS: Array<{ value: SignalFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'active', label: '有信号' },
  { value: 'buy', label: '仅买点' },
  { value: 'sell', label: '仅卖点' },
]

function initialMarket(): DowMonitorMarket {
  if (typeof window === 'undefined') return 'all'
  const value = new URLSearchParams(window.location.search).get('market')
  return value === 'cn' || value === 'hk' || value === 'us' || value === 'all'
    ? value
    : 'all'
}

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
  const [market, setMarket] = useState<DowMonitorMarket>(() => initialMarket())
  const [signal, setSignal] = useState<SignalFilter>('all')
  const [symbolInput, setSymbolInput] = useState('')
  const [suggestions, setSuggestions] = useState<InstrumentSuggestion[]>([])
  const [suggestionsOpen, setSuggestionsOpen] = useState(false)
  const [suggestionsLoading, setSuggestionsLoading] = useState(false)
  const [pendingToggles, setPendingToggles] = useState<Set<string>>(() => new Set())
  const [pendingRemovals, setPendingRemovals] = useState<Set<string>>(() => new Set())
  const [pendingReads, setPendingReads] = useState<Set<string>>(() => new Set())
  const [toggleErrors, setToggleErrors] = useState<Set<string>>(() => new Set())
  const [removeErrors, setRemoveErrors] = useState<Set<string>>(() => new Set())
  const [readErrors, setReadErrors] = useState<Map<string, string>>(() => new Map())
  const [realtimeActive, setRealtimeActive] = useState(false)
  const [detail, setDetail] = useState<{ symbol: string; timeframe: DowTimeframe } | null>(null)
  const symbolFormRef = useRef<HTMLFormElement>(null)
  const detailScrollPosition = useRef(0)
  const overview = useDowMonitorOverview(market, realtimeActive)
  const notificationQuery = useDowNotifications(market)
  const status = useDowMonitorStatus()
  const addSymbol = useAddDowMonitorSymbol()
  const removeSymbol = useRemoveDowMonitorSymbol()
  const setEnabled = useSetDowMonitorEnabled()
  const markRead = useMarkDowNotificationRead()

  useEffect(() => {
    const query = symbolInput.trim()
    if (!query) {
      setSuggestions([])
      setSuggestionsLoading(false)
      return
    }

    let cancelled = false
    const timer = window.setTimeout(async () => {
      setSuggestionsLoading(true)
      try {
        const response = await api.instrumentSearch(query, 8, 'stock', market)
        if (!cancelled) setSuggestions(response.results ?? [])
      } catch {
        if (!cancelled) setSuggestions([])
      } finally {
        if (!cancelled) setSuggestionsLoading(false)
      }
    }, 150)

    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [market, symbolInput])

  useEffect(() => {
    const closeSuggestions = (event: MouseEvent) => {
      if (!symbolFormRef.current?.contains(event.target as Node)) {
        setSuggestionsOpen(false)
      }
    }
    document.addEventListener('mousedown', closeSuggestions)
    return () => document.removeEventListener('mousedown', closeSuggestions)
  }, [])

  const symbols = overview.data?.symbols ?? []
  const realtimeSymbols = useMemo(
    () => symbols
      .filter(item => item.enabled && sameMarket(market, item.market))
      .map(item => item.symbol),
    [market, symbols],
  )
  const realtime = useRealtimeMarketData(
    realtimeSymbols,
    ['quote', 'depth', 'candlestick'],
    1,
  )
  useEffect(() => {
    setRealtimeActive(realtime.status === 'realtime')
  }, [realtime.status])
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
    setSuggestionsOpen(false)
    addSymbol.mutate(
      { symbol, enabled: true },
      {
        onSuccess: () => {
          setSymbolInput('')
          setSuggestions([])
        },
      },
    )
  }

  const setMarketScope = (nextMarket: DowMonitorMarket) => {
    setMarket(nextMarket)
    if (typeof window === 'undefined') return
    const url = new URL(window.location.href)
    url.searchParams.set('market', nextMarket)
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`)
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
            ref={symbolFormRef}
            className="relative flex items-center gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              submitSymbol()
            }}
          >
            <input
              value={symbolInput}
              onChange={(event) => {
                setSymbolInput(event.target.value)
                setSuggestionsOpen(true)
              }}
              onFocus={() => {
                if (symbolInput.trim()) setSuggestionsOpen(true)
              }}
              onKeyDown={(event) => {
                if (event.key === 'Escape') setSuggestionsOpen(false)
              }}
              aria-label="股票代码"
              aria-autocomplete="list"
              aria-controls="dow-monitor-symbol-suggestions"
              aria-expanded={suggestionsOpen && Boolean(symbolInput.trim())}
              placeholder="代码或名称"
              className="h-8 w-52 rounded-btn border border-border bg-elevated px-2.5 text-xs outline-none transition-colors placeholder:font-sans focus:border-accent/50"
            />
            <button
              type="submit"
              aria-label={addSymbol.isPending ? '添加中' : '添加'}
              disabled={addSymbol.isPending}
              className="h-8 rounded-btn bg-accent px-3 text-xs font-medium text-white transition-opacity disabled:cursor-wait disabled:opacity-50"
            >
              {addSymbol.isPending ? '添加中' : '添加'}
            </button>
            {suggestionsOpen && symbolInput.trim() && (
              <div
                id="dow-monitor-symbol-suggestions"
                role="listbox"
                aria-label="股票候选"
                className="absolute right-0 top-full z-50 mt-1 max-h-72 w-80 overflow-y-auto rounded-btn border border-border bg-base shadow-xl"
              >
                {suggestionsLoading ? (
                  <div className="px-3 py-3 text-xs text-muted">搜索中…</div>
                ) : suggestions.length === 0 ? (
                  <div className="px-3 py-3 text-xs text-muted">未找到匹配的股票</div>
                ) : suggestions.map(suggestion => (
                  <button
                    key={suggestion.symbol}
                    type="button"
                    role="option"
                    aria-selected={symbolInput.trim().toUpperCase() === suggestion.symbol}
                    onClick={() => {
                      setSymbolInput(suggestion.symbol)
                      setSuggestionsOpen(false)
                    }}
                    className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition-colors hover:bg-elevated"
                  >
                    <span className="w-24 shrink-0 font-mono text-foreground">
                      {suggestion.symbol}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-secondary">
                      {suggestion.name}
                    </span>
                    {suggestion.code && (
                      <span className="shrink-0 font-mono text-[10px] text-muted">
                        {suggestion.code}
                      </span>
                    )}
                  </button>
                ))}
              </div>
            )}
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
        <MarketFilterTabs value={market} onChange={setMarketScope} />
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
                realtimeState={realtime.states.get(item.symbol.toUpperCase())}
                realtimeStatus={realtime.status}
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
