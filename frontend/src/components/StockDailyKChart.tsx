import { useCallback, useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Info } from 'lucide-react'
import { api, type KlineRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import {
  EChartsCandlestick,
  OVERLAY_INDICATORS,
  SUB_CHARTS,
  type ChartMarker,
  type ChartPriceLine,
  type ChartRange,
  type OHLC,
  type StockInfo,
} from '@/components/EChartsCandlestick'

const SUB_INFO_H = 16
const SUB_GAP = 4
const MAX_DAYS = 2000

export interface StockDailyKChartResult {
  rows: OHLC[]
  rawRows: KlineRow[]
  stockInfo?: StockInfo
  name?: string
  adjustment?: string
}

interface Props {
  symbol: string
  height?: number
  className?: string
  dateRange?: { start: string; end: string }
  markers?: ChartMarker[]
  ranges?: ChartRange[]
  priceLines?: ChartPriceLine[]
  showLimitMarkers?: boolean
  showIndicatorControls?: boolean
  showMarkerToggle?: boolean
  showMA?: boolean
  showInfoBar?: boolean
  visibleBars?: number
  linkedPrice?: number | null
  onDateClick?: (date: string) => void
  onDataChange?: (result: StockDailyKChartResult) => void
  /** 扩展数据列参数（逗号分隔 config_id.field_name），透传给 klineDaily 接口 */
  extColumns?: string
}

function isValidRow(r: any): boolean {
  return r && r.date != null && r.open != null && r.close != null
}

export function toOHLC(rows: KlineRow[]): OHLC[] {
  return rows
    .filter(isValidRow)
    .map(r => ({
      date: typeof r.date === 'string' ? r.date.slice(0, 10) : String(r.date),
      open: Number(r.open),
      high: Number(r.high),
      low: Number(r.low),
      close: Number(r.close),
      volume: Number(r.volume ?? 0),
      change_pct: r.change_pct != null ? Number(r.change_pct) : null,
      change_amount: r.change_amount != null ? Number(r.change_amount) : null,
      amplitude: r.amplitude != null ? Number(r.amplitude) : null,
      ma5: r.ma5 != null ? Number(r.ma5) : null,
      ma10: r.ma10 != null ? Number(r.ma10) : null,
      ma20: r.ma20 != null ? Number(r.ma20) : null,
      ma60: r.ma60 != null ? Number(r.ma60) : null,
      macd_dif: r.macd_dif != null ? Number(r.macd_dif) : null,
      macd_dea: r.macd_dea != null ? Number(r.macd_dea) : null,
      macd_hist: r.macd_hist != null ? Number(r.macd_hist) : null,
      rsi_6: r.rsi_6 != null ? Number(r.rsi_6) : null,
      rsi_14: r.rsi_14 != null ? Number(r.rsi_14) : null,
      rsi_24: r.rsi_24 != null ? Number(r.rsi_24) : null,
      kdj_k: r.kdj_k != null ? Number(r.kdj_k) : null,
      kdj_d: r.kdj_d != null ? Number(r.kdj_d) : null,
      kdj_j: r.kdj_j != null ? Number(r.kdj_j) : null,
      boll_upper: r.boll_upper != null ? Number(r.boll_upper) : null,
      boll_lower: r.boll_lower != null ? Number(r.boll_lower) : null,
      main_net_inflow: r.main_net_inflow != null ? Number(r.main_net_inflow) : null,
      expma_12: r.expma_12 != null ? Number(r.expma_12) : null,
      expma_50: r.expma_50 != null ? Number(r.expma_50) : null,
      trix: r.trix != null ? Number(r.trix) : null,
      trix_ma: r.trix_ma != null ? Number(r.trix_ma) : null,
      bbi: r.bbi != null ? Number(r.bbi) : null,
      dfma_dif: r.dfma_dif != null ? Number(r.dfma_dif) : null,
      dfma: r.dfma != null ? Number(r.dfma) : null,
      dmi_pdi: r.dmi_pdi != null ? Number(r.dmi_pdi) : null,
      dmi_mdi: r.dmi_mdi != null ? Number(r.dmi_mdi) : null,
      dmi_adx: r.dmi_adx != null ? Number(r.dmi_adx) : null,
      dmi_adxr: r.dmi_adxr != null ? Number(r.dmi_adxr) : null,
      xsii_upper: r.xsii_upper != null ? Number(r.xsii_upper) : null,
      xsii_lower: r.xsii_lower != null ? Number(r.xsii_lower) : null,
      xsii_mid: r.xsii_mid != null ? Number(r.xsii_mid) : null,
      wr_14: r.wr_14 != null ? Number(r.wr_14) : null,
      cci_14: r.cci_14 != null ? Number(r.cci_14) : null,
      psy_12: r.psy_12 != null ? Number(r.psy_12) : null,
      psyma_6: r.psyma_6 != null ? Number(r.psyma_6) : null,
      bias_6: r.bias_6 != null ? Number(r.bias_6) : null,
      bias_12: r.bias_12 != null ? Number(r.bias_12) : null,
      bias_24: r.bias_24 != null ? Number(r.bias_24) : null,
      roc_12: r.roc_12 != null ? Number(r.roc_12) : null,
      roc_ma_6: r.roc_ma_6 != null ? Number(r.roc_ma_6) : null,
      mtm_12: r.mtm_12 != null ? Number(r.mtm_12) : null,
      mtm_ma_6: r.mtm_ma_6 != null ? Number(r.mtm_ma_6) : null,
      dpo_20: r.dpo_20 != null ? Number(r.dpo_20) : null,
      dpo_ma_6: r.dpo_ma_6 != null ? Number(r.dpo_ma_6) : null,
      ktn_mid: r.ktn_mid != null ? Number(r.ktn_mid) : null,
      ktn_upper: r.ktn_upper != null ? Number(r.ktn_upper) : null,
      ktn_lower: r.ktn_lower != null ? Number(r.ktn_lower) : null,
      taq_mid: r.taq_mid != null ? Number(r.taq_mid) : null,
      taq_upper: r.taq_upper != null ? Number(r.taq_upper) : null,
      taq_lower: r.taq_lower != null ? Number(r.taq_lower) : null,
      obv: r.obv != null ? Number(r.obv) : null,
      vr_26: r.vr_26 != null ? Number(r.vr_26) : null,
      emv_14: r.emv_14 != null ? Number(r.emv_14) : null,
      emv_ma_14: r.emv_ma_14 != null ? Number(r.emv_ma_14) : null,
      mfi_14: r.mfi_14 != null ? Number(r.mfi_14) : null,
      cr_26: r.cr_26 != null ? Number(r.cr_26) : null,
      mass_9_25: r.mass_9_25 != null ? Number(r.mass_9_25) : null,
      asi: r.asi != null ? Number(r.asi) : null,
    }))
}

function buildLimitUpMarkers(rows: KlineRow[]): ChartMarker[] {
  const markers: ChartMarker[] = []
  for (const r of rows) {
    const date = typeof r.date === 'string' ? r.date.slice(0, 10) : String(r.date)
    if (r.signal_broken_limit_up) {
      markers.push({ date, kind: 'neutral', above: true, color: '#8B5CF6', label: '炸' })
    } else if (r.signal_limit_up) {
      const boards: number = r.consecutive_limit_ups ?? 1
      markers.push({ date, kind: 'buy', above: true, color: '#FACC15', label: boards <= 1 ? '板' : String(boards) })
    }
  }
  return markers
}

export function getDefaultRange(): { start: string; end: string } {
  const now = new Date()
  const end = now.toISOString().slice(0, 10)
  const s = new Date(now)
  s.setMonth(s.getMonth() - 6)
  const start = s.toISOString().slice(0, 10)
  return { start, end }
}

function rangeDays(range: { start: string; end: string }): number {
  const start = new Date(range.start)
  const end = new Date(range.end)
  return Math.min(Math.ceil((end.getTime() - start.getTime()) / 86400000) + 30, MAX_DAYS)
}

export function StockDailyKChart({
  symbol,
  height = 520,
  className,
  dateRange: externalDateRange,
  markers,
  ranges,
  priceLines,
  showLimitMarkers = true,
  showIndicatorControls = true,
  showMarkerToggle = true,
  showMA = true,
  showInfoBar = true,
  visibleBars = 60,
  linkedPrice,
  onDateClick,
  onDataChange,
  extColumns,
}: Props) {
  const [activeIndicators, setActiveIndicators] = useState<string[]>(['vol'])
  const [showMarkers, setShowMarkers] = useState(true)
  const dateRange = externalDateRange ?? getDefaultRange()
  const days = useMemo(() => rangeDays(dateRange), [dateRange])

  // extColumns 纳入 query key：勾选/取消扩展字段时需重新请求（带 ext_columns 参数）
  const kline = useQuery({
    queryKey: QK.kline(symbol, dateRange.start, dateRange.end, extColumns),
    queryFn: () => api.klineDaily(symbol, days, dateRange, extColumns),
    enabled: !!symbol,
    placeholderData: (prev) => prev,
  })

  const rows = useMemo(() => toOHLC(kline.data?.rows ?? []), [kline.data?.rows])
  const stockInfo = kline.data?.stock_info
  const limitMarkers = useMemo(() => buildLimitUpMarkers(kline.data?.rows ?? []), [kline.data?.rows])
  const allMarkers = useMemo(() => [
    ...(markers ?? []),
    ...(showLimitMarkers ? limitMarkers : []),
  ], [limitMarkers, markers, showLimitMarkers])

  const toggleIndicator = useCallback((key: string) => {
    setActiveIndicators(prev => prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key])
  }, [])

  const renderIndicatorButton = (ind: { key: string; label: string; description?: string }) => (
    <button
      key={ind.key}
      type="button"
      title={ind.description}
      onClick={() => toggleIndicator(ind.key)}
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
        activeIndicators.includes(ind.key)
          ? 'bg-accent/20 text-accent'
          : 'bg-elevated text-muted hover:text-secondary'
      }`}
    >
      <span>{ind.label}</span>
      {ind.description && <Info className="h-3 w-3 opacity-70" strokeWidth={1.5} />}
    </button>
  )

  const activeSubDefs = activeIndicators
    .map(key => SUB_CHARTS.find(s => s.key === key))
    .filter((d): d is typeof SUB_CHARTS[number] => !!d)
  let subExtraH = 0
  activeSubDefs.forEach(def => { subExtraH += SUB_INFO_H + def.height })
  if (activeSubDefs.length > 0) subExtraH += activeSubDefs.length * SUB_GAP + 14
  const chartHeight = height + subExtraH

  useEffect(() => {
    onDataChange?.({ rows, rawRows: kline.data?.rows ?? [], stockInfo, name: kline.data?.name, adjustment: kline.data?.adjustment })
  }, [kline.data?.adjustment, kline.data?.name, kline.data?.rows, onDataChange, rows, stockInfo])

  if (!symbol) return null

  return (
    <div className={className} style={{ minHeight: chartHeight }}>
      {showIndicatorControls && rows.length > 0 && (
        <div className="flex items-center gap-1.5 px-1 pb-0.5">
          {SUB_CHARTS.map(renderIndicatorButton)}
          {OVERLAY_INDICATORS.map(renderIndicatorButton)}
          {showMarkerToggle && showLimitMarkers && (
            <button
              onClick={() => setShowMarkers(v => !v)}
              className={`ml-auto px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
                showMarkers
                  ? 'text-[#FACC15] bg-[#FACC15]/10'
                  : 'bg-elevated text-muted hover:text-secondary'
              }`}
            >
              异动
            </button>
          )}
        </div>
      )}
      {kline.isLoading && <div className="text-sm text-muted py-4">加载中…</div>}
      {kline.isError && <div className="text-sm text-danger py-2">日K加载失败</div>}
      {!kline.isLoading && !kline.isError && (kline.data?.rows?.length ?? 0) > 0 && rows.length === 0 && (
        <div className="text-sm text-danger py-2">数据格式异常，请刷新页面</div>
      )}
      {rows.length > 0 && (
        <EChartsCandlestick
          data={rows}
          markers={allMarkers}
          ranges={ranges}
          priceLines={priceLines}
          height={chartHeight - 22}
          showMA={showMA}
          showInfoBar={showInfoBar}
          showMarkers={showMarkers}
          stockInfo={stockInfo}
          symbol={symbol}
          linkedPrice={linkedPrice}
          onDateClick={onDateClick}
          visibleBars={visibleBars}
          activeIndicators={activeIndicators}
        />
      )}
    </div>
  )
}
