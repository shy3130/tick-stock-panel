import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { X } from 'lucide-react'
import { type KlineRow, type FinancialMetricRecord } from '@/lib/api'
import { klineDailyQueryOptions, klineMinuteQueryOptions, klineMinuteRangeQueryOptions, DEFAULT_INTRADAY_DAYS } from '@/lib/kline'
import { StockInfoBar } from '@/components/StockInfoBar'
import { StockDailyKChart, getDefaultRange, toOHLC } from '@/components/StockDailyKChart'
import { StockIntradayChart } from '@/components/StockIntradayChart'
import { financialMetricsQueryOptions, useFinancialMetrics } from '@/lib/useFinancials'
import { useCapabilities } from '@/lib/useSharedQueries'
import { scheduleNeighborPrefetch } from '@/lib/neighborPrefetch'
import type { ChartMarker, ChartPriceLine, ChartRange } from '@/components/EChartsCandlestick'
import {
  loadInfoFields,
  saveInfoFields,
  buildInfoExtColumnsParam,
  type ColumnConfig,
} from '@/lib/stock-info-fields'

interface Props {
  symbol: string
  height?: number
  showIntraday?: boolean
  className?: string
  /** 当用户点击蜡烛选中日期时回调（用于外部自动开启分时图）。 */
  onSelectDate?: (date: string) => void
  /** 外部传入的日期范围 */
  dateRange?: { start: string; end: string }
  markers?: ChartMarker[]
  ranges?: ChartRange[]
  priceLines?: ChartPriceLine[]
  showLimitMarkers?: boolean
  showMarkerToggle?: boolean
  /** 加监控回调 (传入后信息条显示 RadioTower 图标) */
  onMonitor?: () => void
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  /** 自选操作（传入后信息条显示 Star 图标） */
  inWatchlist?: boolean
  onAddToWatchlist?: (groupId: string | null) => void
  onRemoveFromWatchlist?: () => void
  watchlistPending?: boolean
  /** 分时图自动刷新间隔(ms)。undefined = 不轮询。个股对话框盘中实时刷新时传入。 */
  refetchIntervalMs?: number
  /** 只渲染信息条, 隐藏图表 (用于分时 tab 共享信息条) */
  infoBarOnly?: boolean
  /** 邻近预取目标 (切股导航的左右邻股): 提前拉取其日K/财务/分时缓存, 切换瞬间免 loading */
  prefetchSymbols?: string[]
  /** 多日分时周期 (分时 tab 使用): 预取邻股 klineMinuteRange 时用同一 days, 保证 queryKey 命中 */
  intradayDays?: number
  /** 日K/分时并排时日K图占宽 (默认 1:1; 弹窗内图表信息栏较宽需更多空间时传 flex-[1.4] 之类) */
  dailyKlineFlex?: string
  /** 初始可见蜡烛根数 (默认 60); 'all' = 初始适配显示全部数据 (用于全区间回放) */
  visibleBars?: number | 'all'
  /** 加入自选日 (北京时间 YYYY-MM-DD); 有值时日K主图绘制「自选」竖虚线 */
  addedDate?: string | null
}

export { getDefaultRange }

