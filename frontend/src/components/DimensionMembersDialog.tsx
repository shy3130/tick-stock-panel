import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useVirtualizer } from '@tanstack/react-virtual'
import { Link } from 'react-router-dom'
import {
  createChart,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type LineData,
  type Time,
} from 'lightweight-charts'
import { Activity, Building2, ChevronRight, Database, RefreshCw, Search, Tags, Users, X } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { boardTag } from '@/components/stock-table/primitives'
import { toNavItems, type NavItem } from '@/components/StockPreviewDialog'
import { api, type DimensionIntradayPoint, type MarketSnapshotRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { fmtBigNum, fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { useChartTheme } from '@/lib/theme'

export type DimensionKind = 'concept' | 'industry'

export interface DimensionMembersTarget {
  kind: DimensionKind
  value: string
  /** 扩展字段完整标识，例如 ext_gn_ths.所属概念。 */
  sourceField: string
  date?: string
}

export function dimensionKindForSourceField(sourceField: string): DimensionKind | null {
  const separator = sourceField.indexOf('.')
  const field = (separator >= 0 ? sourceField.slice(separator + 1) : sourceField).trim().toLowerCase()
  if (/(概念|题材)|(?:^|[_\s])(concept|theme)(?:$|[_\s])/i.test(field)) return 'concept'
  if (/(行业|申万|中信)|(?:^|[_\s])(industry|sector)(?:$|[_\s])/i.test(field)) return 'industry'
  return null
}

interface Props {
  target: DimensionMembersTarget | null
  onClose: () => void
  onStockClick?: (symbol: string, name?: string, navList?: NavItem[]) => void
}

interface ResolvedSource {
  configId: string
  field: string
}

type SortMode = 'change_desc' | 'change_asc' | 'amount_desc' | 'name'

function resolveSource(sourceField: string): ResolvedSource | null {
  const separator = sourceField.indexOf('.')
  if (separator <= 0 || separator === sourceField.length - 1) return null
  return {
    configId: sourceField.slice(0, separator),
    field: sourceField.slice(separator + 1),
  }
}

function symbolKeys(symbol: unknown): string[] {
  const raw = String(symbol ?? '').trim().toUpperCase()
  if (!raw) return []
  return Array.from(new Set([raw, raw.replace(/\.\w+$/, '')]))
}

function finite(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function stockName(row: Record<string, any>): string {
  return String(row.name ?? row['股票简称'] ?? row['名称'] ?? '')
}

function stockSymbol(row: Record<string, any>): string {
  return String(row.symbol ?? row.code ?? row['股票代码'] ?? row['代码'] ?? '')
}

export function DimensionMembersDialog({ target, onClose, onStockClick }: Props) {
  if (!target) return null
  return (
    <DimensionMembersDialogContent
      key={`${target.sourceField}:${target.value}:${target.date ?? ''}`}
      target={target}
      onClose={onClose}
      onStockClick={onStockClick}
    />
  )
}

function DimensionMembersDialogContent({ target, onClose, onStockClick }: Omit<Props, 'target'> & { target: DimensionMembersTarget }) {
  const source = useMemo(() => resolveSource(target.sourceField), [target.sourceField])
  const [search, setSearch] = useState('')
  const [sortMode, setSortMode] = useState<SortMode>('change_desc')
  const listRef = useRef<HTMLDivElement>(null)

  const membersQuery = useQuery({
    queryKey: source ? QK.dimensionMembers(source.configId, source.field, target.value, target.date) : ['dimension-members-invalid'],
    queryFn: () => api.dimensionMembers(source!.configId, {
      field: source!.field,
      value: target.value,
      date: target.date,
      limit: 10000,
    }),
    enabled: !!source,
    staleTime: 5 * 60_000,
  })

  const marketQuery = useQuery({
    queryKey: QK.marketSnapshot,
    queryFn: api.marketSnapshot,
    enabled: (membersQuery.data?.rows.length ?? 0) > 0,
    staleTime: 60_000,
  })

  const marketMap = useMemo(() => {
    const map = new Map<string, MarketSnapshotRow>()
    for (const row of marketQuery.data?.rows ?? []) {
      for (const key of symbolKeys(row.symbol)) map.set(key, row)
    }
    return map
  }, [marketQuery.data?.rows])

  const rows = useMemo(() => {
    const seen = new Set<string>()
    return (membersQuery.data?.rows ?? []).flatMap(member => {
      const rawSymbol = stockSymbol(member)
      const market = symbolKeys(rawSymbol).map(key => marketMap.get(key)).find(Boolean)
      const symbol = String(market?.symbol ?? rawSymbol)
      if (!symbol || seen.has(symbol)) return []
      seen.add(symbol)
      return [{
        ...member,
        ...market,
        symbol,
        name: market?.name ?? stockName(member),
      }]
    })
  }, [marketMap, membersQuery.data?.rows])

  const visibleRows = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    const filtered = keyword
      ? rows.filter(row => `${row.symbol} ${row.name ?? ''}`.toLowerCase().includes(keyword))
      : rows
    return [...filtered].sort((a, b) => {
      if (sortMode === 'name') return String(a.name ?? a.symbol).localeCompare(String(b.name ?? b.symbol), 'zh-CN')
      if (sortMode === 'amount_desc') return (finite(b.amount) ?? -Infinity) - (finite(a.amount) ?? -Infinity)
      const av = finite(a.change_pct)
      const bv = finite(b.change_pct)
      if (sortMode === 'change_asc') return (av ?? Infinity) - (bv ?? Infinity)
      return (bv ?? -Infinity) - (av ?? -Infinity)
    })
  }, [rows, search, sortMode])

  const stats = useMemo(() => {
    const changes = rows.map(row => finite(row.change_pct)).filter((value): value is number => value != null)
    return {
      up: changes.filter(value => value > 0).length,
      down: changes.filter(value => value < 0).length,
      flat: rows.length - changes.filter(value => value !== 0).length,
      average: changes.length ? changes.reduce((sum, value) => sum + value, 0) / changes.length : null,
    }
  }, [rows])

  const rowVirtualizer = useVirtualizer({
    count: visibleRows.length,
    getScrollElement: () => listRef.current,
    estimateSize: () => 54,
    getItemKey: index => visibleRows[index]?.symbol ?? index,
    overscan: 8,
  })

  useEffect(() => {
    listRef.current?.scrollTo({ top: 0 })
  }, [search, sortMode])

  const accent = target.kind === 'concept'
    ? { icon: Tags, badge: '概念', iconCls: 'text-orange-700 dark:text-orange-300', badgeCls: 'bg-orange-500/10 text-orange-700 dark:text-orange-300' }
    : { icon: Building2, badge: '行业', iconCls: 'text-sky-700 dark:text-sky-300', badgeCls: 'bg-sky-500/10 text-sky-700 dark:text-sky-300' }
  const AccentIcon = accent.icon
  const titleId = 'dimension-members-title'
  const total = membersQuery.data?.total ?? 0

  return (
    <Modal
      onClose={onClose}
      labelledBy={titleId}
      panelClassName="flex h-[86vh] max-h-[760px] w-[94vw] max-w-4xl flex-col overflow-hidden rounded-card border border-border bg-base shadow-2xl"
    >
      <div className="flex shrink-0 items-center gap-3 border-b border-border px-4 py-3">
        <div className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-elevated ${accent.iconCls}`}>
          <AccentIcon className="h-4 w-4" />
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h2 id={titleId} className="truncate text-sm font-semibold text-foreground">{target.value}</h2>
            <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${accent.badgeCls}`}>{accent.badge}</span>
          </div>
          <div className="mt-0.5 flex items-center gap-2 text-[10px] text-muted">
            <span>{membersQuery.data?.label ?? source?.configId ?? '扩展数据'}</span>
            {membersQuery.data?.date && <span>{membersQuery.data.date}</span>}
          </div>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <span className="inline-flex items-center gap-1 text-xs text-secondary">
            <Users className="h-3.5 w-3.5 text-muted" />
            {membersQuery.isLoading ? '—' : total}
          </span>
          <button onClick={onClose} className="inline-flex h-7 w-7 items-center justify-center rounded text-muted hover:bg-elevated hover:text-foreground" title="关闭">
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      {!source ? (
        <div className="grid min-h-64 place-items-center px-6 text-sm text-danger">扩展字段格式无效</div>
      ) : membersQuery.isLoading ? (
        <div className="grid min-h-64 place-items-center text-muted"><RefreshCw className="h-5 w-5 animate-spin" /></div>
      ) : membersQuery.isError ? (
        <div className="grid min-h-64 place-items-center px-6 text-center text-sm text-danger">{String((membersQuery.error as Error).message)}</div>
      ) : (
        <>
          <div className="grid shrink-0 grid-cols-4 divide-x divide-border border-b border-border bg-surface/50">
            <Summary label="上涨" value={stats.up} className="text-bull" />
            <Summary label="下跌" value={stats.down} className="text-bear" />
            <Summary label="平盘/待更新" value={stats.flat} className="text-secondary" />
            <Summary label="平均涨跌" value={fmtPct(stats.average)} className={priceColorClass(stats.average)} />
          </div>

          {source && (
            <DimensionIntradaySection
              configId={source.configId}
              field={source.field}
              value={target.value}
              date={target.date}
              kind={target.kind}
            />
          )}

          <div className="flex shrink-0 items-center gap-2 border-b border-border px-4 py-2.5">
            <div className="relative min-w-0 flex-1">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
              <input
                value={search}
                onChange={event => setSearch(event.target.value)}
                placeholder="搜索代码或名称"
                className="h-8 w-full rounded-input border border-border bg-surface pl-8 pr-3 text-xs text-foreground placeholder:text-muted focus:border-accent/60 focus:outline-none"
              />
            </div>
            <select
              value={sortMode}
              onChange={event => setSortMode(event.target.value as SortMode)}
              className="h-8 rounded-input border border-border bg-surface px-2 text-xs text-secondary focus:border-accent/60 focus:outline-none"
              aria-label="排序方式"
            >
              <option value="change_desc">涨幅从高到低</option>
              <option value="change_asc">涨幅从低到高</option>
              <option value="amount_desc">成交额从高到低</option>
              <option value="name">名称排序</option>
            </select>
          </div>

          <div className="grid shrink-0 grid-cols-[minmax(132px,1fr)_74px_74px_18px] border-b border-border bg-elevated/60 px-4 py-2 text-[10px] font-medium text-muted md:grid-cols-[minmax(180px,1fr)_90px_84px_88px_100px_18px]">
            <span>股票</span><span className="text-right">现价</span><span className="text-right">涨跌幅</span>
            <span className="hidden text-right md:block">换手率</span><span className="hidden text-right md:block">成交额</span><span />
          </div>

          {visibleRows.length === 0 ? (
            <div className="grid min-h-56 place-items-center text-sm text-muted">{search ? '没有匹配的股票' : '暂无成分股'}</div>
          ) : (
            <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto">
              <div className="relative w-full" style={{ height: rowVirtualizer.getTotalSize() }}>
                {rowVirtualizer.getVirtualItems().map(virtualRow => {
                  const row = visibleRows[virtualRow.index]
                  const board = boardTag(row.symbol)
                  return (
                    <button
                      key={virtualRow.key}
                      ref={rowVirtualizer.measureElement}
                      data-index={virtualRow.index}
                      onClick={() => onStockClick?.(row.symbol, row.name, toNavItems(visibleRows))}
                      disabled={!onStockClick}
                      className="absolute left-0 top-0 grid min-h-[54px] w-full grid-cols-[minmax(132px,1fr)_74px_74px_18px] items-center border-b border-border/60 px-4 text-left text-xs transition-colors hover:bg-elevated/50 disabled:cursor-default md:grid-cols-[minmax(180px,1fr)_90px_84px_88px_100px_18px]"
                      style={{ transform: `translateY(${virtualRow.start}px)` }}
                    >
                      <span className="min-w-0">
                        <span className="flex min-w-0 items-center gap-1.5">
                          <span className="min-w-0 truncate font-medium text-foreground">{row.name || row.symbol}</span>
                          {board && <span className={`inline-flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded border text-[9px] font-bold ${board.color}`}>{board.label}</span>}
                        </span>
                        <span className="block font-mono text-[10px] text-muted">{row.symbol}</span>
                      </span>
                      <span className="text-right tabular-nums text-secondary">{fmtPrice(finite(row.close))}</span>
                      <span className={`text-right tabular-nums font-medium ${priceColorClass(finite(row.change_pct))}`}>{fmtPct(finite(row.change_pct))}</span>
                      <span className="hidden text-right tabular-nums text-secondary md:block">{finite(row.turnover_rate) != null ? `${finite(row.turnover_rate)!.toFixed(2)}%` : '—'}</span>
                      <span className="hidden text-right tabular-nums text-secondary md:block">{fmtBigNum(finite(row.amount))}</span>
                      <ChevronRight className="h-3.5 w-3.5 text-muted" />
                    </button>
                  )
                })}
              </div>
            </div>
          )}

          {total > rows.length && (
            <div className="shrink-0 border-t border-border px-4 py-2 text-center text-[10px] text-muted">显示前 {rows.length} / {total} 只</div>
          )}
        </>
      )}
    </Modal>
  )
}

