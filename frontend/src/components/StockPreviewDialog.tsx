import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { ChevronLeft, ChevronRight, Clock, RefreshCw, X } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cnSignal } from '@/lib/signals'
import { StockPanel, getDefaultRange } from '@/components/StockPanel'
import { DatePicker } from '@/components/DatePicker'
import { RuleEditor } from '@/components/monitor/RuleEditor'
import { usePreferences, useQuoteStatus } from '@/lib/useSharedQueries'
import { setFocusSymbol, clearFocusSymbol } from '@/lib/useQuoteStream'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'
import { splitTags } from '@/lib/tags'
import { boardTag } from '@/components/stock-table/primitives'

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
  /** 有序候选列表: 提供后支持左右键/顶栏按钮切股, 标题栏显示 n/N */
  navList?: NavItem[]
  /** 切股回调: 收到目标 symbol/name, 由调用方更新预览状态 */
  onNavigate?: (symbol: string, name?: string) => void
}

/** 切股导航列表项 */
export interface NavItem { symbol: string; name?: string }

/** 把 symbol+name 的列表转成切股导航列表项 (统一 name 归一化为 undefined, 免去各处重复 map + as 断言) */
export function toNavItems<T extends { symbol: string; name?: string | null }>(xs: T[]): NavItem[] {
  return xs.map(x => ({ symbol: x.symbol, name: x.name ?? undefined }))
}

/** 榜单里同一标的可能多次出现 (多概念/行业 leader、监控重复触发), 去重以免切股/计数空跳; 保留首次出现。 */
function uniqueNavItems(xs: NavItem[]): NavItem[] {
  const seen = new Set<string>()
  const out: NavItem[] = []
  for (const n of xs) {
    if (seen.has(n.symbol)) continue
    seen.add(n.symbol)
    out.push(n)
  }
  return out
}

/** 首↔尾循环的索引换算: go(delta) 与 邻近预取 共用, 保证换行规则单源 */
function wrapNavIndex(navIdx: number, delta: number, navTotal: number): number {
  return (navIdx + delta + navTotal) % navTotal
}

// 预设快捷范围（只保留半年和1年）
const PRESETS: { label: string; months: number }[] = [
  { label: '半年', months: 6 },
  { label: '1年', months: 12 },
]