export function StockPanel({
  symbol,
  height = 520,
  showIntraday = true,
  className,
  onSelectDate,
  dateRange: externalDateRange,
  markers,
  ranges,
  priceLines,
  showLimitMarkers = true,
  showMarkerToggle = true,
  onMonitor,
  onPriceDoubleClick,
  inWatchlist,
  onAddToWatchlist,
  onRemoveFromWatchlist,
  watchlistPending,
  refetchIntervalMs,
  infoBarOnly = false,
  prefetchSymbols,
  intradayDays = DEFAULT_INTRADAY_DAYS,
  dailyKlineFlex = 'flex-1',
  visibleBars,
  addedDate,
}: Props) {
  const [linkedPrice, setLinkedPrice] = useState<number | null>(null)
  const [selectedDate, setSelectedDate] = useState<string | null>(null)
  const [intradayDismissed, setIntradayDismissed] = useState(false)
  // 信息条指标配置提升到此层：同时供 StockInfoBar 渲染与 StockDailyKChart 请求 ext 数据
  const [fields, setFields] = useState<ColumnConfig[]>(loadInfoFields)
  const extColumns = useMemo(() => buildInfoExtColumnsParam(fields), [fields])

  const handleFieldsChange = useCallback((next: ColumnConfig[]) => {
    setFields(next)
    saveInfoFields(next)
  }, [])

  // 财务指标：仅当信息条配置含可见的财务字段且用户具备财务数据能力 (financial) 时才请求
  // 无能力时跳过请求, 避免后端抛 CapabilityDenied (403) 导致 free/starter 档弹错误提示
  const { data: caps } = useCapabilities()
  const hasFinancialCap = !!caps?.capabilities?.['financial']
  const hasFinanceField = useMemo(
    () => fields.some(f => f.visible && f.source.type === 'builtin'
      && ['eps', 'bps', 'roe', 'pe_ttm', 'pb', 'gross_margin', 'net_margin', 'debt_ratio', 'revenue_yoy', 'net_income_yoy'].includes(f.source.key)),
    [fields],
  )
  const financials = useFinancialMetrics(hasFinanceField && hasFinancialCap ? symbol : undefined)

  const defaultDateRange = getDefaultRange()
  const rangeStart = externalDateRange?.start ?? defaultDateRange.start
  const rangeEnd = externalDateRange?.end ?? defaultDateRange.end
  const dateRange = useMemo(() => ({ start: rangeStart, end: rangeEnd }), [rangeStart, rangeEnd])

  // 日K查询由本组件持有 (与 StockDailyKChart 共享同一 cache key/配置, 只发一次请求)。
  // 信息条直接读 query data: 切股到已预取邻股时首帧即有数据, 配合 StockInfoBar 加载态占位,
  // 弹窗整体高度在切换瞬间不塌陷 (不抖动)。
  const kline = useQuery({ ...klineDailyQueryOptions(symbol, dateRange, extColumns), enabled: !!symbol })
  const rawRows: KlineRow[] = kline.data?.rows ?? []
  // OHLC 视图用于日期选中/昨收价推导 (与图表侧同口径)
  const rows = useMemo(() => toOHLC(rawRows), [rawRows])
  const stockInfo = kline.data?.stock_info
  const name = kline.data?.name

  const handleDateClick = useCallback((date: string) => {
    setSelectedDate(date)
    setIntradayDismissed(false)
    onSelectDate?.(date)
  }, [onSelectDate])

  // 当前日K就绪后, 等可见查询空闲再串行预取邻股, 避免首屏争抢公网带宽。
  // 日K/分时预取 staleTime 30s 防来回切换重复请求; 成为当前股后 useQuery(staleTime=0) 立即后台刷新,
  // SSE 也只按焦点股精准失效, 实时性不受影响。财务指标与正式查询同 staleTime, 5min 内不重复拉取。
  // prefetchKey 按内容 join: 自选页 navList 随行情 tick 重建但邻股集合通常不变, 避免 effect 每次 tick 重跑。
  const qc = useQueryClient()
  const prefetchKey = prefetchSymbols?.join(',') ?? ''
  useEffect(() => {
    if (!symbol || !prefetchKey || !kline.isSuccess || kline.isPlaceholderData) return
    const tasks: Array<() => Promise<unknown>> = []
    for (const s of new Set(prefetchKey.split(',').filter(Boolean))) {
      if (s === symbol) continue
      let lastDate: string | undefined
      tasks.push(async () => {
        const res = await qc.fetchQuery({ ...klineDailyQueryOptions(s, dateRange, extColumns), staleTime: 30_000 })
        const date = res?.rows?.at(-1)?.date
        lastDate = date ? String(date).slice(0, 10) : undefined
      })
      // 独立排队, 切股/关闭后的日K响应不会继续级联分钟请求。
      tasks.push(async () => {
        if (lastDate) await qc.prefetchQuery({ ...klineMinuteQueryOptions(s, lastDate, true), staleTime: 30_000 })
      })
      if (hasFinanceField && hasFinancialCap) {
        tasks.push(() => qc.prefetchQuery(financialMetricsQueryOptions(s)))
      }
      tasks.push(() => qc.prefetchQuery({ ...klineMinuteRangeQueryOptions(s, intradayDays), staleTime: 30_000 }))
      tasks.push(() => qc.prefetchQuery({ ...klineMinuteQueryOptions(s, undefined, true), staleTime: 30_000 }))
    }
    return scheduleNeighborPrefetch(qc, symbol, tasks)
  }, [prefetchKey, symbol, dateRange, extColumns, hasFinanceField, hasFinancialCap, intradayDays, qc, kline.isSuccess, kline.isPlaceholderData])

  // symbol 变化时重置分时相关状态，避免切股后残留旧日期。
  // 日K信息直接读 query data (切股到已预取邻股首帧即有), 无需清空或门控。
  const prevSymbol = useRef<string | null>(symbol)
  useEffect(() => {
    if (prevSymbol.current === symbol) return
    prevSymbol.current = symbol
    setSelectedDate(null)
    setLinkedPrice(null)
  }, [symbol])

  // 当分时开启、无选中日期时，自动选中最新日期
  useEffect(() => {
    if (showIntraday && !selectedDate && rows.length > 0) {
      setSelectedDate(rows[rows.length - 1].date)
    }
  }, [showIntraday, selectedDate, rows])

  const selectedIdx = selectedDate ? rows.findIndex(r => r.date === selectedDate) : -1
  const selectedRow = selectedIdx >= 0 ? rows[selectedIdx] : undefined
  const prevClose = selectedIdx > 0
    ? rows[selectedIdx - 1].close
    : rows.length >= 2
      ? rows[rows.length - 2].close
      : undefined
  if (!symbol) return null

  // 财务指标最新一期（metrics 按 period_end 排序，取首项）
  const financialMetrics: FinancialMetricRecord | undefined = financials.data?.data?.[0]

  return (
    <div className={className}>
      <StockInfoBar
        symbol={symbol}
        name={name}
        stockInfo={stockInfo}
        rows={rawRows}
        fields={fields}
        onFieldsChange={handleFieldsChange}
        financialMetrics={financialMetrics}
        onMonitor={onMonitor}
        inWatchlist={inWatchlist}
        onAddToWatchlist={onAddToWatchlist}
        onRemoveFromWatchlist={onRemoveFromWatchlist}
        watchlistPending={watchlistPending}
        addedDate={addedDate}
      />

      {infoBarOnly ? null : (
      <div className="flex gap-3 items-start">
        <StockDailyKChart
          symbol={symbol}
          height={height}
          className={`${dailyKlineFlex} min-w-0`}
          dateRange={dateRange}
          markers={markers}
          ranges={ranges}
          priceLines={priceLines}
          showLimitMarkers={showLimitMarkers}
          showMarkerToggle={showMarkerToggle}
          linkedPrice={linkedPrice}
          onDateClick={handleDateClick}
          onPriceDoubleClick={onPriceDoubleClick}
          visibleBars={visibleBars ?? (showIntraday ? 40 : 60)}
          extColumns={extColumns}
          addedDate={addedDate}
        />

        {showIntraday && selectedDate && !intradayDismissed && (
          <div className="relative flex-1 min-w-0 border-l border-border pl-3">
            <button
              onClick={() => setIntradayDismissed(true)}
              className="absolute -left-1.5 -top-1.5 z-10 flex h-5 w-5 items-center justify-center rounded-full border border-border bg-surface text-muted shadow-sm transition-colors hover:text-foreground hover:bg-elevated"
              title="收起分时图"
              aria-label="收起分时图"
            >
              <X className="h-3 w-3" />
            </button>
            <StockIntradayChart
              symbol={symbol}
              date={selectedDate}
              height={height}
              prevClose={prevClose}
              dailySummary={selectedRow}
              onPriceHover={setLinkedPrice}
              onPriceDoubleClick={onPriceDoubleClick}
              currentPrice={rows[rows.length - 1]?.close}
              priceLines={priceLines}
              refetchIntervalMs={refetchIntervalMs}
            />
          </div>
        )}
      </div>
      )}
    </div>
  )
}
