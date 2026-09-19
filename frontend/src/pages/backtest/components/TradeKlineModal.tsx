import { useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Clock, X } from 'lucide-react'
import { StockPanel } from '@/components/StockPanel'
import { NavPager, NavWrapToast } from '@/components/NavPager'
import type { ChartPriceLine, ChartRange } from '@/components/EChartsCandlestick'
import type { StrategyBacktestTrade } from '@/lib/api'
import { useListNav } from '@/lib/useListNav'
import { tradeNavKey, type TradeNavItem } from '../tradeNav'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'

/** 单笔回放来自哪个入口: 决定切交易时同步翻哪张表的页 */
export type TradeNavSource = 'daily' | 'trades'

interface Props {
  trade: StrategyBacktestTrade | null
  /** 交易回放链 (与当前表格同序): 提供后支持左右键/顶栏按钮切交易 */
  navItems?: TradeNavItem[]
  /** 当前交易在链中的 key (取自链中对应项的 key) */
  currentKey?: string | null
  /** 切交易回调: 收到目标项, 由调用方更新选中并同步分页 */
  onNavigate?: (item: TradeNavItem) => void
  onClose: () => void
}

function addDays(date: string, days: number): string {
  const d = new Date(date)
  d.setDate(d.getDate() + days)
  return d.toISOString().slice(0, 10)
}

function fmtMoney(v: number | null | undefined): string {
  if (v == null || Number.isNaN(Number(v))) return '—'
  const n = Number(v)
  const abs = Math.abs(n)
  if (abs >= 100_000_000) return `${(n / 100_000_000).toFixed(2)}亿`
  if (abs >= 10_000) return `${(n / 10_000).toFixed(2)}万`
  return n.toFixed(0)
}

function fmtSignedMoney(v: number | null | undefined): string {
  if (v == null || Number.isNaN(Number(v))) return '—'
  const prefix = Number(v) > 0 ? '+' : ''
  return `${prefix}${fmtMoney(v)}`
}

export function TradeKlineModal({ trade, navItems, currentKey, onNavigate, onClose }: Props) {
  const [showIntraday, setShowIntraday] = useState(false)
  const backdrop = useDialogBackdrop(onClose)

  // onClose 只有 ESC 用; onNavigate 由 useListNav 内部承接 (支持内联 lambda)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  const nav = useListNav<TradeNavItem>({
    items: navItems ?? [],
    keyOf: tradeNavKey,
    currentKey: currentKey ?? null,
    onNavigate: item => onNavigate?.(item),
    // 「笔」而非「只」: 链的粒度是交易, 同一标的可以出现多笔
    wrapHints: { head: '已到首笔', tail: '已到末笔' },
  })

  // ESC 关闭 (左右键切交易由 useListNav 接管)
  useEffect(() => {
    if (!trade) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCloseRef.current()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [trade])

  // 弹窗内切交易时保留当前视图 (分时下切交易不应跳回日K); 仅首次打开时重置为日K。
  const prevTradeRef = useRef<StrategyBacktestTrade | null>(null)
  useEffect(() => {
    if (prevTradeRef.current == null && trade != null) setShowIntraday(false)
    prevTradeRef.current = trade
  }, [trade])

  const dateRange = useMemo(() => {
    if (!trade) return null
    return {
      start: addDays(String(trade.entry_date).slice(0, 10), -45),
      end: addDays(String(trade.exit_date).slice(0, 10), 20),
    }
  }, [trade])

  const ranges = useMemo<ChartRange[]>(() => {
    if (!trade) return []
    return [{
      start: String(trade.entry_date).slice(0, 10),
      end: String(trade.exit_date).slice(0, 10),
      label: '持仓区间',
      color: 'rgba(59,130,246,0.07)',
    }]
  }, [trade])

  const priceLines = useMemo<ChartPriceLine[]>(() => {
    if (!trade) return []
    const start = String(trade.entry_date).slice(0, 10)
    const end = String(trade.exit_date).slice(0, 10)
    return [
      {
        value: Number(trade.entry_price),
        label: `买入价 ${fmtPrice(trade.entry_price)}`,
        color: '#C74040',
        start,
        end,
      },
      {
        value: Number(trade.exit_price),
        label: `卖出价 ${fmtPrice(trade.exit_price)}`,
        color: '#2D9B65',
        start,
        end,
      },
    ]
  }, [trade])

  return (
    <AnimatePresence>
      {trade && dateRange && (
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
            <div className="flex items-center justify-between gap-4 border-b border-border px-5 py-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-sm font-semibold text-foreground">{trade.symbol}</span>
                  <span className="truncate text-sm text-foreground">{trade.name || '交易回放'}</span>
                  <span className="rounded border border-accent/30 bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">交易回放</span>

                  {/* 切交易: 上一笔 / n·N / 下一笔 */}
                  <NavPager nav={nav} prevLabel="上一笔" nextLabel="下一笔" />
                </div>
                <div className="mt-1 text-[11px] text-muted">
                  {String(trade.entry_date).slice(0, 10)} 买入 → {String(trade.exit_date).slice(0, 10)} 卖出 · 持仓 {trade.duration ?? '—'} 天
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-3 text-xs">
                <div className="text-right">
                  <div className="text-muted">买 / 卖</div>
                  <div className="num text-foreground">{fmtPrice(trade.entry_price)} / {fmtPrice(trade.exit_price)}</div>
                </div>
                <div className="text-right">
                  <div className="text-muted">盈亏</div>
                  <div className={`num font-semibold ${priceColorClass(trade.pnl_amount ?? trade.pnl_pct)}`}>
                    {fmtSignedMoney(trade.pnl_amount)} / {fmtPct(trade.pnl_pct)}
                  </div>
                </div>
                <button
                  onClick={() => setShowIntraday((v) => !v)}
                  className={`inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs transition-colors ${
                    showIntraday
                      ? 'border border-accent/30 bg-accent/15 text-accent'
                      : 'border border-border bg-elevated text-secondary hover:border-accent/30'
                  }`}
                >
                  <Clock className="h-3 w-3" />
                  分时
                </button>
                <button
                  onClick={onClose}
                  className="rounded-btn p-1 text-secondary transition-colors hover:bg-elevated hover:text-foreground"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            </div>

            <div className="flex-1 overflow-auto p-4">
              <StockPanel
                symbol={trade.symbol}
                height={520}
                dateRange={dateRange}
                ranges={ranges}
                priceLines={priceLines}
                showLimitMarkers={false}
                showMarkerToggle={false}
                showIntraday={showIntraday}
                onSelectDate={() => { if (!showIntraday) setShowIntraday(true) }}
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
