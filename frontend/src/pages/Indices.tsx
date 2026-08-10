import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, Loader2, Lock, RefreshCw, Search } from 'lucide-react'
import { api, type IndexInstrument, type KlineRow, type MinuteKlineRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useCapabilities } from '@/lib/useSharedQueries'
import { EChartsCandlestick, type OHLC } from '@/components/EChartsCandlestick'
import { EChartsIntraday } from '@/components/EChartsIntraday'

function defaultRange() {
  const now = new Date()
  const end = now.toISOString().slice(0, 10)
  const s = new Date(now)
  s.setMonth(s.getMonth() - 6)
  return { start: s.toISOString().slice(0, 10), end }
}

function toOHLC(rows: KlineRow[]): OHLC[] {
  return rows
    .filter(r => r?.date != null && r.open != null && r.close != null)
    .map(r => ({
      date: typeof r.date === 'string' ? r.date.slice(0, 10) : String(r.date),
      open: Number(r.open),
      high: Number(r.high),
      low: Number(r.low),
      close: Number(r.close),
      volume: Number(r.volume ?? 0),
      ma5: r.ma5 != null ? Number(r.ma5) : null,
      ma10: r.ma10 != null ? Number(r.ma10) : null,
      ma20: r.ma20 != null ? Number(r.ma20) : null,
      ma60: r.ma60 != null ? Number(r.ma60) : null,
      macd_dif: r.macd_dif != null ? Number(r.macd_dif) : null,
      macd_dea: r.macd_dea != null ? Number(r.macd_dea) : null,
      macd_hist: r.macd_hist != null ? Number(r.macd_hist) : null,
      rsi_6: r.rsi_6 != null ? Number(r.rsi_6) : null,
      rsi_14: r.rsi_14 != null ? Number(r.rsi_14) : null,
      rsi_24: r.rsi_24 != null ? Number(r.rsi_24) : null,
      kdj_k: r.kdj_k != null ? Number(r.kdj_k) : null,
      kdj_d: r.kdj_d != null ? Number(r.kdj_d) : null,
      kdj_j: r.kdj_j != null ? Number(r.kdj_j) : null,
      boll_upper: r.boll_upper != null ? Number(r.boll_upper) : null,
      boll_lower: r.boll_lower != null ? Number(r.boll_lower) : null,
    }))
}

function fmtPct(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return '--'
  return `${Number(v).toFixed(2)}%`
}

function fmtNum(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(Number(v))) return '--'
  return Number(v).toFixed(digits)
}

const PINNED_INDEXES = [
  { symbol: '000001.SH', name: '上证指数' },
  { symbol: '399001.SZ', name: '深证成指' },
  { symbol: '399006.SZ', name: '创业板指' },
  { symbol: '000680.SH', name: '科创综指' },
]

function pinnedRank(item: IndexInstrument) {
  return PINNED_INDEXES.findIndex(p => item.symbol === p.symbol || item.name === p.name)
}

