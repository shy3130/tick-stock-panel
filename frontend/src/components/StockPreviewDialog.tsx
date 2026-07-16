import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence, useReducedMotion } from 'framer-motion'
import { X, RefreshCw, Clock, Zap } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cnSignal } from '@/lib/signals'
import { StockPanel, getDefaultRange } from '@/components/StockPanel'
import { DatePicker } from '@/components/DatePicker'
import { Modal } from '@/components/Modal'
import { RuleEditor } from '@/components/monitor/RuleEditor'
import { usePreferences, useQuoteStatus } from '@/lib/useSharedQueries'
import { setFocusSymbol, clearFocusSymbol } from '@/lib/useQuoteStream'

interface Props {
  symbol: string | null
  name?: string
  onClose: () => void
  /** 触发信息 (来自监控触发记录, 有值时在顶栏下方显示) */
  triggerInfo?: {
    price?: number | null
    changePct?: number | null
    ts?: number
    signals?: string[]
    message?: string
  } | null
}

// ===== 板块标识（与 Screener 列表一致）=====

// 预设快捷范围（只保留半年和1年）
const PRESETS: { label: string; months: number }[] = [
  { label: '半年', months: 6 },
  { label: '1年', months: 12 },
]

function boardTag(symbol: string): { label: string; color: string } | null {
  if (/^(300|301)/.test(symbol)) return { label: '创', color: 'text-[#f97316] bg-[#f97316]/12 border-[#f97316]/25' }
  if (/^688/.test(symbol))       return { label: '科', color: 'text-purple-400 bg-purple-400/12 border-purple-400/25' }
  if (/^[48]/.test(symbol))      return { label: '北', color: 'text-cyan-400 bg-cyan-400/12 border-cyan-400/25' }
  return null
}

