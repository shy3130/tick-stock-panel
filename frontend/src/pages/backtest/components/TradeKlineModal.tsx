import { useEffect, useMemo, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Clock, X } from 'lucide-react'
import { StockPanel } from '@/components/StockPanel'
import type { ChartPriceLine, ChartRange } from '@/components/EChartsCandlestick'
import type { StrategyBacktestTrade } from '@/lib/api'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'

interface Props {
  trade: StrategyBacktestTrade | null
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

export function TradeKlineModal({ trade, onClose }: Props) {
  const [showIntraday, setShowIntraday] = useState(false)
  const backdrop = useDialogBackdrop(onClose)

  useEffect(() => {
    if (!trade) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [trade, onClose])

  useEffect(() => {
    if (trade) setShowIntraday(false)
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
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}
