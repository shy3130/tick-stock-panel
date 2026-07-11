import { useEffect, useRef, useCallback, useMemo } from 'react'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import { computeTdSequential } from '@/lib/tdSequential'
import { buildClusterSignals, type LockKey } from '@/lib/threeLocks'

export interface OHLC {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume?: number
  change_pct?: number | null
  change_amount?: number | null
  amplitude?: number | null
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
  main_net_inflow?: number | null
}

export interface ChartMarker {
  date: string
  kind: 'buy' | 'sell' | 'neutral'
  label?: string
  /** 若为 true，标记放在蜡烛上方（如涨停连板标签）。 */
  above?: boolean
  /** 自定义标签颜色，覆盖默认的 kind 对应色。 */
  color?: string
}

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

/** 子图定义 */
export interface SubChartDef {
  key: string
  label: string
  description?: string
  /** 子图固定高度 px */
  height: number
  /** 构建 series 数组 */
  buildSeries: (data: OHLC[]) => any[]
  /** 构建信息栏文字 (当前数据行 -> 显示内容) */
  buildInfo: (d: OHLC | null) => { label: string; color: string; value: string }[]
  /** Y 轴特殊配置 */
  yAxisConfig?: Record<string, any>
}

// ===== 成交量 N 日均量 =====
function volMaN(data: OHLC[], n: number): (number | null)[] {
  const result: (number | null)[] = []
  for (let i = 0; i < data.length; i++) {
    if (i < n - 1) { result.push(null); continue }
    let sum = 0
    for (let j = i - n + 1; j <= i; j++) sum += data[j].volume ?? 0
    result.push(sum / n)
  }
  return result
}

function fmtVol(v: number | null | undefined): string {
  if (v == null) return '—'
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(0) + '万'
  return v.toFixed(0)
}

function pctPoints(d: OHLC, prev?: OHLC | null): number | null {
  if (d.change_pct != null && Number.isFinite(d.change_pct)) return d.change_pct * 100
  return prev?.close ? ((d.close - prev.close) / prev.close) * 100 : null
}