export function Indices() {
  const qc = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [keyword, setKeyword] = useState('')
  const symbolParam = searchParams.get('symbol') ?? ''
  const [selected, setSelected] = useState<string>(symbolParam)
  const [range, setRange] = useState(defaultRange)
  const [selectedDate, setSelectedDate] = useState<string | null>(null)
  const [linkedPrice, setLinkedPrice] = useState<number | null>(null)

  // 分时数据需 kline.minute.batch 能力
  const caps = useCapabilities()
  const hasMinuteCap = !!caps.data?.capabilities?.['kline.minute.batch']

  const list = useQuery({
    queryKey: QK.indexList,
    queryFn: api.indexList,
  })

  const search = useQuery({
    queryKey: ['index-search', keyword],
    queryFn: () => api.indexSearch(keyword, 50),
    enabled: keyword.trim().length > 0,
  })

  const rows: IndexInstrument[] = keyword.trim()
    ? (search.data?.results ?? [])
    : (list.data?.results ?? [])
  const topRows = useMemo(() => {
    const all = list.data?.results ?? []
    return PINNED_INDEXES.map(p => (
      all.find(item => item.symbol === p.symbol || item.name === p.name) ?? { symbol: p.symbol, name: p.name, asset_type: 'index' as const }
    ))
  }, [list.data?.results])
  const listRows = useMemo(() => rows.filter(item => pinnedRank(item) < 0), [rows])

  const selectedSymbol = selected || topRows[0]?.symbol || listRows[0]?.symbol || ''

  useEffect(() => {
    if (symbolParam && symbolParam !== selected) setSelected(symbolParam)
  }, [selected, symbolParam])

  const selectIndex = (symbol: string) => {
    setSelected(symbol)
    setSearchParams({ symbol })
  }

  const quotes = useQuery({
    queryKey: QK.indexQuotes,
    queryFn: () => api.indexQuotes(),
    placeholderData: (prev) => prev,
  })

  const daily = useQuery({
    queryKey: QK.indexDaily(selectedSymbol, range.start, range.end),
    queryFn: () => api.indexDaily(selectedSymbol, 180, range),
    enabled: !!selectedSymbol,
    placeholderData: (prev) => prev,
  })

  const minute = useQuery({
    queryKey: QK.indexMinute(selectedSymbol, selectedDate ?? ''),
    queryFn: () => api.indexMinute(selectedSymbol, selectedDate ?? undefined),
    enabled: !!selectedSymbol && !!selectedDate && hasMinuteCap,
    placeholderData: (prev) => prev,
  })

  const syncInstruments = useMutation({
    mutationFn: api.syncIndexInstruments,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.indexList })
      qc.invalidateQueries({ queryKey: QK.indexQuotes })
    },
  })

  const syncDaily = useMutation({
    mutationFn: () => api.syncIndexDaily(365),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.indexList })
      qc.invalidateQueries({ queryKey: QK.indexQuotes })
      qc.invalidateQueries({ queryKey: ['index-daily'] })
    },
  })

  const quoteBySymbol = useMemo(() => {
    const m = new Map<string, any>()
    for (const q of quotes.data?.rows ?? []) m.set(q.symbol, q)
    return m
  }, [quotes.data?.rows])
  const selectedQuote = selectedSymbol ? quoteBySymbol.get(selectedSymbol) : null
  const selectedQuoteValue = selectedQuote?.last_price ?? selectedQuote?.price ?? selectedQuote?.close
  const selectedQuotePct = selectedQuote?.change_pct ?? selectedQuote?.pct

  const chartRows = useMemo(() => toOHLC(daily.data?.rows ?? []), [daily.data?.rows])
  const selectedInfo = [...topRows, ...listRows].find(r => r.symbol === selectedSymbol) || daily.data?.index_info
  const minuteRows: MinuteKlineRow[] = minute.data?.rows ?? []
  const selectedIdx = selectedDate ? chartRows.findIndex(r => r.date === selectedDate) : -1
  const prevClose = selectedIdx > 0
    ? chartRows[selectedIdx - 1].close
    : chartRows.length >= 2
      ? chartRows[chartRows.length - 2].close
      : undefined

  useEffect(() => {
    setSelectedDate(null)
    setLinkedPrice(null)
  }, [selectedSymbol])

  useEffect(() => {
    if ((!selectedDate || !chartRows.some(r => r.date === selectedDate)) && chartRows.length > 0 && daily.data?.symbol === selectedSymbol) {
      setSelectedDate(chartRows[chartRows.length - 1].date)
    }
  }, [chartRows, daily.data?.symbol, selectedDate, selectedSymbol])
  const renderIndexItem = (item: IndexInstrument) => {
    const q = quoteBySymbol.get(item.symbol)
    const pct = q?.change_pct ?? q?.pct
    const current = q?.last_price ?? q?.price ?? q?.close
    const active = item.symbol === selectedSymbol
    return (
      <button
        key={item.symbol}
        onClick={() => selectIndex(item.symbol)}
        className={`w-full rounded-btn px-2 py-2 text-left transition-colors ${active ? 'bg-accent/15 text-foreground' : 'hover:bg-elevated text-secondary'}`}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-xs font-medium">{item.name || item.symbol}</span>
          <span className={`text-[10px] font-mono ${Number(pct ?? 0) >= 0 ? 'text-bull' : 'text-bear'}`}>{fmtPct(pct)}</span>
        </div>
        <div className="mt-0.5 flex items-center justify-between text-[10px] font-mono text-muted">
          <span>{item.symbol}</span>
          <span>{fmtNum(current)}</span>
        </div>
      </button>
    )
  }

  return (
    <div className="workspace-page h-full overflow-auto">
      <div className="workspace-content gap-3">
        <div className="workspace-toolbar justify-between">
          <div className="min-w-0">
            <h1 className="section-title text-base">指数</h1>
            <p className="mt-0.5 text-xs text-muted">
              指数使用独立 kline_index_* parquet，不进入股票选股和策略链路。
            </p>
          </div>
          <div className="workspace-toolbar">
            <button
              onClick={() => syncInstruments.mutate()}
              disabled={syncInstruments.isPending}
              className="btn-secondary text-xs"
            >
              {syncInstruments.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
              同步指数列表
            </button>
            <button
              onClick={() => syncDaily.mutate()}
              disabled={syncDaily.isPending}
              className="btn-primary text-xs"
            >
              {syncDaily.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
              同步指数日K
            </button>
          </div>
        </div>

        <div className="grid min-h-0 grid-cols-1 gap-3 lg:grid-cols-[15rem_minmax(0,1fr)]">
          <aside className="panel">
            <div className="panel-body space-y-3">
              <div className="relative">
                <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
                <input
                  value={keyword}
                  onChange={e => setKeyword(e.target.value)}
                  placeholder="搜索指数代码/名称"
                  className="control w-full pl-7"
                />
              </div>
              <div className="space-y-1 border-b border-border pb-3">
                {topRows.map(renderIndexItem)}
              </div>
              <div className="max-h-[calc(100vh-24rem)] space-y-1 overflow-auto pr-1">
                {(list.isLoading || search.isLoading) && <div className="py-4 text-center text-xs text-muted">加载中…</div>}
                {!list.isLoading && listRows.length === 0 && (
                  <div className="rounded-btn bg-elevated p-3 text-xs text-muted">
                    {keyword.trim() ? '无匹配指数。' : '暂无更多指数，先点击“同步指数列表”。'}
                  </div>
                )}
                {listRows.map(renderIndexItem)}
              </div>
            </div>
          </aside>

          <main className="panel min-w-0">
            <div className="panel-header flex-wrap">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <Activity className="h-4 w-4 text-accent" />
                  <h2 className="section-title truncate">
                    {selectedInfo?.name || selectedSymbol || '未选择指数'}
                  </h2>
                  {selectedSymbol && <span className="font-mono text-xs text-muted">{selectedSymbol}</span>}
                  {selectedSymbol && <span className="metric-value text-sm">{fmtNum(selectedQuoteValue)}</span>}
                  {selectedSymbol && <span className={`font-mono text-xs ${Number(selectedQuotePct ?? 0) >= 0 ? 'text-bull' : 'text-bear'}`}>{fmtPct(selectedQuotePct)}</span>}
                </div>
                <div className="mt-1 text-xs text-muted">
                  实时缓存 {quotes.data?.count ?? 0} 只指数 · 日K来源 {daily.data?.source ?? '--'}
                </div>
              </div>
              <div className="workspace-toolbar text-xs">
                <input
                  type="date"
                  value={range.start}
                  onChange={e => setRange(r => ({ ...r, start: e.target.value }))}
                  className="control"
                />
                <span className="text-muted">至</span>
                <input
                  type="date"
                  value={range.end}
                  onChange={e => setRange(r => ({ ...r, end: e.target.value }))}
                  className="control"
                />
              </div>
            </div>
            <div className="panel-body">
              {daily.isLoading && <div className="py-10 text-center text-sm text-muted">日K加载中…</div>}
              {daily.isError && <div className="py-4 text-sm text-danger">指数日K加载失败</div>}
              {!daily.isLoading && !daily.isError && chartRows.length === 0 && (
                <div className="rounded-btn bg-elevated p-6 text-center text-sm text-muted">
                  暂无日K数据。可以先同步指数日K，或选择其他指数。
                </div>
              )}
              {chartRows.length > 0 && (
                <div className="flex min-w-0 flex-col items-stretch gap-3 xl:flex-row xl:items-start">
                  <div className="min-w-0 flex-1">
                    <EChartsCandlestick
                      data={chartRows}
                      height={620}
                      showMA={true}
                      showInfoBar={true}
                      showMarkers={false}
                      symbol={selectedSymbol}
                      linkedPrice={linkedPrice}
                      onDateClick={setSelectedDate}
                      visibleBars={48}
                      activeIndicators={['vol', 'macd']}
                    />
                  </div>
                  <div className="min-w-0 flex-1 border-t border-border pt-3 xl:border-l xl:border-t-0 xl:pl-3 xl:pt-0" style={{ height: 620 }}>
                    {!hasMinuteCap ? (
                      <div className="flex h-full flex-col items-center justify-center gap-2 text-center">
                        <Lock className="h-5 w-5 text-muted" />
                        <div className="text-xs text-secondary">当前数据源缺少批量分钟K能力</div>
                        <div className="text-[10px] text-muted">切换到支持该能力的数据源后可查看指数分时走势</div>
                      </div>
                    ) : (
                      <>
                        {minute.isLoading && <div className="py-2 text-xs text-muted">分时加载中…</div>}
                        {!minute.isLoading && minuteRows.length === 0 && (
                          <div className="flex h-full items-center justify-center text-xs text-muted">
                            暂无分时数据
                          </div>
                        )}
                        {minuteRows.length > 0 && (
                          <EChartsIntraday
                            data={minuteRows}
                            height={620}
                            prevClose={prevClose}
                            date={selectedDate ?? undefined}
                            symbol={selectedSymbol}
                            showLimitLines={false}
                            showAvgLine={false}
                            onPriceHover={setLinkedPrice}
                          />
                        )}
                      </>
                    )}
                  </div>
                </div>
              )}
            </div>
          </main>
        </div>
      </div>
    </div>
  )
}
