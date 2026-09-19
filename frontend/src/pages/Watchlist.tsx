import React, { useState, useCallback, useRef, useEffect, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useVirtualizer } from '@tanstack/react-virtual'
import { motion, AnimatePresence } from 'framer-motion'
import { Trash2, RefreshCw, Star, X, Search, LayoutGrid, List, Rows3, BarChart3, Settings2, Plus, Check, Filter, Eye, EyeOff, Minus, ChevronsUp, Clock, RotateCcw, FileUp, FolderOpen, FolderMinus, FolderPlus } from 'lucide-react'
import { api, type KlineRow, type MinuteKlineRow, type WatchlistGroup, type WatchlistGroupColor } from '@/lib/api'
import { fetchMinuteBatchIncremental } from '@/lib/minuteBatchIncremental'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { fmtPrice, fmtPct, fmtBigNum, priceColorClass, formatExtNumber } from '@/lib/format'
import { cn } from '@/lib/cn'
import { computeGroupPcts, loadGroupStatsConfig, type GroupStatsConfigPatch } from '@/lib/watchlistGroupStats'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { toNavItems, type NavItem } from '@/lib/listNav'
import {
  DimensionMembersDialog,
  dimensionKindForSourceField,
  type DimensionMembersTarget,
} from '@/components/DimensionMembersDialog'
import { WatchlistImportDialog } from '@/components/WatchlistImportDialog'
import { WatchlistAddMenu } from '@/components/WatchlistAddMenu'
import {
  WatchlistGroupBar,
  WatchlistGroupPicker,
  type WatchlistGroupFilter,
} from '@/components/WatchlistGroups'
import { WatchlistGroupCards } from '@/components/WatchlistGroupCards'
import { WatchlistGroupStatsBar } from '@/components/WatchlistGroupStatsBar'
import { ExtensionSlot } from '@/extensions/ExtensionSlot'

// 分时列开放排序 (StockDataTable 实例级白名单; 表头眼睛/刷新按钮已 stopPropagation)
const INTRADAY_SORTABLE_KEYS = new Set(['intraday'])
import { ColumnCustomizer } from '@/components/ColumnCustomizer'
import { StockDataTable } from '@/components/stock-table/StockDataTable'
import { VIRTUAL_LIST_THRESHOLD, useParentScroll } from '@/components/virtual-list/useParentScroll'
import { useTableSort } from '@/components/stock-table/useTableSort'
import { MiniCandlestick } from '@/components/stock-table/MiniCandlestick'
import { MiniIntraday } from '@/components/stock-table/MiniIntraday'
import { boardTag, renderBuiltinDataCell } from '@/components/stock-table/primitives'
import { getSignals, signalCls, getSortValue, getIntradaySortValue, UNSORTABLE_KEYS } from '@/lib/stock-table'
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

