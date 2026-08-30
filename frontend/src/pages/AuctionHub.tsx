import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { FlaskConical, Loader2, RefreshCw, Star } from 'lucide-react'
import { DatePicker } from '@/components/DatePicker'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { api, type AuctionStyle } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { fmtPct, fmtPrice, fmtVolume, fmtBigNum, priceColorClass } from '@/lib/format'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'

const STYLES: { id: AuctionStyle; label: string }[] = [
  { id: 'limit_up', label: '打板' },
  { id: 'volume_price', label: '量价' },
  { id: 'momentum', label: '动量' },
  { id: 'swing', label: '波段' },
]

const STAGE_LABEL: Record<string, string> = {
  pre_open: '未开竞价',
  cancellable: '可撤单',
  locked: '不可撤',
  final: '正式撮合',
  post_open: '连续竞价',
  closed: '已收盘',
}

function cnTodayISO() {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date())
}

const chip = (active: boolean) =>
  cn(
    'inline-flex h-7 items-center rounded border px-2.5 text-[11px] font-medium transition-colors',
    active
      ? 'border-accent/40 bg-accent/12 text-accent'
      : 'border-border bg-base text-secondary hover:text-foreground',
  )

export function AuctionHub() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [tab, setTab] = useState<'market' | 'live' | 'research'>('market')
  const [selected, setSelected] = useState<string | null>(null)
  const today = cnTodayISO()
  const [style, setStyle] = useState<AuctionStyle>('limit_up')
  const [tradeDate, setTradeDate] = useState(cnTodayISO)
  const [marketSort, setMarketSort] = useState('涨幅')
  const marketCount = 200
  const [preview, setPreview] = useState<{ symbol: string; name: string } | null>(null)

  const status = useQuery({
    queryKey: QK.auctionStatus(tradeDate),
    queryFn: () => api.auctionStatus(tradeDate),
    refetchInterval: tradeDate === today ? 5000 : false,
  })
  const market = useQuery({
    queryKey: QK.auctionMarket(marketSort, marketCount),
    queryFn: () => api.auctionMarket({ sortBy: marketSort, count: marketCount }),
    refetchInterval: tradeDate === today ? 5000 : false,
    placeholderData: previous => previous,
  })
  const rankings = useQuery({
    queryKey: QK.auctionRankings(tradeDate, 0, style),
    queryFn: () => api.auctionRankings({ tradeDate, style, limit: 50 }),
    refetchInterval: tradeDate === today ? 5000 : false,
    placeholderData: previous => previous,
  })
  const seriesSymbol = selected ?? rankings.data?.rows[0]?.symbol
  const series = useQuery({
    queryKey: QK.auctionSeries(seriesSymbol ?? '', tradeDate, rankings.data?.as_of_ms ?? 0),
    queryFn: () => api.auctionSeries(seriesSymbol!, tradeDate, rankings.data?.as_of_ms),
    enabled: Boolean(seriesSymbol),
    placeholderData: previous => previous,
  })

  const refresh = useMutation({
    mutationFn: () => api.auctionRefresh(tradeDate),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['auction-status'] })
      void qc.invalidateQueries({ queryKey: ['auction-rankings'] })
      void qc.invalidateQueries({ queryKey: ['auction-series'] })
    },
  })
  const saveCandidate = useMutation({
    mutationFn: () => api.auctionSaveCandidate({ trade_date: tradeDate, style, limit: 20 }),
    onSuccess: () => toast('已保存为研究候选 (pending, 不会自动发布)', 'success'),
  })
  const addWatchlist = useMutation({
    mutationFn: (symbols: string[]) => api.watchlistBatchAdd(symbols, '竞价排行'),
    onSuccess: () => toast('已加入自选', 'success'),
  })

  const rows = rankings.data?.rows ?? []
  const seriesName = rows.find(r => r.symbol === seriesSymbol)?.name ?? seriesSymbol
  const sources = status.data?.sources ?? []
  const degraded = status.data?.degraded ?? rankings.data?.degraded
  const stage = status.data?.stage ?? ''
  const loading = rankings.isLoading && !rankings.data
  const loadError = rankings.isError ? (rankings.error as Error).message : null

  const curve = useMemo(() => {
    const points = series.data?.points ?? []
    const priced = points
      .filter(p => p.indicative_price != null)
      .map(p => ({ t: p.source_time_ms, price: p.indicative_price as number }))
    if (priced.length < 2) return null
    const prices = priced.map(p => p.price)
    const min = Math.min(...prices)
    const max = Math.max(...prices)
    const span = max - min || 1
    const t0 = priced[0].t
    const t1 = priced[priced.length - 1].t
    const tSpan = t1 - t0 || 1
    return priced
      .map(p => {
        const y = 88 - ((p.price - min) / span) * 72
        const x = ((p.t - t0) / tSpan) * 100
        return `${x.toFixed(2)},${y.toFixed(2)}`
      })
      .join(' ')
  }, [series.data])

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0">
        <PageHeader
          title="竞价"
          subtitle={`${STAGE_LABEL[stage] ?? (stage || '开盘集合竞价')} · ${tradeDate}`}
          titleExtra={
            degraded ? (
              <span className="rounded bg-warning/15 px-1.5 py-0.5 text-[10px] text-warning">
                {status.data?.has_finals ? '无过程序列' : '降级回放'}
              </span>
            ) : null
          }
          right={
            <div className="flex flex-wrap items-center justify-end gap-2">
              <DatePicker value={tradeDate} onChange={setTradeDate} align="right" />
              <button
                type="button"
                onClick={() => refresh.mutate()}
                disabled={refresh.isPending}
                className="inline-flex h-7 items-center gap-1 rounded border border-border bg-base px-2 text-[11px] text-secondary transition-colors hover:text-foreground disabled:opacity-50"
                title="刷新"
              >
                <RefreshCw className={`h-3 w-3 ${refresh.isPending ? 'animate-spin' : ''}`} />
                刷新
              </button>
            </div>
          }
        />

        <div className="flex flex-wrap items-center gap-2 border-b border-border px-5 py-2">
          <div className="flex gap-1">
            {([
              ['market', '全市场'],
              ['live', '当日'],
              ['research', '研究'],
            ] as const).map(([id, label]) => (
              <button key={id} type="button" onClick={() => setTab(id)} className={chip(tab === id)}>
                {label}
              </button>
            ))}
          </div>
          <div className="ml-auto flex flex-wrap gap-1">
            {STYLES.map(item => (
              <button
                key={item.id}
                type="button"
                onClick={() => setStyle(item.id)}
                className={chip(style === item.id)}
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-auto px-5 py-3">
        {tab === 'market' && (
          <div className="min-h-0 flex-1 overflow-auto rounded-card border border-border">
            <div className="flex flex-wrap items-center gap-2 border-b border-border bg-surface px-3 py-2 text-[11px]">
              <span className="text-muted">全市场实时排行初筛（非竞价过程口径）</span>
              <div className="ml-auto flex flex-wrap items-center gap-1">
                {['涨幅', '成交额', '量比'].map(sort => (
                  <button
                    key={sort}
                    type="button"
                    onClick={() => setMarketSort(sort)}
                    className={chip(marketSort === sort)}
                  >
                    {sort}
                  </button>
                ))}
              </div>
            </div>
            <table className="w-full min-w-[720px] text-left text-xs">
              <thead className="sticky top-0 z-10 bg-surface text-muted">
                <tr>
                  <th className="px-3 py-2 font-normal">#</th>
                  <th className="px-3 py-2 font-normal">代码</th>
                  <th className="px-3 py-2 font-normal">涨跌幅</th>
                  <th className="px-3 py-2 font-normal">成交额</th>
                  <th className="px-3 py-2 font-normal">量(手)</th>
                  <th className="px-3 py-2 font-normal">开盘冲</th>
                  <th className="px-3 py-2 font-normal">封单额</th>
                  <th className="px-3 py-2 font-normal" />
                </tr>
              </thead>
              <tbody>
                {(market.data?.rows ?? []).map((row, i) => (
                  <tr key={row.symbol} className="border-t border-border/60 hover:bg-elevated/40">
                    <td className="px-3 py-1.5 text-muted">{i + 1}</td>
                    <td className="px-3 py-1.5">
                      <button
                        type="button"
                        className="max-w-[160px] truncate text-left hover:underline"
                        onClick={() => setPreview({ symbol: row.symbol, name: row.name || row.symbol })}
                      >
                        <div className="truncate">{row.name || row.symbol}</div>
                        <div className="font-mono text-[10px] text-muted">{row.symbol}</div>
                      </button>
                    </td>
                    <td className={`num px-3 py-1.5 ${priceColorClass(row.change_pct)}`}>{fmtPct(row.change_pct)}</td>
                    <td className="num px-3 py-1.5">{fmtBigNum(row.amount)}</td>
                    <td className="num px-3 py-1.5">{fmtVolume(row.volume_hand)}</td>
                    <td className="num px-3 py-1.5">{row.opening_rush != null ? `${row.opening_rush}%` : '—'}</td>
                    <td className="num px-3 py-1.5">{row.seal_amount != null ? fmtBigNum(row.seal_amount) : '—'}</td>
                    <td className="px-3 py-1.5">
                      <button
                        type="button"
                        className="rounded p-1 text-muted hover:text-foreground"
                        title="加入自选"
                        onClick={() => addWatchlist.mutate([row.symbol])}
                      >
                        <Star className="h-3.5 w-3.5" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {tab === 'research' && (
          <div className="shrink-0 rounded-card border border-border bg-surface p-3 text-xs leading-relaxed text-secondary">
            <p>
              排名只形成研究候选。保存后进入候选库 pending，不会自动发布策略或监控。
              回测股票池应使用上一交易日 09:25 正式撮合；09:25 证据不能当成已经成交。
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              <button
                type="button"
                className="inline-flex h-7 items-center gap-1 rounded border border-border bg-base px-2.5 text-[11px] text-secondary hover:text-foreground disabled:opacity-50"
                onClick={() => saveCandidate.mutate()}
                disabled={!rows.length || saveCandidate.isPending}
              >
                <FlaskConical className="h-3.5 w-3.5" />
                保存研究候选
              </button>
              <button
                type="button"
                className="inline-flex h-7 items-center gap-1 rounded border border-border bg-base px-2.5 text-[11px] text-secondary hover:text-foreground disabled:opacity-50"
                onClick={() => navigate(`/backtest?symbols=${encodeURIComponent(rows.map(r => r.symbol).join(','))}`)}
                disabled={!rows.length}
              >
                用该股票池去回测
              </button>
              <button
                type="button"
                className="inline-flex h-7 items-center gap-1 rounded border border-border bg-base px-2.5 text-[11px] text-secondary hover:text-foreground disabled:opacity-50"
                onClick={() => addWatchlist.mutate(rows.map(r => r.symbol))}
                disabled={!rows.length || addWatchlist.isPending}
              >
                <Star className="h-3.5 w-3.5" />
                全部加入自选
              </button>
            </div>
          </div>
        )}

        <div className="flex flex-wrap gap-1.5 text-[11px]">
          {sources.length === 0 && <span className="text-muted">尚未发现竞价数据源</span>}
          {sources.map(src => {
            const caps = [src.series ? '过程' : null, src.finals ? '撮合' : null].filter(Boolean).join('+')
            return (
            <span
              key={src.name}
              className={cn(
                'rounded border px-1.5 py-0.5',
                src.available
                  ? 'border-border bg-elevated/60 text-secondary'
                  : 'border-warning/30 bg-warning/10 text-warning',
              )}
              title={src.reason}
            >
              {src.name}
              {caps ? ` · ${caps}` : ''}
              {src.available ? '' : ` · ${src.reason}`}
            </span>
            )
          })}
        </div>

        {tab === 'live' && curve && seriesSymbol ? (
          <div className="shrink-0 rounded-card border border-border bg-surface p-3">
            <div className="mb-1 text-[11px] text-secondary">虚拟参考价 · {seriesName}</div>
            <svg viewBox="0 0 100 100" className="h-24 w-full text-accent" preserveAspectRatio="none">
              <polyline fill="none" stroke="currentColor" strokeWidth="1.4" points={curve} />
            </svg>
          </div>
        ) : null}

        {loading ? (
          <div className="grid flex-1 place-items-center py-16 text-muted">
            <Loader2 className="h-5 w-5 animate-spin" />
          </div>
        ) : loadError ? (
          <EmptyState title="竞价数据加载失败" hint={loadError} />
        ) : rows.length === 0 ? (
          <EmptyState
            title="暂无竞价排行"
            hint={
              status.data?.has_finals && !status.data?.has_series
                ? '当前可用源只有 09:25 正式撮合，没有开盘过程。点刷新拉取撮合；过程序列需要 eltdx。'
                : '竞价是独立数据集，未声明 auction 的行情源会跳过。09:15-09:25 轮询自选过程；历史日可刷新 Tushare 撮合或 eltdx 过程。'
            }
          />
        ) : (
          <div className="min-h-0 flex-1 overflow-auto rounded-card border border-border">
            <table className="w-full min-w-[720px] text-left text-xs">
              <thead className="sticky top-0 z-10 bg-surface text-muted">
                <tr>
                  <th className="px-3 py-2 font-normal">#</th>
                  <th className="px-3 py-2 font-normal">代码</th>
                  <th className="px-3 py-2 font-normal">得分</th>
                  <th className="px-3 py-2 font-normal">缺口</th>
                  <th className="px-3 py-2 font-normal">指示价</th>
                  <th className="px-3 py-2 font-normal">匹配量</th>
                  <th className="px-3 py-2 font-normal">质量</th>
                  <th className="px-3 py-2 font-normal">说明</th>
                  <th className="px-3 py-2 font-normal" />
                </tr>
              </thead>
              <tbody>
                {rows.map((row, i) => (
                  <tr
                    key={row.symbol}
                    className={`border-t border-border/60 hover:bg-elevated/40 ${row.symbol === seriesSymbol ? 'bg-elevated/40' : ''}`}
                    onClick={() => setSelected(row.symbol)}
                  >
                    <td className="px-3 py-1.5 text-muted">{i + 1}</td>
                    <td className="px-3 py-1.5">
                      <button
                        type="button"
                        className="max-w-[160px] truncate text-left hover:underline"
                        onClick={e => { e.stopPropagation(); setSelected(row.symbol) }}
                      >
                        <div className="truncate">{row.name || row.symbol}</div>
                        <div className="font-mono text-[10px] text-muted">{row.symbol}</div>
                      </button>
                    </td>
                    <td className="num px-3 py-1.5">{row.score.toFixed(1)}</td>
                    <td className={`num px-3 py-1.5 ${priceColorClass(row.gap_pct)}`}>{fmtPct(row.gap_pct)}</td>
                    <td className="num px-3 py-1.5">{fmtPrice(row.indicative_price)}</td>
                    <td className="num px-3 py-1.5">{fmtVolume(row.matched_volume)}</td>
                    <td className="num px-3 py-1.5">{row.quality_score?.toFixed(0) ?? '—'}</td>
                    <td className="px-3 py-1.5">
                      <div className="flex max-w-[220px] flex-wrap gap-1">
                        {(row.reasons ?? []).slice(0, 3).map(reason => (
                          <span key={reason} className="rounded bg-elevated px-1 py-px text-[10px] text-secondary">
                            {reason}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td className="px-3 py-1.5">
                      <button
                        type="button"
                        className="rounded p-1 text-muted hover:text-foreground"
                        title="加入自选"
                        onClick={e => { e.stopPropagation(); addWatchlist.mutate([row.symbol]) }}
                      >
                        <Star className="h-3.5 w-3.5" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {preview && (
        <StockPreviewDialog
          symbol={preview.symbol}
          name={preview.name}
          onClose={() => setPreview(null)}
        />
      )}
    </div>
  )
}