function Summary({ label, value, className }: { label: string; value: string | number; className: string }) {
  return (
    <div className="flex items-baseline justify-center gap-1.5 px-2 py-2.5">
      <span className="text-[10px] text-muted">{label}</span>
      <span className={`text-xs font-semibold tabular-nums ${className}`}>{value}</span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 板块分时 (等权): 点击触发 + 60s 轮询续期, 不预计算
// ---------------------------------------------------------------------------

const INTRADAY_SECTOR_COLOR: Record<DimensionKind, string> = {
  concept: '#F97316',
  industry: '#0EA5E9',
}
const INTRADAY_MARKET_COLOR = '#94A3B8'
// 横轴伪时间戳基点 (2020-01-01 UTC), 每点 +60s 保持均匀间距
const INTRADAY_BASE_TS = 1577836800

function lastNonNull(points: DimensionIntradayPoint[], key: 'sector' | 'market'): number | null {
  for (let i = points.length - 1; i >= 0; i--) {
    const value = points[i]?.[key]
    if (value != null) return value
  }
  return null
}

function DimensionIntradaySection({ configId, field, value, date, kind }: {
  configId: string
  field: string
  value: string
  date?: string
  kind: DimensionKind
}) {
  const query = useQuery({
    queryKey: QK.dimensionIntraday(configId, field, value, date),
    queryFn: () => api.dimensionIntraday(configId, { field, value, date }),
    staleTime: 15_000,
    refetchInterval: 60_000,
  })
  const data = query.data

  return (
    <section className="shrink-0 border-b border-border bg-surface/30">
      <div className="flex items-center gap-2 px-4 pt-2">
        <Activity className="h-3 w-3 text-muted" />
        <span className="text-[10px] font-medium text-muted">分时走势 · 等权</span>
        {data?.member_count != null && data.members_with_minute != null && (
          <span className="rounded bg-elevated px-1 py-px font-mono text-[9px] text-muted" title="有当日分钟数据的成分股数">
            {data.members_with_minute}/{data.member_count}只
          </span>
        )}
        {data?.basis && data.basis !== 'prev_close' && (
          <span
            className="rounded bg-amber-500/10 px-1 py-px text-[9px] text-amber-600 dark:text-amber-400"
            title="前一交易日收盘缺失, 部分标的以当日首根分钟价为基准, 曲线起点约为 0"
          >
            基准:当日首价
          </span>
        )}
        {data?.status === 'ok' && (
          <div className="ml-auto flex items-center gap-2.5 font-mono text-[10px]">
            <span className="inline-flex items-center gap-1">
              <span className="h-[3px] w-3 rounded-full" style={{ background: INTRADAY_SECTOR_COLOR[kind] }} />
              <span className="text-muted">板块</span>
              <span className={priceColorClass(lastNonNull(data.points, 'sector'))}>
                {fmtPct(lastNonNull(data.points, 'sector'))}
              </span>
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="h-[3px] w-3 rounded-full" style={{ background: INTRADAY_MARKET_COLOR }} />
              <span className="text-muted">全市场</span>
              <span className={priceColorClass(lastNonNull(data.points, 'market'))}>
                {fmtPct(lastNonNull(data.points, 'market'))}
              </span>
            </span>
            {data.date && <span className="text-muted">{data.date}</span>}
          </div>
        )}
      </div>

      {query.isLoading ? (
        <div className="mx-4 mb-2 mt-1.5 h-[132px] animate-pulse rounded-md bg-elevated/50" />
      ) : query.isError ? (
        <div className="mx-4 mb-2 mt-1 grid h-[72px] place-items-center rounded-md border border-dashed border-border px-4 text-center text-[11px] text-muted">
          分时加载失败:{String((query.error as Error).message)}
        </div>
      ) : data?.status === 'no_data' ? (
        <div className="mx-4 mb-2 mt-1 flex h-[96px] flex-col items-center justify-center gap-1 rounded-md border border-dashed border-border">
          <Database className="h-4 w-4 text-muted" />
          <p className="text-[11px] text-muted">分钟数据未落盘, 暂无分时走势</p>
          <p className="text-[10px] text-muted/70">需 TickFlow Pro+ 盘后分钟同步 / Expert 盘中增量, 或自定义分钟源</p>
          <Link to="/data" className="text-[10px] text-accent hover:text-accent/80">前往数据页 →</Link>
        </div>
      ) : !data || data.status === 'empty' || data.points.length < 2 ? (
        <div className="grid h-[44px] place-items-center text-[11px] text-muted">
          {data?.reason === 'no_member_bars' ? '成分股当日无分钟数据 (ETF 等标的无分钟落盘)' : '暂无成分股分时数据'}
        </div>
      ) : (
        <IntradayChart points={data.points} kind={kind} />
      )}
    </section>
  )
}

function IntradayChart({ points, kind }: { points: DimensionIntradayPoint[]; kind: DimensionKind }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const sectorRef = useRef<ISeriesApi<'Line'> | null>(null)
  const marketRef = useRef<ISeriesApi<'Line'> | null>(null)
  const ct = useChartTheme()
  const ctRef = useRef(ct)
  ctRef.current = ct
  // v4 不支持字符串时间: 用均匀伪时间戳作横轴, 标签经 formatter 映射回 HH:MM
  const labelsRef = useRef<string[]>([])
  const labelAt = (time: number) => labelsRef.current[time - INTRADAY_BASE_TS] ?? ''

  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    const chart = createChart(el, {
      width: el.clientWidth,
      height: 132,
      layout: {
        // 关闭 TV 角标 (licence 归属改由 README 技术栈外链承担)
        attributionLogo: false,
        background: { color: 'transparent' },
        textColor: ctRef.current.text,
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 10,
      },
      grid: {
        vertLines: { color: ctRef.current.grid },
        horzLines: { color: ctRef.current.grid },
      },
      rightPriceScale: { borderColor: ctRef.current.border, scaleMargins: { top: 0.12, bottom: 0.04 } },
      timeScale: {
        borderColor: ctRef.current.border,
        rightOffset: 2,
        barSpacing: 4,
        tickMarkFormatter: (time: number) => labelAt(time),
      },
      localization: {
        timeFormatter: (time: number) => labelAt(time),
      },
      crosshair: {
        vertLine: { labelVisible: false },
        horzLine: { labelVisible: true },
      },
      handleScroll: false,
      handleScale: false,
    })
    const sector = chart.addLineSeries({
      color: INTRADAY_SECTOR_COLOR[kind],
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
      priceFormat: { type: 'custom', formatter: (v: number) => `${(v * 100).toFixed(2)}%`, minMove: 0.0001 },
      crosshairMarkerRadius: 3,
    })
    const market = chart.addLineSeries({
      color: INTRADAY_MARKET_COLOR,
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerRadius: 2,
    })
    sector.createPriceLine({
      price: 0,
      color: ctRef.current.border,
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: false,
    })
    chartRef.current = chart
    sectorRef.current = sector
    marketRef.current = market

    const observer = new ResizeObserver(() => {
      chart.applyOptions({ width: el.clientWidth })
    })
    observer.observe(el)
    return () => {
      observer.disconnect()
      chart.remove()
      chartRef.current = null
      sectorRef.current = null
      marketRef.current = null
    }
  }, [kind])

  useEffect(() => {
    chartRef.current?.applyOptions({
      layout: { textColor: ct.text },
      grid: { vertLines: { color: ct.grid }, horzLines: { color: ct.grid } },
      rightPriceScale: { borderColor: ct.border },
      timeScale: { borderColor: ct.border },
    })
  }, [ct])

  useEffect(() => {
    const sectorSeries = sectorRef.current
    const marketSeries = marketRef.current
    if (!sectorSeries || !marketSeries) return
    labelsRef.current = points.map(p => p.time)
    const toData = (key: 'sector' | 'market'): LineData[] =>
      points
        .map((p, i) => ({ time: (INTRADAY_BASE_TS + i * 60) as Time, value: p[key] }))
        .filter((d): d is LineData => d.value != null)
    sectorSeries.setData(toData('sector'))
    marketSeries.setData(toData('market'))
    chartRef.current?.timeScale().fitContent()
  }, [points])

  return (
    <div className="px-2 pb-2 pt-1">
      <div ref={containerRef} className="w-full" />
    </div>
  )
}
