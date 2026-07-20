/**
 * 策略结果表格。
 *
 * 表格骨架（表头排序/sticky/遍历）由共享的 StockDataTable 承担；本组件只负责
 * 策略页特有的单元格内容：symbol 列（含加自选按钮 + 失效行灰显）、strategies、
 * score、signals、candle、ext 列。其余纯数据列（价格/指标/财务…）交给共享原语。
 */
import { useState, type CSSProperties, type ReactNode } from 'react'
import { Check, Plus, Eye, EyeOff, RefreshCw } from 'lucide-react'
import type { KlineRow, MinuteKlineRow } from '@/lib/api'
import { fmtPrice } from '@/lib/format'
import type { ColumnConfig } from '@/lib/screener-columns'
import { getSignals, signalCls } from '@/lib/stock-table'
import { boardTag, renderBuiltinDataCell } from '@/components/stock-table/primitives'
import { resolveCandleConfig, resolveIntradayConfig } from '@/lib/list-columns'
import { MiniCandlestick } from '@/components/stock-table/MiniCandlestick'
import { MiniIntraday } from '@/components/stock-table/MiniIntraday'
import { StockDataTable, type SortState } from '@/components/stock-table/StockDataTable'
import {
  DimensionMembersDialog,
  dimensionKindForSourceField,
  type DimensionMembersTarget,
} from '@/components/DimensionMembersDialog'

interface ScreenerTableProps {
  rows: any[]
  columns: ColumnConfig[]
  strategyIdToName: Record<string, string>
  symbolStrategyMap: Map<string, string[]>
  activeStrategy: string | null
  watchlistSet: Set<string>
  onPreview: (symbol: string, name: string) => void
  onToggleWatchlist: (symbol: string, inList: boolean) => void
  watchlistPending: boolean
  /** symbol → 日k 数据，仅当启用日k列时传入 */
  klineData?: Record<string, KlineRow[]>
  /** 日k蜡烛图是否显示（表头眼睛开关） */
  dailyKChartVisible?: boolean
  onToggleDailyKChart?: () => void
  /** symbol → 分时数据，仅当启用分时列时传入 */
  minuteData?: Record<string, MinuteKlineRow[]>
  /** 分时图是否显示（表头眼睛开关） */
  intradayChartVisible?: boolean
  onToggleIntradayChart?: () => void
  /** 分时是否正在自动轮询 (true 时隐藏手动刷新按钮, 避免重复请求) */
  intradayAutoRefresh?: boolean
  /** 手动刷新分时数据 */
  onRefreshIntraday?: () => void
  /** 分时数据正在刷新中 (按钮 loading 态) */
  intradayRefreshing?: boolean
  /** 表头排序（受控，由 Screener.tsx 传入） */
  sort?: SortState | null
  onSortToggle?: (colId: string) => void
}

