import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { type KlineRow, type FinancialMetricRecord } from '@/lib/api'
import { klineDailyQueryOptions } from '@/lib/kline'
import { StockInfoBar } from '@/components/StockInfoBar'
import { StockDailyKChart, getDefaultRange } from '@/components/StockDailyKChart'
import { StockIntradayChart } from '@/components/StockIntradayChart'
import { financialMetricsQueryOptions, useFinancialMetrics } from '@/lib/useFinancials'
import { useCapabilities } from '@/lib/useSharedQueries'
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
  /** 加自选 (传入后信息条显示 Star 图标) */
  inWatchlist?: boolean
  onToggleWatchlist?: () => void
  /** 分时图自动刷新间隔(ms)。undefined = 不轮询。个股对话框盘中实时刷新时传入。 */
  refetchIntervalMs?: number
  /** 邻近预取目标 (切股导航的左右邻股): 提前拉取其日K/财务指标缓存, 切换瞬间免 loading */
  prefetchSymbols?: string[]
  /** 当前股自选标签 (无自选上下文则不传) */
  tags?: string[]
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
  inWatchlist,
  onToggleWatchlist,
  refetchIntervalMs,
  prefetchSymbols,
  tags,
}: Props) {
  const [linkedPrice, setLinkedPrice] = useState<number | null>(null)
  const [selectedDate, setSelectedDate] = useState<string | null>(null)
  // 信息条指标配置提升到此层：同时供 StockInfoBar 渲染与 StockDailyKChart 请求 ext 数据
  const [fields, setFields] = useState<ColumnConfig[]>(loadInfoFields)
  const extColumns = useMemo(() => buildInfoExtColumnsParam(fields), [fields])

  const handleFieldsChange = useCallback((next: ColumnConfig[]) => {
    setFields(next)
    saveInfoFields(next)
  }, [])

  // 财务指标：仅当信息条配置含可见的财务字段且用户具备 FINANCIAL 能力 (Expert) 时才请求
  // 无能力时跳过请求, 避免后端抛 CapabilityDenied (403) 导致 free/starter 档弹错误提示
  const { data: caps } = useCapabilities()
  const hasFinancialCap = !!caps?.capabilities?.['financial']
  const hasFinanceField = useMemo(
    () => fields.some(f => f.visible && f.source.type === 'builtin'
      && ['eps', 'bps', 'roe', 'pe_ttm', 'pb', 'gross_margin', 'net_margin', 'debt_ratio', 'revenue_yoy', 'net_income_yoy'].includes(f.source.key)),
    [fields],
  )
  const financials = useFinancialMetrics(hasFinanceField && hasFinancialCap ? symbol : undefined)

  const dateRange = externalDateRange ?? getDefaultRange()

  // 日K查询由本组件持有 (与 StockDailyKChart 共享同一 cache key/配置, 只发一次请求),
  // 信息条直接读 query data, 切股到已预取邻股时首帧即有数据, 避免信息条塌陷导致弹窗高度抖动。
  const kline = useQuery({ ...klineDailyQueryOptions(symbol, dateRange, extColumns), enabled: !!symbol })
  const rawRows: KlineRow[] = kline.data?.rows ?? []
  const stockInfo = kline.data?.stock_info
  const name = kline.data?.name

  // 邻近预取: 对切股导航的左右邻股提前拉取缓存, 切股瞬间免 loading。
  // 日K预取 staleTime 30s 防来回切换重复请求; 成为当前股后 useQuery(staleTime=0) 会立即后台刷新,
  // SSE 也只按焦点股精准失效, 实时性不受影响。财务指标与正式查询同 staleTime, 5min 内不重复拉取。
  // prefetchKey 按内容 join: 自选页 navList 随行情 tick 重建但邻股集合通常不变, 避免 effect 每次 tick 重跑。
  const qc = useQueryClient()
  const prefetchKey = prefetchSymbols?.join(',') ?? ''
  useEffect(() => {
    if (!prefetchKey) return
    for (const s of prefetchKey.split(',')) {
      if (s === symbol) continue
      qc.prefetchQuery({ ...klineDailyQueryOptions(s, dateRange, extColumns), staleTime: 30_000 })
      if (hasFinanceField && hasFinancialCap) {
        qc.prefetchQuery(financialMetricsQueryOptions(s))
      }
    }
  }, [prefetchKey, symbol, dateRange, extColumns, hasFinanceField, hasFinancialCap, qc])

  const handleDateClick = useCallback((date: string) => {
    setSelectedDate(date)
    onSelectDate?.(date)
  }, [onSelectDate])

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
    if (showIntraday && !selectedDate && rawRows.length > 0) {
      setSelectedDate(rawRows[rawRows.length - 1].date)
    }
  }, [showIntraday, selectedDate, rawRows])

  const selectedIdx = selectedDate ? rawRows.findIndex(r => r.date === selectedDate) : -1
  const prevClose = selectedIdx > 0
    ? rawRows[selectedIdx - 1].close
    : rawRows.length >= 2
      ? rawRows[rawRows.length - 2].close
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
        onToggleWatchlist={onToggleWatchlist}
        tags={tags}
      />

      <div className="flex gap-3 items-start">
        <StockDailyKChart
          symbol={symbol}
          height={height}
          className="flex-1 min-w-0"
          dateRange={dateRange}
          markers={markers}
          ranges={ranges}
          priceLines={priceLines}
          showLimitMarkers={showLimitMarkers}
          showMarkerToggle={showMarkerToggle}
          linkedPrice={linkedPrice}
          onDateClick={handleDateClick}
          visibleBars={showIntraday ? 40 : 60}
          extColumns={extColumns}
        />

        {showIntraday && selectedDate && (
          <StockIntradayChart
            symbol={symbol}
            date={selectedDate}
            height={height}
            prevClose={prevClose}
            onPriceHover={setLinkedPrice}
            className="flex-1 min-w-0 border-l border-border pl-3"
            refetchIntervalMs={refetchIntervalMs}
          />
        )}
      </div>
    </div>
  )
}
