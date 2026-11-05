export interface OHLC {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume?: number
  ma5?: number | null
  ma10?: number | null
  ma20?: number | null
  ma60?: number | null
  macd_dif?: number | null
  macd_dea?: number | null
  macd_hist?: number | null
  rsi_6?: number | null
  rsi_14?: number | null
  rsi_24?: number | null
  kdj_k?: number | null
  kdj_d?: number | null
  kdj_j?: number | null
  boll_upper?: number | null
  boll_lower?: number | null
}

export interface ChartMarker {
  date: string
  kind: 'buy' | 'sell' | 'neutral'
  label?: string
  /** 若为 true，标记放在蜡烛上方（如涨停连板标签）。 */
  above?: boolean
  /** 自定义标签颜色，覆盖默认的 kind 对应色。 */
  color?: string
  /** 明确的异动来源，避免把策略买卖点按颜色猜成涨停或炸板。 */
  event?: 'limit_up' | 'broken_limit_up' | 'limit_down' | 'limit_down_recovery'
}

export type DailyMarkerStyle = 'label' | 'candle'

export interface ChartRange {
  start: string
  end: string
  label?: string
  color?: string
}

export interface ChartPriceLine {
  value: number
  label?: string
  color?: string
  start?: string
  end?: string
}

export interface StockInfo {
  name?: string
  total_shares?: number
  float_shares?: number
  /** 扩展数据（key: configId__fieldName），来自 klineDaily 的 ext_columns */
  ext?: Record<string, unknown>
}

export interface VolumeCompareConfig {
  enabled: boolean
  days: number
}

export interface DailyChartProps {
  data: OHLC[]
  assetType?: 'stock' | 'etf' | 'index'
  markers?: ChartMarker[]
  ranges?: ChartRange[]
  priceLines?: ChartPriceLine[]
  height?: number
  showMA?: boolean
  showInfoBar?: boolean
  showMarkers?: boolean
  markerStyle?: DailyMarkerStyle
  onToggleMarkers?: () => void
  stockInfo?: StockInfo
  symbol?: string
  linkedPrice?: number | null
  onDateClick?: (date: string) => void
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  /** 初始可见根数；ECharts 默认 60，新详情按窗口宽度适配；'all' 显示全部。 */
  visibleBars?: number | 'all'
  /** 已激活的子图 key 列表 (含 vol, 按点击顺序) */
  activeIndicators?: string[]
  /** 成交量柱相对前 N 个交易日均量的显示设置 */
  volumeCompare?: VolumeCompareConfig
  /** 加入自选日 (北京时间 YYYY-MM-DD); 有值且落在当前区间内时, 主图画一条「自选」竖虚线 */
  addedDate?: string | null
}

export type ChartRenderer = 'echarts' | 'lightweight'