/** 渲染标签数组（含 maxTags 折叠/展开、横竖排列）。策略列与 ext 列共用。 */
function renderTagList(
  tags: string[],
  col: ColumnConfig,
  expanded: boolean,
  onToggle: () => void,
  tagClassName: string,
  onTagClick?: (tag: string) => void,
): ReactNode {
  if (tags.length === 0) return <span className="text-muted">—</span>

  const cfg = col.extDisplay
  const maxTags = cfg?.maxTags ?? 0
  const showAll = maxTags <= 0 || expanded || tags.length <= maxTags
  const sliced = showAll ? tags : tags.slice(0, maxTags)
  const hiddenIndices = maxTags > 0 ? cfg?.hiddenIndices : undefined
  const visibleTags = hiddenIndices?.length
    ? sliced.filter((_, i) => !hiddenIndices.includes(i))
    : sliced
  const hiddenCount = tags.length - visibleTags.length
  const isVertical = cfg?.tagLayout === 'vertical' && !expanded

  return (
    <div className={isVertical ? 'flex flex-col items-start gap-0.5' : 'flex flex-wrap gap-0.5'}>
      {visibleTags.map((tag, i) => onTagClick ? (
        <button
          key={i}
          type="button"
          onClick={event => { event.stopPropagation(); onTagClick(tag) }}
          className={`${tagClassName} hover:brightness-95`}
        >
          {tag}
        </button>
      ) : (
        <span key={i} className={tagClassName}>{tag}</span>
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
    </div>
  )
}

const EXT_TAG_CLS = 'inline-block px-1.5 py-px rounded text-[10px] font-medium leading-tight text-yellow-500 bg-yellow-500/10'
const STRATEGY_TAG_CLS = 'inline-block px-1.5 py-px rounded text-[10px] font-medium leading-tight bg-amber-500/10 text-amber-600 border border-amber-500/20'

function renderExtValue(
  val: any,
  col: ColumnConfig,
  expanded: boolean,
  onToggle: () => void,
  onTagClick?: (tag: string) => void,
): ReactNode {
  if (val == null || Number.isNaN(val)) return <span className="text-muted">—</span>
  if (typeof val === 'number') {
    const displayVal = Number.isInteger(val) ? fmtPrice(val, 0) : fmtPrice(val)
    return <span className="tabular-nums">{displayVal}</span>
  }
  if (typeof val === 'boolean') {
    return <span className={val ? 'text-bull' : 'text-muted'}>{val ? '是' : '否'}</span>
  }

  const cfg = col.extDisplay
  const str = String(val)
  if (cfg?.displayMode === 'text') return <span className="text-foreground">{str}</span>

  const separator = cfg?.separator?.trim() || null
  const tags = separator
    ? str.split(separator).map(s => s.trim()).filter(Boolean)
    : str.split(/[、,，;；\-]/).map(s => s.trim()).filter(Boolean)

  return renderTagList(tags, col, expanded, onToggle, EXT_TAG_CLS, onTagClick)
}

export function ScreenerTable({
  rows, columns, strategyIdToName, symbolStrategyMap, activeStrategy,
  watchlistSet, onPreview, onToggleWatchlist, watchlistPending, klineData = {},
  dailyKChartVisible = true, onToggleDailyKChart,
  minuteData = {}, intradayChartVisible = true, onToggleIntradayChart,
  intradayAutoRefresh = false, onRefreshIntraday, intradayRefreshing = false,
  sort, onSortToggle,
}: ScreenerTableProps) {
  const [expandedCells, setExpandedCells] = useState<Set<string>>(new Set())
  const [dimensionTarget, setDimensionTarget] = useState<DimensionMembersTarget | null>(null)

  // 日k列渲染尺寸（按眼睛开关取开启/收起尺寸）
  const candleCol = columns.find(c => c.source.type === 'builtin' && c.source.key === 'candle' && c.visible)
  const candleResolved = resolveCandleConfig(candleCol?.candleConfig)
  const candleSize = dailyKChartVisible
    ? { width: candleResolved.enabledWidth, height: candleResolved.enabledHeight }
    : { width: candleResolved.disabledWidth, height: candleResolved.disabledHeight }

  // 分时列渲染尺寸（开启用配置宽高，收起用 40×40 占位，与自选页一致）
  const intradayCol = columns.find(c => c.source.type === 'builtin' && c.source.key === 'intraday' && c.visible)
  const intradayResolved = resolveIntradayConfig(intradayCol?.intradayConfig)
  const intradaySize = intradayChartVisible
    ? { width: intradayResolved.width, height: intradayResolved.height }
    : { width: 40, height: 40 }

  const toggleExpand = (key: string) => {
    setExpandedCells(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const renderCell = (r: any, col: ColumnConfig): ReactNode => {
    // ext 列
    if (col.source.type === 'ext') {
      const { configId, fieldName } = col.source
      const val = r[`${configId}__${fieldName}`]
      const cellKey = `${r.symbol}::${col.id}`
      const expanded = expandedCells.has(cellKey)
      const sourceField = `${configId}.${fieldName}`
      const dimensionKind = dimensionKindForSourceField(sourceField)
      const tdClass = val == null || Number.isNaN(val)
        ? 'px-3 py-2 text-center text-muted'
        : typeof val === 'number'
          ? 'px-3 py-2 text-right num tabular-nums'
          : 'px-3 py-2 text-center'
      const style: CSSProperties = {}
      if (col.extDisplay?.maxWidth) style.maxWidth = col.extDisplay.maxWidth
      return (
        <td key={col.id} className={tdClass} style={style}>
          {renderExtValue(
            val,
            col,
            expanded,
            () => toggleExpand(cellKey),
            dimensionKind ? value => setDimensionTarget({ kind: dimensionKind, value, sourceField }) : undefined,
          )}
        </td>
      )
    }

    const isExpired = !!r._expired
    const key = col.source.key

    // 策略页特有 / 需上下文的列
    switch (key) {
      case 'symbol': {
        const board = boardTag(r.symbol)
        const inWatchlist = watchlistSet.has(r.symbol)
        return (
          <td key={col.id} className="px-4 py-2">
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => onPreview(r.symbol, r.name ?? '')}
                className={`flex items-center gap-2 text-left ${isExpired ? 'cursor-default' : ''}`}
              >
                {board ? (
                  <span className={`shrink-0 inline-flex items-center justify-center w-[18px] h-[18px] rounded text-[9px] font-bold leading-none border ${board.color}`}>
                    {board.label}
                  </span>
                ) : (
                  <span className="shrink-0 w-[18px]" />
                )}
                <span className="font-mono text-secondary group-hover:text-accent transition-colors duration-150 leading-snug">
                  {r.symbol}
                </span>
                {r.name && (
                  <span className="text-[11px] text-muted truncate group-hover:text-secondary transition-colors duration-150 leading-snug">
                    {r.name}
                  </span>
                )}
              </button>
              {isExpired ? (
                <span className="shrink-0 inline-flex items-center px-1.5 py-px rounded text-[9px] font-medium leading-tight bg-red-500/10 text-red-400/60 border border-red-500/15">
                  失效
                </span>
              ) : (
                <button
                  type="button"
                  onClick={() => onToggleWatchlist(r.symbol, inWatchlist)}
                  disabled={watchlistPending}
                  className={`shrink-0 inline-flex items-center justify-center w-5 h-5 rounded-full border transition-colors cursor-pointer
                    disabled:opacity-50
                    ${inWatchlist
                      ? 'border-accent/40 bg-accent/10 text-accent'
                      : 'border-border text-muted hover:border-accent/40 hover:text-accent'
                    }`}
                  title={inWatchlist ? '移出自选' : '加入自选'}
                >
                  {inWatchlist ? <Check className="h-3 w-3" /> : <Plus className="h-3 w-3" />}
                </button>
              )}
            </div>
          </td>
        )
      }
      case 'strategies': {
        const strats = symbolStrategyMap.get(r.symbol) ?? (activeStrategy ? [activeStrategy] : [])
        const tags = strats.map(sid => strategyIdToName[sid] ?? sid)
        const cellKey = `${r.symbol}::${col.id}`
        const expanded = expandedCells.has(cellKey)
        return (
          <td key={col.id} className="px-3 py-2">
            {renderTagList(tags, col, expanded, () => toggleExpand(cellKey), STRATEGY_TAG_CLS)}
          </td>
        )
      }
      case 'score': {
        const numCls = 'px-3 py-2 text-right num tabular-nums'
        return (
          <td key={col.id} className={numCls}>
            {r.score != null ? (
              <span className={r.score >= 70 ? 'text-accent font-medium' : r.score >= 50 ? 'text-amber-400' : 'text-secondary'}>
                {Number(r.score).toFixed(1)}
              </span>
            ) : (
              <span className="text-muted">—</span>
            )}
          </td>
        )
      }
      case 'signals': {
        const signals = getSignals(r)
        return (
          <td key={col.id} className="px-3 py-2">
            {signals.length > 0 ? (
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
            ) : (
              <span className="text-muted text-xs">—</span>
            )}
          </td>
        )
      }
      case 'candle': {
        const candleRows = klineData[r.symbol] ?? []
        // 锁定列宽与行高：minWidth=maxWidth 防止 kline 加载前后整列宽度跳动（闪烁）
        // padding/宽度与自选页一致（width+4 留内边距余量）
        return (
          <td
            key={col.id}
            className="pl-2 pr-3 py-1.5"
            style={{ width: candleSize.width + 4, minWidth: candleSize.width + 4, maxWidth: candleSize.width + 4, height: candleSize.height }}
          >
            <MiniCandlestick rows={candleRows} width={candleSize.width} height={candleSize.height} />
          </td>
        )
      }
      case 'intraday': {
        const rows: MinuteKlineRow[] = minuteData[r.symbol] ?? []
        const iw = intradaySize.width
        const ih = intradaySize.height
        // border-l 与自选页一致：当日k/分时相邻时提供视觉分隔
        return (
          <td
            key={col.id}
            className="pl-3 pr-2 py-1.5 border-l border-border/30"
            style={{ width: iw + 4, minWidth: iw + 4, maxWidth: iw + 4, height: ih }}
          >
            <div className="flex items-center justify-center">
              {intradayChartVisible
                ? <MiniIntraday rows={rows} prevClose={r.prev_close} changePct={r.change_pct} width={iw - 4} height={ih} />
                : <span className="text-[10px] text-muted">分时</span>}
            </div>
          </td>
        )
      }
      default:
        // 纯数据列 → 共享原语
        return renderBuiltinDataCell(r, col)
    }
  }

  return (
    <>
      <StockDataTable
        columns={columns}
        rows={rows}
        renderCell={renderCell}
        sort={sort}
        onSortToggle={onSortToggle}
        minWidth={Math.max(900, columns.filter(c => c.visible).length * 110)}
        rowKey={(r: any) => `${r.symbol}${r._expired ? '-expired' : ''}`}
        rowClassName={(r: any) => r._expired
          ? 'border-border/50 opacity-40'
          : 'border-border hover:bg-elevated/50'
        }
        // 日k / 分时列表头：标签 + 显示/隐藏的眼睛按钮（与自选页一致）
        renderHeaderContent={(col) => {
        if (col.source.type !== 'builtin') return undefined
        const key = col.source.key
        // 日k 蜡烛图开关
        if (key === 'candle' && onToggleDailyKChart) {
          return (
            <span className="inline-flex items-center justify-center gap-1.5">
              <span>{col.label}</span>
              <button
                type="button"
                onClick={(event) => { event.stopPropagation(); onToggleDailyKChart() }}
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
        // 分时图开关 + 手动刷新按钮 (自动轮询开启时不显示, 避免重复请求)
        if (key === 'intraday' && onToggleIntradayChart) {
          return (
            <span className="inline-flex items-center justify-center gap-1.5">
              <span>{col.label}</span>
              <button
                type="button"
                onClick={(event) => { event.stopPropagation(); onToggleIntradayChart() }}
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
              {intradayChartVisible && !intradayAutoRefresh && onRefreshIntraday && (
                <button
                  type="button"
                  onClick={(event) => { event.stopPropagation(); onRefreshIntraday() }}
                  disabled={intradayRefreshing}
                  className="inline-flex items-center justify-center w-5 h-5 rounded text-muted hover:text-accent hover:bg-accent/10 transition-colors disabled:opacity-40"
                  title="刷新分时数据"
                  aria-label="刷新分时数据"
                >
                  <RefreshCw className={`h-3.5 w-3.5 ${intradayRefreshing ? 'animate-spin' : ''}`} />
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
      />
      <DimensionMembersDialog
        target={dimensionTarget}
        onClose={() => setDimensionTarget(null)}
        onStockClick={(symbol, name) => {
          setDimensionTarget(null)
          onPreview(symbol, name ?? '')
        }}
      />
    </>
  )
}