export function StockPreviewDialog({ symbol, name, onClose, triggerInfo, navList: navListSource, onNavigate }: Props) {
  const [showIntraday, setShowIntraday] = useState(false)
  const [dateRange, setDateRange] = useState(getDefaultRange)
  const [showMonitorEditor, setShowMonitorEditor] = useState(false)
  const qc = useQueryClient()
  const backdrop = useDialogBackdrop(onClose)

  const watchlist = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
    enabled: !!symbol,
  })
  const watchlistEntry = (watchlist.data?.symbols ?? []).find(s => s.symbol === symbol)
  const inWatchlist = !!watchlistEntry
  const tags = splitTags(watchlistEntry?.tags)

  const toggleWatchlist = useMutation({
    mutationFn: () => inWatchlist ? api.watchlistRemove(symbol!) : api.watchlistAdd(symbol!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.watchlist })
      qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })
    },
  })

  // ===== 切股导航 =====
  const navList = useMemo(() => uniqueNavItems(navListSource ?? []), [navListSource])

  // 当前 symbol 在 navList 中的位置 (不在列表则为 -1, 此时不显示计数/按钮)
  const navIdx = navList.findIndex(n => n.symbol === symbol)
  const navTotal = navList.length
  const navEnabled = navTotal >= 2 && navIdx >= 0

  // 邻近预取目标: 当前股左右相邻两只 (首↔尾循环), 交由 StockPanel 提前拉取日K/财务指标缓存
  const prefetchSymbols = useMemo(() => {
    if (!navEnabled) return []
    return [
      navList[wrapNavIndex(navIdx, -1, navTotal)].symbol,
      navList[wrapNavIndex(navIdx, 1, navTotal)].symbol,
    ]
  }, [navEnabled, navIdx, navTotal, navList])

  // 首↔尾循环的弱提示 (自显 ~1.5s, 不引全局 Toast)
  const [wrapMsg, setWrapMsg] = useState<string | null>(null)
  const wrapTimer = useRef<number | null>(null)
  useEffect(() => {
    return () => { if (wrapTimer.current) window.clearTimeout(wrapTimer.current) }
  }, [])

  // 父级 onNavigate/onClose 多为内联 lambda, 用最新值 ref 承接, 避免每次父渲染重建 go/键盘监听
  const onNavigateRef = useRef(onNavigate)
  onNavigateRef.current = onNavigate
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  // 前后切股: 返回是否真正导航 (供键盘判断是否要 preventDefault)
  const go = useCallback((delta: 1 | -1): boolean => {
    if (!navEnabled) return false
    const nextIdx = wrapNavIndex(navIdx, delta, navTotal)
    const wrapped = nextIdx === (delta === 1 ? 0 : navTotal - 1)
    if (wrapped) {
      // 提示词描述切股后的落点 (而非起点)
      setWrapMsg(delta === 1 ? '已到榜首' : '已到末尾')
      if (wrapTimer.current) window.clearTimeout(wrapTimer.current)
      wrapTimer.current = window.setTimeout(() => setWrapMsg(null), 1500)
    }
    const next = navList[nextIdx]
    onNavigateRef.current?.(next.symbol, next.name)
    return true
  }, [navList, navIdx, navTotal])

  // ESC 关闭 + 左右键切股
  useEffect(() => {
    if (!symbol) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { onCloseRef.current(); return }
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        // 焦点在输入框/编辑器时方向键让位给光标/输入, 不切股
        const t = e.target as HTMLElement | null
        if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return
        if (showMonitorEditor) return
        if (go(e.key === 'ArrowRight' ? 1 : -1)) e.preventDefault()
      }
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [symbol, go, showMonitorEditor])

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
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          {/* 遮罩 */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            {...backdrop}
          />

          {/* 弹窗主体 */}
          <motion.div
            initial={{ opacity: 0, scale: 0.95, y: 12 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 8 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className="relative w-[92vw] max-w-[1100px] max-h-[95vh] rounded-card border border-border bg-base shadow-2xl overflow-hidden flex flex-col"
          >
            {/* 顶栏 */}
            <div className="flex items-center justify-between px-5 py-3 border-b border-border shrink-0">
              <div className="flex items-center gap-2">
                {(() => {
                  const board = symbol ? boardTag(symbol) : null
                  return board ? (
                    <span className={`inline-flex items-center justify-center w-[18px] h-[18px] rounded text-[9px] font-bold leading-none border ${board.color}`}>
                      {board.label}
                    </span>
                  ) : null
                })()}
                <span className="font-mono text-sm font-medium text-foreground">{symbol}</span>
                {name && <span className="text-xs text-muted">{name}</span>}

                {/* 切股导航: 上一只 / n·N / 下一只 */}
                {navEnabled && (
                  <>
                    <span className="text-muted/20 mx-0.5">|</span>
                    <button
                      onClick={() => go(-1)}
                      title="上一只 (←)"
                      className="p-1 rounded-btn text-secondary hover:text-foreground hover:bg-elevated transition-colors cursor-pointer"
                    >
                      <ChevronLeft className="h-3.5 w-3.5" />
                    </button>
                    <span className="font-mono text-[11px] text-secondary tabular-nums whitespace-nowrap">
                      {navIdx + 1} / {navTotal}
                    </span>
                    <button
                      onClick={() => go(1)}
                      title="下一只 (→)"
                      className="p-1 rounded-btn text-secondary hover:text-foreground hover:bg-elevated transition-colors cursor-pointer"
                    >
                      <ChevronRight className="h-3.5 w-3.5" />
                    </button>
                  </>
                )}
              </div>

              <div className="flex items-center gap-1.5">
                {/* 日期范围快捷 */}
                {PRESETS.map(p => {
                  const now = new Date()
                  const s = new Date(now)
                  s.setMonth(s.getMonth() - p.months)
                  const expected = s.toISOString().slice(0, 10)
                  const isActive = dateRange.start === expected
                  return (
                    <button
                      key={p.label}
                      onClick={() => {
                        const end = new Date().toISOString().slice(0, 10)
                        const ns = new Date()
                        ns.setMonth(ns.getMonth() - p.months)
                        setDateRange({ start: ns.toISOString().slice(0, 10), end })
                      }}
                      className={`h-6 px-1.5 rounded text-[11px] transition-colors cursor-pointer
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
                />
                <span className="text-muted/40 text-[10px]">~</span>
                <DatePicker
                  value={dateRange.end}
                  onChange={(v) => setDateRange(prev => ({ ...prev, end: v }))}
                  min={dateRange.start}
                />

                <span className="text-muted/20 mx-0.5">|</span>

                {/* 分时开关 */}
                <button
                  onClick={() => setShowIntraday((v) => !v)}
                  className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs transition-colors ${
                    showIntraday
                      ? 'bg-accent/15 text-accent border border-accent/30'
                      : 'bg-elevated text-secondary border border-border hover:border-accent/30'
                  }`}
                >
                  <Clock className="h-3 w-3" />
                  分时
                </button>

                <span className="text-muted/20 mx-0.5">|</span>

                {/* 刷新 */}
                <button
                  onClick={handleRefresh}
                  className="p-1 rounded-btn text-secondary hover:text-foreground hover:bg-elevated transition-colors"
                  title="刷新"
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                </button>

                {/* 关闭 */}
                <button
                  onClick={onClose}
                  className="p-1 rounded-btn text-secondary hover:text-foreground hover:bg-elevated transition-colors"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            </div>

            {/* 触发信息条 (来自监控触发记录) */}
            {triggerInfo && (
              <div className="flex items-center gap-4 border-b border-amber-400/20 bg-amber-400/[0.06] px-5 py-2 shrink-0">
                {/* 左: 触发标记 + 时间 */}
                <div className="flex items-center gap-2 shrink-0">
                  <span className="text-[10px] font-semibold text-amber-400">⚡ 触发</span>
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
                prefetchSymbols={prefetchSymbols}
                tags={tags}
              />
            </div>

            {/* 加监控编辑器弹层 */}
            <AnimatePresence>
              {showMonitorEditor && symbol && (
                <motion.div
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
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

            {/* 首↔尾循环弱提示 */}
            <AnimatePresence>
              {wrapMsg && (
                <motion.div
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: 8 }}
                  transition={{ duration: 0.2 }}
                  className="pointer-events-none absolute bottom-4 left-1/2 z-30 -translate-x-1/2 rounded-full border border-border bg-surface/95 px-3 py-1.5 text-[11px] text-secondary shadow-lg backdrop-blur"
                >
                  {wrapMsg}
                </motion.div>
              )}
            </AnimatePresence>
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}
