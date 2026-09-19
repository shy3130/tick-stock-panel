import { useEffect, useMemo, useRef } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { X } from 'lucide-react'
import { StockPanel } from '@/components/StockPanel'
import { NavPager, NavWrapToast } from '@/components/NavPager'
import type { ChartMarker } from '@/components/EChartsCandlestick'
import type { StrategyBacktestResult, StrategyBacktestTrade } from '@/lib/api'
import { navItemKey, type NavItem } from '@/lib/listNav'
import { useListNav } from '@/lib/useListNav'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { boardTag } from '@/lib/board'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'

interface Props {
  /** 选中的标的 (null = 关闭) */
  symbol: string | null
  /** 回测结果, 用于取该标的所有交易与聚合统计 */
  result: StrategyBacktestResult | null
  /** 回测有效区间 (图默认展示范围) */
  periodStart: string
  periodEnd: string
  /** 有序候选列表 (选股分析全部标的): 提供后支持左右键/顶栏按钮切标的 */
  navList?: NavItem[]
  /** 切标的回调: 收到目标 symbol/name, 由调用方更新选中状态 */
  onNavigate?: (symbol: string, name?: string) => void
  onClose: () => void
}

/** 「选股分析」行的标的级K线弹窗 (单笔回放见 TradeKlineModal) */
export function PicksSymbolKlineModal({ symbol, result, periodStart, periodEnd, navList, onNavigate, onClose }: Props) {
  const backdrop = useDialogBackdrop(onClose)

  // onClose 只有 ESC 用; onNavigate 由 useListNav 内部承接 (支持内联 lambda)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  const nav = useListNav<NavItem>({
    items: navList ?? [],
    keyOf: navItemKey,
    currentKey: symbol,
    onNavigate: n => onNavigate?.(n.symbol, n.name),
    wrapHints: { head: '已到榜首', tail: '已到末尾' },
  })

  useEffect(() => {
    if (!symbol) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCloseRef.current()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [symbol])

  const view = useMemo(() => {
    if (!symbol || !result) return { trades: [] as StrategyBacktestTrade[], stat: null, name: '' }
    const trades = result.trades.filter(t => t.symbol === symbol)
    return {
      trades,
      stat: result.per_symbol_stats.find(p => p.symbol === symbol) ?? null,
      name: trades.find(t => t.name)?.name ?? '',
    }
  }, [symbol, result])
  const { trades, stat, name } = view

  const markers = useMemo<ChartMarker[]>(() => {
    type Agg = { count: number; lo: number; hi: number }
    // 同日同方向多笔合成单个箭头 (多箭头会重叠); 价格异则标签显示区间
    const byDate = new Map<string, { buy?: Agg; sell?: Agg }>()
    const add = (date: string, side: 'buy' | 'sell', price: number) => {
      let g = byDate.get(date)
      if (!g) { g = {}; byDate.set(date, g) }
      const s = g[side]
      if (s) {
        s.count += 1
        s.lo = Math.min(s.lo, price)
        s.hi = Math.max(s.hi, price)
      } else {
        g[side] = { count: 1, lo: price, hi: price }
      }
    }
    const label = (s: Agg) => {
      const range = s.lo === s.hi ? fmtPrice(s.lo) : `${fmtPrice(s.lo)}~${fmtPrice(s.hi)}`
      return s.count > 1 ? `${range} ×${s.count}` : range
    }
    for (const t of trades) {
      add(String(t.entry_date).slice(0, 10), 'buy', Number(t.entry_price))
      add(String(t.exit_date).slice(0, 10), 'sell', Number(t.exit_price))
    }
    const out: ChartMarker[] = []
    for (const [date, g] of byDate) {
      if (g.buy) out.push({ date, kind: 'buy', label: label(g.buy) })
      if (g.sell) out.push({ date, kind: 'sell', label: label(g.sell) })
    }
    return out.sort((a, b) => a.date.localeCompare(b.date))
  }, [trades])

  const tag = symbol ? boardTag(symbol) : ''
  const dateRange = useMemo(() => ({ start: periodStart, end: periodEnd }), [periodStart, periodEnd])

  return (
    <AnimatePresence>
      {symbol && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center">
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            {...backdrop}
          />
          <motion.div
            initial={{ opacity: 0, scale: 0.95, y: 12 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 8 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className="relative flex max-h-[94vh] w-[92vw] max-w-[1120px] flex-col overflow-hidden rounded-card border border-border bg-base shadow-2xl"
          >
            <div className="border-b border-border px-5 py-3">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-sm font-semibold text-foreground">{symbol}</span>
                    {name && <span className="truncate text-sm text-foreground">{name}</span>}
                    {tag && (
                      <span className="rounded border border-accent/30 bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">
                        {tag}
                      </span>
                    )}
                    <span className="rounded border border-accent/30 bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">标的回放</span>

                    {/* 切标的: 上一只 / n·N / 下一只 */}
                    <NavPager nav={nav} prevLabel="上一只" nextLabel="下一只" />
                  </div>
                  <div className="mt-1 text-[11px] text-muted">
                    回测 {periodStart} ~ {periodEnd} · {trades.length} 笔交易
                  </div>
                  <div className="mt-1 text-[10px] text-muted/70">
                    ▲ 买入(红) · ▼ 卖出(绿) · 箭头旁为成交价 · 同日多笔合并显示
                  </div>
                </div>

                <div className="flex shrink-0 items-center gap-3 text-xs">
                  {stat && (
                    <div className="flex items-center gap-3">
                      <div className="text-right">
                        <div className="text-muted">总收益</div>
                        <div className={`num font-semibold ${priceColorClass(stat.total_return)}`}>{fmtPct(stat.total_return)}</div>
                      </div>
                      <div className="text-right">
                        <div className="text-muted">胜率</div>
                        <div className="num text-secondary">{fmtPct(stat.win_rate)}</div>
                      </div>
                      <div className="text-right">
                        <div className="text-muted">最佳 / 最差</div>
                        <div className="num">
                          <span className="text-bull">{fmtPct(stat.best)}</span>
                          <span className="text-muted"> / </span>
                          <span className="text-bear">{fmtPct(stat.worst)}</span>
                        </div>
                      </div>
                    </div>
                  )}
                  <button
                    onClick={onClose}
                    className="rounded-btn p-1 text-secondary transition-colors hover:bg-elevated hover:text-foreground"
                    aria-label="关闭标的回放"
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>
              </div>
            </div>

            <div className="flex-1 overflow-auto p-4">
              <StockPanel
                symbol={symbol}
                height={520}
                dateRange={dateRange}
                markers={markers}
                showLimitMarkers={false}
                showMarkerToggle={false}
                showIntraday={false}
                visibleBars="all"
              />
            </div>

            {/* 首↔尾循环弱提示 */}
            <NavWrapToast message={nav.wrapMsg} />
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}
