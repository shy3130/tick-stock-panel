import { useState, useMemo, useEffect, useRef, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { Play, FlaskConical, Clock, Loader2, Square, Search, Plus, X, SlidersHorizontal, BarChart3, Gauge, Zap, ListPlus, HelpCircle } from 'lucide-react'
import {
  api,
  type StrategyBacktestResult,
  type StrategyBacktestTrade,
  type StrategyDetail,
  type StrategyParamDef,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { boardTag } from '@/lib/board'
import { BUILTIN_COLUMNS } from '@/lib/watchlist-columns'
import { cnSignal } from '@/lib/signals'
import { SignalPicker } from '@/components/screener/SignalPicker'
import { startBacktest, stopBacktest, tryReconnect, useBacktestTask } from '@/lib/backtestTask'
import { useDataStatus, useCapabilities } from '@/lib/useSharedQueries'
import { EmptyState } from '@/components/EmptyState'
import { WarmupBadge } from '@/components/WarmupBadge'
import { DatePicker } from '@/components/DatePicker'
import { StrategyNavChart } from './charts/StrategyNavChart'
import { ReturnDistributionChart } from './charts/ReturnDistributionChart'
import { TradeKlineModal } from './components/TradeKlineModal'
import { SignalTriggerActions } from '@/components/signals/SignalTriggerActions'

const formatDate = (date: Date) => date.toISOString().slice(0, 10)
const monthsAgo = (months: number) => {
  const date = new Date()
  date.setMonth(date.getMonth() - months)
  return formatDate(date)
}
const TODAY = formatDate(new Date())
const THREE_MONTHS_AGO = monthsAgo(3)

type QuickRangeUnit = 'month' | 'year' | 'all'
type QuickRangeConfig = { id: string; enabled: boolean; unit: QuickRangeUnit; value: number }

const QUICK_RANGE_LIMITS = {
  month: { min: 1, max: 120 },
  year: { min: 1, max: 10 },
} as const
const DEFAULT_QUICK_RANGES: QuickRangeConfig[] = [
  { id: 'range-1', enabled: true, unit: 'month', value: 3 },
  { id: 'range-2', enabled: true, unit: 'month', value: 6 },
  { id: 'range-3', enabled: true, unit: 'year', value: 1 },
  { id: 'range-4', enabled: true, unit: 'all', value: 0 },
]
const quickRangeValue = (unit: QuickRangeUnit, value: unknown, fallback: number) => {
  if (unit === 'all') return 0
  const limits = QUICK_RANGE_LIMITS[unit]
  const num = Number(value)
  const safe = Number.isFinite(num) ? Math.round(num) : fallback
  return clamp(safe, limits.min, limits.max)
}
const normalizeQuickRange = (raw: unknown, fallback: QuickRangeConfig): QuickRangeConfig => {
  const obj = raw && typeof raw === 'object' ? raw as Partial<QuickRangeConfig> : {}
  const unit: QuickRangeUnit = obj.unit === 'month' || obj.unit === 'year' || obj.unit === 'all'
    ? obj.unit
    : fallback.unit
  const enabled = typeof obj.enabled === 'boolean' ? obj.enabled : fallback.enabled
  return { id: fallback.id, enabled, unit, value: quickRangeValue(unit, obj.value, fallback.value) }
}
const normalizeQuickRanges = (raw: unknown) => {
  const items = Array.isArray(raw) ? raw : []
  const ranges = DEFAULT_QUICK_RANGES.map((fallback, index) => {
    const byId = items.find(item => item && typeof item === 'object' && (item as { id?: unknown }).id === fallback.id)
    return normalizeQuickRange(byId ?? items[index], fallback)
  })
  return ranges.some(range => range.enabled)
    ? ranges
    : ranges.map((range, index) => index === 0 ? { ...range, enabled: true } : range)
}
const loadQuickRanges = () => normalizeQuickRanges(storage.strategyBacktestQuickRanges.get(DEFAULT_QUICK_RANGES))
const quickRangeMonths = (range: QuickRangeConfig) => range.unit === 'year' ? range.value * 12 : range.value
const quickRangeLabel = (range: QuickRangeConfig) => range.unit === 'all'
  ? '全部'
  : range.unit === 'year'
    ? `${range.value}年`
    : `${range.value}个月`
const quickRangeTitle = (range: QuickRangeConfig) => range.unit === 'all'
  ? '全部历史'
  : range.unit === 'year'
    ? `近 ${range.value} 年`
    : `近 ${range.value} 个月`

const INPUT_CLS = `w-full px-2.5 py-1.5 rounded-input bg-surface border border-border text-xs
  focus:outline-none focus:border-accent transition-colors duration-150 ease-smooth`

/** 建仓/清仓口径说明 — 黄色问号图标, 点击弹出气泡。
 * 用 fixed 定位脱离父容器 overflow 裁剪(左侧表单是 overflow-y-auto, absolute 气泡会被裁)。 */
function FillRuleHint() {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  const iconRef = useRef<HTMLDivElement>(null)

  const handleOpen = () => {
    if (!open && iconRef.current) {
      const r = iconRef.current.getBoundingClientRect()
      setPos({ top: r.bottom + 4, left: r.left })
    }
    setOpen(v => !v)
  }

  // 气泡宽度 256px(w-64), 若右侧超出视口则向左对齐
  const bubbleLeft = pos ? Math.min(pos.left, window.innerWidth - 256 - 8) : 0

  return (
    <div ref={iconRef} className="relative inline-flex items-center">
      <HelpCircle
        className="h-3.5 w-3.5 text-yellow-500/80 hover:text-yellow-500 cursor-help transition-colors"
        onClick={handleOpen}
      />
      <AnimatePresence>
        {open && pos && (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
            <motion.div
              initial={{ opacity: 0, y: -4, scale: 0.95 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -4, scale: 0.95 }}
              transition={{ duration: 0.15 }}
              style={{ top: pos.top, left: bubbleLeft }}
              className="fixed z-50 w-64 bg-surface border border-border rounded-md shadow-xl p-3 text-[11px] text-secondary leading-relaxed"
              onClick={e => e.stopPropagation()}
            >
              <div className="font-medium text-foreground mb-1.5">成交口径说明</div>
              <div className="space-y-1">
                <div><b className="text-foreground">建仓</b>默认<b className="text-foreground">次日开盘</b>(避免未来函数)</div>
                <div><b className="text-foreground">清仓</b>默认<b className="text-foreground">当日收盘</b>(持仓中可盘中/收盘卖)</div>
                <div>买卖点由<b className="text-foreground">策略触发器</b>决定,这里只决定成交价。</div>
              </div>
            </motion.div>
          </>
        )}
      </AnimatePresence>
    </div>
  )
}

const SRC_MAP: Record<string, string> = { builtin: '内置', custom: '自定义', ai: 'AI' }
const TRADE_PAGE_SIZE_OPTIONS = [10, 20, 30, 50, 100]
const BADGE_CLS_MAP: Record<string, string> = {
  builtin: 'bg-secondary/10 text-muted border-border',
  ai: 'bg-purple-500/10 text-purple-400 border-purple-500/30',
  custom: 'bg-amber-400/10 text-amber-400 border-amber-400/30',
}
const FIELD_LABEL: Record<string, string> = {}
for (const c of BUILTIN_COLUMNS) {
  if (c.source.type === 'builtin') FIELD_LABEL[c.source.key] = c.label
}
Object.assign(FIELD_LABEL, {
  change_pct: '涨跌幅', consecutive_limit_ups: '连板',
  momentum_60d: '60D动量', turnover_rate: '换手率',
  rsi_14: 'RSI14', rsi_6: 'RSI6', rsi_24: 'RSI24',
  vol_ratio_5d: '量比', vol_ratio_20d: '20日量比',
  macd_dif: 'MACD-DIF', macd_dea: 'MACD-DEA', macd_hist: 'MACD柱',
  boll_upper: '布林上轨', boll_lower: '布林下轨',
})
const BOARD_OPTIONS = ['沪主板', '深主板', '创业板', '科创板', '北交所']
const BASIC_FILTER_FIELDS = [
  { key: 'price_min', label: '最低价', unit: '元' },
  { key: 'price_max', label: '最高价', unit: '元' },
  { key: 'amount_min', label: '最低成交额', unit: '亿', scale: 1e8 },
  { key: 'market_cap_min', label: '最低总市值', unit: '亿', scale: 1e8 },
  { key: 'turnover_min', label: '最低换手率', unit: '%' },
  { key: 'turnover_max', label: '最高换手率', unit: '%' },
]
type AdvancedSettingsTab = 'params' | 'filter' | 'entry' | 'exit' | 'scoring' | 'risk' | 'range'
type StrategyGroup = 'all' | 'custom' | 'ai' | 'builtin'
const STRATEGY_GROUPS: { id: StrategyGroup; label: string }[] = [
  { id: 'all', label: '全部' },
  { id: 'custom', label: '自定义' },
  { id: 'ai', label: 'AI' },
  { id: 'builtin', label: '内置' },
]
const ADVANCED_TABS: { id: AdvancedSettingsTab; label: string }[] = [
  { id: 'params', label: '策略参数' },
  { id: 'filter', label: '基础过滤' },
  { id: 'entry', label: '入场触发器' },
  { id: 'exit', label: '出场触发器' },
  { id: 'scoring', label: '评分权重' },
  { id: 'risk', label: '风控' },
  { id: 'range', label: '回测范围' },
]
const toSignalId = (sig: string) => (sig.startsWith('signal_') || sig.startsWith('csg_')) ? sig : `signal_${sig}`
const numOrNull = (v: string) => v === '' || Number.isNaN(Number(v)) ? null : Number(v)
const clamp = (v: number, min?: number, max?: number) => {
  let next = v
  if (min != null) next = Math.max(next, min)
  if (max != null) next = Math.min(next, max)
  return next
}
const strategyDefaultParams = (detail: StrategyDetail) => {
  const values: Record<string, any> = { ...detail.params_defaults }
  detail.params.forEach(p => {
    if (!(p.id in values)) values[p.id] = p.default
  })
  return values
}
const mergeStrategyParams = (detail: StrategyDetail, values?: Record<string, any> | null) => ({
  ...strategyDefaultParams(detail),
  ...(values ?? {}),
})
const buildDefaultOverrides = (detail: StrategyDetail) => ({
  basic_filter: { ...detail.basic_filter },
  entry_signals: detail.entry_signals.map(toSignalId),
  exit_signals: detail.exit_signals.map(toSignalId),
  scoring: { ...detail.scoring },
  stop_loss: detail.stop_loss,
  take_profit: detail.take_profit,
  trailing_stop: detail.trailing_stop,
  trailing_take_profit_activate: detail.trailing_take_profit_activate,
  trailing_take_profit_drawdown: detail.trailing_take_profit_drawdown,
  score_min: null,
  score_max: null,
  max_hold_days: detail.max_hold_days,
})

const fmtMoney = (v: number | null | undefined) => {
  if (v == null || Number.isNaN(v)) return '—'
  return v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

const fmtSignedMoney = (v: number | null | undefined) => {
  if (v == null || Number.isNaN(v)) return '—'
  const sign = v > 0 ? '+' : ''
  return `${sign}${fmtMoney(v)}`
}

const fmtShares = (v: number | null | undefined) => {
  if (v == null || Number.isNaN(v)) return '—'
  return v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

const fmtLots = (v: number | null | undefined) => {
  if (v == null || Number.isNaN(v)) return '—'
  return v.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

const statValueColor = (v: number | null | undefined) => {
  // 中性值继承页面前景色 (亮暗主题都可读), 不再写死近白色
  if (v == null || Number.isNaN(v) || v === 0) return 'inherit'
  return v > 0 ? '#f87171' : '#34d399'
}

/** 信号 ID → 可读名称映射 (内置 + 自定义), 供交易记录显示具体触发信号。 */
function useSignalNames(): Record<string, string> {
  const customQ = useQuery({ queryKey: QK.customSignals, queryFn: api.customSignalsList })
  return useMemo(() => {
    const names: Record<string, string> = {}
    for (const cs of customQ.data?.signals ?? []) names[`csg_${cs.id}`] = cs.name
    return names
  }, [customQ.data])
}

function ExitReasonBadge({ reason, signalId, signalNames }: { reason: string; signalId?: string | null; signalNames?: Record<string, string> }) {
  const config: Record<string, { label: string; cls: string }> = {
    signal: { label: '信号', cls: 'bg-accent/10 text-accent border-accent/30' },
    stop_loss: { label: '止损', cls: 'bg-red-500/10 text-red-400 border-red-500/30' },
    take_profit: { label: '止盈', cls: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30' },
    trailing_stop: { label: '移损', cls: 'bg-orange-500/10 text-orange-400 border-orange-500/30' },
    trailing_take_profit: { label: '回撤止盈', cls: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30' },
    max_hold: { label: '超期', cls: 'bg-amber-400/10 text-amber-400 border-amber-400/30' },
    pending_exit: { label: '待卖', cls: 'bg-orange-400/10 text-orange-400 border-orange-400/30' },
    end: { label: '期末', cls: 'bg-secondary/10 text-secondary border-border' },
  }
  const c = config[reason] ?? { label: reason, cls: 'bg-elevated text-muted border-border' }
  // 信号类退出且能解析出具体信号名时, 显示具体信号而非笼统的"信号"
  const specific = reason === 'signal' && signalId ? cnSignal(signalId, signalNames) : null
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded border ${c.cls} ${specific ? 'max-w-[7rem] truncate' : ''}`} title={specific ?? c.label}>
      {specific ?? c.label}
    </span>
  )
}

type DailyTradeRow = {
  date: string
  buys: StrategyBacktestTrade[]
  sells: StrategyBacktestTrade[]
  buyValue: number
  sellValue: number
  realizedPnl: number
  cumulativePnl: number
}

function fmtPositionPct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${(Math.abs(v) * 100).toFixed(digits)}%`
}

function fmtScore(v: number | null | undefined): string {
  if (v == null || Number.isNaN(Number(v))) return '—'
  return Number(v).toFixed(1)
}

function DailyTradeChip({ trade, side, strategyName, onClick, signalNames }: { trade: StrategyBacktestTrade; side: 'buy' | 'sell'; strategyName?: string; onClick?: () => void; signalNames?: Record<string, string> }) {
  const isBuy = side === 'buy'
  const tag = boardTag(trade.symbol)
  const price = isBuy ? trade.entry_price : trade.exit_price
  const amount = isBuy ? trade.entry_value : trade.exit_value
  const pnlColor = priceColorClass(trade.pnl_amount ?? trade.pnl_pct)
  const footerColor = isBuy ? 'text-secondary' : pnlColor
  const footerText = `仓位 ${fmtPositionPct(trade.position_pct, 2)}`
  const scoreText = fmtScore(trade.entry_score)
  const buyStrategy = strategyName || '策略'

  return (
    <button type="button" onClick={onClick} className={`inline-flex ${isBuy ? 'w-[14.5rem]' : 'w-[14.5rem]'} flex-col gap-0.5 rounded-btn border px-1.5 py-1 text-left text-[11px] leading-4 transition-colors hover:border-accent/45 hover:bg-elevated/60 focus:outline-none focus:ring-1 focus:ring-accent/40 ${
      isBuy ? 'border-accent/25 bg-accent/5' : 'border-border/70 bg-base/45'
    }`}>
      <span className="flex items-center gap-1">
        <span className={`shrink-0 rounded px-1 py-px text-[9px] font-medium ${
          isBuy ? 'bg-accent/15 text-accent' : 'bg-elevated text-secondary'
        }`}>
          {isBuy ? '买' : '卖'}
        </span>
        <span className="min-w-0 flex-1 truncate text-foreground">{trade.name || trade.symbol}</span>
        {tag && <span className={`shrink-0 rounded px-1 text-[9px] font-medium ${isBuy ? 'bg-accent/20 text-accent' : 'bg-elevated text-secondary'}`}>{tag}</span>}
      </span>
      <span className="flex items-center justify-between gap-2 text-muted">
        <span className="min-w-0 truncate">
          <span className="font-mono">{trade.symbol}</span>
          <span className="mx-1">·</span>
          <span className="num">{fmtLots(trade.lots)}手</span>
        </span>
        {isBuy ? (
          <span className="num shrink-0 text-secondary">{fmtPrice(price)}</span>
        ) : (
          <span className="flex shrink-0 items-center gap-1.5">
            <span className="num text-secondary">{fmtPrice(price)}</span>
            <ExitReasonBadge reason={trade.exit_reason} signalId={trade.exit_signal_id} signalNames={signalNames} />
          </span>
        )}
      </span>
      {isBuy ? (
        <>
          <span className="flex items-center justify-between gap-2">
            <span className="min-w-0 truncate text-muted" title={buyStrategy}>策略 {buyStrategy}</span>
            <span className="shrink-0 rounded border border-accent/25 bg-accent/10 px-1.5 py-px font-mono text-[10px] text-accent">
              评分 {scoreText}
            </span>
          </span>
          <span className="flex items-center justify-between gap-2">
            <span className="num font-medium text-foreground">{fmtMoney(amount)}</span>
            <span className={`min-w-0 truncate text-right num ${footerColor}`}>{footerText}</span>
          </span>
        </>
      ) : (
        <>
          <span className="flex items-center justify-between gap-2">
            <span className="text-muted">卖出</span>
            <span className="num font-medium text-foreground">{fmtMoney(amount)}</span>
          </span>
          <span className="flex items-center justify-between gap-2">
            <span className="text-muted">盈亏</span>
            <span className={`flex shrink-0 items-center gap-1.5 text-right num font-medium ${pnlColor}`}>
              <span>{fmtSignedMoney(trade.pnl_amount)}</span>
              <span className="text-muted/40">/</span>
              <span>{fmtPct(trade.pnl_pct)}</span>
            </span>
          </span>
        </>
      )}
    </button>
  )
}

function TradeLegCell({ trade, side, signalNames }: { trade: StrategyBacktestTrade; side: 'buy' | 'sell'; signalNames?: Record<string, string> }) {
  const isBuy = side === 'buy'
  const date = String(isBuy ? trade.entry_date : trade.exit_date).slice(0, 10)
  const signalDate = String(isBuy ? trade.entry_signal_date ?? '' : trade.exit_signal_date ?? '').slice(0, 10)
  const price = isBuy ? trade.entry_price : trade.exit_price
  const amount = isBuy ? trade.entry_value : trade.exit_value
  const signalId = isBuy ? trade.entry_signal_id : trade.exit_signal_id
  const signalLabel = signalId ? cnSignal(signalId, signalNames) : null

  return (
    <div className="min-w-[8.25rem] rounded-btn border border-border/60 bg-base/35 px-2 py-1 text-xs leading-4">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-secondary">{date}</span>
        <span className={`rounded px-1.5 py-px text-[10px] font-medium ${
          isBuy ? 'bg-accent/15 text-accent' : 'bg-elevated text-secondary'
        }`}>
          {isBuy ? '买' : '卖'}
        </span>
      </div>
      <div className="mt-0.5 flex items-center justify-between gap-2">
        <span className="num text-foreground">{fmtPrice(price)}</span>
        <span className="num font-medium text-foreground">{fmtMoney(amount)}</span>
      </div>
      {signalLabel && (
        <div className="mt-0.5 text-[10px] text-accent/80 truncate" title={signalLabel}>{signalLabel}</div>
      )}
      {!signalLabel && signalDate && signalDate !== date && (
        <div className="mt-0.5 text-[10px] text-muted">信号 {signalDate}</div>
      )}
    </div>
  )
}

function fmtDuration(ms: number): string {
  const s = ms / 1000
  if (s < 1) return `${ms.toFixed(0)}ms`
  if (s < 60) return `${s.toFixed(1)}秒`
  const m = Math.floor(s / 60)
  const rest = Math.round(s % 60)
  return `${m}分${rest}秒`
}

function SharpeLabel() {
  const [open, setOpen] = useState(false)
  const [alignRight, setAlignRight] = useState(false)
  const ref = useRef<HTMLSpanElement>(null)
  useEffect(() => {
    if (!open) return
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [open])
  const toggle = () => {
    if (!open && ref.current) {
      const rect = ref.current.getBoundingClientRect()
      setAlignRight(rect.left + 240 > window.innerWidth)
    }
    setOpen(o => !o)
  }
  return (
    <span className="relative inline-flex items-center gap-1" ref={ref}>
      夏普
      <button
        type="button"
        onClick={toggle}
        className="inline-flex h-3.5 w-3.5 items-center justify-center rounded-full border border-border bg-base text-[10px] text-muted transition-colors hover:border-accent/50 hover:text-accent"
      >
        ?
      </button>
      {open && (
        <span className={`absolute top-full z-50 mt-1.5 w-60 max-w-[calc(100vw-1.5rem)] rounded-lg border border-border bg-elevated px-3 py-2.5 text-[11px] leading-relaxed text-secondary shadow-xl ${alignRight ? 'right-0' : 'left-0'}`}>
          <span className="block font-medium text-foreground">夏普比率 (Sharpe Ratio)</span>
          <span className="mt-1 block">衡量<b className="text-foreground">单位波动风险</b>换来的超额收益。</span>
          <span className="mt-0.5 block">数值越高，收益相对波动越优秀；</span>
          <span className="mt-0.5 block text-warning">短周期或交易次数少时容易偏高，仅供参考。</span>
        </span>
      )}
    </span>
  )
}

function Stat({ label, value, color }: { label: ReactNode; value: string; color?: string }) {
  return (
    <div className="min-w-0 rounded-btn border border-border/70 bg-elevated/70 px-3 py-2">
      <div className="text-[11px] text-secondary">{label}</div>
      <div
        className="mt-1 break-words text-sm font-mono font-semibold leading-tight tracking-tight num xl:text-base"
        style={{ color: color ?? 'inherit' }}
        title={value}
      >
        {value}
      </div>
    </div>
  )
}

function ConfigSection({ title, hint, actions, children }: { title: string; hint?: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return (
    <div className="rounded-btn border border-border bg-surface/70 p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="text-xs font-medium text-foreground">
          {title}
          {hint && <span className="ml-1 text-[10px] font-normal text-muted">{hint}</span>}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-1">{actions}</div>}
      </div>
      <div className="mt-3 space-y-2">{children}</div>
    </div>
  )
}


const scoringToPct = (values: Record<string, number>) => {
  const total = Object.values(values).reduce((a, b) => a + Math.max(0, Number(b) || 0), 0)
  if (total <= 0) return Object.fromEntries(Object.keys(values).map(k => [k, 0])) as Record<string, number>
  return Object.fromEntries(Object.entries(values).map(([k, v]) => [k, Math.round((Math.max(0, Number(v) || 0) / total) * 100)])) as Record<string, number>
}

const normalizePctWeights = (values: Record<string, number>) => {
  const total = Object.values(values).reduce((a, b) => a + Math.max(0, Number(b) || 0), 0)
  if (total <= 0) return Object.fromEntries(Object.keys(values).map(k => [k, 0])) as Record<string, number>
  return Object.fromEntries(Object.entries(values).map(([k, v]) => [k, +(Math.max(0, Number(v) || 0) / total).toFixed(4)])) as Record<string, number>
}

function ScoringWeightRow({ name, weight, pct, editing, onChange }: {
  name: string
  weight: number
  pct: number
  editing: boolean
  onChange: (value: number) => void
}) {
  const label = FIELD_LABEL[name] ?? name
  return (
    <div className="flex items-center gap-2">
      <span className="w-20 shrink-0 truncate text-right text-[11px] text-secondary" title={name}>{label}</span>
      {editing ? (
        <input
          type="range"
          min={0}
          max={100}
          step={1}
          value={weight}
          onChange={e => onChange(Number(e.target.value))}
          className="h-1 flex-1 cursor-pointer accent-amber-400"
        />
      ) : (
        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-elevated">
          <div className="h-full rounded-full bg-amber-400/70 transition-all duration-300" style={{ width: `${Math.min(pct, 100)}%` }} />
        </div>
      )}
      <span className="w-10 text-right font-mono text-[10px] text-muted">{editing ? weight : `${pct}%`}</span>
    </div>
  )
}

function StrategyParamInput({ param, value, onChange }: {
  param: StrategyParamDef
  value: any
  onChange: (value: any) => void
}) {
  if (param.type === 'bool') {
    const checked = value === true || value === 'true' || value === 'True' || value === true
    return (
      <label className="block">
        <span className="mb-1 block text-[11px] text-secondary">{param.label}</span>
        <button
          type="button"
          onClick={() => onChange(!checked)}
          className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors duration-200 cursor-pointer ${
            checked ? 'bg-accent shadow-[0_0_6px_rgba(59,130,246,0.3)]' : 'bg-elevated'
          }`}
          aria-pressed={checked}
        >
          <span className={`inline-block h-4 w-4 rounded-full bg-white shadow-sm transition-transform duration-200 ${
            checked ? 'translate-x-[18px]' : 'translate-x-0.5'
          }`} />
        </button>
      </label>
    )
  }
  if (param.type === 'select') {
    return (
      <label className="block">
        <span className="mb-1 block text-[11px] text-secondary">{param.label}</span>
        <select value={value ?? param.default} onChange={e => onChange(e.target.value)} className={INPUT_CLS}>
          {(param.options ?? []).map(opt => <option key={opt} value={opt}>{opt}</option>)}
        </select>
      </label>
    )
  }
  return (
    <label className="block">
      <span className="mb-1 block text-[11px] text-secondary">{param.label}</span>
      <input
        type="number"
        value={value ?? ''}
        min={param.min}
        max={param.max}
        step={param.step ?? (param.type === 'int' ? 1 : 0.01)}
        onChange={e => {
          const n = numOrNull(e.target.value)
          if (n == null) return onChange('')
          const next = clamp(n, param.min, param.max)
          onChange(param.type === 'int' ? Math.round(next) : next)
        }}
        className={INPUT_CLS}
      />
    </label>
  )
}

function StockPoolPicker({ value, onChange, assetType = 'stock' }: { value: string; onChange: (value: string) => void; assetType?: 'stock' | 'etf' }) {
  const symbols = useMemo(() => value.split(',').map(s => s.trim()).filter(Boolean), [value])
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const [symbolNames, setSymbolNames] = useState<Record<string, string>>({})
  const ref = useRef<HTMLDivElement>(null)
  const searchAssetTypes = assetType === 'etf' ? 'stock,etf' : 'stock'
  const search = useQuery({
    queryKey: QK.instrumentSearch(query, searchAssetTypes),
    queryFn: () => api.instrumentSearch(query, 20, searchAssetTypes),
    enabled: query.trim().length > 0,
    staleTime: 30_000,
  })
  const results = search.data?.results ?? []
  // 自选列表 — 供「从自选导入」一键填入回测范围
  const watchlist = useQuery({
    queryKey: QK.watchlist,
    queryFn: () => api.watchlistList(),
    staleTime: 30_000,
  })

  useEffect(() => {
    if (results.length === 0) return
    setSymbolNames(prev => {
      const next = { ...prev }
      results.forEach(r => {
        if (r.name) next[r.symbol] = r.name
      })
      return next
    })
  }, [results])

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [])

  const setSymbols = (next: string[]) => onChange(Array.from(new Set(next)).join(','))
  const addSymbol = (symbol: string, name?: string | null) => {
    if (name) setSymbolNames(prev => ({ ...prev, [symbol]: name }))
    setSymbols([...symbols, symbol])
    setQuery('')
    setOpen(false)
  }
  const removeSymbol = (symbol: string) => setSymbols(symbols.filter(s => s !== symbol))
  // 一键导入自选: 合并去重, 顺带回填股票名
  const importFromWatchlist = () => {
    const entries = watchlist.data?.symbols ?? []
    if (entries.length === 0) return
    setSymbolNames(prev => {
      const next = { ...prev }
      entries.forEach(e => { if (e.name) next[e.symbol] = e.name })
      return next
    })
    setSymbols([...symbols, ...entries.map(e => e.symbol)])
  }
  const watchlistCount = watchlist.data?.symbols?.length ?? 0

  return (
    <div className="space-y-2" ref={ref}>
      <div className="flex items-center gap-2">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
          <input
            type="text"
            value={query}
            onChange={e => { setQuery(e.target.value); setOpen(true) }}
            onFocus={() => { if (query.trim()) setOpen(true) }}
            placeholder="搜索股票名称/代码添加股票池"
            className="w-full rounded-input border border-border bg-surface py-1.5 pl-8 pr-2.5 text-xs focus:border-accent focus:outline-none"
          />
          {open && results.length > 0 && (
            <div className="absolute left-0 right-0 top-full z-50 mt-1 max-h-56 overflow-y-auto rounded-card border border-border bg-base shadow-xl">
              {results.map(r => {
                const added = symbols.includes(r.symbol)
                return (
                  <button
                    key={r.symbol}
                    type="button"
                    disabled={added}
                    onClick={() => addSymbol(r.symbol, r.name)}
                    className={`flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition-colors ${added ? 'cursor-default text-muted' : 'text-foreground hover:bg-elevated'}`}
                  >
                    <span className="w-[78px] shrink-0 font-mono">{r.symbol}</span>
                    <span className="min-w-0 flex-1 truncate text-secondary">{r.name}</span>
                    <Plus className={`h-3.5 w-3.5 ${added ? 'opacity-30' : 'text-accent'}`} />
                  </button>
                )
              })}
            </div>
          )}
        </div>
        {/* 操作按钮 — 紧贴输入框右侧 */}
        <div className="flex shrink-0 items-center gap-1.5">
          {/* 当前范围 — 有范围显示个数, 无范围显示全市场 */}
          <span className={`whitespace-nowrap text-[11px] font-medium ${symbols.length === 0 ? 'text-amber-400' : 'text-accent'}`}>
            {symbols.length === 0 ? '全市场' : `共 ${symbols.length} 只`}
          </span>
          <button
            type="button"
            onClick={importFromWatchlist}
            disabled={watchlist.isLoading || watchlistCount === 0}
            className="inline-flex items-center gap-1 whitespace-nowrap rounded-input border border-border bg-surface px-2 py-1.5 text-[11px] text-secondary transition-colors hover:border-accent/50 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
            title="把自选列表的个股加入回测范围"
          >
            <ListPlus className="h-3 w-3" />
            {watchlist.isLoading ? '加载…' : watchlistCount === 0 ? '自选空' : `导入自选(${watchlistCount})`}
          </button>
          <button
            type="button"
            onClick={() => setSymbols([])}
            disabled={symbols.length === 0}
            className="inline-flex items-center gap-1 whitespace-nowrap rounded-input border border-border bg-surface px-2 py-1.5 text-[11px] text-secondary transition-colors hover:border-danger/50 hover:text-danger disabled:cursor-not-allowed disabled:opacity-50"
            title="清空回测范围"
          >
            <X className="h-3 w-3" />
            清空
          </button>
        </div>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {symbols.length === 0 ? (
          <span className="text-[11px] text-muted">默认全市场回测，由基础过滤和策略条件筛选。</span>
        ) : symbols.map(symbol => {
          const name = symbolNames[symbol]
          return (
          <span key={symbol} className="inline-flex items-center gap-1 rounded-btn border border-accent/30 bg-accent/10 px-2 py-1 text-[10px] text-accent">
            <span className="font-mono">{symbol}</span>
            {name && <span className="max-w-[7rem] truncate text-accent/80">{name}</span>}
            <button type="button" onClick={() => removeSymbol(symbol)} className="text-accent/70 hover:text-accent">
              <X className="h-3 w-3" />
            </button>
          </span>
          )
        })}
      </div>
    </div>
  )
}

export function StrategyBacktest() {
  const signalNames = useSignalNames()
  const [saved] = useState(() => storage.strategyBacktestLast.get(null))
  const [selectedStrategy, setSelectedStrategy] = useState<string | null>(saved?.selectedStrategy ?? null)
  const [strategyGroup, setStrategyGroup] = useState<StrategyGroup>('all')
  const [symbols, setSymbols] = useState(saved?.symbols ?? '')
  const [assetType, setAssetType] = useState<'stock' | 'etf'>(saved?.assetType ?? 'stock')
  const [start, setStart] = useState(saved?.start ?? THREE_MONTHS_AGO)
  const [end, setEnd] = useState(saved?.end ?? TODAY)
  // 成交口径: 建仓/清仓可独立配置。向后兼容老 matching (派生为 entry=exit=matching)。
  const [matching] = useState<'close_t' | 'open_t+1'>(saved?.matching ?? 'open_t+1')
  const [entryFill, setEntryFill] = useState<'close_t' | 'open_t+1'>(saved?.entryFill ?? saved?.matching ?? 'open_t+1')
  const [exitFill, setExitFill] = useState<'close_t' | 'open_t+1'>(saved?.exitFill ?? saved?.matching ?? 'close_t')
  const [fees, setFees] = useState(saved?.fees ?? '2')
  const [stampTax, setStampTax] = useState(saved?.stampTax ?? '1')
  const [slippage, setSlippage] = useState(saved?.slippage ?? '5')
  const [maxPositions, setMaxPositions] = useState(saved?.maxPositions ?? '10')
  const [maxExposure, setMaxExposure] = useState(saved?.maxExposure ?? '100')
  const [initialCapital, setInitialCapital] = useState(saved?.initialCapital ?? '1000000')
  const [positionSizing, setPositionSizing] = useState<'equal' | 'score_weight'>(saved?.positionSizing ?? 'equal')
  const [simMode, setSimMode] = useState<'position' | 'full'>(saved?.mode ?? 'position')
  const [holdingDays, setHoldingDays] = useState(saved?.holdingDays ?? '5')
  const [settingsOpen, setSettingsOpen] = useState(false)
  // 分钟K精确回测: 用当日分钟K确定精确成交价 (穿越价/VWAP), 需 Pro+ 分钟K能力
  const [highGranularity, setHighGranularity] = useState(false)
  const { data: caps } = useCapabilities()
  const hasMinuteBatch = !!caps?.capabilities?.['kline.minute.batch']
  const [rangeSettingsOpen, setRangeSettingsOpen] = useState(false)
  const [quickRanges, setQuickRanges] = useState(loadQuickRanges)
  const [settingsTab, setSettingsTab] = useState<AdvancedSettingsTab>('params')
  const [editingScoring, setEditingScoring] = useState(false)
  const [scoringDraft, setScoringDraft] = useState<Record<string, number>>({})
  const [strategyParams, setStrategyParams] = useState<Record<string, any>>(saved?.params ?? {})
  const [overrides, setOverrides] = useState<Record<string, any>>(saved?.overrides ?? {})
  // result 不从 localStorage 恢复:它是运行产物(净值/交易),大且易过时,
  // 跨会话/拉新代码后自动渲染一个可能对应已失效策略的旧结果会造成困惑
  // (切页不卸载组件,内存中的 result 仍保留,无需靠 localStorage 恢复)。
  const [result, setResult] = useState<StrategyBacktestResult | null>(null)
  const [resultTab, setResultTab] = useState<'daily' | 'trades' | 'picks'>('daily')
  const [dailyPage, setDailyPage] = useState(0)
  const [tradePage, setTradePage] = useState(0)
  const [tradePageSize, setTradePageSize] = useState(10)
  const [selectedTrade, setSelectedTrade] = useState<StrategyBacktestTrade | null>(null)
  const loadedStrategyRef = useRef<string | null>(null)

  const strategies = useQuery({
    queryKey: QK.screenerStrategies(assetType),
    queryFn: () => api.screenerStrategies(assetType),
  })

  const strategyList = useMemo(() => strategies.data?.presets ?? [], [strategies.data])
  const filteredStrategyList = useMemo(() => (
    strategyGroup === 'all' ? strategyList : strategyList.filter(st => st.source === strategyGroup)
  ), [strategyGroup, strategyList])

  // 校验 localStorage 里保存的上次选中策略是否仍存在(本地开发残留的自定义策略
  // 拉新代码后会失效,导致 strategyGet 一直 404/加载中)。列表就绪后若失效,
  // 连带清除其专属的 params/overrides/result(这些是该策略的运行配置/产物,
  // 策略失效后留着会造成"孤儿"状态:界面显示旧回测结果却无对应策略)。
  useEffect(() => {
    if (strategies.isLoading || strategyList.length === 0) return
    if (selectedStrategy && !strategyList.some(st => st.id === selectedStrategy)) {
      setSelectedStrategy(null)
      setStrategyParams({})
      setOverrides({})
      setResult(null)
    }
  }, [strategies.isLoading, strategyList, selectedStrategy])

  const strategyDetail = useQuery({
    queryKey: QK.strategyDetail(selectedStrategy ?? ''),
    queryFn: () => api.strategyGet(selectedStrategy!),
    enabled: !!selectedStrategy,
  })

  const backtestTask = useBacktestTask()
  const isPending = backtestTask?.isPending ?? false

  const dataStatus = useDataStatus()
  const earliestDate = dataStatus.data?.daily?.earliest_date ?? null

  const resetConfigFromDetail = (detail: StrategyDetail) => {
    setStrategyParams(strategyDefaultParams(detail))
    setOverrides(buildDefaultOverrides(detail))
  }

  // 刷新页面后: 从 localStorage 恢复未完成的回测任务
  useEffect(() => {
    tryReconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    const detail = strategyDetail.data
    if (!detail || loadedStrategyRef.current === detail.id) return
    loadedStrategyRef.current = detail.id
    if (saved?.selectedStrategy === detail.id && (saved.params || saved.overrides)) {
      setStrategyParams(mergeStrategyParams(detail, saved.params))
      setOverrides(saved.overrides ?? buildDefaultOverrides(detail))
      return
    }
    resetConfigFromDetail(detail)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategyDetail.data])

  // 当全局回测任务完成时, 把结果写入组件 (切页回来也能恢复)
  useEffect(() => {
    if (backtestTask && !backtestTask.isPending && backtestTask.result) {
      setResult(backtestTask.result)
      setResultTab('daily')
      setDailyPage(0)
      setTradePage(0)
      storage.strategyBacktestLast.set({
        selectedStrategy,
        symbols,
        assetType,
        start,
        end,
        matching,
        entryFill,
        exitFill,
        fees,
        stampTax,
        slippage,
        maxPositions,
        maxExposure,
        initialCapital,
        positionSizing,
        mode: simMode,
        holdingDays,
        params: strategyParams,
        overrides,
        result: backtestTask.result,
      })
    }
  }, [backtestTask])

  const handleRun = () => {
    if (!selectedStrategy) return
    startBacktest({
      strategy_id: selectedStrategy,
      asset_type: assetType,
      symbols: symbols ? symbols.split(',').map(s => s.trim()).filter(Boolean) : null,
      start: start || null,
      end: end || undefined,
      matching,
      entry_fill: entryFill,
      exit_fill: exitFill,
      commission_pct: Number(fees) / 10000,
      stamp_tax_pct: Number(stampTax) / 1000,
      slippage_bps: Number(slippage),
      max_positions: Number(maxPositions),
      max_exposure_pct: Number(maxExposure) / 100,
      initial_capital: Number(initialCapital),
      position_sizing: positionSizing,
      params: strategyParams,
      overrides,
      mode: simMode,
      holding_days: Number(holdingDays) || 5,
      minute_fill: highGranularity,
    })
  }

  // 提取统计
  const s = result?.stats
  const pick = (...keys: string[]) => {
    for (const k of keys) {
      if (s && k in s && s[k] != null) return s[k]
    }
    return null
  }

  const benchmarkReturn = useMemo(() => {
    const values = (result?.benchmark_curve ?? [])
      .map(r => Number(r.close ?? r.value))
      .filter(v => Number.isFinite(v) && v > 0)
    if (values.length < 2) return null
    return values[values.length - 1] / values[0] - 1
  }, [result?.benchmark_curve])

  const strategyReturn = pick('total_return') as number | null
  const excessReturn = strategyReturn != null && benchmarkReturn != null
    ? strategyReturn - benchmarkReturn
    : null

  const applyRange = (months: number) => {
    setStart(monthsAgo(months))
    setEnd(formatDate(new Date()))
  }

  const applyAllRange = () => {
    setStart(earliestDate ?? '')
    setEnd(formatDate(new Date()))
  }

  // 进入页面/还在加载时就点了"全部": earliestDate 就绪后回填, 让 DatePicker 显示真实起始日
  useEffect(() => {
    if (earliestDate && start === '' && end === TODAY) {
      setStart(earliestDate)
    }
  }, [earliestDate, start, end])

  const applyQuickRange = (range: QuickRangeConfig) => {
    if (range.unit === 'all') {
      applyAllRange()
      return
    }
    applyRange(quickRangeMonths(range))
  }

  const saveQuickRanges = (next: QuickRangeConfig[]) => {
    const normalized = normalizeQuickRanges(next)
    storage.strategyBacktestQuickRanges.set(normalized)
    return normalized
  }

  const updateQuickRange = (id: string, patch: Partial<Pick<QuickRangeConfig, 'enabled' | 'unit' | 'value'>>) => {
    setQuickRanges(prev => {
      const current = prev.find(range => range.id === id)
      if (patch.enabled === false && current?.enabled && prev.filter(range => range.enabled).length <= 1) return prev
      return saveQuickRanges(prev.map(range => range.id === id
        ? normalizeQuickRange({ ...range, ...patch }, range)
        : range
      ))
    })
  }

  const visibleQuickRanges = quickRanges.filter(range => range.enabled)
  const matchedQuickRange = visibleQuickRanges.find(range => range.unit === 'all'
    ? end === TODAY && (start === earliestDate || start === '')
    : end === TODAY && start === monthsAgo(quickRangeMonths(range))
  )
  const rangeKey = matchedQuickRange?.id ?? 'custom'
  const rangeTitle = matchedQuickRange ? quickRangeTitle(matchedQuickRange) : '自定义区间'
  const rangeButtonCls = (key: string) => `rounded-btn px-2 py-1 text-[11px] font-medium transition-colors ${rangeKey === key
    ? 'bg-accent/15 text-accent'
    : 'text-muted hover:bg-elevated/70 hover:text-secondary'
  }`

  const sortedTrades = useMemo(() => {
    return [...(result?.trades ?? [])].sort((a, b) => {
      const exitCmp = String(b.exit_date).localeCompare(String(a.exit_date))
      if (exitCmp !== 0) return exitCmp
      return String(b.entry_date).localeCompare(String(a.entry_date))
    })
  }, [result?.trades])

  const dailyTradeRows = useMemo<DailyTradeRow[]>(() => {
    const rows = new Map<string, Omit<DailyTradeRow, 'cumulativePnl'>>()
    const ensure = (date: string) => {
      if (!rows.has(date)) {
        rows.set(date, { date, buys: [], sells: [], buyValue: 0, sellValue: 0, realizedPnl: 0 })
      }
      return rows.get(date)!
    }

    for (const t of result?.trades ?? []) {
      const entryDate = String(t.entry_date).slice(0, 10)
      const exitDate = String(t.exit_date).slice(0, 10)
      const buyRow = ensure(entryDate)
      buyRow.buys.push(t)
      buyRow.buyValue += Number(t.entry_value ?? 0)

      const sellRow = ensure(exitDate)
      sellRow.sells.push(t)
      sellRow.sellValue += Number(t.exit_value ?? 0)
      sellRow.realizedPnl += Number(t.pnl_amount ?? 0)
    }

    let cumulativePnl = 0
    return [...rows.values()]
      .sort((a, b) => a.date.localeCompare(b.date))
      .map(row => {
        cumulativePnl += row.realizedPnl
        return { ...row, cumulativePnl }
      })
      .reverse()
  }, [result?.trades])

  const tradePageCount = sortedTrades.length
    ? Math.ceil(sortedTrades.length / tradePageSize)
    : 0
  const dailyPageSize = 10
  const dailyPageCount = dailyTradeRows.length
    ? Math.ceil(dailyTradeRows.length / dailyPageSize)
    : 0
  const safeDailyPage = Math.min(dailyPage, Math.max(dailyPageCount - 1, 0))
  const dailyStart = safeDailyPage * dailyPageSize
  const visibleDailyRows = dailyTradeRows.slice(dailyStart, dailyStart + dailyPageSize)
  const dailyEnd = Math.min(dailyStart + visibleDailyRows.length, dailyTradeRows.length)
  const safeTradePage = Math.min(tradePage, Math.max(tradePageCount - 1, 0))
  const tradeStart = safeTradePage * tradePageSize
  const visibleTrades = sortedTrades.slice(tradeStart, tradeStart + tradePageSize)
  const tradeEnd = Math.min(tradeStart + visibleTrades.length, sortedTrades.length)
  const symbolNames = useMemo(() => {
    const names: Record<string, string> = {}
    result?.trades.forEach(t => {
      if (t.name) names[t.symbol] = t.name
    })
    return names
  }, [result?.trades])

  const detail = strategyDetail.data
  const basicFilter = (overrides.basic_filter ?? {}) as Record<string, any>
  const entrySignals = (overrides.entry_signals ?? []) as string[]
  const exitSignals = (overrides.exit_signals ?? []) as string[]

  const scoring = useMemo(() => (overrides.scoring ?? {}) as Record<string, number>, [overrides.scoring])
  const scoreMinValue = overrides.score_min == null ? '' : String(overrides.score_min)
  const scoreMaxValue = overrides.score_max == null ? '' : String(overrides.score_max)
  const stopLossPct = overrides.stop_loss == null ? '' : String(Math.abs(Number(overrides.stop_loss)) * 100)
  const takeProfitPct = overrides.take_profit == null ? '' : String(Math.abs(Number(overrides.take_profit)) * 100)
  const trailingStopPct = overrides.trailing_stop == null ? '' : String(Math.abs(Number(overrides.trailing_stop)) * 100)
  const trailingTakeProfitActivatePct = overrides.trailing_take_profit_activate == null ? '' : String(Math.abs(Number(overrides.trailing_take_profit_activate)) * 100)
  const trailingTakeProfitDrawdownPct = overrides.trailing_take_profit_drawdown == null ? '' : String(Math.abs(Number(overrides.trailing_take_profit_drawdown)) * 100)
  const maxHoldDaysValue = overrides.max_hold_days == null ? '' : String(overrides.max_hold_days)
  const targetPositionPct = Number(maxPositions) > 0 ? Number(maxExposure) / Number(maxPositions) : 0

  useEffect(() => {
    if (!editingScoring) setScoringDraft(scoringToPct(scoring))
  }, [scoring, editingScoring])

  const updateOverride = (key: string, value: any) => {
    setOverrides(prev => ({ ...prev, [key]: value }))
  }
  const updateBasicFilter = (key: string, value: any) => {
    updateOverride('basic_filter', { ...basicFilter, [key]: value })
  }
  const startScoringEdit = () => {
    setScoringDraft(scoringToPct(scoring))
    setEditingScoring(true)
  }
  const cancelScoringEdit = () => {
    setScoringDraft(scoringToPct(scoring))
    setEditingScoring(false)
  }
  const saveScoringDraft = () => {
    updateOverride('scoring', normalizePctWeights(scoringDraft))
    setEditingScoring(false)
  }
  const scoreFilterSummary = scoreMinValue !== '' && scoreMaxValue !== ''
    ? `评分 ${scoreMinValue}~${scoreMaxValue}`
    : scoreMinValue !== ''
      ? `评分 ≥${scoreMinValue}`
      : scoreMaxValue !== ''
        ? `评分 ≤${scoreMaxValue}`
        : '评分不过滤'
  const advancedSummary = detail
    ? [
        detail.params.length > 0 ? `参数 ${detail.params.length}` : '无策略参数',
        basicFilter.enabled !== false ? '过滤开' : '过滤关',
        `买点 ${entrySignals.length}`,
        `卖点 ${exitSignals.length}`,
        scoreFilterSummary,
        stopLossPct !== '' ? `止损 ${stopLossPct}%` : '止损未设',
        takeProfitPct !== '' ? `止盈 ${takeProfitPct}%` : '止盈未设',
        trailingStopPct !== '' ? `移损 ${trailingStopPct}%` : '移损未设',
        trailingTakeProfitActivatePct !== '' && trailingTakeProfitDrawdownPct !== '' ? `回撤 ${trailingTakeProfitActivatePct}-${trailingTakeProfitDrawdownPct}点` : '回撤未设',
        maxHoldDaysValue !== '' ? `最长 ${maxHoldDaysValue}天` : '不限持仓',
      ].join(' · ')
    : '选择策略后可调整参数 / 过滤 / 买卖触发器 / 评分 / 风控'
  const selectedStrategyName = detail?.name ?? strategyList.find(st => st.id === selectedStrategy)?.name ?? '未选择策略'
  const selectedStrategySource = detail?.source ?? strategyList.find(st => st.id === selectedStrategy)?.source
  const stockPoolCount = symbols.split(',').map(s => s.trim()).filter(Boolean).length
  const stockPoolSummary = stockPoolCount > 0 ? `股票池 已限定 ${stockPoolCount} 只` : '股票池 全市场'
  const resultStartDate = result?.config?.start ?? result?.equity_curve?.[0]?.date ?? start
  const resultEndDate = result?.config?.end ?? result?.equity_curve?.[result.equity_curve.length - 1]?.date ?? end
  const resultTradeDays = result?.equity_curve?.length ?? 0
  const executionStats = (result?.stats?.execution ?? {}) as Record<string, number>
  const executionSummary = [
    ['buy_no_slot', '满仓未买'],
    ['buy_exposure', '仓位上限'],
    ['buy_score_filter', '评分过滤'],
    ['buy_limit_up', '涨停未买'],
    ['buy_suspended', '停牌未买'],
    ['sell_limit_down', '跌停阻塞'],
    ['sell_suspended', '停牌阻塞'],
    ['pending_exit', '待卖阻塞'],
  ]
    .map(([key, label]) => ({ key, label, value: Number(executionStats[key] ?? 0) }))
    .filter(item => item.value > 0)

  return (
    <div className="h-full min-h-0 overflow-hidden rounded-card border border-border bg-surface/80 grid grid-cols-1 xl:grid-cols-[18rem_minmax(0,1fr)]">
      {/* 配置面板 */}
      <section className="space-y-3 border-b xl:border-b-0 xl:border-r border-border bg-base/25 px-3 py-3 xl:overflow-y-auto">
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-xs font-medium text-secondary">选择策略</label>
            {/* 分钟K精确回测 */}
            <div className="flex items-center gap-1">
              <Gauge className={`h-3 w-3 ${highGranularity ? 'text-amber-400' : 'text-muted/50'}`} />
              <button
                onClick={() => { if (!hasMinuteBatch) return; setHighGranularity(v => !v) }}
                disabled={!hasMinuteBatch}
                title={!hasMinuteBatch
                  ? '分钟K精确回测：需 Pro+ 权限 (分钟K批量)'
                  : '分钟K精确回测：用当日分钟K确定精确成交价（穿越价/VWAP），比收盘价更真实。⚠️ 回测速度会变慢。'
                }
                className={`group relative inline-flex h-3.5 w-6 items-center rounded-full shrink-0 transition-colors duration-200 ${
                  !hasMinuteBatch ? 'bg-elevated opacity-50 cursor-not-allowed'
                  : highGranularity ? 'bg-amber-500 cursor-pointer'
                  : 'bg-elevated cursor-pointer'
                }`}
              >
                <span className={`inline-block h-2.5 w-2.5 rounded-full bg-white shadow-sm transition-transform duration-200 ${
                  highGranularity ? 'translate-x-[13px]' : 'translate-x-0.5'
                }`} />
              </button>
              <span className={`text-[9px] font-medium ${highGranularity ? 'text-amber-400' : 'text-muted/50'}`}>分钟K</span>
              {!hasMinuteBatch && (
                <span className="text-[8px] text-accent/70 font-medium bg-accent/10 px-1 py-px rounded">Pro+</span>
              )}
            </div>
          </div>
          {/* 分钟K开启时的提示条 */}
          {highGranularity && hasMinuteBatch && (
            <div className="mb-2 flex items-start gap-1.5 rounded-btn border border-amber-400/30 bg-amber-400/5 px-2 py-1.5">
              <Zap className="h-3 w-3 text-amber-400 shrink-0 mt-px" />
              <div className="text-[10px] leading-snug text-amber-400/90">
                <span className="font-medium">分钟K精确回测</span>
                ：信号触发日用当日分钟K确定成交价（均线类信号按穿越价, 其他按 VWAP 均价）。需本地有足够的分钟K历史, 回测速度会变慢。
              </div>
            </div>
          )}
          <div className="overflow-hidden rounded-input border border-border bg-surface">
            <div className="flex border-b border-border/60 bg-base/30 p-0.5">
              {STRATEGY_GROUPS.map(group => (
                <button
                  key={group.id}
                  type="button"
                  onClick={() => setStrategyGroup(group.id)}
                  className={`flex-1 rounded-[6px] px-1.5 py-1 text-[10px] font-medium transition-colors ${strategyGroup === group.id
                    ? 'bg-accent/15 text-accent shadow-sm'
                    : 'text-muted hover:bg-elevated/70 hover:text-secondary'
                  }`}
                >
                  {group.label}
                </button>
              ))}
            </div>
            <div className="flex max-h-[128px] flex-wrap gap-1 overflow-y-auto p-1">
            {strategies.isLoading && (
              <span className="text-xs text-muted px-2 py-1">加载中…</span>
            )}
            {!strategies.isLoading && filteredStrategyList.length === 0 && (
              <span className="text-xs text-muted px-2 py-1">当前分组暂无策略</span>
            )}
            {filteredStrategyList.map(st => (
              <button
                key={st.id}
                onClick={() => setSelectedStrategy(st.id)}
                className={`px-2 py-1 rounded-btn text-[11px] border transition-all duration-150 ease-smooth cursor-pointer
                  ${selectedStrategy === st.id
                    ? 'border-accent/50 bg-accent/10 text-accent shadow-[0_0_10px_rgba(59,130,246,0.1)]'
                    : 'border-border bg-base text-secondary hover:border-accent/40'
                  }`}
              >
                <span className="font-medium">{st.name}</span>
                {st.source && st.source !== 'builtin' && (
                  <span className={`ml-1 text-[8px] px-1 py-px rounded border ${BADGE_CLS_MAP[st.source] ?? ''}`}>
                    {SRC_MAP[st.source] ?? ''}
                  </span>
                )}
              </button>
            ))}
            </div>
          </div>
        </div>

        {selectedStrategy && strategyDetail.isLoading && (
          <div className="rounded-btn border border-border bg-surface px-2.5 py-2 text-xs text-muted">加载策略配置…</div>
        )}

        <button
          type="button"
          onClick={() => detail && setSettingsOpen(true)}
          disabled={!detail || strategyDetail.isLoading}
          className="group w-full rounded-btn border border-border bg-surface px-3 py-2.5 text-left transition-colors hover:border-accent/40 hover:bg-elevated/70 disabled:cursor-not-allowed disabled:opacity-55"
        >
          <span className="flex items-center gap-2 text-xs font-semibold text-foreground">
            <SlidersHorizontal className="h-3.5 w-3.5 text-accent" />
            策略设置
            <span className="ml-auto text-[10px] font-normal text-muted group-hover:text-accent">编辑</span>
          </span>
          <span className="mt-1 flex min-w-0 items-center gap-1.5 text-[11px] font-medium text-secondary">
            <span className="truncate">{selectedStrategyName}</span>
            {selectedStrategySource && (
              <span className={`shrink-0 text-[8px] px-1 py-px rounded border ${BADGE_CLS_MAP[selectedStrategySource] ?? ''}`}>
                {SRC_MAP[selectedStrategySource] ?? selectedStrategySource}
              </span>
            )}
          </span>
          <span className="mt-1 block text-[10px] font-medium text-secondary">{stockPoolSummary}</span>
          <span className="mt-1 block text-[10px] leading-4 text-muted">{advancedSummary}</span>
        </button>

        <div className="rounded-btn border border-border bg-surface p-2.5">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-1.5">
              <div className="text-xs font-medium text-foreground">回测区间</div>
              <WarmupBadge />
            </div>
            <span className="shrink-0 rounded-full border border-accent/25 bg-accent/10 px-2 py-0.5 text-[10px] font-medium text-accent">
              {rangeTitle}
            </span>
          </div>

          <div className="mt-2 grid grid-cols-2 gap-2">
            <div>
              <label className="text-[11px] text-secondary block mb-1">开始</label>
              <DatePicker
                value={start}
                onChange={setStart}
                max={end || undefined}
                placeholder="全部历史"
                className="w-full"
                buttonClassName="w-full justify-start"
                align="left"
              />
            </div>
            <div>
              <label className="text-[11px] text-secondary block mb-1">结束</label>
              <DatePicker
                value={end}
                onChange={setEnd}
                min={start || undefined}
                className="w-full"
                buttonClassName="w-full justify-start"
              />
            </div>
          </div>

          <div className="mt-2 flex items-center gap-1">
            <div className="flex min-w-0 flex-1 rounded-input bg-base/60 p-0.5">
              {visibleQuickRanges.map(range => (
                <button
                  key={range.id}
                  type="button"
                  onClick={() => applyQuickRange(range)}
                  className={`${rangeButtonCls(range.id)} flex-1`}
                >
                  {quickRangeLabel(range)}
                </button>
              ))}
            </div>
            <button
              type="button"
              onClick={() => setRangeSettingsOpen(v => !v)}
              title="设置快捷区间"
              aria-label="设置快捷区间"
              className={`shrink-0 rounded-btn border px-2 py-1.5 transition-colors ${rangeSettingsOpen
                ? 'border-accent/40 bg-accent/10 text-accent'
                : 'border-border bg-base text-secondary hover:border-accent/40 hover:text-accent'
              }`}
            >
              <SlidersHorizontal className="h-3.5 w-3.5" />
            </button>
          </div>

          {rangeSettingsOpen && (
            <div className="mt-2 rounded-input border border-border/60 bg-base/50 p-2">
              <div className="mb-1.5 flex items-center justify-between gap-2 text-[10px] text-muted">
                <span>快捷区间</span>
                <span>月 1-120 / 年 1-10</span>
              </div>
              <div className="space-y-1.5">
                {quickRanges.map((range, index) => {
                  const limits = range.unit === 'all' ? null : QUICK_RANGE_LIMITS[range.unit]
                  return (
                    <div key={range.id} className="grid grid-cols-[3rem_1fr_4.5rem] items-center gap-1.5">
                      <label className="flex items-center gap-1 text-[11px] text-secondary">
                        <input
                          type="checkbox"
                          checked={range.enabled}
                          onChange={e => updateQuickRange(range.id, { enabled: e.target.checked })}
                          className="h-3 w-3 accent-accent"
                        />
                        {index + 1}
                      </label>
                      <select
                        value={range.unit}
                        onChange={e => updateQuickRange(range.id, { unit: e.target.value as QuickRangeUnit })}
                        className={INPUT_CLS}
                      >
                        <option value="month">月</option>
                        <option value="year">年</option>
                        <option value="all">全部</option>
                      </select>
                      <input
                        type="number"
                        min={limits?.min}
                        max={limits?.max}
                        disabled={range.unit === 'all'}
                        value={range.unit === 'all' ? '' : range.value}
                        onChange={e => updateQuickRange(range.id, { value: Number(e.target.value) })}
                        placeholder="—"
                        className={`${INPUT_CLS} ${range.unit === 'all' ? 'opacity-50' : ''}`}
                      />
                    </div>
                  )
                })}
              </div>
            </div>
          )}
        </div>

        <div className="grid grid-cols-2 gap-2">
          <div>
            <div className="flex items-center gap-1 mb-1.5">
              <label className="text-xs font-medium text-secondary">建仓口径</label>
              <FillRuleHint />
            </div>
            <select value={entryFill} onChange={e => setEntryFill(e.target.value as any)} className={INPUT_CLS}>
              <option value="open_t+1">次日开盘成交（推荐）</option>
              <option value="close_t">信号日收盘成交</option>
            </select>
          </div>
          <div>
            <label className="text-xs font-medium text-secondary block mb-1.5">清仓口径</label>
            <select value={exitFill} onChange={e => setExitFill(e.target.value as any)} className={INPUT_CLS}>
              <option value="close_t">到期/信号日收盘成交（推荐）</option>
              <option value="open_t+1">次日开盘成交</option>
            </select>
          </div>
        </div>

        {simMode === 'position' && (
        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="text-xs font-medium text-secondary block mb-1.5">初始资金</label>
            <input type="number" value={initialCapital} onChange={e => setInitialCapital(e.target.value)}
              className={INPUT_CLS} />
          </div>
          <div>
            <label className="text-xs font-medium text-secondary block mb-1.5">买入权重</label>
            <select value={positionSizing} onChange={e => setPositionSizing(e.target.value as any)} className={INPUT_CLS}>
              <option value="equal">等权买入</option>
              <option value="score_weight">评分加权</option>
            </select>
          </div>
          <div>
            <label className="text-xs font-medium text-secondary block mb-1.5">最大持仓数</label>
            <input type="number" value={maxPositions} onChange={e => setMaxPositions(e.target.value)}
              className={INPUT_CLS} />
          </div>
          <div>
            <label className="text-xs font-medium text-secondary block mb-1.5">最大总仓位(%)</label>
            <input type="number" min={0} max={100} value={maxExposure} onChange={e => setMaxExposure(e.target.value)}
              className={INPUT_CLS} />
          </div>
        </div>
        )}
        {simMode === 'position' && (
        <div className="grid grid-cols-3 gap-2">
          <div>
            <label className="text-[10px] font-medium text-secondary block mb-1">佣金 ‱</label>
            <input type="number" min={0} value={fees} onChange={e => setFees(e.target.value)} className={INPUT_CLS} />
          </div>
          <div>
            <label className="text-[10px] font-medium text-secondary block mb-1">印花税 ‰</label>
            <input type="number" min={0} value={stampTax} onChange={e => setStampTax(e.target.value)} className={INPUT_CLS} />
          </div>
          <div>
            <label className="text-[10px] font-medium text-secondary block mb-1">滑点 ‱</label>
            <input type="number" min={0} value={slippage} onChange={e => setSlippage(e.target.value)} className={INPUT_CLS} />
          </div>
        </div>
        )}
        {simMode === 'position' && (
        <div className="text-[10px] leading-4 text-muted">
          单票目标约 {Number.isFinite(targetPositionPct) ? targetPositionPct.toFixed(1) : '—'}%。最大总仓位控制资金投入；剩余现金不是新增持仓名额，只有实际卖出成功才释放持仓数。
        </div>
        )}
        {simMode === 'full' && (
        <div className="rounded-btn border border-accent/20 bg-accent/5 px-3 py-2.5 text-[11px] leading-relaxed text-secondary">
          <span className="font-medium text-foreground">全量模拟</span>：每日将策略选出的全部候选独立买入，不受资金/最大持仓数限制；每一笔仍按策略卖点、止损、移动止盈/止损和最长持仓执行，用于评估策略本身的选股 + 交易规则质量。
        </div>
        )}

        {isPending ? (
          <button
            onClick={stopBacktest}
            className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-2 rounded-btn
              bg-danger/15 border border-danger/40 text-sm font-medium text-danger hover:bg-danger/25
              transition-colors duration-150 ease-smooth"
          >
            <Square className="h-3.5 w-3.5 fill-current" />
            停止回测
          </button>
        ) : (
          <button
            onClick={handleRun}
            disabled={!selectedStrategy || strategyDetail.isLoading}
            className="group w-full inline-flex items-center justify-center gap-2.5 rounded-btn border border-accent/40
              bg-gradient-to-r from-accent to-blue-500 px-3 py-2.5 text-white shadow-[0_10px_24px_rgba(59,130,246,0.22)]
              transition-all duration-150 ease-smooth hover:-translate-y-0.5 hover:shadow-[0_14px_28px_rgba(59,130,246,0.28)]
              disabled:translate-y-0 disabled:cursor-not-allowed disabled:opacity-50 disabled:shadow-none"
          >
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-white/18 ring-1 ring-white/25 transition-transform group-hover:scale-105">
              <Play className="h-3.5 w-3.5 translate-x-px fill-current" />
            </span>
            <span className="text-sm font-semibold tracking-wide">运行回测</span>
          </button>
        )}
      </section>

      {/* 结果面板 */}
      <section className="min-w-0 space-y-3 bg-base/15 px-3 py-3 xl:overflow-y-auto">
        {/* 模式切换: 仓位模拟 / 全量模拟 */}
        <div className="flex items-center justify-between gap-2">
          <div className="inline-flex rounded-btn border border-border bg-surface/80 p-0.5 shadow-sm">
            {([['position', '仓位模拟'], ['full', '全量模拟']] as const).map(([val, label]) => (
              <button
                key={val}
                onClick={() => setSimMode(val)}
                className={`inline-flex items-center gap-1.5 rounded-[5px] px-3 py-1.5 text-xs font-medium transition-colors cursor-pointer ${
                  simMode === val
                    ? 'bg-accent text-white shadow-sm'
                    : 'text-secondary hover:bg-elevated hover:text-foreground'
                }`}
                title={val === 'position' ? '受仓位/资金约束的真实账户模拟' : '全部候选独立执行，不受资金和持仓数量约束'}
              >
                {val === 'position' ? <Play className="h-3.5 w-3.5" /> : <BarChart3 className="h-3.5 w-3.5" />}
                {label}
              </button>
            ))}
          </div>
          {simMode === 'full' && (
            maxHoldDaysValue !== '' ? (
              <div className="rounded-btn border border-border bg-surface px-2 py-1 text-[11px] text-secondary">
                策略最长 <span className="font-mono text-foreground">{maxHoldDaysValue}</span> 天
              </div>
            ) : (
              <div className="flex items-center gap-1.5 text-[11px] text-secondary">
                <span>兜底上限</span>
                <div className="flex rounded-btn border border-border overflow-hidden">
                  {(['1', '5', '10', '20'] as const).map(d => (
                    <button
                      key={d}
                      onClick={() => setHoldingDays(d)}
                      className={`px-2 py-1 text-[11px] font-medium transition-colors cursor-pointer ${
                        holdingDays === d
                          ? 'bg-accent/10 text-accent'
                          : 'text-muted hover:text-secondary hover:bg-elevated'
                      }`}
                    >
                      {d}天
                    </button>
                  ))}
                </div>
              </div>
            )
          )}
        </div>

        {result?.error && (
          <div className="text-sm text-danger bg-danger/10 border border-danger/30 rounded-btn px-3 py-2">
            {result.error}
          </div>
        )}

        {backtestTask?.error && (
          <div className="text-sm text-danger bg-danger/10 border border-danger/30 rounded-btn px-3 py-2">
            {backtestTask.error}
          </div>
        )}

        {!result && !isPending && (
          <EmptyState
            icon={FlaskConical}
            title="选择策略并开始回测"
            hint="策略回测复用策略定义 ( 买入/卖出触发器、止损、最大持仓 ) 做全周期模拟。服务器建议优先使用最近3个月；长周期建议本机或 8GB 以上内存环境运行。"
          />
        )}

        {isPending && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            className="rounded-card border border-accent/40 bg-accent/10 px-4 py-2.5"
          >
            <div className="flex items-center gap-2.5">
              <span className="relative flex h-4 w-4 shrink-0">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent/50" />
                <Loader2 className="relative h-4 w-4 animate-spin text-accent" />
              </span>
              <div className="min-w-0">
                <div className={backtestTask?.reconnecting ? 'text-xs font-medium text-warning' : 'text-xs font-medium text-accent'}>
                  {backtestTask?.reconnecting
                    ? '连接中断，重试中…'
                    : backtestTask?.progress
                      ? `回测中 · 第 ${backtestTask.progress.day}/${backtestTask.progress.total} 天 (${backtestTask.progress.date})`
                      : '正在重新计算回测…'}
                </div>
                <div className="mt-0.5 text-[11px] text-secondary">
                  {backtestTask?.reconnecting
                    ? '正在尝试恢复连接，若持续失败可停止后重试'
                    : result ? '当前展示上次结果，完成后自动替换' : '正在加载回测数据…'}
                </div>
              </div>
              {backtestTask?.progress && (
                <span className="ml-auto shrink-0 font-mono text-sm font-semibold text-accent">
                  {((backtestTask.progress.day / backtestTask.progress.total) * 100).toFixed(0)}%
                </span>
              )}
              <button
                type="button"
                onClick={stopBacktest}
                className="inline-flex shrink-0 items-center gap-1 rounded-btn border border-danger/40 bg-danger/10 px-2 py-1 text-[11px] text-danger transition-colors hover:bg-danger/20"
              >
                <Square className="h-3 w-3 fill-current" />
                停止
              </button>
            </div>
            {backtestTask?.progress && (
              <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-base/60">
                <div
                  className="h-full rounded-full bg-accent transition-all duration-300 ease-out"
                  style={{ width: `${(backtestTask.progress.day / backtestTask.progress.total) * 100}%` }}
                />
              </div>
            )}
          </motion.div>
        )}

        {/* 旧全量模拟结果: 固定前瞻收益统计 (兼容历史缓存结果) */}
        {result && !result.error && result.stats && result.stats.mode === 'full' && result.stats.full_kind !== 'candidate_execution' && (
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
            className="space-y-4"
          >
            <div className="flex items-center gap-3">
              <span className="text-sm font-medium text-foreground">{result.strategy_info?.name ?? '策略'}</span>
              <span className="text-[10px] px-1 py-px rounded border border-accent/30 bg-accent/10 text-accent">全量模拟</span>
              <span className="text-[10px] text-secondary">持有 {result.config?.holding_days ?? 5} 天</span>
              <span className="ml-auto text-[11px] text-muted font-mono">
                {String(result.config?.start).slice(0,10)} ~ {String(result.config?.end).slice(0,10)}
              </span>
            </div>

            {/* 统计卡片 */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
              <Stat label="平均收益" value={fmtPct(result.stats.avg_return)} color={statValueColor(result.stats.avg_return)} />
              <Stat label="中位数" value={fmtPct(result.stats.median_return)} color={statValueColor(result.stats.median_return)} />
              <Stat label="胜率" value={fmtPct(result.stats.win_rate)} color={statValueColor(result.stats.win_rate)} />
              <Stat label="盈亏比" value={result.stats.profit_factor != null ? Number(result.stats.profit_factor).toFixed(2) : '—'} />
              <Stat label="超额(vs基准)" value={fmtPct(result.stats.excess)} color={statValueColor(result.stats.excess)} />
              <Stat label="夏普" value={result.stats.sharpe != null ? Number(result.stats.sharpe).toFixed(2) : '—'} />
              <Stat label="最大回撤" value={fmtPct(result.stats.max_drawdown)} color={statValueColor(result.stats.max_drawdown)} />
              <Stat label="累计收益" value={fmtPct(result.stats.total_return)} color={statValueColor(result.stats.total_return)} />
            </div>

            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted">
              <span>候选样本 <b className="text-foreground num">{result.stats.n_candidates ?? 0}</b> (标的×信号日)</span>
              <span>信号天数 <b className="text-foreground num">{result.stats.n_days ?? 0}</b></span>
              <span>日均候选 <b className="text-foreground num">{result.stats.avg_daily_candidates ?? 0}</b></span>
              <span>最佳 <b className="text-bull num">{fmtPct(result.stats.best)}</b></span>
              <span>最差 <b className="text-bear num">{fmtPct(result.stats.worst)}</b></span>
              <span>基准(上证) <b className="text-foreground num">{fmtPct(result.stats.benchmark_return)}</b></span>
            </div>

            {/* 累计超额曲线 (复用 StrategyNavChart) */}
            {result.equity_curve.length > 1 && (
              <div className="rounded-card border border-border p-3">
                <div className="mb-2 text-xs font-medium text-secondary">累计收益曲线(日均复利)</div>
                <StrategyNavChart result={result} />
              </div>
            )}

            {/* 收益分布直方图 */}
            {Array.isArray(result.stats.return_distribution) && result.stats.return_distribution.length > 0 && (
              <div className="rounded-card border border-border p-3">
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-xs font-medium text-secondary">候选标的收益分布(持有 {result.config?.holding_days ?? 5} 天)</span>
                  <span className="text-[10px] text-muted">红=正收益 · 绿=负收益</span>
                </div>
                <ReturnDistributionChart distribution={result.stats.return_distribution} />
              </div>
            )}

            <div className="text-[11px] text-muted">run_id: {result.run_id}</div>
          </motion.div>
        )}

        {result && !result.error && result.stats && !result.stats.error && (result.stats.mode !== 'full' || result.stats.full_kind === 'candidate_execution') && (
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
            className="space-y-4"
          >
            {/* 策略信息 */}
            {result.strategy_info && (
              <div className="flex items-center gap-3">
                <div className="flex items-center gap-1.5">
                  <span className="text-sm font-medium text-foreground">{result.strategy_info.name}</span>
                  {result.stats.full_kind === 'candidate_execution' && (
                    <span className="text-[9px] px-1 py-px rounded border border-accent/30 bg-accent/10 text-accent">全量独立执行</span>
                  )}
                  {result.strategy_info.source && (
                    <span className={`text-[9px] px-1 py-px rounded border ${BADGE_CLS_MAP[result.strategy_info.source] ?? ''}`}>
                      {SRC_MAP[result.strategy_info.source] ?? ''}
                    </span>
                  )}
                </div>
                {result.strategy_info.stop_loss != null && (
                  <span className="text-[10px] text-secondary">止损 {fmtPct(result.strategy_info.stop_loss)}</span>
                )}
                {result.strategy_info.take_profit != null && (
                  <span className="text-[10px] text-secondary">止盈 {fmtPct(result.strategy_info.take_profit)}</span>
                )}
                {result.strategy_info.trailing_stop != null && (
                  <span className="text-[10px] text-secondary">移损 {fmtPct(result.strategy_info.trailing_stop)}</span>
                )}
                {result.strategy_info.trailing_take_profit_activate != null && result.strategy_info.trailing_take_profit_drawdown != null && (
                  <span className="text-[10px] text-secondary">回撤 {fmtPct(result.strategy_info.trailing_take_profit_activate)}-{fmtPct(result.strategy_info.trailing_take_profit_drawdown)}</span>
                )}
                {result.strategy_info.max_hold_days != null && (
                  <span className="text-[10px] text-secondary">最长 {result.strategy_info.max_hold_days} 天</span>
                )}
                {resultTradeDays > 0 && (
                  <span className="ml-auto flex items-center gap-2 text-[11px] text-muted">
                    <span className="font-mono">{String(resultStartDate).slice(0, 10)} ~ {String(resultEndDate).slice(0, 10)}</span>
                    <span>{resultTradeDays} 天</span>
                  </span>
                )}
                {result.elapsed_ms > 0 && (
                  <span className={`flex items-center gap-1 text-[11px] text-muted ${resultTradeDays > 0 ? '' : 'ml-auto'}`}>
                    <Clock className="h-3 w-3" />
                    <span>总耗时</span>
                    <span className="num">{fmtDuration(result.elapsed_ms)}</span>
                  </span>
                )}
              </div>
            )}

            {/* 统计卡片 */}
            <div className="rounded-card border border-border bg-surface p-4">
              <div className="grid grid-cols-[repeat(auto-fit,minmax(9rem,1fr))] gap-3">
                <Stat label="总收益" value={strategyReturn != null ? fmtPct(strategyReturn) : '—'}
                  color={statValueColor(strategyReturn)} />
                <Stat label="年化" value={pick('annual_return') != null ? fmtPct(pick('annual_return') as number) : '—'}
                  color={statValueColor(pick('annual_return') as number)} />
                <Stat label="同期上证" value={benchmarkReturn != null ? fmtPct(benchmarkReturn) : '—'}
                  color={statValueColor(benchmarkReturn)} />
                <Stat label="超额收益" value={excessReturn != null ? fmtPct(excessReturn) : '—'}
                  color={statValueColor(excessReturn)} />
                <Stat label={<SharpeLabel />} value={pick('sharpe') != null ? Number(pick('sharpe')).toFixed(2) : '—'} />
                <Stat label="索提诺" value={pick('sortino') != null ? Number(pick('sortino')).toFixed(2) : '—'} />
                <Stat label="最大回撤" value={pick('max_drawdown') != null ? fmtPct(pick('max_drawdown') as number) : '—'}
                  color="#34d399" />
                <Stat label="蒙卡回撤(中位)" value={pick('mc_maxdd_p50') != null ? fmtPct(pick('mc_maxdd_p50') as number) : '—'}
                  color="#34d399" />
                <Stat label="蒙卡回撤(95%置信不差于此)" value={pick('mc_maxdd_p95') != null ? fmtPct(pick('mc_maxdd_p95') as number) : '—'}
                  color="#34d399" />
                <Stat label="胜率" value={pick('win_rate') != null ? fmtPct(pick('win_rate') as number) : '—'} />
                <Stat label="交易数" value={pick('n_trades') != null ? String(pick('n_trades')) : '—'} />
                {result.stats.full_kind === 'candidate_execution' ? (
                  <Stat label="平均持仓" value={pick('avg_duration') != null ? `${Number(pick('avg_duration')).toFixed(1)}天` : '—'} />
                ) : (
                  <Stat label="最终权益" value={pick('final_equity') != null ? fmtPrice(pick('final_equity') as number) : '—'} />
                )}
              </div>
            </div>

            {executionSummary.length > 0 && (
              <div className="rounded-card border border-amber-400/25 bg-amber-400/5 px-3 py-2 text-[11px] leading-5 text-secondary">
                <span className="font-medium text-amber-300">成交约束：</span>
                {executionSummary.map((item, index) => (
                  <span key={item.key} className="ml-2">
                    {index > 0 ? '· ' : ''}{item.label} <span className="font-mono text-foreground">{item.value}</span> 次
                  </span>
                ))}
              </div>
            )}

            {/* 净值曲线 */}
            {result.equity_curve.length > 0 && (
              <div className="rounded-card border border-border overflow-hidden">
                <StrategyNavChart result={result} />
              </div>
            )}

            {Array.isArray(result.stats.return_distribution) && result.stats.return_distribution.length > 0 && (
              <div className="rounded-card border border-border p-3">
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-xs font-medium text-secondary">独立候选交易收益分布</span>
                  <span className="text-[10px] text-muted">红=正收益 · 绿=负收益</span>
                </div>
                <ReturnDistributionChart distribution={result.stats.return_distribution} />
              </div>
            )}

            {/* Tab: 按日期 / 交易明细 / 选股分析 */}
            {(result.trades.length > 0 || result.per_symbol_stats.length > 0) && (
              <div className="rounded-card border border-border overflow-hidden">
                <div className="flex items-center gap-1 border-b border-border px-4 pt-2">
                  {(['daily', 'trades', 'picks'] as const).map(t => (
                    <button
                      key={t}
                      onClick={() => setResultTab(t)}
                      className={`px-3 py-1.5 text-xs font-medium border-b-2 transition-colors cursor-pointer ${
                        resultTab === t
                          ? 'border-accent text-accent'
                          : 'border-transparent text-secondary hover:text-foreground'
                      }`}
                    >
                      {t === 'daily'
                        ? `每日交易 (${dailyTradeRows.length})`
                        : t === 'trades'
                          ? `交易明细 (${sortedTrades.length})`
                          : `选股分析 (${result.per_symbol_stats.length})`}
                    </button>
                  ))}
                </div>

                {resultTab === 'daily' && (
                  <div>
                    <div className="overflow-x-auto">
                    <table className="w-full min-w-[960px] text-sm text-foreground">
                      <thead className="bg-elevated">
                        <tr className="text-left text-secondary">
                          <th className="px-3 py-2.5 font-medium w-[8.5rem]">日期</th>
                          <th className="px-3 py-2.5 font-medium">买入</th>
                          <th className="px-3 py-2.5 font-medium">卖出</th>
                          <th className="px-3 py-2.5 font-medium text-right w-[8rem]">当日收益</th>
                          <th className="px-3 py-2.5 font-medium text-right w-[8rem]">累计收益</th>
                        </tr>
                      </thead>
                      <tbody>
                        {visibleDailyRows.map(row => (
                          <tr key={row.date} className="border-t border-border hover:bg-elevated/50 transition-colors">
                            <td className="px-3 py-2.5 whitespace-nowrap">
                              <div className="font-mono text-foreground">{row.date}</div>
                              <div className="mt-0.5 text-[11px] text-muted">
                                买 {row.buys.length} / 卖 {row.sells.length}
                              </div>
                            </td>
                            <td className="px-3 py-2.5">
                              {row.buys.length === 0 ? (
                                <span className="text-muted">—</span>
                              ) : (
                                <div className="flex flex-wrap gap-1.5">
                                  {row.buys.map((t, i) => (
                                    <DailyTradeChip key={`buy-${t.symbol}-${t.entry_date}-${t.exit_date}-${i}`} trade={t} side="buy" strategyName={result?.strategy_info?.name ?? selectedStrategyName} onClick={() => setSelectedTrade(t)} signalNames={signalNames} />
                                  ))}
                                </div>
                              )}
                            </td>
                            <td className="px-3 py-2.5">
                              {row.sells.length === 0 ? (
                                <span className="text-muted">—</span>
                              ) : (
                                <div className="flex flex-wrap gap-1.5">
                                  {row.sells.map((t, i) => (
                                    <DailyTradeChip key={`sell-${t.symbol}-${t.entry_date}-${t.exit_date}-${i}`} trade={t} side="sell" onClick={() => setSelectedTrade(t)} signalNames={signalNames} />
                                  ))}
                                </div>
                              )}
                            </td>
                            <td className={`px-3 py-2.5 text-right num font-semibold whitespace-nowrap ${priceColorClass(row.realizedPnl)}`}>
                              {fmtSignedMoney(row.realizedPnl)}
                            </td>
                            <td className={`px-3 py-2.5 text-right num font-semibold whitespace-nowrap ${priceColorClass(row.cumulativePnl)}`}>
                              {fmtSignedMoney(row.cumulativePnl)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    </div>
                    {dailyTradeRows.length > 0 && (
                      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-4 py-2 text-xs text-muted">
                        <span>
                          显示 {dailyStart + 1}-{dailyEnd} 天 / 共 {dailyTradeRows.length} 天，每页 10 天
                        </span>
                        <div className="flex flex-wrap items-center gap-2">
                          <button
                            type="button"
                            onClick={() => setDailyPage(p => Math.max(0, p - 1))}
                            disabled={safeDailyPage <= 0}
                            className="rounded-btn border border-border bg-surface px-2.5 py-1 text-xs text-secondary transition-colors hover:border-accent/40 hover:text-accent disabled:cursor-not-allowed disabled:opacity-45"
                          >
                            上一页
                          </button>
                          <span className="num text-secondary">
                            {safeDailyPage + 1} / {dailyPageCount}
                          </span>
                          <button
                            type="button"
                            onClick={() => setDailyPage(p => Math.min(dailyPageCount - 1, p + 1))}
                            disabled={safeDailyPage >= dailyPageCount - 1}
                            className="rounded-btn border border-border bg-surface px-2.5 py-1 text-xs text-secondary transition-colors hover:border-accent/40 hover:text-accent disabled:cursor-not-allowed disabled:opacity-45"
                          >
                            下一页
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                )}

                {resultTab === 'trades' && (
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[960px] text-sm text-foreground">
                      <thead className="bg-elevated">
                        <tr className="text-left text-secondary">
                          <th className="px-4 py-2.5 font-medium">标的</th>
                          <th className="px-4 py-2.5 font-medium">买入</th>
                          <th className="px-4 py-2.5 font-medium">卖出</th>
                          <th className="px-4 py-2.5 font-medium text-right">仓位 / 手数</th>
                          <th className="px-4 py-2.5 font-medium text-right">单票盈亏</th>
                          <th className="px-4 py-2.5 font-medium text-right">持仓</th>
                          <th className="px-4 py-2.5 font-medium">原因</th>
                        </tr>
                      </thead>
                      <tbody>
                        {visibleTrades.map((t: StrategyBacktestTrade, i: number) => (
                          <tr key={`${t.symbol}-${t.entry_date}-${tradeStart + i}`} className="border-t border-border hover:bg-elevated/50 transition-colors group">
                            <td className="px-4 py-2.5">
                              <div className="font-medium text-foreground group-hover:text-accent transition-colors">
                                {t.name || t.symbol}
                              </div>
                              <div className="mt-0.5 font-mono text-[11px] text-muted">{t.symbol}</div>
                            </td>
                            <td className="px-4 py-2.5">
                              <TradeLegCell trade={t} side="buy" signalNames={signalNames} />
                            </td>
                            <td className="px-4 py-2.5">
                              <TradeLegCell trade={t} side="sell" signalNames={signalNames} />
                            </td>
                            <td className="px-4 py-2.5 text-right">
                              <div className="num text-foreground">{fmtPct(t.position_pct, 2)}</div>
                              <div className="mt-0.5 text-[11px] text-muted">
                                <span className="num">{fmtLots(t.lots)}</span> 手
                                <span className="ml-1 num">{fmtShares(t.shares)}</span> 股
                              </div>
                            </td>
                            <td className={`px-4 py-2.5 text-right num ${priceColorClass(t.pnl_amount ?? t.pnl_pct)}`}>
                              <div>{fmtSignedMoney(t.pnl_amount)}</div>
                              <div className="mt-0.5 text-[11px]">{fmtPct(t.pnl_pct)}</div>
                            </td>
                            <td className="px-4 py-2.5 text-right num text-secondary">
                              <div>{t.duration} 天</div>
                              {!!t.blocked_exit_days && <div className="mt-0.5 text-[11px] text-amber-400">阻塞 {t.blocked_exit_days} 天</div>}
                            </td>
                            <td className="px-4 py-2.5"><ExitReasonBadge reason={t.exit_reason} signalId={t.exit_signal_id} signalNames={signalNames} /></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {sortedTrades.length > 0 && (
                      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-4 py-2 text-xs text-muted">
                        <span>
                          显示 {tradeStart + 1}-{tradeEnd} 条 / 共 {sortedTrades.length} 条
                        </span>
                        <div className="flex flex-wrap items-center gap-2">
                          <label className="flex items-center gap-1.5">
                            <span>每页</span>
                            <select
                              value={tradePageSize}
                              onChange={e => {
                                setTradePageSize(Number(e.target.value))
                                setTradePage(0)
                              }}
                              className="rounded-btn border border-border bg-surface px-2 py-1 text-xs text-secondary focus:outline-none focus:border-accent"
                            >
                              {TRADE_PAGE_SIZE_OPTIONS.map(size => (
                                <option key={size} value={size}>{size}</option>
                              ))}
                            </select>
                            <span>条</span>
                          </label>
                          <button
                            type="button"
                            onClick={() => setTradePage(p => Math.max(0, p - 1))}
                            disabled={safeTradePage <= 0}
                            className="rounded-btn border border-border bg-surface px-2.5 py-1 text-xs text-secondary transition-colors hover:border-accent/40 hover:text-accent disabled:cursor-not-allowed disabled:opacity-45"
                          >
                            上一页
                          </button>
                          <span className="num text-secondary">
                            {safeTradePage + 1} / {tradePageCount}
                          </span>
                          <button
                            type="button"
                            onClick={() => setTradePage(p => Math.min(tradePageCount - 1, p + 1))}
                            disabled={safeTradePage >= tradePageCount - 1}
                            className="rounded-btn border border-border bg-surface px-2.5 py-1 text-xs text-secondary transition-colors hover:border-accent/40 hover:text-accent disabled:cursor-not-allowed disabled:opacity-45"
                          >
                            下一页
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                )}

                {resultTab === 'picks' && (
                  <table className="w-full text-sm">
                    <thead className="bg-elevated">
                      <tr className="text-left text-secondary">
                        <th className="px-4 py-2.5 font-medium">标的</th>
                        <th className="px-4 py-2.5 font-medium text-right">选股次数</th>
                        <th className="px-4 py-2.5 font-medium text-right">总收益</th>
                        <th className="px-4 py-2.5 font-medium text-right">胜率</th>
                        <th className="px-4 py-2.5 font-medium text-right">最佳</th>
                        <th className="px-4 py-2.5 font-medium text-right">最差</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.per_symbol_stats.map((r) => (
                        <tr key={r.symbol} className="border-t border-border hover:bg-elevated/50 transition-colors group">
                          <td className="px-4 py-2">
                            <div className="font-medium text-foreground group-hover:text-accent transition-colors">
                              {symbolNames[r.symbol] || r.symbol}
                            </div>
                            <div className="mt-0.5 font-mono text-[11px] text-muted">{r.symbol}</div>
                          </td>
                          <td className="px-4 py-2 text-right num">{r.n_trades}</td>
                          <td className={`px-4 py-2 text-right num ${priceColorClass(r.total_return)}`}>
                            {fmtPct(r.total_return)}
                          </td>
                          <td className="px-4 py-2 text-right num">{fmtPct(r.win_rate)}</td>
                          <td className="px-4 py-2 text-right num text-bull">{fmtPct(r.best)}</td>
                          <td className="px-4 py-2 text-right num text-bear">{fmtPct(r.worst)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}

            <div className="text-[11px] text-muted">
              run_id: {result.run_id}
            </div>
          </motion.div>
        )}
      </section>

      {settingsOpen && detail && (
        <>
          <motion.button
            type="button"
            aria-label="关闭高级策略设置"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            onClick={() => setSettingsOpen(false)}
            className="fixed inset-0 z-50 bg-black/45 backdrop-blur-[1px]"
          />
          <motion.aside
            initial={{ x: 32, opacity: 0 }}
            animate={{ x: 0, opacity: 1 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className="fixed inset-y-0 right-0 z-[60] flex w-full max-w-3xl flex-col border-l border-border bg-base shadow-2xl"
          >
            <div className="border-b border-border px-4 py-3">
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-semibold text-foreground">高级策略设置</span>
                    <span className={`text-[9px] px-1 py-px rounded border ${BADGE_CLS_MAP[detail.source] ?? ''}`}>
                      {SRC_MAP[detail.source] ?? ''}
                    </span>
                  </div>
                  <div className="mt-1 truncate text-xs text-secondary">{detail.name}</div>
                  <div className="mt-0.5 text-[10px] leading-4 text-muted">{advancedSummary}</div>
                </div>
                <button
                  type="button"
                  onClick={() => setSettingsOpen(false)}
                  className="rounded-btn border border-border bg-surface p-1.5 text-muted transition-colors hover:border-accent/40 hover:text-foreground"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              <div className="mt-3 flex gap-1 overflow-x-auto">
                {ADVANCED_TABS.map(tab => (
                  <button
                    key={tab.id}
                    type="button"
                    onClick={() => setSettingsTab(tab.id)}
                    className={`shrink-0 rounded-btn border px-3 py-1.5 text-xs transition-colors ${settingsTab === tab.id
                      ? 'border-accent/50 bg-accent/10 text-accent'
                      : 'border-border bg-surface text-secondary hover:border-accent/40 hover:text-foreground'
                    }`}
                  >
                    {tab.label}
                  </button>
                ))}
              </div>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
              <div className="mb-4 rounded-btn border border-accent/25 bg-accent/5 px-3 py-2.5 text-[11px] leading-5 text-secondary">
                <div className="font-medium text-foreground">触发 / 成交 / 仓位关系</div>
                <div className="mt-1">触发器决定什么时候产生买卖信号；评分只在多个买点同时出现时排序。</div>
                <div>成交口径可分别设置建仓/清仓：默认建仓次日开盘（避免未来函数）、清仓当日收盘（持仓中可盘中/收盘卖）。</div>
                <div>退出优先级：止损/移动止损 &gt; 卖点信号 &gt; 到期平仓；到期只作兜底，不抢占卖点或风控。</div>
                <div>最大持仓数控制同时持股数量，最大总仓位控制资金投入比例；剩余现金不等于可新增持仓名额。</div>
              </div>

              {settingsTab === 'range' && (
                <ConfigSection title="回测范围">
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-muted">资产类型</span>
                    <div className="inline-flex h-8 rounded-btn border border-border overflow-hidden">
                      {(['stock', 'etf'] as const).map(t => (
                        <button
                          key={t}
                          type="button"
                          onClick={() => { setAssetType(t); setSelectedStrategy(null); setSymbols('') }}
                          className={`h-full px-3 text-xs font-medium transition-colors cursor-pointer
                            ${assetType === t ? 'bg-accent/10 text-accent' : 'text-muted hover:text-foreground'}`}
                        >
                          {t === 'stock' ? '股票' : 'ETF'}
                        </button>
                      ))}
                    </div>
                    <span className="text-[11px] text-muted/70">ETF 仅技术类策略,读 ETF enriched</span>
                  </div>
                  <StockPoolPicker value={symbols} onChange={setSymbols} assetType={assetType} />
                  <div className="text-[11px] leading-5 text-muted">默认全市场回测，由基础过滤、策略条件和买卖触发器筛选；需要单票调试或自选池回测时再限定股票池。</div>
                </ConfigSection>
              )}

              {settingsTab === 'params' && (
                <ConfigSection title="策略参数" hint="自动限制 min/max">
                  {detail.params.length > 0 ? (
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                      {detail.params.map(param => (
                        <StrategyParamInput
                          key={param.id}
                          param={param}
                          value={strategyParams[param.id]}
                          onChange={value => setStrategyParams(prev => ({ ...prev, [param.id]: value }))}
                        />
                      ))}
                    </div>
                  ) : (
                    <div className="text-xs text-muted">当前策略没有可调参数。</div>
                  )}
                </ConfigSection>
              )}

              {settingsTab === 'filter' && (
                <ConfigSection title="基础过滤" hint="用于候选池">
                  <label className="flex items-center gap-2 text-xs text-secondary">
                    <input
                      type="checkbox"
                      checked={basicFilter.enabled !== false}
                      onChange={e => updateBasicFilter('enabled', e.target.checked)}
                    />
                    启用基础过滤
                  </label>
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    {BASIC_FILTER_FIELDS.map(field => {
                      const scale = field.scale ?? 1
                      const value = basicFilter[field.key] == null ? '' : Number(basicFilter[field.key]) / scale
                      return (
                        <label key={field.key} className="block">
                          <span className="mb-1 block text-[11px] text-secondary">{field.label}({field.unit})</span>
                          <input
                            type="number"
                            value={value}
                            min={0}
                            step={field.unit === '%' ? 0.1 : 0.01}
                            onChange={e => {
                              const n = numOrNull(e.target.value)
                              updateBasicFilter(field.key, n == null ? null : n * scale)
                            }}
                            className={INPUT_CLS}
                          />
                        </label>
                      )
                    })}
                  </div>
                  <label className="flex items-center gap-2 text-xs text-secondary">
                    <input
                      type="checkbox"
                      checked={!!basicFilter.exclude_st}
                      onChange={e => updateBasicFilter('exclude_st', e.target.checked)}
                    />
                    排除 ST / 退市
                  </label>
                  <div className="flex flex-wrap gap-1.5">
                    {BOARD_OPTIONS.map(board => {
                      const boards = Array.isArray(basicFilter.boards) ? basicFilter.boards : []
                      const checked = boards.includes(board)
                      return (
                        <button
                          key={board}
                          type="button"
                          onClick={() => updateBasicFilter('boards', checked ? boards.filter((b: string) => b !== board) : [...boards, board])}
                          className={`rounded-btn border px-2.5 py-1.5 text-[11px] transition-colors ${checked ? 'border-accent/50 bg-accent/10 text-accent' : 'border-border bg-base text-muted hover:border-accent/40'}`}
                        >
                          {board}
                        </button>
                      )
                    })}
                  </div>
                </ConfigSection>
              )}

              {settingsTab === 'entry' && (
                <ConfigSection
                  title="入场触发器"
                  hint="任一入场点满足即可进入候选"
                  actions={<SignalTriggerActions kind="entry" signals={entrySignals} onChange={next => updateOverride('entry_signals', next)} />}
                >
                  <SignalPicker
                    signals={entrySignals}
                    onChange={next => updateOverride('entry_signals', next)}
                    kind="entry"
                  />
                </ConfigSection>
              )}

              {settingsTab === 'exit' && (
                <ConfigSection
                  title="出场触发器"
                  hint="任一出场点满足即触发出场"
                  actions={<SignalTriggerActions kind="exit" signals={exitSignals} onChange={next => updateOverride('exit_signals', next)} />}
                >
                  <SignalPicker
                    signals={exitSignals}
                    onChange={next => updateOverride('exit_signals', next)}
                    kind="exit"
                  />
                </ConfigSection>
              )}

              {settingsTab === 'scoring' && (
                <ConfigSection title="评分权重" hint="临时拖动滑块，保存时统一归权">
                  {Object.entries(scoring).length > 0 ? (() => {
                    const visibleWeights = editingScoring ? scoringDraft : scoringToPct(scoring)
                    const total = Object.values(visibleWeights).reduce((a, b) => a + b, 0)
                    return (
                      <div className="space-y-3">
                        <div className="space-y-2">
                          {Object.keys(scoring).map(key => (
                            <ScoringWeightRow
                              key={key}
                              name={key}
                              weight={visibleWeights[key] ?? 0}
                              pct={visibleWeights[key] ?? 0}
                              editing={editingScoring}
                              onChange={value => setScoringDraft(prev => ({ ...prev, [key]: Math.max(0, value) }))}
                            />
                          ))}
                        </div>
                        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/40 pt-2">
                          <div className="text-[10px] text-muted">
                            总和 <span className={`font-mono text-xs font-medium ${editingScoring && total !== 100 ? 'text-amber-400' : 'text-emerald-400'}`}>{editingScoring ? total : 100}</span>
                            <span className="ml-1 text-muted/70">保存时自动归一化计算</span>
                          </div>
                          <div className="flex items-center gap-2">
                            {editingScoring && (
                              <button
                                type="button"
                                onClick={cancelScoringEdit}
                                className="rounded-btn border border-border bg-base px-2.5 py-1 text-[11px] text-secondary transition-colors hover:border-accent/40 hover:text-foreground"
                              >
                                取消
                              </button>
                            )}
                            <button
                              type="button"
                              onClick={editingScoring ? saveScoringDraft : startScoringEdit}
                              className="rounded-btn border border-amber-400/40 bg-amber-400/10 px-2.5 py-1 text-[11px] text-amber-400 transition-colors hover:bg-amber-400/15"
                            >
                              {editingScoring ? '保存归权' : '调整权重'}
                            </button>
                          </div>
                        </div>
                      </div>
                    )
                  })() : (
                    <div className="text-xs text-muted">当前策略没有评分权重。</div>
                  )}
                  <div className="border-t border-border/40 pt-3">
                    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                      <span className="text-[11px] font-medium text-secondary">评分过滤</span>
                      <span className="text-[10px] text-muted">留空 = 不过滤；命中范围后按评分从高到低买入</span>
                    </div>
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                      <label className="block">
                        <span className="mb-1 block text-[11px] text-secondary">最小评分</span>
                        <input
                          type="number"
                          value={scoreMinValue}
                          min={0}
                          max={100}
                          step={1}
                          placeholder="不限"
                          onChange={e => {
                            const n = numOrNull(e.target.value)
                            updateOverride('score_min', n == null ? null : clamp(n, 0, 100))
                          }}
                          className={INPUT_CLS}
                        />
                      </label>
                      <label className="block">
                        <span className="mb-1 block text-[11px] text-secondary">最大评分</span>
                        <input
                          type="number"
                          value={scoreMaxValue}
                          min={0}
                          max={100}
                          step={1}
                          placeholder="不限"
                          onChange={e => {
                            const n = numOrNull(e.target.value)
                            updateOverride('score_max', n == null ? null : clamp(n, 0, 100))
                          }}
                          className={INPUT_CLS}
                        />
                      </label>
                    </div>
                    <div className="mt-2 text-[10px] leading-4 text-muted">例如最小值 71 表示只把评分 ≥ 71 的股票放入下一交易日买入预选池。</div>
                  </div>
                </ConfigSection>
              )}

              {settingsTab === 'risk' && (
                <ConfigSection title="风控">
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <label className="block">
                      <span className="mb-1 block text-[11px] text-secondary">止损(%)</span>
                      <input
                        type="number"
                        value={stopLossPct}
                        min={0}
                        max={99}
                        step={0.5}
                        onChange={e => {
                          const n = numOrNull(e.target.value)
                          updateOverride('stop_loss', n == null ? null : -Math.abs(n) / 100)
                        }}
                        className={INPUT_CLS}
                      />
                    </label>
                    <label className="block">
                      <span className="mb-1 block text-[11px] text-secondary">止盈(%)</span>
                      <input
                        type="number"
                        value={takeProfitPct}
                        min={1}
                        max={500}
                        step={0.5}
                        onChange={e => {
                          const n = numOrNull(e.target.value)
                          updateOverride('take_profit', n == null ? null : clamp(Math.abs(n), 1, 500) / 100)
                        }}
                        className={INPUT_CLS}
                      />
                    </label>
                    <label className="block">
                      <span className="mb-1 block text-[11px] text-secondary">移动止损(%)</span>
                      <input
                        type="number"
                        value={trailingStopPct}
                        min={0.5}
                        max={50}
                        step={0.5}
                        onChange={e => {
                          const n = numOrNull(e.target.value)
                          updateOverride('trailing_stop', n == null ? null : -clamp(Math.abs(n), 0.5, 50) / 100)
                        }}
                        className={INPUT_CLS}
                      />
                    </label>
                    <label className="block">
                      <span className="mb-1 block text-[11px] text-secondary">回撤止盈启动(%)</span>
                      <input
                        type="number"
                        value={trailingTakeProfitActivatePct}
                        min={1}
                        max={200}
                        step={0.5}
                        onChange={e => {
                          const n = numOrNull(e.target.value)
                          const next = n == null ? null : clamp(Math.abs(n), 1, 200) / 100
                          updateOverride('trailing_take_profit_activate', next)
                          const drawdown = numOrNull(trailingTakeProfitDrawdownPct)
                          if (next != null && drawdown != null && drawdown / 100 > next) {
                            updateOverride('trailing_take_profit_drawdown', next)
                          }
                        }}
                        className={INPUT_CLS}
                      />
                    </label>
                    <label className="block">
                      <span className="mb-1 block text-[11px] text-secondary">回撤止盈回撤(点)</span>
                      <input
                        type="number"
                        value={trailingTakeProfitDrawdownPct}
                        min={0.5}
                        max={50}
                        step={0.5}
                        onChange={e => {
                          const n = numOrNull(e.target.value)
                          const activate = numOrNull(trailingTakeProfitActivatePct)
                          const maxValue = activate == null ? 50 : Math.min(50, Math.abs(activate))
                          updateOverride('trailing_take_profit_drawdown', n == null ? null : clamp(Math.abs(n), 0.5, maxValue) / 100)
                        }}
                        className={INPUT_CLS}
                      />
                    </label>
                    <label className="block">
                      <span className="mb-1 block text-[11px] text-secondary">最长持仓(天)</span>
                      <input
                        type="number"
                        value={maxHoldDaysValue}
                        min={1}
                        step={1}
                        onChange={e => {
                          const n = numOrNull(e.target.value)
                          updateOverride('max_hold_days', n == null ? null : Math.max(1, Math.round(n)))
                        }}
                        className={INPUT_CLS}
                      />
                    </label>
                  </div>
                </ConfigSection>
              )}
            </div>

            <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
              <button
                type="button"
                onClick={() => resetConfigFromDetail(detail)}
                className="rounded-btn border border-border bg-surface px-3 py-1.5 text-xs text-secondary transition-colors hover:border-accent/40 hover:text-accent"
              >
                恢复默认
              </button>
              <button
                type="button"
                onClick={() => setSettingsOpen(false)}
                className="rounded-btn bg-accent px-4 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent/90"
              >
                完成
              </button>
            </div>
          </motion.aside>
        </>
      )}

      <TradeKlineModal trade={selectedTrade} onClose={() => setSelectedTrade(null)} />
    </div>
  )
}
