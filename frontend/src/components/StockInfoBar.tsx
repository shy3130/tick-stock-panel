import { useState, type ReactNode } from 'react'
import { Settings2, RadioTower, Star } from 'lucide-react'
import type { KlineRow, FinancialMetricRecord } from '@/lib/api'
import { fmtPrice, fmtBigNum, fmtVolume } from '@/lib/format'
import { ListColumnCustomizer } from '@/components/ListColumnCustomizer'
import { INFO_GROUPS, type ColumnConfig } from '@/lib/stock-info-fields'
import { bestBidAsk } from '@/lib/realtimeOverlays'
import type {
  RealtimeStatus,
  RealtimeSymbolState,
} from '@/lib/realtimeMarketData'

const BULL = '#C74040'
const BEAR = '#2D9B65'

interface Props {
  symbol: string
  name?: string
  stockInfo?: { name?: string; total_shares?: number; float_shares?: number; ext?: Record<string, unknown> }
  rows: KlineRow[]
  /** 信息条字段配置（由 StockPanel 提升，受控） */
  fields: ColumnConfig[]
  onFieldsChange: (fields: ColumnConfig[]) => void
  /** 财务指标最新一期（来自 useFinancialMetrics，受 Cap.FINANCIAL 门控） */
  financialMetrics?: FinancialMetricRecord
  /** 加监控回调 (个股弹窗传入, 有值时渲染 RadioTower 图标) */
  onMonitor?: () => void
  /** 加自选回调 + 是否已自选 (有 onToggle 时渲染 Star 图标) */
  inWatchlist?: boolean
  onToggleWatchlist?: () => void
  realtimeState?: RealtimeSymbolState
  realtimeStatus?: RealtimeStatus
}

/**
 * 精简渲染扩展数据值（信息条专用）。
 * 仍尊重 extDisplay 配置：text=纯文本，tag(默认)=按分隔符拆成小标签 + maxTags 截断。
 * 与自选列表的差异：标签模式无 maxWidth/排列方向，但保留 +N 展开交互。
 */
function renderExtInline(
  val: unknown,
  col: ColumnConfig,
  expanded: boolean,
  onToggle: () => void,
): ReactNode {
  if (val == null || (typeof val === 'number' && Number.isNaN(val))) {
    return <span className="text-muted">—</span>
  }
  if (typeof val === 'number') {
    const displayVal = Number.isInteger(val) ? fmtPrice(val, 0) : fmtPrice(val)
    return <span className="tabular-nums">{displayVal}</span>
  }
  if (typeof val === 'boolean') {
    return <span className={val ? 'text-bull' : 'text-muted'}>{val ? '是' : '否'}</span>
  }
  const str = String(val)
  // 纯文本模式
  if (col.extDisplay?.displayMode === 'text') {
    return <span>{str}</span>
  }
  // 标签模式（默认）：按分隔符拆成小标签
  const sep = col.extDisplay?.separator?.trim() || null
  const tags = sep
    ? str.split(sep).map(s => s.trim()).filter(Boolean)
    : str.split(/[、,，;；\-]/).map(s => s.trim()).filter(Boolean)
  if (tags.length === 0) return <span className="text-muted">—</span>
  // maxTags 截断 + 展开交互：收起时显示前 N 个 + +N，展开时显示全部 + 收起
  const maxTags = col.extDisplay?.maxTags ?? 0
  const hiddenIndices = maxTags > 0 ? col.extDisplay?.hiddenIndices : undefined
  const showAll = maxTags <= 0 || expanded
  const sliced = showAll ? tags : tags.slice(0, maxTags)
  const shown = hiddenIndices?.length ? sliced.filter((_, i) => !hiddenIndices.includes(i)) : sliced
  const overflow = tags.length - shown.length
  return (
    <span className="inline-flex flex-wrap items-center gap-0.5">
      {shown.map((tag, i) => (
        <span key={i} className="inline-block px-1 rounded text-[10px] leading-tight text-yellow-500 bg-yellow-500/10">
          {tag}
        </span>
      ))}
      {!showAll && overflow > 0 && (
        <button
          onClick={onToggle}
          className="inline-block px-1 rounded text-[10px] leading-tight text-accent bg-accent/10 hover:bg-accent/20 transition-colors"
        >
          +{overflow}
        </button>
      )}
      {showAll && maxTags > 0 && tags.length > maxTags && (
        <button
          onClick={onToggle}
          className="inline-block px-1 rounded text-[10px] leading-tight text-muted hover:text-foreground transition-colors"
        >
          收起
        </button>
      )}
    </span>
  )
}

