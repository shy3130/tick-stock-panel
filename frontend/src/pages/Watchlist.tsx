import React, { useState, useCallback, useRef, useEffect, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useVirtualizer } from '@tanstack/react-virtual'
import { motion, AnimatePresence } from 'framer-motion'
import { Trash2, RefreshCw, Star, X, Search, LayoutGrid, List, Settings2, Plus, Check, Filter, Eye, EyeOff, Minus, ChevronsUp, Clock, RotateCcw, ImagePlus } from 'lucide-react'
import { api, type KlineRow, type MinuteKlineRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { fmtPrice, fmtPct, fmtBigNum, priceColorClass } from '@/lib/format'
import { PageHeader } from '@/components/PageHeader'
import { MarketFilterTabs } from '@/components/MarketFilterTabs'
import { EmptyState } from '@/components/EmptyState'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import {
  DimensionMembersDialog,
  dimensionKindForSourceField,
  type DimensionMembersTarget,
} from '@/components/DimensionMembersDialog'
import { WatchlistImportDialog } from '@/components/WatchlistImportDialog'
import { ColumnCustomizer } from '@/components/ColumnCustomizer'
import { StockDataTable } from '@/components/stock-table/StockDataTable'
import { VIRTUAL_LIST_THRESHOLD, useParentScroll } from '@/components/virtual-list/useParentScroll'
import { useTableSort } from '@/components/stock-table/useTableSort'
import { MiniCandlestick } from '@/components/stock-table/MiniCandlestick'
import { MiniIntraday } from '@/components/stock-table/MiniIntraday'
import { boardTag, renderBuiltinDataCell } from '@/components/stock-table/primitives'
import { getSignals, signalCls, getSortValue, UNSORTABLE_KEYS } from '@/lib/stock-table'
import { marketLabel, matchesMarketFilter, type MarketFilter } from '@/lib/market-display'
import { useMarketScope } from '@/lib/market-scope'
import { resolveCandleConfig, resolveIntradayConfig } from '@/lib/list-columns'
import { useQuoteStatus, useCapabilities, usePreferences } from '@/lib/useSharedQueries'
import {
  type ColumnConfig,
  BUILTIN_COLUMNS,
  COLUMN_GROUPS,
  loadColumnConfig,
  saveColumnConfig,
  buildExtColumnsParam,
} from '@/lib/watchlist-columns'

// ===== 板块标识（筛选/卡片用） =====
// 注: boardTag（创/科/北 标签）已移至共享 @/components/stock-table/primitives

const BOARDS = ['沪主板', '深主板', '创业板', '科创板', '北交所'] as const
type BoardType = typeof BOARDS[number]

function getBoardType(symbol: string): BoardType | null {
  if (/^(300|301)/.test(symbol)) return '创业板'
  if (/^688/.test(symbol))       return '科创板'
  if (/\.BJ$/.test(symbol))      return '北交所'
  if (/^60[0135]/.test(symbol))  return '沪主板'
  if (/^00[012]/.test(symbol))   return '深主板'
  return null
}

// ===== 换手率分档色（卡片/表格用） =====

function turnoverColor(rate: number | null | undefined): string {
  if (rate == null || Number.isNaN(rate)) return 'text-[#888]'
  if (rate < 5)   return 'text-[#888]'
  if (rate < 10)  return 'text-[#d4a800]'
  if (rate < 20)  return 'text-[#f97316]'
  if (rate < 35)  return 'text-[#d94a3d]'
  return 'text-[#b84a8a]'
}

// ===== 动态列渲染 =====
// 表头/单元格渲染已共享化：纯数据列由 @/components/stock-table/primitives 的
// renderBuiltinDataCell 处理；symbol/signals/candle/ext 等需上下文的列由下方
// 表格 renderCell 回调处理。表格骨架使用 StockDataTable。

/** 渲染扩展数据列的值（含分隔/标签/展开配置） */
function renderExtValue(
  val: any,
  col: ColumnConfig,
  expanded: boolean,
  onToggle: () => void,
  inline?: boolean,
  onTagClick?: (tag: string) => void,
): React.ReactNode {
  if (val == null || Number.isNaN(val)) return <span className="text-muted">—</span>
  if (typeof val === 'number') {
    // int 类型不显示小数
    const displayVal = Number.isInteger(val) ? fmtPrice(val, 0) : fmtPrice(val)
    return <span className="tabular-nums">{displayVal}</span>
  }
  if (typeof val === 'boolean') {
    return <span className={val ? 'text-bull' : 'text-muted'}>{val ? '是' : '否'}</span>
  }

  // String — 按 extDisplay 配置渲染
  const cfg = col.extDisplay
  const str = String(val)

  // 纯文本模式
  if (cfg?.displayMode === 'text') {
    return <span className="text-foreground">{str}</span>
  }

  // 标签模式（默认）
  const separator = cfg?.separator?.trim() || null
  const tags = separator
    ? str.split(separator).map(s => s.trim()).filter(Boolean)
    : str.split(/[、,，;；\-]/).map(s => s.trim()).filter(Boolean)

  if (tags.length === 0) return <span className="text-muted">—</span>

  const maxTags = cfg?.maxTags ?? 0
  const showAll = maxTags <= 0 || expanded || tags.length <= maxTags
  const sliced = showAll ? tags : tags.slice(0, maxTags)
  const hiddenIndices = maxTags > 0 ? cfg?.hiddenIndices : undefined
  const visibleTags = hiddenIndices?.length
    ? sliced.filter((_, i) => !hiddenIndices.includes(i))
    : sliced
  const hiddenCount = tags.length - visibleTags.length

  // 竖向排列：仅在表格视图、收起状态、设定了显示上限时生效
  const isVertical = !inline && cfg?.tagLayout === 'vertical' && !expanded

  const tagEls = (
    <>
      {visibleTags.map((tag, i) => onTagClick ? (
        <button
          key={i}
          type="button"
          onClick={event => { event.stopPropagation(); onTagClick(tag) }}
          className="inline-block px-1.5 py-px rounded text-[10px] font-medium leading-tight text-yellow-500 bg-yellow-500/10 hover:brightness-95"
        >
          {tag}
        </button>
      ) : (
        <span key={i} className="inline-block px-1.5 py-px rounded text-[10px] font-medium leading-tight text-yellow-500 bg-yellow-500/10">
          {tag}
        </span>
      ))}
      {!showAll && hiddenCount > 0 && (
        <button
          onClick={onToggle}
          className="inline-block px-1.5 py-px rounded text-[10px] font-medium leading-tight text-accent bg-accent/10 hover:bg-accent/20 transition-colors"
        >
          +{hiddenCount}
        </button>
      )}
      {showAll && maxTags > 0 && tags.length > maxTags && (
        <button
          onClick={onToggle}
          className="inline-block px-1.5 py-px rounded text-[10px] font-medium leading-tight text-muted hover:text-foreground transition-colors"
        >
          收起
        </button>
      )}
    </>
  )

  if (inline) {
    // 卡片视图：返回 inline 片段
    return tagEls
  }
  // 表格视图：用 <div> 包裹
  return <div className={isVertical ? 'flex flex-col items-start gap-0.5' : 'flex flex-wrap gap-0.5'}>{tagEls}</div>
}

/** 渲染扩展数据列的 <td> */
function renderExtCell(
  r: any,
  col: ColumnConfig,
  expandedCells: Set<string>,
  onToggleExpand: (key: string) => void,
  onDimensionClick: (target: DimensionMembersTarget) => void,
): React.ReactNode {
  if (col.source.type !== 'ext') return null
  const { configId, fieldName } = col.source
  const val = r[`${configId}__${fieldName}`]
  const cellKey = `${r.symbol}::${col.id}`
  const expanded = expandedCells.has(cellKey)
  const sourceField = `${configId}.${fieldName}`
  const dimensionKind = dimensionKindForSourceField(sourceField)

  const style: React.CSSProperties = {}
  if (col.extDisplay?.maxWidth) {
    style.maxWidth = col.extDisplay.maxWidth
  }

  // 根据值类型决定 td class
  const tdClass = val == null || Number.isNaN(val)
    ? 'px-2 py-1.5 text-right num tabular-nums text-muted'
    : typeof val === 'number'
      ? 'px-2 py-1.5 text-right num tabular-nums'
      : typeof val === 'boolean'
        ? 'px-2 py-1.5 text-right'
        : 'px-2 py-1.5'

  return (
    <td className={tdClass} style={style}>
      {renderExtValue(
        val,
        col,
        expanded,
        () => onToggleExpand(cellKey),
        false,
        dimensionKind ? value => onDimensionClick({ kind: dimensionKind, value, sourceField }) : undefined,
      )}
    </td>
  )
}

// ===== 搜索框组件（紧凑内联式）=====

function StockSearchBox({
  onPreview,
  existingSymbols,
  onAdd,
  marketFilter,
}: {
  onPreview: (symbol: string, name: string) => void
  existingSymbols: string[]
  onAdd: (symbol: string) => void
  marketFilter: MarketFilter
}) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const [activeIdx, setActiveIdx] = useState(-1)

  const search = useQuery({
    queryKey: QK.instrumentSearch(query, 'stock,etf', marketFilter),
    queryFn: () => api.instrumentSearch(query, 20, 'stock,etf', marketFilter),
    enabled: query.trim().length > 0,
    staleTime: 30_000,
  })

  const results = search.data?.results ?? []

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape') { setOpen(false); return }
    if (!open || results.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIdx(i => Math.min(i + 1, results.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIdx(i => Math.max(i - 1, -1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (activeIdx >= 0) handleSelect(results[activeIdx])
      else if (results.length > 0) handleSelect(results[0])
    }
  }

  function handleSelect(r: { symbol: string; name: string }) {
    onPreview(r.symbol, r.name)
    setQuery('')
    setOpen(false)
    setActiveIdx(-1)
  }

  return (
    <div ref={containerRef} className="relative">
      <div className="relative flex items-center">
        <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted pointer-events-none" />
        <input
          ref={inputRef}
          type="text"
          placeholder="搜索…"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setOpen(true); setActiveIdx(-1) }}
          onFocus={() => { if (query.trim()) setOpen(true) }}
          onKeyDown={handleKeyDown}
          className="w-44 h-8 pl-8 pr-2.5 rounded-btn bg-elevated border border-border text-xs text-foreground placeholder:text-muted focus:outline-none focus:border-accent/50 focus:w-56 transition-all duration-200"
        />
      </div>

      <AnimatePresence>
        {open && results.length > 0 && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.12, ease: [0.16, 1, 0.3, 1] }}
            className="absolute right-0 top-full mt-1 z-50 w-64 max-h-[320px] overflow-y-auto rounded-card border border-border bg-base shadow-xl"
          >
            {results.map((r, i) => {
              const inWatchlist = existingSymbols.includes(r.symbol)
              return (
                <div
                  key={r.symbol}
                  className={`flex items-center gap-2.5 px-3 py-2 text-xs transition-colors duration-100 ${
                    i === activeIdx ? 'bg-accent/10 text-accent' : 'hover:bg-elevated text-foreground'
                  }`}
                >
                  <button
                    type="button"
                    onClick={() => handleSelect(r)}
                    className="flex items-center gap-2.5 flex-1 min-w-0 text-left"
                  >
                    <span className="font-mono shrink-0 w-[80px]">{r.symbol}</span>
                    <span className="truncate text-secondary flex-1">{r.name}</span>
                    <span className="shrink-0 rounded border border-border px-1 py-0.5 text-[9px] text-muted">
                      {marketLabel(r.market)}
                    </span>
                    {r.asset_type === 'etf' && (
                      <span className="shrink-0 px-1 py-0.5 rounded text-[10px] leading-none bg-accent/10 text-accent">ETF</span>
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={e => { e.stopPropagation(); onAdd(r.symbol) }}
                    disabled={inWatchlist}
                    className={`shrink-0 p-1 rounded transition-colors ${
                      inWatchlist
                        ? 'text-accent bg-accent/10 cursor-default'
                        : 'text-muted hover:text-accent hover:bg-accent/10'
                    }`}
                    title={inWatchlist ? '已加自选' : '加入自选'}
                  >
                    {inWatchlist ? <Check className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
                  </button>
                </div>
              )
            })}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// ===== 实时监控圆点 =====
// 自选页 symbol 列代码后的小圆点, 标识该标的正在被实时行情监控 (Free/低档按自选监控模式)。
// 视觉: 内圈实心点 + 外圈 animate-ping 扩散晕, 语义=「在线/活动」。
// 配色用 accent (电光蓝) 而非绿/红: 项目设计规范规定红绿仅用于价格/K线,
// UI 状态用 accent, 避免与 A 股涨跌色混淆。
// 全市场模式 (Starter+) 不显示 —— 全部都在监控, 标记无信息量。
function RealtimeDot({ title = '实时监控中' }: { title?: string }) {
  return (
    <span
      title={title}
      className="relative inline-flex h-2 w-2 shrink-0"
      aria-label={title}
    >
      {/* 外圈: 扩散晕 (ping 动画) */}
      <span className="absolute inline-flex h-full w-full rounded-full bg-accent/60 animate-ping motion-reduce:hidden" />
      {/* 内圈: 实心点 + 微辉光 */}
      <span className="relative inline-flex rounded-full h-2 w-2 bg-accent shadow-[0_0_5px_rgba(61,214,140,0.6)]" />
    </span>
  )
}

// ===== 卡片组件 =====

// 共享的空 K 线数组常量 — 避免每次渲染传入新的 [] 破坏 StockCard 的 memo
const EMPTY_KLINE: KlineRow[] = []

function cardColumnCount(viewportWidth: number): number {
  if (viewportWidth >= 1536) return 6
  if (viewportWidth >= 1280) return 5
  if (viewportWidth >= 768) return 4
  if (viewportWidth >= 640) return 3
  return 2
}

function useCardColumnCount(): number {
  const [count, setCount] = useState(() => cardColumnCount(window.innerWidth))

  useEffect(() => {
    const update = () => setCount(cardColumnCount(window.innerWidth))
    window.addEventListener('resize', update)
    return () => window.removeEventListener('resize', update)
  }, [])

  return count
}

const StockCard = React.memo(function StockCard({
  r,
  candleRows,
  showCandle,
  onPreview,
  onConfirmRemove,
  onCancelRemove,
  onRequestRemove,
  isConfirming,
  extCols,
  expandedCells,
  onToggleExpand,
  onDimensionClick,
  isMonitored,
}: {
  r: any
  candleRows: KlineRow[]
  showCandle: boolean
  onPreview: (symbol: string, name: string) => void
  onConfirmRemove: (symbol: string) => void
  onCancelRemove: () => void
  onRequestRemove: (symbol: string) => void
  isConfirming: boolean
  extCols: ColumnConfig[]
  expandedCells: Set<string>
  onToggleExpand: (key: string) => void
  onDimensionClick: (target: DimensionMembersTarget) => void
  isMonitored?: boolean
}) {
  const board = boardTag(r.symbol)
  const price = r.rt_price ?? r.close
  const pct = r.rt_pct ?? r.change_pct
  const name = r.rt_name ?? r.name
  const signals = getSignals(r)
  const isUp = (pct ?? 0) > 0
  const isDown = (pct ?? 0) < 0

  // 动态背景渐变: 涨=红底, 跌=绿底, 平=无色
  const bgGlow = isUp
    ? 'bg-gradient-to-br from-bull/[0.06] via-transparent to-bull/[0.02]'
    : isDown
      ? 'bg-gradient-to-br from-bear/[0.06] via-transparent to-bear/[0.02]'
      : ''
  // 左侧指示条颜色
  const barColor = isUp ? 'bg-bull/70' : isDown ? 'bg-bear/70' : 'bg-muted/30'
  // 涨跌幅标签背景
  const pctBg = isUp ? 'bg-bull/12 text-bull' : isDown ? 'bg-bear/12 text-bear' : 'bg-elevated text-secondary'

  return (
    <div
      className={`relative rounded-lg border border-border bg-surface hover:border-border/80 transition-all duration-200 group cursor-pointer overflow-hidden ${bgGlow}`}
      onClick={() => onPreview(r.symbol, name ?? '')}
    >
      {/* 左侧彩色指示条 */}
      <div className={`absolute left-0 top-0 bottom-0 w-[3px] rounded-l-lg ${barColor}`} />

      {/* 删除按钮 / 确认区 */}
      <div className="absolute top-1.5 right-1.5 z-10">
        {isConfirming ? (
          <div className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
            <button
              onClick={() => onConfirmRemove(r.symbol)}
              className="px-1.5 py-0.5 rounded text-[10px] text-danger bg-danger/10 hover:bg-danger/20 transition-colors"
            >
              确认
            </button>
            <button onClick={() => onCancelRemove()} className="p-0.5 text-muted hover:text-foreground transition-colors">
              <X className="h-3 w-3" />
            </button>
          </div>
        ) : (
          <button
            onClick={e => { e.stopPropagation(); onRequestRemove(r.symbol) }}
            className="opacity-0 group-hover:opacity-100 text-muted hover:text-danger transition-all duration-150 p-0.5 rounded hover:bg-elevated"
            aria-label="移除"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      {/* 卡片内容 */}
      <div className="pl-4 pr-2.5 pt-2.5 pb-0">
        {/* 第一行: 代码 + 名称 + 板块标识 */}
        <div className="flex items-center gap-1.5 min-w-0 mb-2">
          <span className="shrink-0 font-mono text-foreground text-xs tracking-wide">
            {r.symbol}
          </span>
          {name && (
            <span className="text-xs text-secondary truncate">{name}</span>
          )}
          {board && (
            <span className={`shrink-0 inline-flex items-center justify-center px-1 h-[16px] rounded text-[9px] font-bold leading-none ${board.color}`}>
              {board.label}
            </span>
          )}
          {r.consecutive_limit_ups > 0 && (
            <span className="shrink-0 inline-flex items-center justify-center px-1 h-[16px] rounded bg-danger/15 text-danger text-[9px] font-bold tabular-nums">
              {r.consecutive_limit_ups === 1 ? '首板' : `${r.consecutive_limit_ups}连`}
            </span>
          )}
          {isMonitored && <span className="ml-auto"><RealtimeDot /></span>}
        </div>

        {/* 第二行: 大价格 + 涨跌幅胶囊 */}
        <div className="flex items-end justify-between gap-2 mb-2">
          <span className={`text-xl tabular-nums tracking-tighter leading-none ${priceColorClass(pct)}`}>
            {fmtPrice(price)}
          </span>
          {pct != null && (
            <span className={`shrink-0 inline-flex items-center px-1.5 py-[2px] rounded text-[11px] tabular-nums ${pctBg}`}>
              {isUp ? '+' : ''}{pct.toFixed(2)}%
            </span>
          )}
        </div>

        {/* 第三行: 指标 */}
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[10px] text-muted leading-relaxed">
          <span title="换手率">换手<span className={`font-mono ml-0.5 ${turnoverColor(r.turnover_rate)}`}>{r.turnover_rate != null ? `${r.turnover_rate.toFixed(2)}%` : '—'}</span></span>
          <span title="量比">量比<span className="font-mono ml-0.5">{fmtPrice(r.vol_ratio_5d)}</span></span>
          <span title="RSI14">RSI<span className="font-mono ml-0.5">{r.rsi_14 != null ? r.rsi_14.toFixed(1) : '—'}</span></span>
          {/* 扩展数据列展示在卡片中 */}
          {extCols.map(col => {
            if (col.source.type !== 'ext') return null
            const { configId, fieldName } = col.source
            const val = r[`${configId}__${fieldName}`]
            if (val == null) return null

            const cellKey = `${r.symbol}::${col.id}`
            const expanded = expandedCells.has(cellKey)
            const sourceField = `${configId}.${fieldName}`
            const dimensionKind = dimensionKindForSourceField(sourceField)

            return (
              <span key={col.id} title={col.label}>
                <span className="text-secondary">{fieldName}</span>
                <span className="font-mono ml-0.5">
                  {renderExtValue(
                    val,
                    col,
                    expanded,
                    () => onToggleExpand(cellKey),
                    true,
                    dimensionKind ? value => onDimensionClick({ kind: dimensionKind, value, sourceField }) : undefined,
                  )}
                </span>
              </span>
            )
          })}
        </div>
      </div>

      {/* 信号标签区 */}
      {signals.length > 0 && (
        <div className="pl-4 pr-2.5 pt-1.5 pb-2 flex flex-wrap gap-1">
          {signals.slice(0, 3).map(s => (
            <span key={s.label} className={`inline-block px-1.5 py-[1px] rounded text-[9px] font-medium leading-tight ${signalCls(s.type)}`}>
              {s.label}
            </span>
          ))}
          {signals.length > 3 && (
            <span className="inline-block px-1 py-[1px] rounded text-[9px] text-muted bg-elevated leading-tight">
              +{signals.length - 3}
            </span>
          )}
        </div>
      )}

      {/* 迷你蜡烛图 */}
      {showCandle && candleRows.length > 0 && (
        <div className="border-t border-border/40 px-3 py-1.5">
          <MiniCandlestick rows={candleRows} height={32} />
        </div>
      )}
    </div>
  )
})

// ===== 主页面 =====

export function Watchlist() {
  const qc = useQueryClient()
  const { market: marketFilter, setMarket } = useMarketScope()
  const setMarketFilter = useCallback((market: MarketFilter) => {
    if (market !== 'all') setMarket(market)
  }, [setMarket])
  const [viewMode, setViewMode] = useState<'table' | 'card'>(() => {
    return (storage.watchlistView.get('table') as 'table' | 'card')
  })
  const [dailyKChartVisible, setDailyKChartVisible] = useState(() => {
    return storage.watchlistCandle.get(true)
  })
  const [intradayChartVisible, setIntradayChartVisible] = useState(() => {
    return storage.watchlistIntraday.get(true)
  })

  // 列配置 — 从后端/localStorage 异步加载
  const [columns, setColumns] = useState<ColumnConfig[]>([...BUILTIN_COLUMNS])
  const [customizerOpen, setCustomizerOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const columnsLoaded = useRef(false)

  useEffect(() => {
    if (columnsLoaded.current) return
    columnsLoaded.current = true
    loadColumnConfig().then(setColumns)
  }, [])

  const handleColumnsChange = useCallback((next: ColumnConfig[]) => {
    setColumns(next)
    saveColumnConfig(next)
  }, [])

  const candleColumn = useMemo(() =>
    columns.find(c => c.source.type === 'builtin' && c.source.key === 'candle' && c.visible),
    [columns],
  )
  const candleColumnEnabled = !!candleColumn
  // 日k列渲染配置（来自列定制，已钳制边界）
  const candleResolved = useMemo(() => resolveCandleConfig(candleColumn?.candleConfig), [candleColumn])
  const candleDays = candleResolved.days
  const candleSize = dailyKChartVisible
    ? { width: candleResolved.enabledWidth, height: candleResolved.enabledHeight }
    : { width: candleResolved.disabledWidth, height: candleResolved.disabledHeight }

  const dailyKVisible = candleColumnEnabled && dailyKChartVisible

  // 分时列检测: 用户开启且列可见时才拉数据
  const intradayColumn = useMemo(() =>
    columns.find(c => c.source.type === 'builtin' && c.source.key === 'intraday' && c.visible),
    [columns],
  )
  // 分时列渲染配置（宽高, 来自列定制, 已钳制边界）
  const intradayResolved = useMemo(() => resolveIntradayConfig(intradayColumn?.intradayConfig), [intradayColumn])
  // 分时图需 Pro+ (kline.minute.batch), 低档用户开了列也不拉数据
  const caps = useCapabilities()
  const hasMinuteBatch = !!caps.data?.capabilities?.['kline.minute.batch']
  const intradayVisible = !!intradayColumn && hasMinuteBatch && intradayChartVisible

  // 计算可见列（列是否出现只由自定义列配置决定）
  const visibleColumns = useMemo(() => {
    return columns.filter(c => c.visible)
  }, [columns])

  // 计算 ext 列参数
  const extColumnsParam = useMemo(() => buildExtColumnsParam(columns), [columns])

  const toggleView = useCallback(() => {
    setViewMode(v => {
      const next = v === 'table' ? 'card' : 'table'
      storage.watchlistView.set(next)
      return next
    })
  }, [])
  const toggleDailyKChart = useCallback(() => {
    setDailyKChartVisible(v => {
      const next = !v
      storage.watchlistCandle.set(next)
      return next
    })
  }, [])
  const toggleIntradayChart = useCallback(() => {
    setIntradayChartVisible(v => {
      const next = !v
      storage.watchlistIntraday.set(next)
      return next
    })
  }, [])
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const [previewName, setPreviewName] = useState<string>('')
  const [dimensionTarget, setDimensionTarget] = useState<DimensionMembersTarget | null>(null)
  const [expandedCells, setExpandedCells] = useState<Set<string>>(new Set())
  const closePreview = useCallback(() => {
    setPreviewSymbol(null)
    setPreviewName('')
  }, [])

  const handleToggleExpand = useCallback((cellKey: string) => {
    setExpandedCells(prev => {
      const next = new Set(prev)
      if (next.has(cellKey)) next.delete(cellKey)
      else next.add(cellKey)
      return next
    })
  }, [])

  const list = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
  })

  // enriched 数据 — 传入 ext_columns 参数
  const enriched = useQuery({
    queryKey: QK.watchlistEnriched(extColumnsParam),
    queryFn: () => api.watchlistEnriched(extColumnsParam || undefined),
    enabled: (list.data?.symbols.length ?? 0) > 0,
  })

  const symbols = enriched.data?.rows?.map((r: any) => r.symbol) ?? []
  const symbolsKey = symbols.join(',')

  // 实时行情状态 (提前到此处: 分时轮询判断需要 realtimeRunning)
  const quoteStatus = useQuoteStatus()
  const realtimeRunning = quoteStatus.data?.running ?? false

  // 批量日k数据 (天数由列配置决定)
  const klineBatch = useQuery({
    queryKey: QK.watchlistKlineBatch(`${symbolsKey}|${candleDays}`),
    queryFn: () => api.klineDailyBatch(symbols, candleDays),
    enabled: dailyKVisible && symbols.length > 0,
    staleTime: 5 * 60_000,  // 5 分钟内不重请求
  })

  const klineData = dailyKVisible ? (klineBatch.data?.data ?? {}) : {}

  // 批量分时数据 (Pro+ 用户, 列可见时才拉)
  // 刷新策略: 仅当实时行情运行 且 用户在实时监控设置里开启 minute_intraday_refresh 时
  // 按用户设定的间隔轮询 (不接 SSE 高频, 避免每秒拉 TickFlow 触限流); 与 Screener / 设置卡片描述一致。
  const { data: prefsData } = usePreferences()
  const intradayRefreshEnabled = prefsData?.minute_intraday_refresh ?? false
  const intradayRefreshInterval = prefsData?.minute_intraday_refresh_interval ?? 6
  const minuteBatch = useQuery({
    queryKey: QK.minuteBatch(symbolsKey),
    queryFn: () => api.klineMinuteBatch(symbols),
    enabled: intradayVisible && symbols.length > 0,
    staleTime: 10_000,
    refetchInterval: (intradayRefreshEnabled && realtimeRunning) ? intradayRefreshInterval * 1000 : false,
  })
  const minuteData = intradayVisible ? (minuteBatch.data?.data ?? {}) : {}

  const addMutation = useMutation({
    mutationFn: (sym: string) => api.watchlistAdd(sym),
    onSuccess: (data) => {
      qc.setQueryData(QK.watchlist, data)
      qc.invalidateQueries({ queryKey: QK.watchlist })
      qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })
      qc.invalidateQueries({ queryKey: ['watchlist-kline-batch'] })
    },
  })

  const remove = useMutation({
    mutationFn: (sym: string) => api.watchlistRemove(sym),
    onSuccess: (_data, sym) => {
      // 1. 立即从 enriched 缓存中移除该股票，UI 即时更新
      qc.setQueryData(['watchlist-enriched', extColumnsParam], (old: any) => {
        if (!old?.rows) return old
        return { ...old, rows: old.rows.filter((r: any) => r.symbol !== sym) }
      })
      // 2. 清除 list 缓存，触发后台 refetch
      qc.invalidateQueries({ queryKey: QK.watchlist })
      qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })
      qc.invalidateQueries({ queryKey: ['watchlist-kline-batch'] })
    },
  })

  const moveToTop = useMutation({
    mutationFn: (sym: string) => api.watchlistMoveToTop(sym),
    onSuccess: (data) => {
      qc.setQueryData(QK.watchlist, data)
      qc.invalidateQueries({ queryKey: QK.watchlist })
      qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })
      qc.invalidateQueries({ queryKey: ['watchlist-kline-batch'] })
      qc.invalidateQueries({ queryKey: QK.preferences })
      qc.invalidateQueries({ queryKey: QK.quoteStatus })
    },
  })

  const clearAll = useMutation({
    mutationFn: () => api.watchlistClear(),
    onSuccess: () => {
      setConfirmClear(false)
      // 立即清空 enriched 缓存
      qc.setQueryData(['watchlist-enriched', extColumnsParam], { rows: [], as_of: null, elapsed_ms: 0 })
      qc.invalidateQueries({ queryKey: QK.watchlist })
      qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })
      qc.invalidateQueries({ queryKey: ['watchlist-kline-batch'] })
    },
  })

  // 二次确认状态
  const [confirmClear, setConfirmClear] = useState(false)
  const [confirmRemove, setConfirmRemove] = useState<string | null>(null)

  // 稳定的 per-symbol 回调 (供 memo 化的 StockCard 使用, 避免每次渲染都传新引用)
  const handleCardPreview = useCallback((sym: string, name: string) => {
    setPreviewSymbol(sym); setPreviewName(name)
  }, [])
  const handleCardConfirmRemove = useCallback((sym: string) => {
    remove.mutate(sym); setConfirmRemove(null)
  }, [remove])
  const handleCardCancelRemove = useCallback(() => setConfirmRemove(null), [])
  const handleCardRequestRemove = useCallback((sym: string) => setConfirmRemove(sym), [])

  const allSymbols = list.data?.symbols?.map(s => s.symbol) ?? []
  const rows = enriched.data?.rows ?? []

  // 实时监控圆点: 仅 Free/低档 "按自选股实时监控" 模式 (mode === 'watchlist') 下显示;
  // Starter+ 全市场模式 (mode === 'full_market') 全部标的都在监控, 标圆点无意义, 故不显示。
  // 后端 Free 档实际只监控自选页前 N 个 (N = watchlist_symbol_count), 顺序与 allSymbols 一致。
  const realtimeMode = quoteStatus.data?.mode
  const watchlistMonitoredCount = quoteStatus.data?.watchlist_symbol_count ?? 0
  const showRealtimeDot = realtimeRunning && realtimeMode === 'watchlist'
  // 真正被监控的标的集合 (自选列表前 watchlistMonitoredCount 个)
  const monitoredSymbols = useMemo(
    () => showRealtimeDot ? new Set(allSymbols.slice(0, watchlistMonitoredCount)) : new Set<string>(),
    [showRealtimeDot, allSymbols, watchlistMonitoredCount],
  )

  // ===== 筛选 =====
  const [filterOpen, setFilterOpen] = useState(false)
  const [filters, setFilters] = useState<Record<string, { min?: string; max?: string; text?: string }>>({})

  // 板块筛选（持久化）
  const [boardFilter, setBoardFilter] = useState<Set<string>>(() => {
    const saved = storage.watchlistBoardFilter.get([])
    return saved.length > 0 ? new Set(saved) : new Set(BOARDS) // 默认全选
  })
  const persistBoardFilter = useCallback((next: Set<string>) => {
    setBoardFilter(next)
    storage.watchlistBoardFilter.set([...next])
  }, [])

  const toggleBoard = useCallback((board: string) => {
    setBoardFilter(prev => {
      const next = new Set(prev)
      if (next.has(board)) next.delete(board)
      else next.add(board)
      persistBoardFilter(next)
      return next
    })
  }, [persistBoardFilter])

  const updateFilter = useCallback((colId: string, patch: { min?: string; max?: string; text?: string }) => {
    setFilters(prev => {
      const next = { ...prev }
      const existing = next[colId] || {}
      const merged = { ...existing, ...patch }
      if (!merged.min && !merged.max && !merged.text) {
        delete next[colId]
      } else {
        next[colId] = merged
      }
      return next
    })
  }, [])

  const resetAllFilters = useCallback(() => {
    setFilters({})
    persistBoardFilter(new Set(BOARDS))
  }, [persistBoardFilter])

  // 可筛选的内置列
  const filterableBuiltinCols = useMemo(
    () => columns.filter(c => c.source.type === 'builtin' && !UNSORTABLE_KEYS.has(c.source.key) && c.id !== 'builtin:symbol'),
    [columns],
  )

  // 按类别索引（复用列配置的分组定义）
  const colsByCategory = useMemo(() => {
    const map: Record<string, { id: string; label: string; col: typeof filterableBuiltinCols[number] }[]> = {}
    for (const cat of COLUMN_GROUPS) {
      map[cat.label] = []
      for (const key of cat.keys) {
        const col = filterableBuiltinCols.find(c => c.source.type === 'builtin' && c.source.key === key)
        if (col) map[cat.label].push({ id: col.id, label: col.label, col })
      }
    }
    return map
  }, [filterableBuiltinCols])

  // 筛选 + 排序
  const filteredRows = useMemo(() => {
    let result = rows.filter(r => matchesMarketFilter(r.symbol, marketFilter))
    // 板块筛选只约束 A 股；港美股没有创/科/北板块语义。
    if (boardFilter.size > 0 && boardFilter.size < BOARDS.length) {
      result = result.filter(r => {
        if (!matchesMarketFilter(r.symbol, 'cn')) return true
        const board = getBoardType(r.symbol)
        return board != null && boardFilter.has(board)
      })
    }
    // 数值/文本筛选
    const activeFilters = Object.entries(filters).filter(([, v]) => v.min || v.max || v.text)
    if (activeFilters.length > 0) {
      result = result.filter(r => {
        for (const [colId, f] of activeFilters) {
          const col = columns.find(c => c.id === colId)
          if (!col) continue
          const val = getSortValue(r, col)
          if (val == null) return false
          if (typeof val === 'number') {
            if (f.min && val < Number(f.min)) return false
            if (f.max && val > Number(f.max)) return false
          } else {
            if (f.text && !String(val).includes(f.text)) return false
          }
        }
        return true
      })
    }
    return result
  }, [rows, filters, columns, boardFilter, marketFilter])

  const activeFilterCount = Object.values(filters).filter(v => v.min || v.max || v.text).length
  const hasBoardFilter = boardFilter.size > 0 && boardFilter.size < BOARDS.length
  const hasActiveFilters = activeFilterCount > 0 || hasBoardFilter

  // 排序（复用共享三态排序 hook）
  const { sort, toggle: handleSortToggle, sortRows } = useTableSort()

  const sortedRows = useMemo(
    () => sortRows(filteredRows, columns),
    [filteredRows, sortRows, columns],
  )

  const cardColumns = useCardColumnCount()
  const cardGridRef = useRef<HTMLDivElement>(null)
  const virtualizeCards = viewMode === 'card' && sortedRows.length > VIRTUAL_LIST_THRESHOLD
  const cardRowCount = Math.ceil(sortedRows.length / cardColumns)
  const { getScrollElement: getCardScrollElement, scrollMargin: cardScrollMargin } = useParentScroll(
    cardGridRef,
    virtualizeCards,
  )
  const cardRowVirtualizer = useVirtualizer({
    count: virtualizeCards ? cardRowCount : 0,
    getScrollElement: getCardScrollElement,
    estimateSize: () => dailyKVisible ? 180 : 140,
    getItemKey: index => `${cardColumns}:${(sortedRows[index * cardColumns] as any)?.symbol ?? index}`,
    gap: 12,
    overscan: 3,
    scrollMargin: cardScrollMargin,
  })

  // 可见的 ext 列（卡片视图使用）
  const visibleExtCols = useMemo(
    () => visibleColumns.filter(c => c.source.type === 'ext'),
    [visibleColumns]
  )

  // "数据未就绪" 的个股数: 后端 LEFT JOIN 保证返回所有自选行,
  // 指标全为 null 的行属于 enriched 缓存未覆盖 (新股/冷门/新用户未同步), 非筛选导致.
  // 用 close 是否为 null/undefined 判断 "整行指标缺失" (close 是 enriched 最基础字段).
  const pendingCount = useMemo(
    () => sortedRows.filter((r: any) => r.close == null).length,
    [sortedRows],
  )

  // "被筛选条件隐藏" 的个股数: 后端返回的行数 vs 经过前端筛选后的行数.
  // rows.length 是后端实际返回 (含 pending 行), 减去 sortedRows (筛选后) 才是真正的筛选隐藏.
  const hiddenCount = Math.max(0, rows.length - sortedRows.length)

  const renderStockCard = (r: any) => (
    <StockCard
      key={r.symbol}
      r={r}
      candleRows={klineData[r.symbol] ?? EMPTY_KLINE}
      showCandle={dailyKVisible}
      onPreview={handleCardPreview}
      onConfirmRemove={handleCardConfirmRemove}
      onCancelRemove={handleCardCancelRemove}
      onRequestRemove={handleCardRequestRemove}
      isConfirming={confirmRemove === r.symbol}
      extCols={visibleExtCols}
      expandedCells={expandedCells}
      onToggleExpand={handleToggleExpand}
      onDimensionClick={setDimensionTarget}
      isMonitored={monitoredSymbols.has(r.symbol)}
    />
  )

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="自选股"
        titleExtra={
          <span className="inline-flex items-center gap-1.5">
            {/* 计数胶囊: 显示数/总数, mono 字体突出数字 */}
            <span className="inline-flex items-baseline gap-0.5 px-2 py-0.5 rounded-md bg-elevated/70 text-[11px]">
              <span className="font-mono font-semibold text-secondary tabular-nums">{sortedRows.length}</span>
              <span className="text-muted/50">/</span>
              <span className="font-mono text-muted tabular-nums">{allSymbols.length}</span>
              <span className="text-muted/60 ml-0.5">只</span>
            </span>
            {/* 数据未就绪提示: 自选了但 enriched 缓存未覆盖 (新股/冷门/新用户未同步), 指标全为 null */}
            {pendingCount > 0 && (
              <span
                className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[10px] font-medium bg-muted/15 text-muted border border-border/50 whitespace-nowrap"
                title={`当前有 ${pendingCount} 只指标暂未就绪 (新股/冷门股或数据尚未同步), 等待每日数据更新后自动补全`}
              >
                <Clock className="h-2.5 w-2.5" />
                待数据 {pendingCount}
              </span>
            )}
            {/* 过滤提示: 仅在有筛选隐藏时出现, 柔和橙色融入整体 */}
            {hiddenCount > 0 && (
              <span
                className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[10px] font-medium bg-warning/12 text-warning/90 border border-warning/25 whitespace-nowrap"
                title={`当前有 ${hiddenCount} 只被筛选条件隐藏,清除筛选可查看全部`}
              >
                <Filter className="h-2.5 w-2.5" />
                已过滤 {hiddenCount}
              </span>
            )}
          </span>
        }
        right={
          <div className="flex items-center gap-2">
            <MarketFilterTabs value={marketFilter} includeAll={false} onChange={setMarketFilter} />
            {/* 筛选 / 重置 / 搜索 */}
            <button
              onClick={() => setFilterOpen(v => !v)}
              className={`inline-flex items-center justify-center h-8 w-8 rounded-btn transition-colors duration-150 ease-smooth ${
                filterOpen || hasActiveFilters
                  ? 'bg-accent/15 text-accent hover:bg-accent/25'
                  : 'bg-elevated text-secondary hover:bg-elevated/80'
              }`}
              title={`筛选${activeFilterCount > 0 ? ` (${activeFilterCount})` : ''}`}
            >
              <Filter className="h-4 w-4" />
            </button>
            {hasActiveFilters && (
              <button
                onClick={resetAllFilters}
                className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated text-secondary hover:bg-danger/10 hover:text-danger transition-colors duration-150 ease-smooth"
                title="重置全部筛选"
                aria-label="重置全部筛选"
              >
                <RotateCcw className="h-4 w-4" />
              </button>
            )}
            <StockSearchBox
              onPreview={(sym, name) => { setPreviewSymbol(sym); setPreviewName(name) }}
              existingSymbols={allSymbols as string[]}
              onAdd={(sym) => addMutation.mutate(sym)}
              marketFilter={marketFilter}
            />
            <button
              onClick={() => setImportOpen(true)}
              className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150 ease-smooth"
              title="从截图导入自选"
            >
              <ImagePlus className="h-4 w-4" />
            </button>
            <div className="w-px h-5 bg-border" />
            {/* 视图 */}
            <button
              onClick={toggleView}
              className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150 ease-smooth"
              title={viewMode === 'table' ? '卡片视图' : '列表视图'}
            >
              {viewMode === 'table' ? <LayoutGrid className="h-4 w-4" /> : <List className="h-4 w-4" />}
            </button>
            <div className="w-px h-5 bg-border" />
            {/* 自定义列 / 刷新 */}
            <button
              onClick={() => setCustomizerOpen(true)}
              className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150 ease-smooth"
              title="自定义列"
            >
              <Settings2 className="h-4 w-4" />
            </button>
            <button
              onClick={() => enriched.refetch()}
              disabled={enriched.isFetching}
              className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150 ease-smooth disabled:opacity-50"
              title="刷新"
            >
              <RefreshCw className={`h-4 w-4 ${enriched.isFetching ? 'animate-spin' : ''}`} />
            </button>
            {allSymbols.length > 0 && (
              <>
                <div className="w-px h-5 bg-border" />
                <button
                  onClick={() => setConfirmClear(true)}
                  className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-danger/10 text-danger hover:bg-danger/20 transition-colors duration-150 ease-smooth"
                  title="清空自选"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </>
            )}
          </div>
        }
      />

      {/* 筛选栏 */}
      {filterOpen && (
        <div className="px-5 py-2 border-b border-border bg-surface/50 max-h-[184px] overflow-y-auto">
          {/* 板块筛选 */}
          <div className="mb-2">
            <div className="text-[10px] text-muted uppercase tracking-wider mb-0.5">板块</div>
            <div className="flex flex-wrap gap-1">
              {BOARDS.map(board => {
                const active = boardFilter.has(board)
                return (
                  <button
                    key={board}
                    onClick={() => toggleBoard(board)}
                    className={`px-2 py-0.5 rounded text-[11px] transition-colors ${
                      active
                        ? 'bg-accent/15 text-accent'
                        : 'bg-elevated text-secondary hover:text-foreground hover:bg-elevated/80'
                    }`}
                  >
                    {board}
                  </button>
                )
              })}
            </div>
          </div>
          {COLUMN_GROUPS.map(cat => {
            const items = colsByCategory[cat.label]?.filter(i => i.col)
            if (!items?.length) return null
            return (
              <div key={cat.label} className="mb-1.5 last:mb-0">
                <div className="text-[10px] text-muted uppercase tracking-wider mb-0.5">{cat.label}</div>
                <div className="flex flex-wrap gap-x-2 gap-y-1">
                  {items.map(item => {
                    const f = filters[item.id] || {}
                    const hasFilter = !!f.min || !!f.max || !!f.text
                    return (
                      <div key={item.id} className="flex items-center gap-0.5 text-[11px]">
                        <span className={`whitespace-nowrap ${hasFilter ? 'text-accent' : 'text-secondary'}`}>{item.label}</span>
                        <input
                          type="number"
                          value={f.min ?? ''}
                          onChange={e => updateFilter(item.id, { min: e.target.value })}
                          placeholder="min"
                          className={`w-12 h-5 rounded border text-[10px] px-1 placeholder:text-muted focus:outline-none ${
                            hasFilter ? 'border-accent/30 bg-accent/5' : 'border-border bg-elevated'
                          } text-foreground focus:border-accent/50`}
                        />
                        <span className="text-muted">~</span>
                        <input
                          type="number"
                          value={f.max ?? ''}
                          onChange={e => updateFilter(item.id, { max: e.target.value })}
                          placeholder="max"
                          className={`w-12 h-5 rounded border text-[10px] px-1 placeholder:text-muted focus:outline-none ${
                            hasFilter ? 'border-accent/30 bg-accent/5' : 'border-border bg-elevated'
                          } text-foreground focus:border-accent/50`}
                        />
                      </div>
                    )
                  })}
                </div>
              </div>
            )
          })}
          {hasActiveFilters && (
            <button onClick={resetAllFilters} className="mt-1 text-[10px] text-danger hover:text-danger/80 transition-colors">
              重置全部筛选
            </button>
          )}
        </div>
      )}

      {/* 可滚动列表区 — 占满剩余高度，内部独立滚动，表头 sticky 固定 */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        <div className="px-5 py-3">
          {/* 列表 */}
          {list.isLoading && <div className="text-sm text-muted">加载中…</div>}
          {list.isError && <div className="text-sm text-danger">读取自选失败</div>}

          {allSymbols.length === 0 ? (
            <EmptyState
              icon={Star}
              title="自选股为空"
              hint="点击右上角搜索添加标的，或点击图片图标从券商自选截图批量导入。"
            />
          ) : viewMode === 'table' ? (
            <StockDataTable
              columns={visibleColumns}
              rows={sortedRows}
              headerSticky
              sort={sort}
              onSortToggle={handleSortToggle}
              rowKey={(r: any) => r.symbol}
              rowClassName={() => 'border-t border-border hover:bg-elevated/50 transition-colors duration-150 ease-smooth'}
              // 日k列表头：标签 + 显示/隐藏眼睛按钮
              renderHeaderContent={(col) => {
                if (col.source.type === 'builtin' && col.source.key === 'candle') {
                  return (
                    <span className="inline-flex items-center justify-center gap-1.5">
                      <span>{col.label}</span>
                      <button
                        type="button"
                        onClick={(event) => { event.stopPropagation(); toggleDailyKChart() }}
                        className={`inline-flex items-center justify-center w-5 h-5 rounded transition-colors ${
                          dailyKChartVisible
                            ? 'text-accent bg-accent/10 hover:bg-accent/20'
                            : 'text-muted hover:text-foreground hover:bg-elevated'
                        }`}
                        title={dailyKChartVisible ? '隐藏日k蜡烛' : '显示日k蜡烛'}
                        aria-label={dailyKChartVisible ? '隐藏日k蜡烛' : '显示日k蜡烛'}
                      >
                        {dailyKChartVisible ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
                      </button>
                    </span>
                  )
                }
                if (col.source.type === 'builtin' && col.source.key === 'intraday') {
                  const intradayAutoRefresh = intradayRefreshEnabled && realtimeRunning
                  return (
                    <span className="inline-flex items-center justify-center gap-1.5">
                      <span>{col.label}</span>
                      <button
                        type="button"
                        onClick={(event) => { event.stopPropagation(); toggleIntradayChart() }}
                        className={`inline-flex items-center justify-center w-5 h-5 rounded transition-colors ${
                          intradayChartVisible
                            ? 'text-accent bg-accent/10 hover:bg-accent/20'
                            : 'text-muted hover:text-foreground hover:bg-elevated'
                        }`}
                        title={intradayChartVisible ? '隐藏分时图' : '显示分时图'}
                        aria-label={intradayChartVisible ? '隐藏分时图' : '显示分时图'}
                      >
                        {intradayChartVisible ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
                      </button>
                      {/* 分时图显示 且 未开自动轮询时, 提供手动刷新按钮 */}
                      {intradayChartVisible && !intradayAutoRefresh && (
                        <button
                          type="button"
                          onClick={(event) => { event.stopPropagation(); minuteBatch.refetch() }}
                          disabled={minuteBatch.isFetching}
                          className="inline-flex items-center justify-center w-5 h-5 rounded text-muted hover:text-accent hover:bg-accent/10 transition-colors disabled:opacity-40"
                          title="刷新分时数据"
                          aria-label="刷新分时数据"
                        >
                          <RefreshCw className={`h-3.5 w-3.5 ${minuteBatch.isFetching ? 'animate-spin' : ''}`} />
                        </button>
                      )}
                      {/* 自动轮询中: 显示旋转图标提示正在实时刷新 */}
                      {intradayChartVisible && intradayAutoRefresh && (
                        <RefreshCw className="h-3 w-3 text-accent/60 animate-spin" aria-label="实时刷新中" />
                      )}
                    </span>
                  )
                }
                return undefined
              }}
              renderCell={(r: any, col: ColumnConfig) => {
                // ext 列
                if (col.source.type === 'ext') {
                  return renderExtCell(r, col, expandedCells, handleToggleExpand, setDimensionTarget)
                }
                const key = col.source.key
                const price = r.rt_price ?? r.close
                const pct = r.rt_pct ?? r.change_pct
                const name = r.rt_name ?? r.name
                // 自选页 symbol 列：预览 + 内嵌删除（减号图标，二次确认）
                if (key === 'symbol') {
                  const board = boardTag(r.symbol)
                  return (
                    <td className="px-1.5 py-1.5">
                      <div className="flex items-center gap-1 w-full">
                        <button
                          type="button"
                          onClick={() => { setPreviewSymbol(r.symbol); setPreviewName(name ?? '') }}
                          className="flex items-center gap-1 text-left min-w-0"
                        >
                          <span className="font-mono text-foreground text-xs group-hover:text-accent transition-colors duration-150">
                            {r.symbol}
                          </span>
                          {name && (
                            <span className="text-xs text-secondary truncate group-hover:text-foreground transition-colors duration-150">
                              {name}
                            </span>
                          )}
                          {board ? (
                            <span className={`shrink-0 inline-flex items-center justify-center w-[18px] h-[18px] rounded text-[9px] font-bold leading-none border ${board.color}`}>
                              {board.label}
                            </span>
                          ) : null}
                          {monitoredSymbols.has(r.symbol) && <span className="ml-2"><RealtimeDot /></span>}
                        </button>
                        {/* 删除入口：默认减号图标，二次确认时替换为确定按钮 */}
                        <div className="ml-auto pl-1 shrink-0">
                          {confirmRemove === r.symbol ? (
                            <div className="flex items-center gap-1">
                              <button
                                onClick={() => { remove.mutate(r.symbol); setConfirmRemove(null) }}
                                className="px-1.5 py-0.5 rounded text-[10px] text-danger bg-danger/10 hover:bg-danger/20 transition-colors"
                              >
                                确认
                              </button>
                              <button
                                onClick={() => setConfirmRemove(null)}
                                className="p-0.5 text-muted hover:text-foreground transition-colors"
                              >
                                <X className="h-3 w-3" />
                              </button>
                            </div>
                          ) : (
                            <div className="flex items-center gap-1">
                              <button
                                onClick={() => setConfirmRemove(r.symbol)}
                                className="p-0.5 text-muted hover:text-danger transition-colors duration-150 ease-smooth"
                                aria-label="移除"
                                title="移除"
                              >
                                <Minus className="h-3.5 w-3.5" />
                              </button>
                              <button
                                onClick={() => moveToTop.mutate(r.symbol)}
                                disabled={moveToTop.isPending || allSymbols[0] === r.symbol}
                                className="p-0.5 text-muted hover:text-accent transition-colors duration-150 ease-smooth disabled:opacity-30 disabled:hover:text-muted"
                                aria-label="移到顶部"
                                title="移到顶部"
                              >
                                <ChevronsUp className="h-3.5 w-3.5" />
                              </button>
                            </div>
                          )}
                        </div>
                      </div>
                    </td>
                  )
                }
                // 实时行情列：price/pct/amount 使用 rt_ 回退（自选页有实时推送）
                const numCls = 'px-2 py-1.5 text-right num tabular-nums'
                if (key === 'price') {
                  return <td className={`${numCls} ${priceColorClass(pct)}`}>{fmtPrice(price)}</td>
                }
                if (key === 'pct') {
                  return <td className={`${numCls} ${priceColorClass(pct)}`}>{fmtPct(pct)}</td>
                }
                if (key === 'amount') {
                  return <td className={`${numCls} text-secondary`}>{fmtBigNum(r.rt_amount ?? r.amount)}</td>
                }
                if (key === 'turnover') {
                  return <td className={`${numCls} ${turnoverColor(r.turnover_rate)}`}>{r.turnover_rate != null ? `${r.turnover_rate.toFixed(2)}%` : '—'}</td>
                }
                // 信号列
                if (key === 'signals') {
                  const signals = getSignals(r)
                  return (
                    <td className="px-2 py-1.5">
                      {signals.length > 0 && (
                        <div className="flex flex-wrap gap-0.5">
                          {signals.slice(0, 3).map((s) => (
                            <span key={s.label} className={`inline-block px-1.5 py-px rounded text-[10px] font-medium leading-tight ${signalCls(s.type)}`}>
                              {s.label}
                            </span>
                          ))}
                          {signals.length > 3 && (
                            <span className="text-[10px] text-muted">+{signals.length - 3}</span>
                          )}
                        </div>
                      )}
                    </td>
                  )
                }
                // 日k列
                if (key === 'candle') {
                  return (
                    <td
                      className="pl-2 pr-3 py-1.5"
                      style={{ width: candleSize.width + 4, minWidth: candleSize.width + 4, maxWidth: candleSize.width + 4, height: candleSize.height }}
                    >
                      <MiniCandlestick rows={klineData[r.symbol] ?? []} width={candleSize.width} height={candleSize.height} />
                    </td>
                  )
                }
                // 分时列
                if (key === 'intraday') {
                  const rows: MinuteKlineRow[] = minuteData[r.symbol] ?? []
                  // 眼睛关闭(收起)时用小尺寸 (和日k收起态一致 40x40); 开启时用配置值
                  const iw = intradayChartVisible ? intradayResolved.width : 40
                  const ih = intradayChartVisible ? intradayResolved.height : 40
                  return (
                    <td className="pl-3 pr-2 py-1.5 border-l border-border/30" style={{ width: iw + 4, minWidth: iw + 4, maxWidth: iw + 4, height: ih }}>
                      <div className="flex items-center justify-center">
                        {intradayChartVisible
                          ? <MiniIntraday rows={rows} prevClose={r.prev_close} changePct={r.change_pct} width={iw - 4} height={ih} />
                          : <span className="text-[10px] text-muted">分时</span>}
                      </div>
                    </td>
                  )
                }
                // 其余纯数据列 → 共享原语
                return renderBuiltinDataCell(r, col)
              }}
              className="rounded-card overflow-x-auto"
            />
          ) : !virtualizeCards ? (
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 gap-3">
              {sortedRows.map(renderStockCard)}
            </div>
          ) : (
            <div
              ref={cardGridRef}
              className="relative"
              style={{ height: cardRowVirtualizer.getTotalSize() }}
            >
              {cardRowVirtualizer.getVirtualItems().map(virtualRow => {
                const start = virtualRow.index * cardColumns
                const row = sortedRows.slice(start, start + cardColumns)
                return (
                  <div
                    key={virtualRow.key}
                    ref={cardRowVirtualizer.measureElement}
                    data-index={virtualRow.index}
                    className="absolute left-0 top-0 w-full grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 gap-3"
                    style={{ transform: `translateY(${virtualRow.start - cardScrollMargin}px)` }}
                  >
                    {row.map(renderStockCard)}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>

      {/* 清空确认弹窗 */}
      <AnimatePresence>
        {confirmClear && (
          <div className="fixed inset-0 z-50 flex items-center justify-center">
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="absolute inset-0 bg-black/60 backdrop-blur-sm"
              onClick={() => setConfirmClear(false)}
            />
            <motion.div
              initial={{ opacity: 0, scale: 0.95, y: 12 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.97, y: 8 }}
              transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
              className="relative w-[90vw] max-w-[380px] rounded-card border border-border bg-base shadow-2xl p-6"
            >
              <h3 className="text-sm font-medium text-foreground mb-2">确认清空自选</h3>
              <p className="text-xs text-secondary mb-5">
                将移除全部 {allSymbols.length} 只自选股，此操作不可恢复。
              </p>
              <div className="flex items-center justify-end gap-2">
                <button
                  onClick={() => setConfirmClear(false)}
                  className="px-3 py-1.5 rounded-btn bg-elevated text-secondary hover:bg-elevated/80 text-sm transition-colors"
                >
                  取消
                </button>
                <button
                  onClick={() => clearAll.mutate()}
                  disabled={clearAll.isPending}
                  className="px-3 py-1.5 rounded-btn bg-danger/15 text-danger hover:bg-danger/25 text-sm font-medium transition-colors disabled:opacity-50"
                >
                  {clearAll.isPending ? '清除中...' : '确认清空'}
                </button>
              </div>
            </motion.div>
          </div>
        )}
      </AnimatePresence>

      {/* 列自定义侧栏 */}
      <ColumnCustomizer
        columns={columns}
        onChange={handleColumnsChange}
        open={customizerOpen}
        onClose={() => setCustomizerOpen(false)}
      />

      <StockPreviewDialog
        symbol={previewSymbol}
        name={previewName}
        onClose={closePreview}
      />

      <DimensionMembersDialog
        target={dimensionTarget}
        onClose={() => setDimensionTarget(null)}
        onStockClick={(symbol, name) => {
          setDimensionTarget(null)
          setPreviewSymbol(symbol)
          setPreviewName(name ?? '')
        }}
      />

      <WatchlistImportDialog open={importOpen} onClose={() => setImportOpen(false)} />
    </div>
  )
}