export const SUB_CHARTS: SubChartDef[] = [
  {
    key: 'vol',
    label: '成交量',
    description: '成交量柱状图,附带 VOL5 / VOL10 均量线。',
    height: 84,
    yAxisConfig: { min: 0 },
    buildSeries: (data) => {
      const ma5Data = volMaN(data, 5)
      const ma10Data = volMaN(data, 10)
      return [
        {
          name: '成交量',
          type: 'bar',
          data: data.map(d => ({
            value: d.volume ?? 0,
            itemStyle: {
              color: d.close >= d.open ? 'rgba(240,68,56,0.6)' : 'rgba(18,183,106,0.6)',
            },
          })),
          barWidth: '60%',
          animation: false,
        },
        {
          name: 'VOL5',
          type: 'line',
          data: ma5Data,
          smooth: true, symbol: 'none', animation: false,
          lineStyle: { width: 1, color: '#FACC15' },
          itemStyle: { color: '#FACC15' },
        },
        {
          name: 'VOL10',
          type: 'line',
          data: ma10Data,
          smooth: true, symbol: 'none', animation: false,
          lineStyle: { width: 1, color: '#8B5CF6' },
          itemStyle: { color: '#8B5CF6' },
        },
      ]
    },
    buildInfo: (d) => {
      if (!d) return []
      return [
        { label: '量', color: d.close >= d.open ? '#C74040' : '#2D9B65', value: fmtVol(d.volume) },
      ]
    },
  },
  {
    key: 'macd',
    label: 'MACD',
    description: 'DIF / DEA 趋势线和 MACD 柱,用于观察趋势动能变化。',
    height: 72,
    buildSeries: (data) => [
      {
        name: 'DIF',
        type: 'line',
        data: data.map(d => d.macd_dif != null ? Number(d.macd_dif) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#FACC15' },
        itemStyle: { color: '#FACC15' },
      },
      {
        name: 'DEA',
        type: 'line',
        data: data.map(d => d.macd_dea != null ? Number(d.macd_dea) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#8B5CF6' },
        itemStyle: { color: '#8B5CF6' },
      },
      {
        name: 'MACD',
        type: 'bar',
        data: data.map(d => {
          const v = d.macd_hist
          if (v == null) return '-'
          return {
            value: Number(v),
            itemStyle: { color: Number(v) >= 0 ? 'rgba(240,68,56,0.6)' : 'rgba(18,183,106,0.6)' },
          }
        }),
        barWidth: '40%',
        animation: false,
      },
    ],
    buildInfo: (d) => {
      if (!d) return []
      return [
        { label: 'DIF', color: '#FACC15', value: d.macd_dif != null ? d.macd_dif.toFixed(3) : '—' },
        { label: 'DEA', color: '#8B5CF6', value: d.macd_dea != null ? d.macd_dea.toFixed(3) : '—' },
        { label: 'MACD', color: d.macd_hist != null && d.macd_hist >= 0 ? '#C74040' : '#2D9B65', value: d.macd_hist != null ? d.macd_hist.toFixed(3) : '—' },
      ]
    },
  },
  {
    key: 'rsi',
    label: 'RSI',
    description: 'RSI6 / RSI14 / RSI24 相对强弱指标,用于观察超买超卖和强弱切换。',
    height: 72,
    yAxisConfig: { min: 0, max: 100 },
    buildSeries: (data) => [
      {
        name: 'RSI6',
        type: 'line',
        data: data.map(d => d.rsi_6 != null ? Number(d.rsi_6) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#FACC15' },
        itemStyle: { color: '#FACC15' },
      },
      {
        name: 'RSI14',
        type: 'line',
        data: data.map(d => d.rsi_14 != null ? Number(d.rsi_14) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#3B82F6' },
        itemStyle: { color: '#3B82F6' },
      },
      {
        name: 'RSI24',
        type: 'line',
        data: data.map(d => d.rsi_24 != null ? Number(d.rsi_24) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#8B5CF6' },
        itemStyle: { color: '#8B5CF6' },
      },
    ],
    buildInfo: (d) => {
      if (!d) return []
      return [
        { label: 'RSI6', color: '#FACC15', value: d.rsi_6 != null ? d.rsi_6.toFixed(1) : '—' },
        { label: 'RSI14', color: '#3B82F6', value: d.rsi_14 != null ? d.rsi_14.toFixed(1) : '—' },
        { label: 'RSI24', color: '#8B5CF6', value: d.rsi_24 != null ? d.rsi_24.toFixed(1) : '—' },
      ]
    },
  },
  {
    key: 'kdj',
    label: 'KDJ',
    description: 'K / D / J 随机指标,用于观察短线拐点和钝化状态。',
    height: 72,
    buildSeries: (data) => [
      {
        name: 'K',
        type: 'line',
        data: data.map(d => d.kdj_k != null ? Number(d.kdj_k) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#FACC15' },
        itemStyle: { color: '#FACC15' },
      },
      {
        name: 'D',
        type: 'line',
        data: data.map(d => d.kdj_d != null ? Number(d.kdj_d) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#3B82F6' },
        itemStyle: { color: '#3B82F6' },
      },
      {
        name: 'J',
        type: 'line',
        data: data.map(d => d.kdj_j != null ? Number(d.kdj_j) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#8B5CF6' },
        itemStyle: { color: '#8B5CF6' },
      },
    ],
    buildInfo: (d) => {
      if (!d) return []
      return [
        { label: 'K', color: '#FACC15', value: d.kdj_k != null ? d.kdj_k.toFixed(1) : '—' },
        { label: 'D', color: '#3B82F6', value: d.kdj_d != null ? d.kdj_d.toFixed(1) : '—' },
        { label: 'J', color: '#8B5CF6', value: d.kdj_j != null ? d.kdj_j.toFixed(1) : '—' },
      ]
    },
  },
]

/** 向后兼容的 INDICATORS 导出 (不含 vol) */
export const INDICATORS = SUB_CHARTS.filter(s => s.key !== 'vol')

/** 主图叠加指标 (画在 K 线上方, 不占副图空间) */
export const OVERLAY_INDICATORS: { key: string; label: string; description?: string }[] = [
  { key: 'boll', label: 'BOLL', description: '布林带上轨/下轨,用于观察价格通道和波动边界。' },
  { key: 'td9', label: '九转', description: 'TD Sequential 九转计数,用于提示连续上涨/下跌后的潜在转折点。' },
  {
    key: 'threelock',
    label: '三锁',
    description: '红色顶点=趋势锁,粉色左下=资金锁,金色右下=形态锁;实心=满足,空心=不满足;三把实心=三锁全开,绿色开锁=三锁刚破开。',
  },
]

interface Props {
  data: OHLC[]
  markers?: ChartMarker[]
  ranges?: ChartRange[]
  priceLines?: ChartPriceLine[]
  height?: number
  showMA?: boolean
  showInfoBar?: boolean
  showMarkers?: boolean
  onToggleMarkers?: () => void
  stockInfo?: StockInfo
  symbol?: string
  linkedPrice?: number | null
  onDateClick?: (date: string) => void
  /** 默认可见蜡烛根数, 默认 60 */
  visibleBars?: number
  /** 已激活的子图 key 列表 (含 vol, 按点击顺序) */
  activeIndicators?: string[]
}

const THEME = {
  bull: '#C74040',
  bear: '#2D9B65',
  bullAlpha: 'rgba(240,68,56,0.7)',
  bearAlpha: 'rgba(18,183,106,0.7)',
  ma5: '#A1A1AA',
  ma10: '#3B82F6',
  ma20: '#F97316',
  ma60: '#8B5CF6',
  text: '#A1A1AA',
  grid: 'rgba(255,255,255,0.04)',
  border: '#27272A',
  bg: 'transparent',
}

const CLOSED_LOCK_SYMBOL =
  'path://M256 448L256 352C256 192 368 96 512 96C656 96 768 192 768 352L768 448L672 448L672 352C672 248 608 192 512 192C416 192 352 248 352 352L352 448ZM224 448H800C835 448 864 477 864 512V864C864 899 835 928 800 928H224C189 928 160 899 160 864V512C160 477 189 448 224 448Z'

const OPEN_LOCK_SYMBOL =
  'path://M224 448H800C835 448 864 477 864 512V864C864 899 835 928 800 928H224C189 928 160 899 160 864V512C160 477 189 448 224 448ZM256 448L256 360C256 200 368 104 512 104C612 104 696 154 744 232L656 288C624 232 576 200 512 200C416 200 352 256 352 360L352 448ZM680 126L776 74C854 212 832 340 716 422L650 346C724 294 734 224 680 126Z'

const LOCK_COLOR: Record<LockKey, string> = {
  trend: '#d23b3b',
  capital: '#c46a7a',
  pattern: '#d99930',
}

const LOCK_WEAK_FILL: Record<LockKey, string> = {
  trend: 'rgba(210,59,59,0.16)',
  capital: 'rgba(196,106,122,0.18)',
  pattern: 'rgba(217,153,48,0.16)',
}

const LOCK_POSITIONS: Array<{ key: LockKey; offset: [number, number] }> = [
  { key: 'trend', offset: [0, 0] },
  { key: 'capital', offset: [-6, 9] },
  { key: 'pattern', offset: [6, 9] },
]

/** 可见蜡烛超过此数量时，涨停/炸板标签切换为小圆点。 */
const COMPACT_THRESHOLD = 60

/** 子图上方信息栏高度 (px) */
const INFO_BAR_H = 16
/** 子图之间的间距 (px) */
const SUB_GAP_PX = 4

function buildSubInfoGraphics(
  data: OHLC[],
  infoIdx: number,
  activeIndicators: string[],
  subStartTop: number,
): any[] {
  const d = infoIdx >= 0 && infoIdx < data.length ? data[infoIdx] : null
  const graphics: any[] = []
  let curTop = subStartTop

  activeIndicators.forEach((key) => {
    const def = SUB_CHARTS.find(s => s.key === key)
    if (!def) return

    const items = def.buildInfo(d)
    if (def.key === 'vol' && d) {
      const calcVolMa = (n: number) => {
        if (infoIdx < n - 1) return null
        let sum = 0
        for (let j = infoIdx - n + 1; j <= infoIdx; j++) sum += data[j].volume ?? 0
        return sum / n
      }
      const vol5 = calcVolMa(5)
      const vol10 = calcVolMa(10)
      items.push({ label: 'VOL5', color: '#FACC15', value: fmtVol(vol5) })
      items.push({ label: 'VOL10', color: '#8B5CF6', value: fmtVol(vol10) })
    }

    // 每个元素加固定 id，确保 ECharts 增量更新时能正确匹配
    graphics.push({
      id: `sub-sep-${key}`,
      type: 'line',
      shape: { x1: 0, y1: curTop, x2: 2000, y2: curTop },
      style: { stroke: 'rgba(255,255,255,0.08)', lineWidth: 1 },
      silent: true, z: 0,
    })
    graphics.push({
      id: `sub-label-${key}`,
      type: 'text',
      style: {
        text: def.label,
        x: 4, y: curTop + 4,
        fill: '#8E8E96',
        fontSize: 10, fontFamily: 'JetBrains Mono, monospace',
        fontWeight: 'bold',
      },
      silent: true, z: 10,
    })

    const richTextParts: string[] = []
    const rich: Record<string, any> = {}
    items.forEach((item, idx) => {
      const styleKey = `s${idx}`
      richTextParts.push(`{${styleKey}|${item.label}:${item.value}}`)
      rich[styleKey] = {
        fill: item.color,
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
      }
    })
    graphics.push({
      id: `sub-val-${key}`,
      type: 'text',
      right: 24,
      style: {
        text: richTextParts.join(`{gap|  }`),
        y: curTop + 3,
        rich: {
          gap: { fill: 'transparent', fontSize: 10 },
          ...rich,
        },
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
        textAlign: 'right',
        textVerticalAlign: 'top',
      },
      silent: true, z: 10,
    })

    curTop += INFO_BAR_H + def.height + SUB_GAP_PX
  })

  return graphics
}

function buildMarkPointData(
  data: OHLC[],
  dateIndexMap: Map<string, number>,
  markers: ChartMarker[] | undefined,
  compact: boolean,
  activeIndicators: string[],
): any[] {
  const markPointData: any[] = []

  if (markers && markers.length > 0) {
    for (const m of markers) {
      const idx = dateIndexMap.get(m.date)
      if (idx == null) continue
      const d = data[idx]
      const isBuy = m.kind === 'buy'
      const isSell = m.kind === 'sell'

      if (m.above) {
        const dotColor = m.color ?? (isBuy ? '#FACC15' : THEME.text)
        if (compact) {
          markPointData.push({
            name: m.date, coord: [m.date, d.high],
            symbol: 'circle', symbolSize: 4, symbolOffset: [0, -10],
            itemStyle: { color: dotColor, cursor: 'pointer' },
            label: { show: false }, z: 100, zlevel: 10,
          })
        } else {
          markPointData.push({
            name: m.date, coord: [m.date, d.high],
            symbol: 'circle', symbolSize: 12, symbolOffset: [0, -2],
            itemStyle: { color: 'transparent' },
            label: {
              show: true, formatter: m.label ?? '', position: 'top', distance: 0,
              color: dotColor, fontSize: 10, fontWeight: 'normal',
              fontFamily: 'JetBrains Mono, monospace',
            },
            z: 100, zlevel: 10,
          })
        }
      } else {
        markPointData.push({
          name: m.date,
          coord: [m.date, isBuy ? d.low : d.high],
          symbol: 'arrow', symbolSize: 12,
          symbolRotate: isBuy ? 0 : 180,
          symbolOffset: isBuy ? [0, '60%'] : [0, '-60%'],
          itemStyle: { color: isBuy ? THEME.bull : isSell ? THEME.bear : THEME.text },
          label: {
            show: !!m.label, formatter: m.label ?? '',
            position: isBuy ? 'bottom' : 'top', distance: 8,
            color: THEME.text, fontSize: 10,
            fontFamily: 'JetBrains Mono, monospace',
          },
        })
      }
    }
  }

  if (activeIndicators.includes('td9')) {
    const closes = data.map(d => d.close)
    const td = computeTdSequential(closes)
    for (const m of td.markers) {
      if (compact && m.count !== 9) continue
      const bar = data[m.i]
      if (!bar) continue
      const isTop = m.kind === 'top'
      const color = isTop ? THEME.bear : THEME.bull
      const coord: [string, number] = isTop ? [bar.date, bar.high] : [bar.date, bar.low]
      const offsetY = isTop ? -12 : 12
      if (m.count === 9) {
        markPointData.push({
          name: bar.date, coord,
          symbol: 'roundRect', symbolSize: [16, 18], symbolOffset: [0, offsetY],
          itemStyle: { color },
          label: {
            show: true, formatter: '9', color: '#fff', fontSize: 11, fontWeight: 'bold',
            fontFamily: 'JetBrains Mono, monospace',
          },
          z: 100, zlevel: 10,
        })
      } else {
        markPointData.push({
          name: bar.date, coord,
          symbol: 'circle', symbolSize: 1, symbolOffset: [0, offsetY],
          itemStyle: { color: 'transparent' },
          label: {
            show: true, formatter: String(m.count), color, fontSize: 10,
            fontFamily: 'JetBrains Mono, monospace',
          },
          z: 100, zlevel: 10,
        })
      }
    }
  }

  if (activeIndicators.includes('threelock')) {
    const lockRows = data.map(d => ({
      date: d.date,
      high: d.high,
      low: d.low,
      close: d.close,
      volume: d.volume,
      main_net_inflow: d.main_net_inflow,
    }))
    let prevAllOpen = false
    for (const signal of buildClusterSignals(lockRows)) {
      const bar = data[signal.index]
      if (!bar) continue
      const allOpen = signal.states.trend && signal.states.capital && signal.states.pattern
      const justOpened = !prevAllOpen && allOpen
      const justBroken = prevAllOpen && !allOpen
      prevAllOpen = allOpen

      const coord: [string, number] = justOpened ? [bar.date, bar.low] : [bar.date, bar.high]
      const direction = justOpened ? 1 : -1
      const highlightColor = justOpened ? THEME.bull : justBroken ? THEME.bear : undefined

      if (compact) {
        markPointData.push({
          name: bar.date,
          coord,
          symbol: 'circle',
          symbolSize: highlightColor ? 6 : 4,
          symbolOffset: [0, direction > 0 ? 14 : -14],
          itemStyle: {
            color: highlightColor ?? THEME.text,
            borderColor: highlightColor,
            borderWidth: highlightColor ? 1 : 0,
          },
          label: { show: false },
          z: 100, zlevel: 10,
        })
        continue
      }

      if (justBroken) {
        markPointData.push({
          name: bar.date,
          coord,
          symbol: OPEN_LOCK_SYMBOL,
          symbolSize: [16, 16],
          symbolOffset: [0, -24],
          itemStyle: { color: THEME.bear },
          label: { show: false },
          z: 100, zlevel: 10,
        })
        continue
      }

      for (const { key, offset } of LOCK_POSITIONS) {
        const locked = signal.states[key]
        const color = LOCK_COLOR[key]
        markPointData.push({
          name: bar.date,
          coord,
          symbol: CLOSED_LOCK_SYMBOL,
          symbolSize: [7, 7],
          symbolOffset: [offset[0], direction > 0 ? offset[1] + 8 : offset[1] - 23],
          itemStyle: locked
            ? { color }
            : { color: LOCK_WEAK_FILL[key], borderColor: color, borderWidth: 1.4 },
          label: { show: false },
          z: 100, zlevel: 10,
        })
      }
    }
  }

  return markPointData
}

function buildOption(
  data: OHLC[],
  dates: string[],
  dateIndexMap: Map<string, number>,
  markers: ChartMarker[] | undefined,
  ranges: ChartRange[] | undefined,
  priceLines: ChartPriceLine[] | undefined,
  showMA: boolean,
  compact: boolean,
  activeIndicators: string[],
  containerHeight: number,
  infoIdx: number,
  linkedPrice: number | null | undefined,
): EChartsOption {
  const candleData = data.map(d => [d.open, d.close, d.low, d.high])

  const hasMA = showMA && data.some(d => d.ma5 != null || d.ma10 != null || d.ma20 != null || d.ma60 != null)

  const markPointData = buildMarkPointData(data, dateIndexMap, markers, compact, activeIndicators)

  // ====== 布局计算 ======
  const left = 60
  const right = 20
  const topPad = 8
  const candleBottomPad = 22

  let subTotalH = 0
  const activeSubDefs: SubChartDef[] = []
  activeIndicators.forEach(key => {
    const def = SUB_CHARTS.find(s => s.key === key)
    if (!def) return
    activeSubDefs.push(def)
    subTotalH += INFO_BAR_H + def.height
  })
  if (activeSubDefs.length > 0) subTotalH += activeSubDefs.length * SUB_GAP_PX

  const candleAvail = Math.max(containerHeight - topPad - candleBottomPad - subTotalH, 100)

  const grids: any[] = []
  const xAxes: any[] = []
  const yAxes: any[] = []
  const series: any[] = []
  const xAxisIndices: number[] = []

  // ===== grid 0: K线主图 =====
  grids.push({ left, right, top: topPad, height: candleAvail })
  xAxes.push({
    type: 'category', data: dates, boundaryGap: true,
    axisLine: { lineStyle: { color: THEME.border } },
    axisLabel: { color: THEME.text, fontSize: 10, fontFamily: 'JetBrains Mono, monospace' },
    axisTick: { show: false },
    splitLine: { show: false },
  })
  yAxes.push({
    scale: true,
    // 上下各留 3% 边距: 防止最高/最低点的蜡烛贴边, 涨停/炸板标签被遮挡
    boundaryGap: [0.03, 0.03],
    splitArea: { show: false },
    axisLine: { show: false }, axisTick: { show: false },
    splitLine: { lineStyle: { color: THEME.grid } },
    axisLabel: { color: THEME.text, fontSize: 10, fontFamily: 'JetBrains Mono, monospace' },
  })
  xAxisIndices.push(0)

  const markAreaData = (ranges ?? [])
    .filter(r => dateIndexMap.has(r.start) && dateIndexMap.has(r.end))
    .map(r => ([
      {
        name: r.label ?? '',
        xAxis: r.start,
        itemStyle: { color: r.color ?? 'rgba(59,130,246,0.08)' },
        label: {
          show: !!r.label,
          position: 'insideTop',
          distance: 8,
          color: '#DBEAFE',
          backgroundColor: 'rgba(15,23,42,0.72)',
          borderColor: 'rgba(59,130,246,0.35)',
          borderWidth: 1,
          borderRadius: 4,
          padding: [2, 6],
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
        },
      },
      { xAxis: r.end },
    ]))

  const markLineData: any[] = (priceLines ?? [])
    .filter(line => Number.isFinite(line.value))
    .map(line => {
      const lineStyle = {
        color: line.color ?? THEME.text,
        type: 'dashed' as const,
        width: 1,
        opacity: 0.92,
      }
      const label = {
        show: !!line.label,
        formatter: line.label ?? '',
        position: 'insideEndTop' as const,
        color: line.color ?? THEME.text,
        backgroundColor: 'rgba(15,23,42,0.72)',
        borderRadius: 4,
        padding: [2, 6],
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
      }
      if (line.start && line.end && dateIndexMap.has(line.start) && dateIndexMap.has(line.end)) {
        return [
          { xAxis: line.start, yAxis: line.value },
          { xAxis: line.end, yAxis: line.value, lineStyle, label, symbol: 'none' },
        ]
      }
      return { yAxis: line.value, lineStyle, label, symbol: 'none' }
    })

  if (linkedPrice != null) {
    markLineData.push({
      yAxis: linkedPrice,
      lineStyle: { color: '#3B82F6', type: 'dashed', width: 1, opacity: 0.7 },
      label: {
        show: true,
        formatter: linkedPrice.toFixed(2),
        position: 'insideEndTop',
        color: '#3B82F6',
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
        backgroundColor: 'rgba(24,24,27,0.85)',
        borderColor: '#3B82F6',
        borderWidth: 1,
        padding: [1, 4],
        borderRadius: 2,
      },
      symbol: 'none',
    })
  }

  series.push({
    name: 'K', type: 'candlestick', data: candleData,
    animation: false,
    itemStyle: {
      color: THEME.bull, color0: THEME.bear,
      borderColor: THEME.bull, borderColor0: THEME.bear,
      cursor: 'pointer',
    },
    markPoint: markPointData.length > 0 ? { data: markPointData, animation: false, symbolKeepAspect: true } : undefined,
    markArea: markAreaData.length > 0 ? { silent: true, data: markAreaData } : undefined,
    markLine: markLineData.length > 0 ? { silent: true, symbol: 'none', data: markLineData, animation: false } : undefined,
  })

  if (hasMA) {
    const maLine = (key: keyof OHLC, color: string, name: string) => ({
      name, type: 'line',
      data: data.map(d => (d[key] != null ? Number(d[key]) : '-')),
      smooth: true, symbol: 'none', animation: false,
      silent: true,
      lineStyle: { width: 1, color }, itemStyle: { color },
    })
    series.push(maLine('ma5', THEME.ma5, 'MA5'))
    series.push(maLine('ma10', THEME.ma10, 'MA10'))
    series.push(maLine('ma20', THEME.ma20, 'MA20'))
    series.push(maLine('ma60', THEME.ma60, 'MA60'))
  }

  // BOLL 布林带 — 需在 activeIndicators 中激活
  const showBOLL = activeIndicators.includes('boll') && data.some(d => d.boll_upper != null || d.boll_lower != null)
  if (showBOLL) {
    const bollLine = (key: keyof OHLC, color: string, name: string) => ({
      name, type: 'line',
      data: data.map(d => (d[key] != null ? Number(d[key]) : '-')),
      smooth: true, symbol: 'none', animation: false,
      silent: true,
      lineStyle: { width: 1, color, type: 'dashed' as const }, itemStyle: { color },
    })
    series.push(bollLine('boll_upper', '#E879F9', 'BOLL上'))
    series.push(bollLine('boll_lower', '#E879F9', 'BOLL下'))
  }

  // ===== 子图区域 =====
  let curTop = topPad + candleAvail + candleBottomPad

  activeSubDefs.forEach((def, i) => {
    const gridIdx = i + 1
    const xAxisIdx = i + 1
    const yAxisIdx = i + 1

    const chartTop = curTop + INFO_BAR_H
    grids.push({
      left, right,
      top: chartTop,
      height: def.height,
      show: true,
      borderColor: 'rgba(255,255,255,0.06)',
      borderWidth: 1,
    })

    xAxes.push({
      type: 'category', gridIndex: gridIdx, data: dates, boundaryGap: true,
      axisLine: { show: false }, axisLabel: { show: false },
      axisTick: { show: false }, splitLine: { show: false },
      axisPointer: { label: { show: false } },
    })

    const isFixedRange = !!def.yAxisConfig
    yAxes.push({
      scale: !isFixedRange,
      ...(isFixedRange ? def.yAxisConfig : {}),
      gridIndex: gridIdx,
      splitNumber: 2,
      axisLine: { show: false }, axisTick: { show: false },
      splitLine: { lineStyle: { color: THEME.grid } },
      axisLabel: {
        show: true, color: THEME.text, fontSize: 9,
        fontFamily: 'JetBrains Mono, monospace',
      },
    })

    xAxisIndices.push(xAxisIdx)

    const subSeries = def.buildSeries(data)
    subSeries.forEach((s: any) => {
      series.push({ ...s, xAxisIndex: xAxisIdx, yAxisIndex: yAxisIdx })
    })

    curTop += INFO_BAR_H + def.height + SUB_GAP_PX
  })

  // 子图信息栏 graphic
  const subStartTop = topPad + candleAvail + candleBottomPad
  const infoGraphics = buildSubInfoGraphics(data, infoIdx, activeIndicators, subStartTop)

  return {
    animation: false,
    backgroundColor: THEME.bg,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross', crossStyle: { color: '#555' } },
      backgroundColor: 'transparent',
      borderWidth: 0,
      textStyle: { fontSize: 0 },
      formatter: () => '',
    },
    axisPointer: {
      link: [{ xAxisIndex: 'all' }],
      label: {
        backgroundColor: '#333',
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 10,
      },
    },
    graphic: infoGraphics.length > 0 ? infoGraphics : undefined,
    grid: grids,
    xAxis: xAxes,
    yAxis: yAxes,
    dataZoom: [
      {
        type: 'inside',
        xAxisIndex: xAxisIndices,
        start: 0,
        end: 100,
        moveOnMouseMove: true,
        zoomOnMouseWheel: true,
      },
    ],
    series,
  }
}


export function EChartsCandlestick({
  data,
  markers,
  ranges,
  priceLines,
  height = 480,
  showMA = true,
  showInfoBar = true,
  showMarkers: showMarkersProp = true,
  onToggleMarkers: _onToggleMarkers,
  stockInfo,
  symbol: _symbol,
  linkedPrice,
  onDateClick,
  visibleBars = 60,
  activeIndicators = [],
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ECharts | null>(null)
  const dataRef = useRef(data)
  dataRef.current = data
  const onDateClickRef = useRef(onDateClick)
  onDateClickRef.current = onDateClick

  // --- 全部用 ref，避免高频交互触发 React 重渲染 ---
  const infoIdxRef = useRef<number>(data.length - 1)
  const compactRef = useRef(false)
  const userZoomRef = useRef<{ start: number; end: number } | null>(null)

  // 需要在闭包中访问最新值的变量 — 先声明占位，后面赋值
  const activeIndicatorsRef = useRef(activeIndicators)
  activeIndicatorsRef.current = activeIndicators
  const markersRef = useRef(markers)
  markersRef.current = markers
  const showMarkersRef = useRef(showMarkersProp)
  showMarkersRef.current = showMarkersProp
  const chartHeightRef = useRef(300)
  const subTotalHRef = useRef(0)
  const getInfoBarHTMLRef = useRef<() => string>(() => '')

  // 强制刷新信息栏 DOM 的回调
  const infoBarRef = useRef<HTMLDivElement>(null)
  const triggerInfoBarUpdate = useRef(() => {
    const idx = infoIdxRef.current
    const curData = dataRef.current
    const d = idx >= 0 && idx < curData.length ? curData[idx] : null
    if (!d) return
    const chart = chartRef.current
    if (!chart) return
    const subStartTop = chartHeightRef.current - subTotalHRef.current
    const infoGraphics = buildSubInfoGraphics(curData, idx, activeIndicatorsRef.current, subStartTop)
    if (infoGraphics.length > 0) {
      chart.setOption({ graphic: infoGraphics }, { lazyUpdate: true })
    }
  }).current

  // 计算子图总高度
  const activeSubDefs = activeIndicators
    .map(key => SUB_CHARTS.find(s => s.key === key))
    .filter((d): d is SubChartDef => !!d)

  let subTotalH = 0
  activeSubDefs.forEach(def => { subTotalH += INFO_BAR_H + def.height })
  if (activeSubDefs.length > 0) subTotalH += activeSubDefs.length * SUB_GAP_PX

  const mainInfoBarH = showInfoBar ? 40 : 0
  const minCandleH = 120

  const chartHeight = Math.max(height - mainInfoBarH, 8 + minCandleH + 14 + subTotalH)
  chartHeightRef.current = chartHeight
  subTotalHRef.current = subTotalH

  // 预计算 date→index Map (O(1) 查找)
  const dates = useMemo(() => data.map(d => d.date), [data])
  const dateIndexMap = useMemo(() => {
    const m = new Map<string, number>()
    dates.forEach((d, i) => m.set(d, i))
    return m
  }, [dates])

  // 计算 dataZoom 初始范围
  const initialZoom = useMemo(() => ({
    start: Math.max(0, 100 - (visibleBars / Math.max(data.length, 1)) * 100),
    end: 100,
  }), [visibleBars, data.length])

  // ===== 信息栏 HTML 内容 (基于 infoIdxRef.current) =====
  const getInfoBarHTML = useCallback(() => {
    let idx = infoIdxRef.current
    let d = idx >= 0 && idx < data.length ? data[idx] : null
    // fallback: 如果当前 idx 无数据，取最后一根 K 线
    if (!d && data.length > 0) {
      idx = data.length - 1
      d = data[idx]
    }
    if (!d) return ''
    const prev = idx > 0 ? data[idx - 1] : null
    const chg = prev ? d.close - prev.close : 0
    const pct = pctPoints(d, prev)
    const isUp = pct != null ? pct >= 0 : chg >= 0
    const clr = isUp ? THEME.bull : THEME.bear
    const floatShares = stockInfo?.float_shares
    const turnoverRate = floatShares && d.volume ? (d.volume / floatShares * 100) : null

    let html = `<div style="display:flex;align-items:center;gap:6px;padding:0 8px;font:11px 'JetBrains Mono',monospace;select:none;height:20px;flex-wrap:wrap">`
    html += `<span style="color:${THEME.text}">${d.date}</span>`
    html += `<span style="color:${THEME.text}">开</span>`
    html += `<span style="color:${d.open >= d.close ? THEME.bear : THEME.bull}">${d.open.toFixed(2)}</span>`
    html += `<span style="color:${THEME.text}">高</span>`
    html += `<span style="color:${THEME.bull}">${d.high.toFixed(2)}</span>`
    html += `<span style="color:${THEME.text}">低</span>`
    html += `<span style="color:${THEME.bear}">${d.low.toFixed(2)}</span>`
    html += `<span style="color:${THEME.text}">收</span>`
    html += `<span style="color:${clr};font-weight:600">${d.close.toFixed(2)}</span>`
    // 涨跌幅 (收盘后, 换手前; 和收间隔一些距离)
    if (prev) {
      const chgPct = pctPoints(d, prev)
      if (chgPct != null) {
        html += `<span style="color:${clr};margin-left:8px">${chgPct >= 0 ? '+' : ''}${chgPct.toFixed(2)}%</span>`
      }
    }
    if (turnoverRate != null) {
      html += `<span style="color:${THEME.text}">换手</span>`
      html += `<span style="color:${THEME.text}">${turnoverRate.toFixed(2)}%</span>`
    }
    html += `</div>`

    // 第二行: MA + BOLL
    if (showMA) {
      html += `<div style="display:flex;align-items:center;gap:10px;padding:0 8px;font:11px 'JetBrains Mono',monospace;select:none;height:20px;flex-wrap:wrap">`
      if (d.ma5 != null) html += `<span style="color:${THEME.ma5}">MA5:${Number(d.ma5).toFixed(2)}</span>`
      if (d.ma10 != null) html += `<span style="color:${THEME.ma10}">MA10:${Number(d.ma10).toFixed(2)}</span>`
      if (d.ma20 != null) html += `<span style="color:${THEME.ma20}">MA20:${Number(d.ma20).toFixed(2)}</span>`
      if (d.ma60 != null) html += `<span style="color:${THEME.ma60}">MA60:${Number(d.ma60).toFixed(2)}</span>`
      if (d.boll_upper != null && activeIndicators.includes('boll')) {
        html += `<span style="color:#E879F9">BOLL:${Number(d.boll_upper).toFixed(2)}/${Number(d.ma20).toFixed(2)}/${Number(d.boll_lower).toFixed(2)}</span>`
      }
      html += `</div>`
    }

    return html
  }, [data, stockInfo, showMA, activeIndicators])
  getInfoBarHTMLRef.current = getInfoBarHTML

  // data 变化时重置 infoIdx
  useEffect(() => {
    infoIdxRef.current = data.length - 1
    compactRef.current = false
    userZoomRef.current = null
  }, [data.length])

  // ===== 初始化 chart (只在 chartHeight 变化时重建) =====
  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    const chart = echarts.init(el, undefined, { renderer: 'canvas' })
    chartRef.current = chart

    // 鼠标移动 → 只更新 ref + DOM，不触发 React re-render
    // 设计原则: 找不到有效数据时保持上次显示，永远不清空信息栏
    chart.on('updateAxisPointer', (event: any) => {
      const axesInfo = event.axesInfo
      if (!axesInfo) return // 鼠标移出图表区域，保持当前显示
      for (const info of Object.values(axesInfo)) {
        const val = (info as any)?.value
        if (val == null) continue
        const d = dataRef.current
        const idx = typeof val === 'number' ? val : d.findIndex(x => x.date === val)
        if (idx >= 0 && idx < d.length) {
          if (infoIdxRef.current === idx) return
          infoIdxRef.current = idx

          // 直接更新信息栏 DOM (通过 ref 读取最新的生成函数)
          const infoEl = infoBarRef.current
          if (infoEl) {
            const html = getInfoBarHTMLRef.current()
            if (html) infoEl.innerHTML = html  // 只在有内容时更新
          }

          // 更新子图 graphic
          triggerInfoBarUpdate()
          return
        }
      }
      // 没有找到有效数据 — 不做任何操作，保持上次显示
    })

    chart.on('click', (params: any) => {
      if (params.componentType === 'markPoint' && params.name) {
        onDateClickRef.current?.(params.name)
        return
      }
      if (params.seriesName !== 'K' || params.dataIndex == null) return
      const d = dataRef.current
      const idx = params.dataIndex
      if (idx >= 0 && idx < d.length) {
        onDateClickRef.current?.(d[idx].date)
      }
    })

    // dataZoom → 只更新 ref，不触发 React re-render
    // compact 变化时需要增量更新 markPoint
    chart.on('dataZoom', () => {
      const opt = chart.getOption() as any
      const zoom = opt?.dataZoom?.[0]
      if (!zoom) return
      userZoomRef.current = { start: zoom.start, end: zoom.end }

      const d = dataRef.current
      const total = d.length
      const visibleCount = Math.round(total * (zoom.end - zoom.start) / 100)
      const newCompact = visibleCount > COMPACT_THRESHOLD
      if (newCompact !== compactRef.current) {
        compactRef.current = newCompact
        // compact 变了需要更新 markPoint，但只更新 markPoint series
        // 通过 dispatch 自定义事件来增量更新
        updateMarkPoints()
      }
    })

    const ro = new ResizeObserver(() => { chart.resize() })
    ro.observe(el)

    return () => {
      chart.off('updateAxisPointer')
      chart.off('click')
      chart.off('dataZoom')
      ro.disconnect()
      chart.dispose()
      chartRef.current = null
    }
  }, [chartHeight]) // eslint-disable-line react-hooks/exhaustive-deps

  // 增量更新 markPoint (compact 切换时)
  function updateMarkPoints() {
    const chart = chartRef.current
    if (!chart) return
    const curData = dataRef.current
    const curDateIndexMap = new Map(curData.map((d, i) => [d.date, i]))
    const markPointData = buildMarkPointData(
      curData,
      curDateIndexMap,
      showMarkersRef.current ? markersRef.current : undefined,
      compactRef.current,
      activeIndicatorsRef.current,
    )
    chart.setOption({
      series: [{
        name: 'K',
        markPoint: markPointData.length > 0 ? { data: markPointData, animation: false, symbolKeepAspect: true } : undefined,
      }]
    })
  }

  // ===== 核心: 仅在数据/配置变更时全量 setOption =====
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return

    const option = buildOption(
      data, dates, dateIndexMap,
      showMarkersProp ? markers : undefined,
      ranges,
      priceLines,
      showMA, compactRef.current,
      activeIndicators, chartHeight,
      infoIdxRef.current,
      linkedPrice,
    )

    chart.setOption(option, true)

    // 恢复用户缩放位置
    const zoom = userZoomRef.current
    if (zoom) {
      chart.dispatchAction({ type: 'dataZoom', start: zoom.start, end: zoom.end })
    } else {
      chart.dispatchAction({ type: 'dataZoom', start: initialZoom.start, end: initialZoom.end })
    }

    // 初始信息栏
    const infoEl = infoBarRef.current
    if (infoEl) {
      infoEl.innerHTML = getInfoBarHTML()
    }
  }, [data, markers, ranges, priceLines, linkedPrice, showMA, showMarkersProp, activeIndicators, chartHeight, dates, dateIndexMap, initialZoom, getInfoBarHTML])

  // 渲染信息栏容器 (内容由 JS 直接写入)
  const initialHTML = useMemo(() => {
    const idx = data.length - 1
    const d = idx >= 0 && idx < data.length ? data[idx] : null
    if (!d) return ''
    const floatShares = stockInfo?.float_shares
    const turnoverRate = floatShares && d.volume ? (d.volume / floatShares * 100) : null
    let html = `<div style="display:flex;align-items:center;gap:6px;padding:0 8px;font:11px 'JetBrains Mono',monospace;height:20px;flex-wrap:wrap">`
    html += `<span style="color:${THEME.text}">${d.date}</span>`
    html += `<span style="color:${THEME.text}">开</span>`
    html += `<span style="color:${d.open >= d.close ? THEME.bear : THEME.bull}">${d.open.toFixed(2)}</span>`
    html += `<span style="color:${THEME.text}">高</span>`
    html += `<span style="color:${THEME.bull}">${d.high.toFixed(2)}</span>`
    html += `<span style="color:${THEME.text}">低</span>`
    html += `<span style="color:${THEME.bear}">${d.low.toFixed(2)}</span>`
    html += `<span style="color:${THEME.text}">收</span>`
    const prevClose0 = data[idx-1]?.close ?? d.close
    const pct0 = pctPoints(d, data[idx - 1])
    const clr0 = (pct0 ?? (d.close - prevClose0)) >= 0 ? THEME.bull : THEME.bear
    html += `<span style="color:${clr0};font-weight:600">${d.close.toFixed(2)}</span>`
    // 涨跌幅 (收盘后, 换手前; 和收间隔一些距离)
    if (idx > 0) {
      const chgPct0 = pct0 ?? ((d.close - prevClose0) / prevClose0 * 100)
      html += `<span style="color:${clr0};margin-left:8px">${chgPct0 >= 0 ? '+' : ''}${chgPct0.toFixed(2)}%</span>`
    }
    if (turnoverRate != null) {
      html += `<span style="color:${THEME.text}">换手</span>`
      html += `<span style="color:${THEME.text}">${turnoverRate.toFixed(2)}%</span>`
    }
    html += `</div>`
    if (showMA) {
      html += `<div style="display:flex;align-items:center;gap:10px;padding:0 8px;font:11px 'JetBrains Mono',monospace;height:20px;flex-wrap:wrap">`
      if (d.ma5 != null) html += `<span style="color:${THEME.ma5}">MA5:${Number(d.ma5).toFixed(2)}</span>`
      if (d.ma10 != null) html += `<span style="color:${THEME.ma10}">MA10:${Number(d.ma10).toFixed(2)}</span>`
      if (d.ma20 != null) html += `<span style="color:${THEME.ma20}">MA20:${Number(d.ma20).toFixed(2)}</span>`
      if (d.ma60 != null) html += `<span style="color:${THEME.ma60}">MA60:${Number(d.ma60).toFixed(2)}</span>`
      if (d.boll_upper != null && activeIndicators.includes('boll')) {
        html += `<span style="color:#E879F9">BOLL:${Number(d.boll_upper).toFixed(2)}/${Number(d.ma20).toFixed(2)}/${Number(d.boll_lower).toFixed(2)}</span>`
      }
      html += `</div>`
    }
    return html
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="w-full">
      {/* 主图信息栏 — 内容由 JS 直接操作 innerHTML */}
      {showInfoBar && (
        <div ref={infoBarRef} style={{ backgroundColor: 'rgba(39,39,42,0.6)' }}
          dangerouslySetInnerHTML={{ __html: initialHTML }} />
      )}

      {/* ECharts canvas */}
      <div ref={containerRef} className="w-full" style={{ height: chartHeight }} />
    </div>
  )
}
