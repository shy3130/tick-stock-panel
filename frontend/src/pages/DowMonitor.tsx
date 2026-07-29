import { useEffect, useMemo, useRef, useState } from 'react'

import { MarketFilterTabs } from '@/components/MarketFilterTabs'
import { PageHeader } from '@/components/PageHeader'
import { DowMonitorDetailPanel } from '@/components/dow-monitor/DowMonitorDetailPanel'
import { DowMonitorList } from '@/components/dow-monitor/DowMonitorList'
import { formatServerTimestamp } from '@/components/dow-monitor/formatServerTimestamp'
import { paginateMonitorSymbols } from '@/components/dow-monitor/monitorListPresentation'
import type {
  DowMonitorMarket,
  DowMonitorNotification,
  DowMonitorOverviewSymbol,
  DowMonitorSymbolMarket,
  DowTimeframe,
} from '@/components/dow-monitor/types'
import {
  useAddDowMonitorSymbol,
  useDowMonitorOverview,
  useDowMonitorStatus,
  useDowNotifications,
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

function initialMarket(): DowMonitorSymbolMarket {
  if (typeof window === 'undefined') return 'hk'
  const value = new URLSearchParams(window.location.search).get('market')
  return value === 'cn' || value === 'hk' || value === 'us' ? value : 'hk'
}

function signalSide(
  item: DowMonitorOverviewSymbol,
  notifications: DowMonitorNotification[],
): string | null {
  const candidates = [
    ...(item.latest_notification ? [item.latest_notification] : []),
    ...notifications.filter(notification => notification.symbol === item.symbol),
  ].sort((left, right) => {
    const leftTime = Date.parse(left.available_at ?? left.triggered_at)
    const rightTime = Date.parse(right.available_at ?? right.triggered_at)
    return (Number.isFinite(rightTime) ? rightTime : 0)
      - (Number.isFinite(leftTime) ? leftTime : 0)
  })
  return candidates[0]?.side ?? null
}

function matchesSignal(filter: SignalFilter, side: string | null) {
  if (filter === 'all') return true
  if (filter === 'active') return side != null
  if (filter === 'buy') return side === 'BUY'
  return side === 'SELL' || side === 'RISK'
}

export function DowMonitor({
  onOpen,
}: {
  onOpen?: (symbol: string, timeframe: DowTimeframe) => void
}) {
  const [market, setMarket] = useState<DowMonitorSymbolMarket>(() => initialMarket())
  const [signal, setSignal] = useState<SignalFilter>('all')
  const [page, setPage] = useState(1)
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null)
  const [symbolInput, setSymbolInput] = useState('')
  const [suggestions, setSuggestions] = useState<InstrumentSuggestion[]>([])
  const [suggestionsOpen, setSuggestionsOpen] = useState(false)
  const [suggestionsLoading, setSuggestionsLoading] = useState(false)
  const [pendingToggles, setPendingToggles] = useState<Set<string>>(() => new Set())
  const [pendingRemovals, setPendingRemovals] = useState<Set<string>>(() => new Set())
  const [toggleErrors, setToggleErrors] = useState<Set<string>>(() => new Set())
  const [removeErrors, setRemoveErrors] = useState<Set<string>>(() => new Set())
  const [realtimeActive, setRealtimeActive] = useState(false)
  const symbolFormRef = useRef<HTMLFormElement>(null)

  const overview = useDowMonitorOverview(market, realtimeActive)
  const notificationQuery = useDowNotifications(market)
  const status = useDowMonitorStatus()
  const addSymbol = useAddDowMonitorSymbol()
  const removeSymbol = useRemoveDowMonitorSymbol()
  const setEnabled = useSetDowMonitorEnabled()

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
  const notifications = notificationQuery.data?.notifications ?? []
  const marketSymbols = useMemo(
    () => symbols.filter(item => item.market === market),
    [market, symbols],
  )
  const filteredSymbols = useMemo(
    () => marketSymbols.filter(item =>
      matchesSignal(signal, signalSide(item, notifications))),
    [marketSymbols, notifications, signal],
  )
  const pagination = useMemo(
    () => paginateMonitorSymbols(filteredSymbols, page),
    [filteredSymbols, page],
  )
  const visibleSymbols = pagination.items
  const realtimeSymbols = useMemo(
    () => visibleSymbols.filter(item => item.enabled).map(item => item.symbol),
    [visibleSymbols],
  )
  const realtime = useRealtimeMarketData(
    realtimeSymbols,
    ['quote', 'depth', 'candlestick'],
    5,
  )

  useEffect(() => {
    setRealtimeActive(realtime.status === 'realtime')
  }, [realtime.status])

  useEffect(() => {
    if (page !== pagination.page) setPage(pagination.page)
  }, [page, pagination.page])

  useEffect(() => {
    if (!selectedSymbol) return
    if (visibleSymbols.some(item => item.symbol === selectedSymbol)) return
    setSelectedSymbol(visibleSymbols[0]?.symbol ?? null)
  }, [selectedSymbol, visibleSymbols])

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
  for (const stockSymbol of toggleErrors) {
    mutationIssues.push(`${stockSymbol} 监控开关更新失败，请重试`)
  }
  for (const stockSymbol of removeErrors) {
    mutationIssues.push(`移除 ${stockSymbol} 失败，请重试`)
  }
  const visibleIssues = [...connectivityIssues, ...mutationIssues]

  const submitSymbol = () => {
    const stockSymbol = symbolInput.trim().toUpperCase()
    if (!stockSymbol) return
    setSuggestionsOpen(false)
    addSymbol.mutate(
      { symbol: stockSymbol, enabled: true },
      {
        onSuccess: () => {
          setSymbolInput('')
          setSuggestions([])
        },
      },
    )
  }

  const setMarketScope = (value: DowMonitorMarket) => {
    if (value === 'all') return
    setMarket(value)
    setPage(1)
    if (typeof window === 'undefined') return
    const url = new URL(window.location.href)
    url.searchParams.set('market', value)
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`)
  }

  const setSignalScope = (value: SignalFilter) => {
    setSignal(value)
    setPage(1)
  }

  const beginToggle = async (stockSymbol: string, enabled: boolean) => {
    setPendingToggles(current => new Set(current).add(stockSymbol))
    setToggleErrors(current => {
      const next = new Set(current)
      next.delete(stockSymbol)
      return next
    })
    try {
      await setEnabled.mutateAsync({ symbol: stockSymbol, enabled })
    } catch {
      setToggleErrors(current => new Set(current).add(stockSymbol))
    } finally {
      setPendingToggles(current => {
        const next = new Set(current)
        next.delete(stockSymbol)
        return next
      })
    }
  }

  const beginRemove = async (stockSymbol: string) => {
    setPendingRemovals(current => new Set(current).add(stockSymbol))
    setRemoveErrors(current => {
      const next = new Set(current)
      next.delete(stockSymbol)
      return next
    })
    try {
      await removeSymbol.mutateAsync(stockSymbol)
    } catch {
      setRemoveErrors(current => new Set(current).add(stockSymbol))
    } finally {
      setPendingRemovals(current => {
        const next = new Set(current)
        next.delete(stockSymbol)
        return next
      })
    }
  }

  const selectSymbol = (stockSymbol: string) => {
    if (onOpen) {
      onOpen(stockSymbol, '15m')
      return
    }
    setSelectedSymbol(current => current === stockSymbol ? null : stockSymbol)
  }

  let statusLabel = '后台状态未知'
  if (status.isLoading) statusLabel = '后台状态加载中'
  else if (status.isError) statusLabel = '后台连接失败'
  else if (!status.data) statusLabel = '后台状态未知'
  else if (!status.data.running) statusLabel = '后台未运行'
  else if (!backendReady) statusLabel = '后台准备中'
  else statusLabel = '后台运行中'
  const sourceTime = formatServerTimestamp(overview.data?.source_timestamp)
  const sourceLabel = !backendReady || !overview.data?.source
    ? '数据源不可用'
    : `数据源 ${overview.data.source}${sourceTime ? ` · ${sourceTime}` : ''}`

  return (
    <div className="min-h-full bg-base">
      <PageHeader
        title="趋势监控"
        subtitle={`${marketSymbols.length} 只 · ${statusLabel}`}
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
        <MarketFilterTabs
          value={market}
          onChange={setMarketScope}
          includeAll={false}
        />
        <div className="flex h-8 items-center overflow-hidden rounded-btn border border-border bg-surface">
          {SIGNAL_FILTERS.map(option => (
            <button
              key={option.value}
              type="button"
              aria-pressed={signal === option.value}
              onClick={() => setSignalScope(option.value)}
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
          <>
            <DowMonitorList
              items={visibleSymbols}
              notifications={notifications}
              realtimeStates={realtime.states}
              selectedSymbol={selectedSymbol}
              page={pagination.page}
              pageCount={pagination.pageCount}
              total={pagination.total}
              forceDelayed={connectivityIssues.length > 0}
              pendingToggles={pendingToggles}
              pendingRemovals={pendingRemovals}
              onPageChange={setPage}
              onSelect={selectSymbol}
              onToggle={beginToggle}
              onRemove={beginRemove}
            />
            {selectedSymbol && !onOpen && (
              <DowMonitorDetailPanel
                key={selectedSymbol}
                symbol={selectedSymbol}
                initialTimeframe="15m"
              />
            )}
          </>
        )}
      </main>
    </div>
  )
}