// 板块筛选选项 = 股票板块 + ETF（ETF 无 symbol 板块语义，按 asset_type 匹配）
const ETF_BOARD = 'ETF'
const BOARD_OPTIONS = [...BOARDS, ETF_BOARD]

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
    // 数字格式化: 千分位 + 单位换算 + 小数位(由列配置控制)
    const cfg = col.extDisplay
    const hasNumFmt = cfg?.thousandSeparator || (cfg?.unitConvert && cfg.unitConvert !== 'none')
    const displayVal = hasNumFmt
      ? formatExtNumber(val, { thousandSeparator: cfg?.thousandSeparator, unitConvert: cfg?.unitConvert, unitDecimals: cfg?.unitDecimals })
      : (Number.isInteger(val) ? fmtPrice(val, 0) : fmtPrice(val))
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
  existingBySymbol,
  groups,
  onAdd,
  onToggleMember,
  preferredGroupId,
  addPending,
  memberPending,
}: {
  onPreview: (symbol: string, name: string) => void
  /** symbol -> 该标的当前所属分组 id 列表; 不在 Map 中 = 未加自选 */
  existingBySymbol: Map<string, string[]>
  groups: WatchlistGroup[]
  onAdd: (symbol: string, groupId: string | null) => void
  onToggleMember: (symbol: string, groupId: string, member: boolean) => void
  preferredGroupId: string | null
  addPending: boolean
  memberPending: boolean
}) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const [activeIdx, setActiveIdx] = useState(-1)

  const search = useQuery({
    queryKey: QK.instrumentSearch(query, 'stock,etf,index'),
    queryFn: () => api.instrumentSearch(query, 20, 'stock,etf,index'),
    enabled: query.trim().length > 0,
    staleTime: 30_000,
  })

  const results = search.data?.results ?? []

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (e.target instanceof Element && e.target.closest('[data-watchlist-group-menu]')) return
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
            className="absolute right-0 top-full mt-1 z-50 w-72 max-h-[320px] overflow-y-auto rounded-card border border-border bg-base shadow-xl"
          >
            {results.map((r, i) => {
              const entryGids = existingBySymbol.get(r.symbol)
              const inWatchlist = entryGids !== undefined
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
                    {/* 名称+标签组: 标签紧贴名称文字, 而不是被 flex-1 推到行尾 */}
                    <span className="flex min-w-0 flex-1 items-center gap-1">
                      <span className="truncate text-secondary">{r.name}</span>
                      {r.asset_type === 'etf' && (
                        <span className="shrink-0 px-1 py-0.5 rounded text-[10px] leading-none bg-accent/10 text-accent">ETF</span>
                      )}
                      {r.asset_type === 'index' && (
                        <span className="shrink-0 px-1 py-0.5 rounded text-[10px] leading-none bg-sky-500/10 text-sky-400">指数</span>
                      )}
                      {(() => {
                        const b = boardTag(r.symbol)
                        return b && (
                          <span className={`shrink-0 px-1 py-0.5 rounded text-[10px] leading-none border ${b.color}`}>{b.label}</span>
                        )
                      })()}
                    </span>
                  </button>
                  {inWatchlist ? (
                    // 已加自选: 对勾标识 + 分组勾选面板, 可继续加入/移出其他分组
                    // (走 members 端点, 不重排列表、不覆盖备注)
                    <span className="flex shrink-0 items-center gap-1">
                      <span
                        className="inline-flex p-1 text-accent/70"
                        title="已加自选"
                        aria-label="已加自选"
                      >
                        <Check className="h-3.5 w-3.5" />
                      </span>
                      <WatchlistGroupPicker
                        groups={groups}
                        groupIds={entryGids}
                        symbol={r.symbol}
                        disabled={memberPending}
                        onToggleMember={onToggleMember}
                      />
                    </span>
                  ) : (
                    // 未加自选: + 一键加入当前分组页签 (全部/未分组页签下加为未分组);
                    // 文件夹图标展开分组菜单, 显式选择目标分组
                    <span className="flex shrink-0 items-center gap-0.5">
                      <button
                        type="button"
                        onClick={event => { event.stopPropagation(); onAdd(r.symbol, preferredGroupId ?? null) }}
                        disabled={addPending}
                        className="shrink-0 rounded p-1 text-muted transition-colors hover:bg-accent/10 hover:text-accent disabled:opacity-50 cursor-pointer"
                        title={
                          preferredGroupId
                            ? `加入自选 · 当前分组「${groups.find(g => g.id === preferredGroupId)?.name ?? ''}」`
                            : '加入自选 (未分组)'
                        }
                        aria-label={`快速加入自选 ${r.symbol}`}
                      >
                        <Plus className="h-3.5 w-3.5" />
                      </button>
                      <WatchlistAddMenu
                        onSelect={groupId => onAdd(r.symbol, groupId)}
                        preferredGroupId={preferredGroupId}
                        disabled={addPending}
                        triggerClassName="shrink-0 rounded p-1 text-muted transition-colors hover:bg-accent/10 hover:text-accent disabled:opacity-50"
                        title="展开分组, 选择要加入的自选分组"
                      >
                        <FolderPlus className="h-3.5 w-3.5" />
                      </WatchlistAddMenu>
                    </span>
                  )}
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
// 全市场模式不显示 —— 全部都在监控, 标记无信息量。
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
  active,
  groups,
  onToggleMember,
  groupChangePending,
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
  /** 正在 K 线弹窗预览中 → 高亮卡片 */
  active?: boolean
  groups: WatchlistGroup[]
  onToggleMember: (symbol: string, groupId: string, member: boolean) => void
  groupChangePending: boolean
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
      className={`relative rounded-lg border border-border bg-surface hover:border-border/80 transition-all duration-200 group cursor-pointer overflow-hidden ${bgGlow} ${active ? 'ring-2 ring-accent/60' : ''}`}
      onClick={() => onPreview(r.symbol, name ?? '')}
    >
      {/* 左侧彩色指示条 */}
      <div className={`absolute left-0 top-0 bottom-0 w-[3px] rounded-l-lg ${barColor}`} />

      {/* 分组与删除入口 */}
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
          <div className="flex items-center gap-0.5" onClick={e => e.stopPropagation()}>
            <WatchlistGroupPicker
              groups={groups}
              groupIds={r.group_ids ?? []}
              symbol={r.symbol}
              disabled={groupChangePending}
              onToggleMember={onToggleMember}
            />
            <button
              onClick={() => onRequestRemove(r.symbol)}
              className="opacity-0 group-hover:opacity-100 text-muted hover:text-danger transition-all duration-150 p-0.5 rounded hover:bg-elevated"
              aria-label="移除"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        )}
      </div>

      {/* 卡片内容 */}
      <div className="pl-4 pr-2.5 pt-2.5 pb-0">
        {/* 第一行: 代码 + 名称 + 板块标识 */}
        <div className="flex items-center gap-1.5 min-w-0 mb-2 pr-8">
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
              {fmtPct(pct)}
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
                <span className="text-secondary">{col.label}</span>
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
  const [viewMode, setViewMode] = useState<'table' | 'card'>(() => {
    return (storage.watchlistView.get('table') as 'table' | 'card')
  })
  // 分组卡片总览: 临时整页模式, 不持久化; 关闭(含刷新)后回到原视图设置
  const [groupCardsOpen, setGroupCardsOpen] = useState(false)
  // 分组统计条: 顶部图形化分组涨跌概览, 会话内开关, 不影响个股视图设置
  const [groupStatsOpen, setGroupStatsOpen] = useState(false)
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
  const [searchParams] = useSearchParams()
  const initialGroup = (searchParams.get('group') as WatchlistGroupFilter | null) ?? 'all'
  const [selectedGroup, setSelectedGroup] = useState<WatchlistGroupFilter>(initialGroup)
  // URL ?group= 变化时同步选中分组 (侧边栏二级菜单切换分组时触发)
  useEffect(() => {
    const g = (searchParams.get('group') as WatchlistGroupFilter | null) ?? 'all'
    setSelectedGroup(g)
  }, [searchParams])
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
  // 分时图依赖分钟K批量数据 (kline.minute.batch), 无数据时开了列也不拉
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
    setGroupCardsOpen(false)
    setViewMode(v => {
      const next = v === 'table' ? 'card' : 'table'
      storage.watchlistView.set(next)
      return next
    })
  }, [])
  // 分组卡片: 整页临时展示, 开关不触碰个股视图设置
  const toggleGroupView = useCallback(() => {
    setGroupCardsOpen(open => !open)
  }, [])
  const toggleGroupStats = useCallback(() => {
    setGroupStatsOpen(open => !open)
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
  // 切股导航: 默认用 previewNavItems(自选列表); 从成分弹窗打开时用成分列表覆盖
  const [previewNavList, setPreviewNavList] = useState<NavItem[]>([])
  const [dimensionTarget, setDimensionTarget] = useState<DimensionMembersTarget | null>(null)
  const [expandedCells, setExpandedCells] = useState<Set<string>>(new Set())
  const closePreview = useCallback(() => {
    setPreviewSymbol(null)
    setPreviewName('')
    setPreviewNavList([])
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

  const groupList = useQuery({
    queryKey: QK.watchlistGroups,
    queryFn: api.watchlistGroups,
  })
  const groups = groupList.data?.groups ?? []
  const activeGroupId = selectedGroup === 'all' || selectedGroup === 'ungrouped'
    ? null
    : selectedGroup

  useEffect(() => {
    if (
      selectedGroup !== 'all'
      && selectedGroup !== 'ungrouped'
      && groupList.isSuccess
      && !groups.some(group => group.id === selectedGroup)
    ) {
      setSelectedGroup('all')
    }
  }, [groupList.isSuccess, groups, selectedGroup])

  // enriched 数据 — 传入 ext_columns 参数
  const enriched = useQuery({
    queryKey: QK.watchlistEnriched(extColumnsParam),
    queryFn: () => api.watchlistEnriched(extColumnsParam || undefined),
    enabled: (list.data?.symbols.length ?? 0) > 0,
  })

  const symbols = enriched.data?.rows?.map((r: any) => r.symbol) ?? []
  const symbolsKey = symbols.join(',')

  // 指数无本地分钟K数据, 分时批量请求剔除指数 symbol (省请求, 避免逐只 404)
  const minuteSymbols = useMemo(
    () => symbols.filter((s: string) => (enriched.data?.rows ?? []).find((r: any) => r.symbol === s)?.asset_type !== 'index'),
    [symbols, enriched.data],
  )
  const minuteSymbolsKey = minuteSymbols.join(',')

  // 实时行情状态 (提前到此处: 分时轮询判断需要 realtimeRunning)
  const quoteStatus = useQuoteStatus()
  const realtimeRunning = quoteStatus.data?.running ?? false

  // 批量日k数据 (天数由列配置决定; 分组卡片视图不展示蜡烛, 挂起请求)
  const klineBatch = useQuery({
    queryKey: QK.watchlistKlineBatch(`${symbolsKey}|${candleDays}`),
    queryFn: () => api.klineDailyBatch(symbols, candleDays),
    enabled: dailyKVisible && symbols.length > 0 && !groupCardsOpen,
    staleTime: 5 * 60_000,  // 5 分钟内不重请求
  })

  // 当日蜡烛实时修补: 历史 K 线按 staleTime 周期拉取 (见 queryKeys 注释), 最后一根
  // 蜡烛用每 tick 刷新的 enriched 当日 OHLC 前端覆盖/追加, 蜡烛随实时行情跳动, 零额外请求。
  const klineData = useMemo(() => {
    const base = dailyKVisible ? (klineBatch.data?.data ?? {}) : {}
    const liveRows = enriched.data?.rows
    const asOf = enriched.data?.as_of
    if (!dailyKVisible || !liveRows?.length || !asOf) return base
    const liveBySymbol = new Map<string, any>(liveRows.map((r: any) => [r.symbol, r]))
    const patched: Record<string, KlineRow[]> = {}
    for (const sym of Object.keys(base)) {
      const arr = base[sym]
      if (!Array.isArray(arr) || arr.length === 0) { patched[sym] = arr; continue }
      const live = liveBySymbol.get(sym)
      const { open, high, low, close } = live ?? {}
      if (open == null || high == null || low == null || close == null) { patched[sym] = arr; continue }
      const last = arr[arr.length - 1]
      if (last.date === asOf) {
        patched[sym] = [...arr.slice(0, -1), { ...last, open, high, low, close }]
      } else if (last.date < asOf) {
        patched[sym] = [...arr, { date: asOf, open, high, low, close }]
      } else {
        patched[sym] = arr
      }
    }
    return patched
  }, [dailyKVisible, klineBatch.data, enriched.data])

  // 批量分时数据 (有分钟K批量能力时, 列可见才拉)
  // 刷新策略: 仅当实时行情运行 且 用户在实时监控设置里开启 minute_intraday_refresh 时
  // 按用户设定的间隔轮询 (不接 SSE 高频, 避免每秒拉 TickFlow 触限流); 与 Screener / 设置卡片描述一致。
  const { data: prefsData } = usePreferences()
  const intradayRefreshEnabled = prefsData?.minute_intraday_refresh ?? false
  const intradayRefreshInterval = prefsData?.minute_intraday_refresh_interval ?? 6
  // 视口感知: 虚拟器渲染中的 symbol 集合 (可见 + overscan 缓冲), 由下方虚拟器
  // 区域的 effect 写入; null = 未就绪/非虚拟化视图, 沿用全列表。首轮全量播种
  // 缓存 (queryFn 在挂载时先于 ref 填充执行), 之后各轮只拉视口集合。
  const minuteRequestSymbolsRef = useRef<string[] | null>(null)
  const minuteBatch = useQuery({
    queryKey: QK.minuteBatch(minuteSymbolsKey),
    // 增量轮询 (prefer_local): 读缓存以最后一根为 since 只拉新增, 本地合并为
    // 完整序列; 全量分钟健康时服务端零补拉, 不健康时也只增量。
    // 请求集合 = 视口内 symbol (缓存按 symbol upsert, 视口外保留旧序列)
    queryFn: () => {
      const reqSymbols = minuteRequestSymbolsRef.current ?? minuteSymbols
      return fetchMinuteBatchIncremental(qc, QK.minuteBatch(minuteSymbolsKey), reqSymbols, true)
    },
    enabled: intradayVisible && minuteSymbols.length > 0 && !groupCardsOpen,
    staleTime: 10_000,
    // SSE tick 新鲜 (enriched 10s 内被行情推送刷新过) → 分时图已由下方续画本地
    // 跳动, 轮询降为 30s 兜底校准; tick 断流时回到用户设定间隔
    refetchInterval: () => {
      if (!(intradayRefreshEnabled && realtimeRunning)) return false
      const tickFresh = Date.now() - enriched.dataUpdatedAt < 10_000
      return tickFresh ? 30_000 : intradayRefreshInterval * 1000
    },
  })

  // 分时图 SSE 续画: enriched 行情列每 tick 刷新 (SSE 触发), 前端本地续写分钟序列
  // 的最后一根 / 分钟滚动追加新K — 轮询间隔内分时图也随实时价跳动, 零额外请求。
  // 纯视图叠加不写回缓存: 轮询拉回的服务端定版K始终是权威值, 覆盖续画值。
  const minuteData = useMemo(() => {
    const base: Record<string, MinuteKlineRow[]> = intradayVisible ? (minuteBatch.data?.data ?? {}) : {}
    const liveRows = enriched.data?.rows
    if (!intradayVisible || !liveRows?.length) return base
    const liveBySymbol = new Map<string, any>(liveRows.map((r: any) => [r.symbol, r]))
    // 仅连续竞价时段续画 (9:31-11:30, 13:01-15:00); 收盘后 rt_price 即收盘价无需续写
    const now = new Date()
    const hh = now.getHours(), mm = now.getMinutes()
    const inSession =
      (hh === 9 && mm >= 31) || hh === 10 ||
      (hh === 11 && mm <= 30) || (hh === 13 && mm >= 1) || hh === 14
    if (!inSession) return base
    // 与服务端同构的 naive 北京时间戳 (手工拼本地时间, 不能用 toISOString — 那是 UTC)
    const pad = (n: number) => String(n).padStart(2, '0')
    const barTs = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(hh)}:${pad(mm)}:00`
    const patched: Record<string, MinuteKlineRow[]> = {}
    for (const sym of Object.keys(base)) {
      const arr = base[sym]
      if (!Array.isArray(arr) || arr.length === 0) { patched[sym] = arr; continue }
      const live = liveBySymbol.get(sym)
      const price = live?.rt_price ?? live?.close
      if (typeof price !== 'number' || !Number.isFinite(price)) { patched[sym] = arr; continue }
      const last = arr[arr.length - 1]
      if (last.datetime === barTs) {
        patched[sym] = [...arr.slice(0, -1), {
          ...last,
          close: price,
          high: Math.max(last.high, price),
          low: Math.min(last.low, price),
        }]
      } else if (barTs > last.datetime) {
        patched[sym] = [...arr, {
          datetime: barTs, open: price, high: price, low: price, close: price,
          volume: 0, amount: 0,   // 量/额由下一轮轮询定版覆盖
        }]
      } else {
        patched[sym] = arr   // 轮询数据已新于本地时钟 (钟差兜底), 不动
      }
    }
    return patched
  }, [intradayVisible, minuteBatch.data, enriched.data])

  const addMutation = useMutation({
    mutationFn: ({ symbol, groupId }: { symbol: string; groupId: string | null }) =>
      api.watchlistAdd(symbol, '', groupId),
    onSuccess: (data) => {
      qc.setQueryData(QK.watchlist, data)
      qc.invalidateQueries({ queryKey: QK.watchlist })
      qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })
      qc.invalidateQueries({ queryKey: ['kline-batch'] })
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
      qc.invalidateQueries({ queryKey: ['kline-batch'] })
    },
  })

  const moveToTop = useMutation({
    mutationFn: (sym: string) => api.watchlistMoveToTop(sym),
    onSuccess: (data) => {
      qc.setQueryData(QK.watchlist, data)
      qc.invalidateQueries({ queryKey: QK.watchlist })
      qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })
      qc.invalidateQueries({ queryKey: ['kline-batch'] })
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
      qc.invalidateQueries({ queryKey: ['kline-batch'] })
    },
  })

  const createGroup = useMutation({
    mutationFn: ({ name, color }: { name: string; color: WatchlistGroupColor }) =>
      api.watchlistGroupCreate(name, color),
    onSuccess: data => {
      qc.setQueryData(QK.watchlistGroups, { groups: data.groups })
      setSelectedGroup(data.group.id)
    },
  })

  const renameGroup = useMutation({
    mutationFn: ({ groupId, name, color }: { groupId: string; name: string; color: WatchlistGroupColor }) =>
      api.watchlistGroupRename(groupId, name, color),
    onSuccess: data => qc.setQueryData(QK.watchlistGroups, data),
  })

  const reorderGroup = useMutation({
    mutationFn: (orderedIds: string[]) => api.watchlistGroupReorder(orderedIds),
    onSuccess: data => qc.setQueryData(QK.watchlistGroups, data),
  })

  const deleteGroup = useMutation({
    mutationFn: (groupId: string) => api.watchlistGroupDelete(groupId),
    onSuccess: (data, groupId) => {
      qc.setQueryData(QK.watchlistGroups, { groups: data.groups })
      qc.setQueryData(QK.watchlist, { symbols: data.symbols })
      if (selectedGroup === groupId) setSelectedGroup('all')
    },
  })

  const clearGroup = useMutation({
    mutationFn: (groupId: string) => api.watchlistGroupClear(groupId),
    onSuccess: data => qc.setQueryData(QK.watchlist, data),
  })

  // 多组并存: 勾选加入 / 取消移出 (仅影响该分组, 标的保留在自选中)
  const addGroupMember = useMutation({
    mutationFn: ({ symbol, groupId }: { symbol: string; groupId: string }) =>
      api.watchlistGroupAddMember(groupId, symbol),
    onSuccess: data => qc.setQueryData(QK.watchlist, data),
  })
  const removeGroupMember = useMutation({
    mutationFn: ({ symbol, groupId }: { symbol: string; groupId: string }) =>
      api.watchlistGroupRemoveMember(groupId, symbol),
    onSuccess: data => qc.setQueryData(QK.watchlist, data),
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
  const handleToggleMember = useCallback((symbol: string, groupId: string, member: boolean) => {
    if (member) addGroupMember.mutate({ symbol, groupId })
    else removeGroupMember.mutate({ symbol, groupId })
  }, [addGroupMember, removeGroupMember])
  // 分组卡片总览下点击分组 tab / 卡片头 = 钻取该分组: 关闭总览并选中分组,
  // 个股视图设置(table/card)保持用户原选择
  const handleGroupSelect = useCallback((group: WatchlistGroupFilter) => {
    setSelectedGroup(group)
    setGroupCardsOpen(false)
  }, [])

  const listEntries = list.data?.symbols ?? []
  const allSymbols = listEntries.map(s => s.symbol)
  const rows = enriched.data?.rows ?? []
  const groupBySymbol = useMemo(
    () => new Map(listEntries.map(entry => [entry.symbol, entry.group_ids ?? []])),
    [listEntries],
  )
  // 分组等权平均涨跌幅 (实时优先 rt_pct, 收盘兜底 change_pct; 与表格同源)
  const groupPcts = useMemo(
    () => computeGroupPcts(
      listEntries,
      new Map(rows.map((r: any) => [r.symbol as string, r])),
    ),
    [listEntries, rows],
  )
  // 分组「指标 + 排序 + 卡片显示项」配置: 分组统计条与分组卡片共享同一份持久化设置
  const [groupStatsConfig, setGroupStatsConfig] = useState(loadGroupStatsConfig)
  const updateGroupStatsConfig = useCallback((patch: GroupStatsConfigPatch) => {
    setGroupStatsConfig(prev => {
      const next = { ...prev, ...patch }
      storage.watchlistGroupStats.set(next)
      return next
    })
  }, [])
  const groupCounts = useMemo(() => {
    // 多组并存: 一股计入每个所属分组的计数; 不属于任何分组才计未分组
    const counts: Record<string, number> = { ungrouped: 0 }
    for (const entry of listEntries) {
      const gids = entry.group_ids ?? []
      if (gids.length === 0) counts.ungrouped += 1
      else for (const gid of gids) counts[gid] = (counts[gid] ?? 0) + 1
    }
    return counts
  }, [listEntries])
  const rowsInSelectedGroup = useMemo(() => {
    const rowsWithGroup = rows.map(row => ({ ...row, group_ids: groupBySymbol.get(row.symbol) ?? [] }))
    if (selectedGroup === 'all') return rowsWithGroup
    if (selectedGroup === 'ungrouped') return rowsWithGroup.filter(row => row.group_ids.length === 0)
    return rowsWithGroup.filter(row => row.group_ids.includes(selectedGroup))
  }, [groupBySymbol, rows, selectedGroup])
  const watchlistContentLoading = list.isLoading || (allSymbols.length > 0 && enriched.isLoading)

  // 实时监控圆点: 仅 Free/低档 "按自选股实时监控" 模式 (mode === 'watchlist') 下显示;
  // 全市场模式 (mode === 'full_market') 全部标的都在监控, 标圆点无意义, 故不显示。
  // 后端自选实时模式实际只监控自选页前 N 个 (N = watchlist_symbol_count), 顺序与 allSymbols 一致。
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
  // 兼容: 旧存储不含 ETF 键 → 加载时补上，保持 ETF 行默认可见
  const [boardFilter, setBoardFilter] = useState<Set<string>>(() => {
    const saved = storage.watchlistBoardFilter.get([])
    return saved.length > 0 ? new Set([...saved, ETF_BOARD]) : new Set(BOARD_OPTIONS) // 默认全选
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

  // 排除 ST (含 *ST/S*ST 等变体, 按简称含 "ST" 判定), 默认关闭并持久化
  const [excludeST, setExcludeST] = useState(() => storage.watchlistExcludeST.get(false))
  const toggleExcludeST = useCallback(() => {
    setExcludeST(prev => {
      storage.watchlistExcludeST.set(!prev)
      return !prev
    })
  }, [])

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
    persistBoardFilter(new Set(BOARD_OPTIONS))
    setExcludeST(false)
    storage.watchlistExcludeST.set(false)
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
    // 板块筛选（全选时跳过）
    let result = rowsInSelectedGroup
    if (boardFilter.size > 0 && boardFilter.size < BOARD_OPTIONS.length) {
      result = result.filter(r => {
        if (r.asset_type === 'etf') return boardFilter.has(ETF_BOARD)
        // 其他非股票 (指数等) 无板块语义, 不受板块筛选影响
        if (r.asset_type && r.asset_type !== 'stock') return true
        const board = getBoardType(r.symbol)
        return board != null && boardFilter.has(board)
      })
    }
    // 排除 ST: 按简称判定 (ST/*ST/S*ST 均含 "ST"); 非股票名称不含该标记, 天然不受影响
    if (excludeST) {
      result = result.filter(r => !((r.rt_name ?? r.name ?? '').toUpperCase().includes('ST')))
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
  }, [rowsInSelectedGroup, filters, columns, boardFilter, excludeST])

  const activeFilterCount = Object.values(filters).filter(v => v.min || v.max || v.text).length
  const hasBoardFilter = boardFilter.size > 0 && boardFilter.size < BOARD_OPTIONS.length
  const hasActiveFilters = activeFilterCount > 0 || hasBoardFilter || excludeST

  // 排序（复用共享三态排序 hook）。分时列按「最新分钟收盘 vs 昨收」排序（分时图最后一点同口径），
  // 其余列走共享取值；眼睛关闭时不拉分钟数据，取值为 null → 保持原序。
  const getWatchlistSortValue = useCallback((r: any, col: ColumnConfig) => {
    if (col.source.type === 'builtin' && col.source.key === 'intraday') {
      return getIntradaySortValue(r, minuteData[r.symbol])
    }
    return getSortValue(r, col)
  }, [minuteData])
  const { sort, toggle: handleSortToggle, sortRows } = useTableSort(getWatchlistSortValue)

  const sortedRows = useMemo(
    () => sortRows(filteredRows, columns),
    [filteredRows, sortRows, columns],
  )

  // 切股导航列表: 按列表当前展示顺序 (与 sortedRows 一致, 排序/筛选后行序随之变化)。
  // 弹窗未打开时跳过构建 — sortedRows 随行情 tick 重建, 避免无谓分配。
  const previewNavItems = useMemo(
    () => previewSymbol ? toNavItems(sortedRows) : [],
    [previewSymbol, sortedRows],
  )

  const cardColumns = useCardColumnCount()
  const cardGridRef = useRef<HTMLDivElement>(null)
  const virtualizeCards = viewMode === 'card' && !groupCardsOpen && sortedRows.length > VIRTUAL_LIST_THRESHOLD
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

  // 视口感知 (数据层): 从虚拟器派生"正在渲染的 symbol" (可见 + overscan 缓冲,
  // 滚动前已就绪)。非虚拟化视图 (小列表/表格/分组) 为 null → 沿用全列表。
  // 每次渲染直读 getVirtualItems (滚动不换 deps, 不能 useMemo), 副作用集中在 effect。
  const visibleCardSymbols = (() => {
    if (!virtualizeCards) return null
    // 与 minuteSymbols 同口径: 剔除指数 (minute-batch 契约只收股票/ETF)
    const scope = new Set(minuteSymbols as string[])
    const items = cardRowVirtualizer.getVirtualItems()
    const out: string[] = []
    for (const item of items) {
      for (let i = item.index * cardColumns; i < (item.index + 1) * cardColumns && i < sortedRows.length; i++) {
        const s = (sortedRows[i] as any)?.symbol
        if (typeof s === 'string' && scope.has(s)) out.push(s)
      }
    }
    return out.length ? out : null
  })()
  const minuteVisibleKey = visibleCardSymbols?.join(',') ?? ''
  const lastVisibleKeyRef = useRef<string | null>(null)
  useEffect(() => {
    minuteRequestSymbolsRef.current = visibleCardSymbols
    if (!minuteVisibleKey) return
    if (lastVisibleKeyRef.current === minuteVisibleKey) return
    const prevKey = lastVisibleKeyRef.current
    lastVisibleKeyRef.current = minuteVisibleKey
    if (prevKey === null) return   // 首次: 挂载播种轮已按全列表发出, 不额外触发
    // 视口集合变化 (滚动到新区段) → 防抖 300ms 补拉一次, 新滚入的 symbol 即时就绪
    const t = setTimeout(() => {
      qc.invalidateQueries({ queryKey: QK.minuteBatch(minuteSymbolsKey) })
    }, 300)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [minuteVisibleKey, minuteSymbolsKey])

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
  // 分组切换不计入筛选隐藏，只比较当前分组内的数据。
  const hiddenCount = Math.max(0, rowsInSelectedGroup.length - sortedRows.length)

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
      active={previewSymbol === r.symbol}
      groups={groups}
      onToggleMember={handleToggleMember}
      groupChangePending={addGroupMember.isPending || removeGroupMember.isPending}
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
              <span className="font-mono text-muted tabular-nums">{rowsInSelectedGroup.length}</span>
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
              existingBySymbol={groupBySymbol}
              groups={groups}
              onAdd={(symbol, groupId) => addMutation.mutate({ symbol, groupId })}
              onToggleMember={handleToggleMember}
              preferredGroupId={activeGroupId}
              addPending={addMutation.isPending}
              memberPending={addGroupMember.isPending || removeGroupMember.isPending}
            />
            <button
              onClick={() => setImportOpen(true)}
              className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150 ease-smooth"
              title="批量导入自选（截图 / CSV / 粘贴代码）"
              aria-label="批量导入自选"
            >
              <FileUp className="h-4 w-4" />
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
            {/* 分组卡片视图 */}
            <button
              onClick={toggleGroupView}
              aria-pressed={groupCardsOpen}
              className={`inline-flex items-center justify-center h-8 w-8 rounded-btn transition-colors duration-150 ease-smooth ${
                groupCardsOpen
                  ? 'bg-accent/15 text-accent hover:bg-accent/25'
                  : 'bg-elevated text-secondary hover:bg-elevated/80 hover:text-foreground'
              }`}
              title={groupCardsOpen ? '退出分组卡片' : '分组卡片视图'}
              aria-label={groupCardsOpen ? '退出分组卡片' : '分组卡片视图'}
            >
              <Rows3 className="h-4 w-4" />
            </button>
            {/* 分组统计条 */}
            <button
              onClick={toggleGroupStats}
              aria-pressed={groupStatsOpen}
              className={`inline-flex items-center justify-center h-8 w-8 rounded-btn transition-colors duration-150 ease-smooth ${
                groupStatsOpen
                  ? 'bg-accent/15 text-accent hover:bg-accent/25'
                  : 'bg-elevated text-secondary hover:bg-elevated/80 hover:text-foreground'
              }`}
              title={groupStatsOpen ? '收起分组统计' : '分组统计'}
              aria-label={groupStatsOpen ? '收起分组统计' : '分组统计'}
            >
              <BarChart3 className="h-4 w-4" />
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
            {/* 扩展插槽: 自选页工具栏二开区 (无注册时不渲染) */}
            <ExtensionSlot
              name="watchlist.toolbar"
              context={{
                symbols: sortedRows.map((row: any) => row.symbol),
                viewMode,
                selectedGroup,
                refresh: () => enriched.refetch(),
              }}
            />
          </div>
        }
      />

      {groupStatsOpen && (
        <WatchlistGroupStatsBar
          groups={groups}
          counts={groupCounts}
          pcts={groupPcts}
          selected={selectedGroup}
          onSelect={handleGroupSelect}
          config={groupStatsConfig}
          onConfigChange={updateGroupStatsConfig}
        />
      )}

      <WatchlistGroupBar
        groups={groups}
        counts={groupCounts}
        selected={selectedGroup}
        total={allSymbols.length}
        pcts={groupPcts}
        onSelect={handleGroupSelect}
        onCreate={(name, color) => createGroup.mutateAsync({ name, color }).then(() => undefined)}
        onRename={(groupId, name, color) => renameGroup.mutateAsync({ groupId, name, color }).then(() => undefined)}
        onDelete={groupId => deleteGroup.mutateAsync(groupId).then(() => undefined)}
        onClearGroup={groupId => clearGroup.mutateAsync(groupId).then(() => undefined)}
        onReorder={orderedIds => reorderGroup.mutateAsync(orderedIds).then(() => undefined)}
      />

      {/* 筛选栏 */}
      {filterOpen && (
        <div className="px-5 py-2 border-b border-border bg-surface/50 max-h-[184px] overflow-y-auto">
          {/* 板块筛选 */}
          <div className="mb-2">
            <div className="text-[10px] text-muted uppercase tracking-wider mb-0.5">板块</div>
            <div className="flex flex-wrap gap-1">
              {BOARD_OPTIONS.map(board => {
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
          {/* 排除 ST */}
          <div className="mb-2">
            <div className="text-[10px] text-muted uppercase tracking-wider mb-0.5">风险警示</div>
            <div className="flex flex-wrap gap-1">
              <button
                onClick={toggleExcludeST}
                className={`px-2 py-0.5 rounded text-[11px] transition-colors ${
                  excludeST
                    ? 'bg-accent/15 text-accent'
                    : 'bg-elevated text-secondary hover:text-foreground hover:bg-elevated/80'
                }`}
                title="勾选后隐藏简称含 ST 标记的标的 (ST/*ST/S*ST)"
              >
                排除ST
              </button>
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
          {watchlistContentLoading ? (
            <div className="text-sm text-muted">加载中…</div>
          ) : list.isError ? (
            <div className="text-sm text-danger">读取自选失败</div>
          ) : enriched.isError ? (
            <div className="text-sm text-danger">读取自选行情失败</div>
          ) : allSymbols.length === 0 ? (
            <EmptyState
              icon={Star}
              title="自选股为空"
              hint="点击右上角搜索添加标的，或用导入按钮从券商自选 CSV / 截图批量导入、粘贴代码。"
            />
          ) : rowsInSelectedGroup.length === 0 ? (
            <EmptyState
              icon={FolderOpen}
              title="该分组暂无标的"
              hint="使用右上角搜索添加、通过股票旁的分组按钮移入，或用导入弹窗把整批标的并入本组。"
            />
          ) : groupCardsOpen ? (
            <WatchlistGroupCards
              groups={groups}
              rows={rows}
              groupBySymbol={groupBySymbol}
              pcts={groupPcts}
              onPreview={handleCardPreview}
              onOpenGroup={handleGroupSelect}
              config={groupStatsConfig}
              onConfigChange={updateGroupStatsConfig}
            />
          ) : viewMode === 'table' ? (
            <StockDataTable
              columns={visibleColumns}
              rows={sortedRows}
              headerSticky
              sort={sort}
              onSortToggle={handleSortToggle}
              extraSortableKeys={INTRADAY_SORTABLE_KEYS}
              rowKey={(r: any) => r.symbol}
              rowClassName={(r: any) => cn('border-t border-border transition-colors duration-150 ease-smooth hover:bg-elevated/50', r.symbol === previewSymbol && 'bg-accent/10 hover:bg-accent/15')}
              // 日k列表头：标签 + 显示/隐藏眼睛按钮
              renderHeaderContent={(col) => {
                if (col.source.type === 'builtin' && col.source.key === 'candle') {
                  return (
                    <span className="inline-flex items-center justify-center gap-1.5">
                      <span className="shrink-0 whitespace-nowrap">{col.label}</span>
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
                      <span className="shrink-0 whitespace-nowrap">{col.label}</span>
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
                        {/* 删除入口：从分组移除 + 从自选移除(二次确认) + 移到顶部 */}
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
                              <WatchlistGroupPicker
                                groups={groups}
                                groupIds={r.group_ids ?? []}
                                symbol={r.symbol}
                                disabled={addGroupMember.isPending || removeGroupMember.isPending}
                                onToggleMember={handleToggleMember}
                              />
                              {selectedGroup !== 'all' && selectedGroup !== 'ungrouped' && r.group_ids?.includes(selectedGroup) && (
                                <button
                                  onClick={() => handleToggleMember(r.symbol, selectedGroup, false)}
                                  disabled={addGroupMember.isPending || removeGroupMember.isPending}
                                  className="p-0.5 text-muted hover:text-warning transition-colors duration-150 ease-smooth disabled:opacity-50"
                                  aria-label="移出当前分组"
                                  title="移出当前分组（仍保留在自选中）"
                                >
                                  <FolderMinus className="h-3.5 w-3.5" />
                                </button>
                              )}
                              <button
                                onClick={() => setConfirmRemove(r.symbol)}
                                className="p-0.5 text-muted hover:text-danger transition-colors duration-150 ease-smooth"
                                aria-label="移除"
                                title="从自选移除"
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
                  // 指数无本地分钟K数据, 分时列降级为占位符
                  if (r.asset_type === 'index') {
                    const iw = intradayChartVisible ? intradayResolved.width : 40
                    const ih = intradayChartVisible ? intradayResolved.height : 40
                    return (
                      <td className="pl-3 pr-2 py-1.5 border-l border-border/30" style={{ width: iw + 4, minWidth: iw + 4, maxWidth: iw + 4, height: ih }}>
                        <div className="flex items-center justify-center">
                          <span className="text-[10px] text-muted">—</span>
                        </div>
                      </td>
                    )
                  }
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
        navList={previewNavList.length > 0 ? previewNavList : previewNavItems}
        onNavigate={(sym, n) => { setPreviewSymbol(sym); setPreviewName(n ?? '') }}
      />

      <DimensionMembersDialog
        target={dimensionTarget}
        onClose={() => setDimensionTarget(null)}
        onStockClick={(symbol, name, navList) => {
          setDimensionTarget(null)
          setPreviewSymbol(symbol)
          setPreviewName(name ?? '')
          // 成分列表作为切股导航 (成员可能不在自选列表, 不能退回 previewNavItems)
          setPreviewNavList(navList ?? previewNavItems)
        }}
      />

      <WatchlistImportDialog
        open={importOpen}
        onClose={() => setImportOpen(false)}
        groupId={activeGroupId}
        groups={groups}
        existingBySymbol={groupBySymbol}
      />
    </div>
  )
}
