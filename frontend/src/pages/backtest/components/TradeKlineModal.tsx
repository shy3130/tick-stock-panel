import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Clock, X } from 'lucide-react'
import { StockPanel } from '@/components/StockPanel'
import { NavPager, NavWrapToast } from '@/components/NavPager'
import type { ChartMarker, ChartPriceLine, ChartRange } from '@/lib/chart-types'
import type { StrategyBacktestTrade } from '@/lib/api'
import { useListNav } from '@/lib/useListNav'
import { tradeNavKey, type TradeNavItem } from '../tradeNav'
import { fmtPct, fmtPrice, priceColorClass, pricePrecision } from '@/lib/format'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'

/** 单笔回放来自哪个入口: 决定切交易时同步翻哪张表的页 */
export type TradeNavSource = 'daily' | 'trades'

interface Props {
  trade: StrategyBacktestTrade | null
  assetType?: 'stock' | 'etf'
  /** 交易回放链 (与当前表格同序): 提供后支持左右键/顶栏按钮切交易 */
  navItems?: TradeNavItem[]
  /** 当前交易在链中的 key (取自链中对应项的 key) */
  currentKey?: string | null
  /** 切交易回调: 收到目标项, 由调用方更新选中并同步分页 */
  onNavigate?: (item: TradeNavItem) => void
  onClose: () => void
}

function addDays(date: string, days: number): string {
  const d = new Date(`${date}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + days)
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

export function TradeKlineModal({ trade, assetType, navItems, currentKey, onNavigate, onClose }: Props) {
  const [showIntraday, setShowIntraday] = useState(false)
  const [selection, setSelection] = useState<{ key: string; date: string } | null>(null)
  const [resolvedAsset, setResolvedAsset] = useState<{ symbol: string; type: string } | null>(null)
  const backdrop = useDialogBackdrop(onClose)
  const entryDay = String(trade?.entry_date ?? '').slice(0, 10)
  const exitDay = String(trade?.exit_date ?? '').slice(0, 10)
  const replayKey = JSON.stringify([currentKey, trade?.symbol, trade?.entry_date, trade?.exit_date])
  const selectedDate = selection?.key === replayKey ? selection.date : entryDay
  const precision = pricePrecision(assetType ?? (resolvedAsset?.symbol === trade?.symbol ? resolvedAsset?.type : undefined))
  const selectDate = useCallback((date: string) => {
    setSelection({ key: replayKey, date })
    setShowIntraday(true)
  }, [replayKey])
  const tradeSymbol = trade?.symbol
  const handleAssetType = useCallback((type: 'stock' | 'etf' | 'index') => {
    if (tradeSymbol) setResolvedAsset(previous => previous?.symbol === tradeSymbol && previous.type === type
      ? previous : { symbol: tradeSymbol, type })
  }, [tradeSymbol])

  useEffect(() => { setSelection(null) }, [replayKey])

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

  const markers = useMemo<ChartMarker[]>(() => trade ? [
    { date: entryDay, kind: 'buy', label: `买 ${fmtPrice(trade.entry_price, precision)}` },
    { date: exitDay, kind: 'sell', label: `卖 ${fmtPrice(trade.exit_price, precision)}` },
  ] : [], [trade, entryDay, exitDay, precision])

  const priceLines = useMemo<ChartPriceLine[]>(() => {
    if (!trade) return []
    const start = String(trade.entry_date).slice(0, 10)
    const end = String(trade.exit_date).slice(0, 10)
    if (Number(trade.entry_price) === Number(trade.exit_price)) {
      return [{ value: Number(trade.entry_price), label: `买卖价 ${fmtPrice(trade.entry_price, precision)}`, color: '#3B82F6', start, end }]
    }
    return [
      {
        value: Number(trade.entry_price),
        label: `买入价 ${fmtPrice(trade.entry_price, precision)}`,
        color: '#C74040',
        start,
        end,
      },
      {
        value: Number(trade.exit_price),
        label: `卖出价 ${fmtPrice(trade.exit_price, precision)}`,
        color: '#2D9B65',
        start,
        end,
      },
    ]
  }, [trade, precision])

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
            role="dialog"
            aria-modal="true"
            aria-label={`${trade.symbol} ${trade.name || ''} 交易回放`}
            initial={{ opacity: 0, scale: 0.95, y: 12 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 8 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className="relative flex max-h-[94dvh] w-[96vw] max-w-[1600px] flex-col overflow-hidden rounded-card border border-border bg-base shadow-2xl"
          >
            <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-3 border-b border-border px-4 py-3 sm:px-5">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="font-mono text-sm font-semibold text-foreground">{trade.symbol}</span>
                  <span className="truncate text-sm text-foreground">{trade.name || '交易回放'}</span>
                  <span className="rounded border border-accent/30 bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">交易回放</span>

                  {/* 切交易: 上一笔 / n·N / 下一笔 */}
                  <NavPager nav={nav} prevLabel="上一笔" nextLabel="下一笔" />
                </div>
                <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted">
                  <button type="button" aria-label="查看买入日" title="查看买入日分时"
                    onClick={() => selectDate(entryDay)}
                    className="rounded px-1 py-0.5 text-bull transition-colors hover:bg-bull/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60">
                    {String(trade.entry_date).replace('T', ' ').slice(0, 16)} 买入
                  </button>
                  <span aria-hidden="true">→</span>
                  <button type="button" aria-label="查看卖出日" title="查看卖出日分时"
                    onClick={() => selectDate(exitDay)}
                    className="rounded px-1 py-0.5 text-bear transition-colors hover:bg-bear/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60">
                    {String(trade.exit_date).replace('T', ' ').slice(0, 16)} 卖出
                  </button>
                  <span>· 持仓 {trade.duration ?? '—'} 天</span>
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
                <div className="text-right">
                  <div className="text-muted">买 / 卖</div>
                  <div className="num text-foreground">{fmtPrice(trade.entry_price, precision)} / {fmtPrice(trade.exit_price, precision)}</div>
                </div>
                <div className="text-right">
                  <div className="text-muted">盈亏</div>
                  <div className={`num font-semibold ${priceColorClass(trade.pnl_amount ?? trade.pnl_pct)}`}>
                    {fmtSignedMoney(trade.pnl_amount)} / {fmtPct(trade.pnl_pct)}
                  </div>
                </div>
                <button
                  type="button"
                  aria-pressed={showIntraday}
                  onClick={() => setShowIntraday((v) => !v)}
                  className={`inline-flex items-center gap-1 rounded px-2 py-1 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 ${
                    showIntraday
                      ? 'border border-accent/30 bg-accent/15 text-accent'
                      : 'border border-border bg-elevated text-secondary hover:border-accent/30'
                  }`}
                >
                  <Clock className="h-3 w-3" />
                  分时
                </button>
                <button
                  type="button"
                  aria-label="关闭交易回放"
                  onClick={onClose}
                  className="rounded-btn p-1 text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            </div>

            <div className="min-h-0 flex-1 overflow-auto p-3 sm:p-4">
              <StockPanel
                symbol={trade.symbol}
                renderer="lightweight"
                height={520}
                visibleBars="all"
                dateRange={dateRange}
                markers={markers}
                ranges={ranges}
                priceLines={priceLines}
                showLimitMarkers={false}
                showMarkerToggle={false}
                showIntraday={showIntraday}
                selectedDate={selectedDate}
                onSelectDate={selectDate}
                onIntradayClose={() => setShowIntraday(false)}
                onAssetType={assetType ? undefined : handleAssetType}
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