function PreviewHeaderActions({ onRefresh, onClose }: { onRefresh: () => void; onClose: () => void }) {
  return (
    <div className="flex shrink-0 items-center gap-1">
      <button
        type="button"
        onClick={onRefresh}
        className="flex h-8 w-8 items-center justify-center rounded-btn text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        title="刷新"
        aria-label="刷新行情"
      >
        <RefreshCw className="h-3.5 w-3.5" />
      </button>
      <button
        type="button"
        onClick={onClose}
        className="flex h-8 w-8 items-center justify-center rounded-btn text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        aria-label="关闭个股预览"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  )
}

export function StockPreviewDialog({ symbol, name, onClose, triggerInfo }: Props) {
  const [showIntraday, setShowIntraday] = useState(false)
  const [dateRange, setDateRange] = useState(getDefaultRange)
  const [showMonitorEditor, setShowMonitorEditor] = useState(false)
  const reduceMotion = useReducedMotion()
  const qc = useQueryClient()

  const watchlist = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
    enabled: !!symbol,
  })
  const inWatchlist = (watchlist.data?.symbols ?? []).some((s: any) => s.symbol === symbol)

  const toggleWatchlist = useMutation({
    mutationFn: () => inWatchlist ? api.watchlistRemove(symbol!) : api.watchlistAdd(symbol!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.watchlist })
      qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })
    },
  })

  // 焦点股票注册: SSE quotes_updated 推送时精准 invalidate 当前股票日K,
  // 让对话框日K最后一根蜡烛随实时价变化 (后端只读内存, 不调 TickFlow)。
  // 关闭/切股时清除, 避免无谓刷新。
  useEffect(() => {
    if (!symbol) return
    setFocusSymbol(symbol)
    return () => clearFocusSymbol()
  }, [symbol])

  // 分时图实时轮询: 复用自选列表的「分时刷新开关 + 间隔」偏好。
  // 仅实时行情运行 且 用户开启分时刷新时才轮询; 否则 undefined (定格)。
  const { data: prefs } = usePreferences()
  const { data: quoteStatus } = useQuoteStatus()
  const realtimeRunning = quoteStatus?.running ?? false
  const intradayRefreshOn = prefs?.minute_intraday_refresh ?? false
  const intradayRefetchMs = (intradayRefreshOn && realtimeRunning)
    ? (prefs?.minute_intraday_refresh_interval ?? 6) * 1000
    : undefined
  const handleRefresh = () => {
    if (!symbol) return
    qc.invalidateQueries({ queryKey: ['kline', symbol!] })
    if (showIntraday) {
      qc.invalidateQueries({ queryKey: ['kline-minute', symbol!] })
    }
  }

  return (
    <AnimatePresence>
      {symbol && (
        <Modal
          onClose={onClose}
          labelledBy="stock-preview-title"
          overlayClassName="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
          panelClassName="w-[92vw] max-w-[1100px]"
        >
          <motion.div
            initial={reduceMotion ? false : { opacity: 0, scale: 0.95, y: 12 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={reduceMotion ? undefined : { opacity: 0, scale: 0.97, y: 8 }}
            transition={reduceMotion ? { duration: 0 } : { duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className="relative flex max-h-[95vh] w-full flex-col overflow-hidden rounded-card border border-border bg-base shadow-2xl"
          >
            {/* 顶栏 */}
            <div className="flex shrink-0 flex-col gap-2 border-b border-border px-3 py-2 md:flex-row md:items-center md:justify-between md:gap-3 md:px-5 md:py-3">
              <div className="flex min-w-0 items-center justify-between gap-2 md:contents">
              <h2 id="stock-preview-title" className="flex min-w-0 items-center gap-2">
                {(() => {
                  const board = symbol ? boardTag(symbol) : null
                  return board ? (
                    <span className={`inline-flex items-center justify-center w-[18px] h-[18px] rounded text-[9px] font-bold leading-none border ${board.color}`}>
                      {board.label}
                    </span>
                  ) : null
                })()}
                <span className="shrink-0 font-mono text-sm font-medium text-foreground">{symbol}</span>
                {name && <span className="truncate text-xs text-muted">{name}</span>}
              </h2>
                <div className="md:hidden">
                  <PreviewHeaderActions onRefresh={handleRefresh} onClose={onClose} />
                </div>
              </div>

              <div className="flex min-w-0 items-center gap-1.5">
                <div className="flex min-w-0 flex-1 items-center gap-1.5 overflow-x-auto pb-1 md:flex-none md:overflow-visible md:pb-0">
                {/* 日期范围快捷 */}
                {PRESETS.map(p => {
                  const now = new Date()
                  const s = new Date(now)
                  s.setMonth(s.getMonth() - p.months)
                  const expected = s.toISOString().slice(0, 10)
                  const isActive = dateRange.start === expected
                  return (
                    <button
                      type="button"
                      key={p.label}
                      onClick={() => {
                        const end = new Date().toISOString().slice(0, 10)
                        const ns = new Date()
                        ns.setMonth(ns.getMonth() - p.months)
                        setDateRange({ start: ns.toISOString().slice(0, 10), end })
                      }}
                      className={`h-7 shrink-0 whitespace-nowrap rounded px-1.5 text-[11px] transition-colors cursor-pointer
                        ${isActive
                          ? 'bg-accent/20 text-accent font-medium border border-accent/30'
                          : 'text-muted hover:text-foreground hover:bg-elevated border border-transparent'
                        }`}
                    >
                      {p.label}
                    </button>
                  )
                })}
                <DatePicker
                  value={dateRange.start}
                  onChange={(v) => setDateRange(prev => ({ ...prev, start: v }))}
                  max={dateRange.end}
                  className="shrink-0"
                />
                <span className="shrink-0 text-[10px] text-muted/40">~</span>
                <DatePicker
                  value={dateRange.end}
                  onChange={(v) => setDateRange(prev => ({ ...prev, end: v }))}
                  min={dateRange.start}
                  className="shrink-0"
                />

                <span className="mx-0.5 shrink-0 text-muted/20">|</span>

                {/* 分时开关 */}
                <button
                  type="button"
                  onClick={() => setShowIntraday((v) => !v)}
                  className={`inline-flex h-7 shrink-0 items-center gap-1 whitespace-nowrap rounded px-2 text-xs transition-colors ${
                    showIntraday
                      ? 'bg-accent/15 text-accent border border-accent/30'
                      : 'bg-elevated text-secondary border border-border hover:border-accent/30'
                  }`}
                >
                  <Clock className="h-3 w-3" />
                  分时
                </button>
                </div>
                <div className="hidden md:block">
                  <PreviewHeaderActions onRefresh={handleRefresh} onClose={onClose} />
                </div>
              </div>
            </div>

            {/* 触发信息条 (来自监控触发记录) */}
            {triggerInfo && (
              <div className="flex items-center gap-4 border-b border-amber-400/20 bg-amber-400/[0.06] px-5 py-2 shrink-0">
                {/* 左: 触发标记 + 时间 */}
                <div className="flex items-center gap-2 shrink-0">
                  <span className="inline-flex items-center gap-1 text-[10px] font-semibold text-amber-400"><Zap className="h-3 w-3" />触发</span>
                  {triggerInfo.ts && (
                    <span className="text-[11px] text-secondary font-mono">
                      {new Date(triggerInfo.ts).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}
                    </span>
                  )}
                </div>

                {/* 中: 价格 + 涨跌幅 */}
                <div className="flex items-center gap-2 shrink-0">
                  {triggerInfo.price != null && (
                    <span className="text-[11px] font-mono text-foreground/80">{triggerInfo.price.toFixed(2)}</span>
                  )}
                  {triggerInfo.changePct != null && (
                    <span className={`text-[11px] font-mono font-medium ${triggerInfo.changePct >= 0 ? 'text-danger' : 'text-bear'}`}>
                      {triggerInfo.changePct >= 0 ? '+' : ''}{(triggerInfo.changePct * 100).toFixed(2)}%
                    </span>
                  )}
                </div>

                {/* 右: 消息 + 信号标签 */}
                <div className="flex items-center gap-2 flex-wrap min-w-0">
                  {triggerInfo.message && (
                    <span className="text-[11px] text-foreground/70 truncate">{triggerInfo.message}</span>
                  )}
                  {triggerInfo.signals && triggerInfo.signals.length > 0 && (
                    <div className="flex items-center gap-1 flex-wrap">
                      {triggerInfo.signals.map((s, j) => (
                        <span key={j} className="rounded bg-accent/10 px-1.5 py-0.5 text-[9px] text-accent/80">{cnSignal(s)}</span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* K 线内容 */}
            <div className="flex-1 overflow-auto p-4">
              <StockPanel
                symbol={symbol}
                height={420}
                showIntraday={showIntraday}
                onSelectDate={() => { if (!showIntraday) setShowIntraday(true) }}
                dateRange={dateRange}
                onMonitor={() => setShowMonitorEditor(true)}
                inWatchlist={inWatchlist}
                onToggleWatchlist={() => toggleWatchlist.mutate()}
                refetchIntervalMs={intradayRefetchMs}
              />
            </div>

            {/* 加监控编辑器弹层 */}
            <AnimatePresence>
              {showMonitorEditor && symbol && (
                <motion.div
                  initial={reduceMotion ? false : { opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={reduceMotion ? undefined : { opacity: 0 }}
                  transition={{ duration: reduceMotion ? 0 : 0.15 }}
                  className="absolute inset-0 z-20 flex items-start justify-center overflow-auto bg-black/40 p-4"
                  onClick={() => setShowMonitorEditor(false)}
                >
                  <div className="mt-8 w-full max-w-2xl" onClick={e => e.stopPropagation()}>
                    <RuleEditor
                      rule={null}
                      simple
                      preset={{
                        scope: 'symbols',
                        symbols: [symbol],
                        type: 'signal',
                        logic: 'or',
                      }}
                      onClose={() => setShowMonitorEditor(false)}
                      onSaved={() => setShowMonitorEditor(false)}
                    />
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </motion.div>
        </Modal>
      )}
    </AnimatePresence>
  )
}