export function StockInfoBar({
  symbol,
  name,
  stockInfo,
  rows,
  fields,
  onFieldsChange,
  financialMetrics,
  onMonitor,
  inWatchlist,
  onToggleWatchlist,
  realtimeState,
  realtimeStatus,
}: Props) {
  // 弹窗开关：纯本地状态，与数据/配置无关，放早期 return 之前
  const [customizerOpen, setCustomizerOpen] = useState(false)
  // ext 标签展开状态：按 symbol::colId，切股/切字段时互不干扰
  const [expandedExt, setExpandedExt] = useState<Set<string>>(new Set())

  const toggleExtExpand = (key: string) => {
    setExpandedExt(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const liveQuote = realtimeState?.quote
  const liveClose = typeof liveQuote?.lastDone === 'number'
    ? liveQuote.lastDone
    : null
  if (rows.length === 0 && liveClose == null) return null

  const latest = rows[rows.length - 1] ?? {
    date: liveQuote?.timestamp?.slice(0, 10) ?? '',
    open: liveQuote?.open ?? liveClose ?? 0,
    high: liveQuote?.high ?? liveClose ?? 0,
    low: liveQuote?.low ?? liveClose ?? 0,
    close: liveClose ?? 0,
    volume: liveQuote?.volume,
  }
  const prev = rows.length >= 2 ? rows[rows.length - 2] : null
  const close = liveClose ?? Number(latest.close)
  const previousClose = typeof liveQuote?.prevClose === 'number'
    ? liveQuote.prevClose
    : prev
      ? Number(prev.close)
      : close
  const chg = close - previousClose
  const chgPct = previousClose !== 0 ? chg / previousClose * 100 : 0
  const isUp = chg >= 0
  const clr = isUp ? BULL : BEAR

  const totalShares = stockInfo?.total_shares
  const floatShares = stockInfo?.float_shares
  const marketCap = totalShares ? close * totalShares : null
  const floatMarketCap = floatShares ? close * floatShares : null
  const turnoverRate = floatShares && latest.volume
    ? (Number(latest.volume) * 100 / floatShares * 100)
    : null

  const displayName = stockInfo?.name ?? name ?? ''
  const extData = stockInfo?.ext ?? {}

  // 按指标 key 计算格式化值，无数据返回 null（渲染时跳过，与原行为一致）。
  // 普通函数：依赖行情值每次 render 都变，useCallback 无收益；且必须定义在早期 return 之后。
  const computeBuiltinValue = (key: string): string | null => {
    switch (key) {
      case 'market_cap':       return marketCap != null ? fmtBigNum(marketCap) : null
      case 'float_market_cap': return floatMarketCap != null ? fmtBigNum(floatMarketCap) : null
      case 'turnover':         return turnoverRate != null ? `${turnoverRate.toFixed(2)}%` : null
      case 'volume': {
        const volume = liveQuote?.volume ?? latest.volume
        return volume != null ? fmtVolume(Number(volume)) : null
      }
      case 'amplitude': {
        const prevClose = prev ? Number(prev.close) : null
        if (prevClose == null || prevClose === 0) return null
        const hi = Number(liveQuote?.high ?? latest.high)
        const lo = Number(liveQuote?.low ?? latest.low)
        return `${((hi - lo) / prevClose * 100).toFixed(2)}%`
      }
      case 'open': return fmtPrice(Number(liveQuote?.open ?? latest.open))
      case 'high': return fmtPrice(Number(liveQuote?.high ?? latest.high))
      case 'low':  return fmtPrice(Number(liveQuote?.low ?? latest.low))
      // 财务指标：百分比字段存储为百分点(12.3 表示 12.3%)，直接 toFixed(2) + %
      case 'eps':         return financialMetrics?.eps_basic != null ? fmtPrice(financialMetrics.eps_basic) : null
      case 'bps':         return financialMetrics?.bps != null ? fmtPrice(financialMetrics.bps) : null
      case 'roe':         return financialMetrics?.roe != null ? `${financialMetrics.roe.toFixed(2)}%` : null
      case 'gross_margin':return financialMetrics?.gross_margin != null ? `${financialMetrics.gross_margin.toFixed(2)}%` : null
      case 'net_margin':  return financialMetrics?.net_margin != null ? `${financialMetrics.net_margin.toFixed(2)}%` : null
      case 'debt_ratio':  return financialMetrics?.debt_to_asset_ratio != null ? `${financialMetrics.debt_to_asset_ratio.toFixed(2)}%` : null
      case 'revenue_yoy': return financialMetrics?.revenue_yoy != null ? `${financialMetrics.revenue_yoy.toFixed(2)}%` : null
      case 'net_income_yoy': return financialMetrics?.net_income_yoy != null ? `${financialMetrics.net_income_yoy.toFixed(2)}%` : null
      // PE/PB 后端无此字段，用现价现算（PE 基于最新一期 EPS，非严格 TTM）
      case 'pe_ttm': {
        const eps = financialMetrics?.eps_basic
        return eps && eps !== 0 ? fmtPrice(close / eps) : null
      }
      case 'pb': {
        const bps = financialMetrics?.bps
        return bps && bps !== 0 ? fmtPrice(close / bps) : null
      }
      default: return null
    }
  }

  const visibleFields = fields.filter(f => f.visible)
  // 按是否单独显示分组：普通列共一行，standalone 列各占一行
  const inlineFields = visibleFields.filter(f => !f.standalone)
  const standaloneFields = visibleFields.filter(f => f.standalone)
  const { bid, ask } = bestBidAsk(realtimeState?.depth)
  const delayed = Boolean(
    realtimeState?.quoteDelayed
    || realtimeState?.depthDelayed
    || realtimeState?.candlestickDelayed,
  )

  // 渲染单个字段（builtin / ext 通用）
  const renderField = (f: ColumnConfig): ReactNode => {
    if (f.source.type === 'ext') {
      const { configId, fieldName } = f.source
      const val = extData[`${configId}__${fieldName}`]
      // 无值的 ext 字段整体跳过（与 builtin 无数据行为一致）
      if (val == null || (typeof val === 'number' && Number.isNaN(val))) return null
      const cellKey = `${symbol}::${f.id}`
      return (
        <span key={f.id} className="inline-flex items-center gap-1">
          <span>{f.label}</span>
          <span className="text-secondary">
            {renderExtInline(val, f, expandedExt.has(cellKey), () => toggleExtExpand(cellKey))}
          </span>
        </span>
      )
    }
    // builtin
    const value = computeBuiltinValue(f.source.type === 'builtin' ? f.source.key : '')
    if (value == null) return null
    return (
      <span key={f.id}>
        {f.label} <span className="text-secondary">{value}</span>
      </span>
    )
  }

  return (
    <div className="px-2 pb-3 font-mono text-[12px] select-none space-y-1">
      {/* Row 1: code, name, price, change, change% */}
      <div className="flex items-baseline gap-x-3 flex-wrap">
        <span className="text-foreground font-bold text-sm tracking-wide">{symbol}</span>
        <span className="text-secondary font-medium">{displayName}</span>
        <span style={{ color: clr }} className="text-lg font-bold tabular-nums">
          {fmtPrice(close)}
        </span>
        <span style={{ color: clr }} className="tabular-nums">
          {isUp ? '+' : ''}{fmtPrice(chg)}
        </span>
        <span style={{ color: clr }} className="tabular-nums">
          {isUp ? '+' : ''}{fmtPrice(chgPct)}%
        </span>
        {bid != null && (
          <span className="text-[10px] text-muted">
            买一 <span className="text-secondary">{fmtPrice(bid)}</span>
          </span>
        )}
        {ask != null && (
          <span className="text-[10px] text-muted">
            卖一 <span className="text-secondary">{fmtPrice(ask)}</span>
          </span>
        )}
        {realtimeStatus && (
          <span
            className={`rounded px-1.5 py-0.5 text-[9px] ${
              realtimeStatus === 'realtime' && !delayed
                ? 'bg-emerald-500/10 text-emerald-400'
                : 'bg-amber-500/10 text-amber-400'
            }`}
          >
            {delayed
              ? '实时数据延迟'
              : realtimeStatus === 'realtime'
                ? '实时'
                : realtimeStatus === 'connecting'
                  ? '实时连接中'
                  : 'HTTP 回退'}
          </span>
        )}
        {/* 右侧操作按钮：加自选 + 加监控 + 信息条配置 */}
        <div className="ml-auto self-center flex items-center gap-1">
          {onToggleWatchlist && (
            <button
              onClick={onToggleWatchlist}
              className={`p-1 rounded-btn transition-colors cursor-pointer ${inWatchlist ? 'text-[#FACC15]' : 'text-muted hover:text-foreground hover:bg-elevated'}`}
              title={inWatchlist ? '移出自选' : '加自选'}
            >
              <Star className="h-3.5 w-3.5" />
            </button>
          )}
          {onMonitor && (
            <button
              onClick={onMonitor}
              className="p-1 rounded-btn text-amber-400 hover:bg-amber-400/10 transition-colors cursor-pointer"
              title="加监控"
            >
              <RadioTower className="h-3.5 w-3.5" />
            </button>
          )}
          <button
            onClick={() => setCustomizerOpen(true)}
            className="p-1 rounded-btn text-muted hover:text-foreground hover:bg-elevated transition-colors"
            title="自定义信息条"
          >
            <Settings2 className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* Row 2: 普通指标（builtin + ext，共一行 flex-wrap） */}
      {inlineFields.length > 0 && (
        <div className="flex items-center gap-x-4 gap-y-1 text-[11px] flex-wrap text-muted">
          {inlineFields.map(renderField)}
        </div>
      )}

      {/* 单独显示的指标：各占一行 */}
      {standaloneFields.map(f => {
        const node = renderField(f)
        if (node == null) return null
        return (
          <div key={f.id} className="flex items-center gap-x-4 text-[11px] flex-wrap text-muted">
            {node}
          </div>
        )
      })}

      <ListColumnCustomizer
        columns={fields}
        groups={INFO_GROUPS}
        onChange={onFieldsChange}
        open={customizerOpen}
        onClose={() => setCustomizerOpen(false)}
        title="信息条指标"
        builtinSectionLabel="可选指标"
        extColumnAlign="left"
        showStandaloneToggle
      />
    </div>
  )
}
