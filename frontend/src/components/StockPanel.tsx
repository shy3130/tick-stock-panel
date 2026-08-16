import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { type KlineRow, type FinancialMetricRecord } from '@/lib/api'
import { StockInfoBar } from '@/components/StockInfoBar'
import { StockDailyKChart, getDefaultRange, type StockDailyKChartResult } from '@/components/StockDailyKChart'
import { StockIntradayChart } from '@/components/StockIntradayChart'
import { useFinancialMetrics } from '@/lib/useFinancials'
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
}: Props) {
  const [linkedPrice, setLinkedPrice] = useState<number | null>(null)
  const [selectedDate, setSelectedDate] = useState<string | null>(null)
  // 按 symbol 记录日K结果: 切股时用 symbol 门控显示, 旧股结果不误显示, 也不被父 effect 清掉而卡在加载态
  const [dailyResult, setDailyResult] = useState<{ symbol: string; result: StockDailyKChartResult | null }>({ symbol, result: null })
  const handleDataChange = useCallback((result: StockDailyKChartResult) => {
    setDailyResult({ symbol, result })
  }, [symbol])
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

  const handleDateClick = useCallback((date: string) => {
    setSelectedDate(date)
    onSelectDate?.(date)
  }, [onSelectDate])

  // 仅当结果属于当前 symbol 时才用于显示 (切股瞬间旧股结果不会误显示)
  const daily = dailyResult.symbol === symbol ? dailyResult.result : null
  const rows = daily?.rows ?? []
  const stockInfo = daily?.stockInfo
  const rawRows: KlineRow[] = daily?.rawRows ?? []
  const name = daily?.name

  // symbol 变化时重置分时相关状态，避免切股后残留旧日期。
  // 注意：不再清空 dailyResult —— 预取后新 symbol 数据与切股同 commit 到达,
  // 若父 effect 在此无条件清空 (子 effect 先执行, 父 effect 后执行), 会把刚到的数据抹掉,
  // 且没有下一次 onDataChange 触发, 导致信息条永久卡在加载态。改由 symbol 门控显示。
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
        onToggleWatchlist={onToggleWatchlist}
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
          onDataChange={handleDataChange}
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
