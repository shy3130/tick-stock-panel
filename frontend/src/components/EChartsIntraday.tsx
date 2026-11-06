import { useEffect, useMemo, useRef, useState } from 'react'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import type { MinuteKlineRow, PriceLimitInfo } from '@/lib/api'
import { computeIntradayAverage, formatMinuteTime, FULL_DAY_TIMES, summarizeMinutes, getLimitPrices, type DailySummary } from '@/lib/intraday-chart'
import { useChartTheme, type ChartTheme } from '@/lib/theme'
import { pricePrecision } from '@/lib/format'
import { intradayScale } from '@/lib/intraday-scale'

type YMode = 'adaptive' | 'limit'

// 序列颜色 (双主题通用); 画布轴/网格/十字线等主题相关色走 ChartTheme
const THEME = {
  line: '#3B82F6',
  areaFill: 'rgba(59,130,246,0.40)',
  avgLine: '#F59E0B',
  volUp: 'rgba(240,68,56,0.6)',
  volDown: 'rgba(18,183,106,0.6)',
}

interface Props {
  assetType?: 'stock' | 'etf' | 'index'
  data: MinuteKlineRow[]
  height?: number
  prevClose?: number
  date?: string
  dailySummary?: DailySummary
  priceLimit?: PriceLimitInfo
  onPriceHover?: (price: number | null) => void
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  currentPrice?: number
  priceLines?: { value: number; label?: string; color?: string }[]
  showLimitLines?: boolean
  showAvgLine?: boolean
}

function fmtAmt(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  if (v >= 1_000_000_000) return `${(v / 1_000_000_000).toFixed(2)}亿`
  if (v >= 10_000) return `${(v / 10_000).toFixed(0)}万`
  return v.toFixed(0)
}

function isValidPrice(v: number | null | undefined): v is number {
  return typeof v === 'number' && Number.isFinite(v) && v > 0
}

function buildOption(data: MinuteKlineRow[], prevClose: number | undefined, avgPrices: (number | null)[], lineColor: string, areaColor: string, yMode: YMode, ct: ChartTheme, priceLimit?: PriceLimitInfo, showLimitLines = true, showAvgLine = true, priceLines: Props['priceLines'] = [], assetType?: Props['assetType']): EChartsOption {
  const digits = pricePrecision(assetType)
  const limitLinesActive = assetType !== 'index' && showLimitLines && !priceLimit?.no_limit
  // 将数据映射到全天时间轴上的正确位置
  const timeIndexMap = new Map(FULL_DAY_TIMES.map((t, i) => [t, i]))
  const closes = new Array(FULL_DAY_TIMES.length).fill(null) as (number | null)[]
  const highs = new Array(FULL_DAY_TIMES.length).fill(null) as (number | null)[]
  const lows = new Array(FULL_DAY_TIMES.length).fill(null) as (number | null)[]
  const avgData = new Array(FULL_DAY_TIMES.length).fill(null) as (number | null)[]
  const volumes = new Array(FULL_DAY_TIMES.length).fill(null) as (any | null)[]

  const volNeutral = 'rgba(161,161,170,0.5)'
  // 量柱着色基准: 前一分钟 close; 第一根用昨收。
  // 不用 row.open — 部分分钟源历史日无真实 open(为 null), close-vs-open 会全偏。
  let prevRef: number | null = prevClose ?? null
  for (let i = 0; i < data.length; i++) {
    const timeKey = formatMinuteTime(data[i].datetime)
    const idx = timeIndexMap.get(timeKey)
    if (idx !== undefined) {
      closes[idx] = data[i].close
      highs[idx] = data[i].high
      lows[idx] = data[i].low
      avgData[idx] = avgPrices[i]
      volumes[idx] = {
        value: data[i].volume,
        itemStyle: {
          color: prevRef == null
            ? volNeutral
            : data[i].close > prevRef
              ? THEME.volUp
              : data[i].close < prevRef
                ? THEME.volDown
                : volNeutral,
        },
      }
      prevRef = data[i].close
    }
  }

  const areaStyle: any = {
    color: {
      type: 'linear',
      x: 0, y: 0, x2: 0, y2: 1,
      colorStops: [
        { offset: 0, color: areaColor },
        { offset: 1, color: 'rgba(0,0,0,0)' },
      ],
    },
  }

  const markLineData: any[] = []
  if (prevClose != null) {
    markLineData.push({
      yAxis: prevClose,
      lineStyle: { color: ct.crosshair, type: 'dashed', width: 1 },
      label: { show: false },
      symbol: 'none',
    })
  }
  for (const line of priceLines) {
    if (!Number.isFinite(line.value) || line.value <= 0) continue
    markLineData.push({
      yAxis: line.value,
      lineStyle: { color: line.color ?? ct.text, type: 'dashed', width: 1, opacity: 0.92 },
      label: {
        show: !!line.label,
        formatter: line.label ?? '',
        position: 'insideEndTop',
        color: line.color ?? ct.text,
        backgroundColor: ct.tooltipBg,
        borderRadius: 4,
        padding: [2, 6],
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
      },
      symbol: 'none',
    })
  }

  const limits = limitLinesActive && isValidPrice(prevClose) ? getLimitPrices(prevClose, priceLimit, assetType) : undefined
  const mode = limits ? yMode : 'adaptive'
  const scale = intradayScale([
    ...closes, ...highs, ...lows,
    ...(mode === 'limit' && limits ? [limits.limitDown, limits.limitUp] : []),
  ], prevClose, digits, limits ? { min: limits.limitDown, max: limits.limitUp } : undefined, mode)
  const yMin = scale?.range.min
  const yMax = scale?.range.max
  const interval = scale ? (scale.range.max - scale.range.min)
    / (scale.range.max - scale.range.min < 10 ** -digits * 4 ? 2 : 4) : undefined
  if (mode === 'limit' && limits) {
    markLineData.push(
      { yAxis: limits.limitUp, lineStyle: { color: 'rgba(199,64,64,0.4)', type: 'dashed', width: 1 }, label: { show: false }, symbol: 'none' },
      { yAxis: limits.limitDown, lineStyle: { color: 'rgba(45,155,101,0.4)', type: 'dashed', width: 1 }, label: { show: false }, symbol: 'none' },
    )
  }

  // x 轴标签: 9:30, 10:30, 11:30/13:00, 14:00, 15:00
  // 11:30(idx 120) 和 13:00(idx 121) 相邻会重叠, 合并为一个标签
  const xAxisLabelMap: Record<number, string> = {
    0: '9:30',
    60: '10:30',
    120: '11:30/13:00',
    181: '14:00',
    241: '15:00',
  }
  const xAxisLabelFormatter = (_value: string, idx: number) => {
    return xAxisLabelMap[idx] ?? ''
  }

  return {
    animation: false,
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'transparent',
      borderWidth: 0,
      textStyle: { fontSize: 0 },
      formatter: () => '',
      axisPointer: {
        type: 'cross',
        label: {
          show: true,
          backgroundColor: ct.tooltipBg,
          borderColor: ct.tooltipBorder,
          borderWidth: 1,
          padding: [2, 5],
          color: ct.tooltipText,
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
        },
        crossStyle: { color: ct.crosshair, type: 'dashed', width: 1 },
        lineStyle: { color: ct.crosshair, type: 'dashed', width: 1 },
      },
    },
    axisPointer: {
      link: [{ xAxisIndex: 'all' }],
    },
    grid: [
      { left: 60, right: 55, top: 24, bottom: '34%' },
      { left: 60, right: 55, top: '69%', bottom: 20 },
    ],
    xAxis: [
      {
        type: 'category',
        data: FULL_DAY_TIMES,
        boundaryGap: false,
        axisPointer: {
          show: true,
          lineStyle: { color: ct.crosshair, type: 'dashed', width: 1 },
          label: {
            show: true,
            backgroundColor: ct.tooltipBg,
            borderColor: ct.tooltipBorder,
            borderWidth: 1,
            padding: [2, 4],
            color: ct.tooltipText,
            fontSize: 10,
            fontFamily: 'JetBrains Mono, monospace',
            formatter: (params: any) => {
              return params.value ?? ''
            },
          },
        },
        axisLine: { show: false },
        axisLabel: {
          color: ct.text,
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
          formatter: xAxisLabelFormatter,
          interval: 0,
        },
        axisTick: { show: false },
        splitLine: {
          show: true,
          lineStyle: { color: ct.grid },
        },
      },
      {
        type: 'category',
        gridIndex: 1,
        data: FULL_DAY_TIMES,
        boundaryGap: false,
        axisLine: { show: false },
        axisLabel: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
      },
    ],
    yAxis: [
      {
        type: 'value',
        min: yMin,
        max: yMax,
        scale: true,
        interval,
        splitArea: { show: false },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: ct.grid } },
        axisPointer: {
          label: {
            formatter: (params: any) => {
              const v = params.value
              return typeof v === 'number' ? v.toFixed(digits) : ''
            },
          },
        },
        axisLabel: {
          color: ct.text,
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
          formatter: (v: number) => v.toFixed(digits),
        },
      },
      {
        scale: true,
        gridIndex: 1,
        splitNumber: 2,
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { show: false },
      },
      ...(isValidPrice(prevClose) && yMin != null && yMax != null ? [{
        type: 'value' as const,
        position: 'right' as const,
        gridIndex: 0,
        min: yMin,
        max: yMax,
        interval,
        splitArea: { show: false },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
        axisPointer: {
          label: {
            formatter: (params: any) => {
              const v = params.value
              if (typeof v !== 'number') return ''
              const pct = (v - prevClose) / prevClose * 100
              if (Math.abs(pct) < 0.01) return '0.00%'
              return (pct > 0 ? '+' : '') + pct.toFixed(2) + '%'
            },
          },
        },
        axisLabel: {
          color: ct.text,
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
          formatter: (v: number) => {
            const pct = (v - prevClose) / prevClose * 100
            if (Math.abs(pct) < 0.01) return '0.00%'
            return (pct > 0 ? '+' : '') + pct.toFixed(2) + '%'
          },
        },
      }] : []),
    ],
    series: [
      {
        name: '价格',
        type: 'line',
        data: closes,
        smooth: false,
        symbol: 'none',
        cursor: 'crosshair',
        lineStyle: { width: 1.2, color: lineColor },
        areaStyle,
        connectNulls: true,
        markLine: markLineData.length > 0 ? { symbol: 'none', data: markLineData, animation: false, silent: true } : undefined,
      },
      ...(showAvgLine ? [{
        name: '均价',
        type: 'line' as const,
        data: avgData,
        smooth: false,
        symbol: 'none',
        cursor: 'crosshair',
        lineStyle: { width: 1, color: THEME.avgLine },
        connectNulls: true,
      }] : []),
      {
        name: '成交量',
        type: 'bar',
        data: volumes,
        xAxisIndex: 1,
        yAxisIndex: 1,
        cursor: 'crosshair',
      },
    ],
  }
}

export function EChartsIntraday({
  data,
  assetType,
  height = 320,
  prevClose,
  date,
  dailySummary,
  priceLimit,
  onPriceHover,
  onPriceDoubleClick,
  currentPrice,
  priceLines,
  showLimitLines = true,
  showAvgLine = true,
}: Props) {
  const digits = pricePrecision(assetType)
  const averageVisible = showAvgLine && assetType !== 'index'
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ECharts | null>(null)
  const roRef = useRef<ResizeObserver | null>(null)
  const moRef = useRef<MutationObserver | null>(null)
  const priceDoubleClickHandlerRef = useRef<((event: { offsetX: number; offsetY: number }) => void) | null>(null)
  const dataRef = useRef(data)
  dataRef.current = data
  const currentPriceRef = useRef(currentPrice)
  currentPriceRef.current = currentPrice
  const onPriceHoverRef = useRef(onPriceHover)
  onPriceHoverRef.current = onPriceHover
  const onPriceDoubleClickRef = useRef(onPriceDoubleClick)
  onPriceDoubleClickRef.current = onPriceDoubleClick
  // 全日索引 → 数据数组索引 的映射 (ref 避免重建 chart)
  const fullDayToDataIdx = useRef<Map<number, number>>(new Map())

  const [infoIdx, setInfoIdx] = useState(-1)
  const [yMode, setYMode] = useState<YMode>('adaptive')
  const ct = useChartTheme()
  const avgPrices = useMemo(() => computeIntradayAverage(data, assetType), [data, assetType])
  const summary = useMemo(() => summarizeMinutes(data), [data])

  // 分时线颜色：基于最新价 vs 昨收
  const lastClose = data.length > 0 ? data[data.length - 1].close : null
  const lineIsUp = lastClose != null && prevClose != null ? lastClose > prevClose : true
  const lineIsFlat = lastClose != null && prevClose != null ? lastClose === prevClose : false
  const lineColor = lineIsFlat ? '#A1A1AA' : lineIsUp ? '#C74040' : '#2D9B65'
  const areaFill = lineIsFlat ? 'rgba(180,180,190,0.40)' : lineIsUp ? 'rgba(199,64,64,0.40)' : 'rgba(34,197,94,0.40)'

  useEffect(() => {
    setInfoIdx(-1)
  }, [date])

  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    let chart = chartRef.current
    if (!chart) {
      chart = echarts.init(el, undefined, { renderer: 'canvas' })
      chartRef.current = chart
      // 强制 canvas 使用十字光标，覆盖 ECharts 默认的 pointer
      const forceCursor = () => {
        const canvases = el.querySelectorAll('canvas')
        canvases.forEach(c => { c.style.setProperty('cursor', 'crosshair', 'important') })
      }
      forceCursor()
      // MutationObserver: ECharts 内部可能重建/修改 canvas 属性，持续强制 cursor
      const mo = new MutationObserver(forceCursor)
      mo.observe(el, { childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'class'] })
      moRef.current = mo
      roRef.current = new ResizeObserver(() => {
        chart!.resize()
        forceCursor()
      })
      roRef.current.observe(el)

      chart.on('updateAxisPointer', (event: any) => {
        const axesInfo = event.axesInfo
        if (!axesInfo) return
        for (const info of Object.values(axesInfo)) {
          if ((info as any)?.axisDim !== 'x') continue
          const val = (info as any)?.value
          if (val == null) continue
          const fullDayIdx = typeof val === 'number' ? val : -1
          if (fullDayIdx >= 0) {
            const dataIdx = fullDayToDataIdx.current.get(fullDayIdx) ?? -1
            setInfoIdx(dataIdx)
            const d = dataRef.current
            if (dataIdx >= 0 && dataIdx < d.length) {
              onPriceHoverRef.current?.(d[dataIdx].close)
            } else {
              onPriceHoverRef.current?.(null)
            }
            return
          }
        }
      })

      chart.on('globalout', () => {
        setInfoIdx(-1)
        onPriceHoverRef.current?.(null)
      })

      const handlePriceDoubleClick = (event: { offsetX: number; offsetY: number }) => {
        const pixel: [number, number] = [event.offsetX, event.offsetY]
        if (!chart!.containPixel({ gridIndex: 0 }, pixel)) return
        const coordinate = chart!.convertFromPixel({ xAxisIndex: 0, yAxisIndex: 0 }, pixel)
        const clickedPrice = Array.isArray(coordinate) ? Number(coordinate[1]) : NaN
        const latestPrice = currentPriceRef.current ?? dataRef.current[dataRef.current.length - 1]?.close
        if (Number.isFinite(clickedPrice) && clickedPrice > 0 && Number.isFinite(latestPrice) && latestPrice > 0) {
          onPriceDoubleClickRef.current?.(clickedPrice, latestPrice)
        }
      }
      priceDoubleClickHandlerRef.current = handlePriceDoubleClick
      chart.getZr().on('dblclick', handlePriceDoubleClick)
    }

    if (data.length > 0) {
      // 构建全日索引 → 数据索引 的映射
      const timeIndexMap = new Map(FULL_DAY_TIMES.map((t, i) => [t, i]))
      const mapping = new Map<number, number>()
      for (let i = 0; i < data.length; i++) {
        const timeKey = formatMinuteTime(data[i].datetime)
        const fullDayIdx = timeIndexMap.get(timeKey)
        if (fullDayIdx !== undefined) {
          mapping.set(fullDayIdx, i)
        }
      }
      fullDayToDataIdx.current = mapping

      chart.setOption(buildOption(data, prevClose, avgPrices, lineColor, areaFill, yMode, ct, priceLimit, showLimitLines, averageVisible, priceLines, assetType), true)
    } else {
      fullDayToDataIdx.current = new Map()
      chart.clear()
    }
  }, [data, prevClose, height, lineColor, areaFill, yMode, ct, priceLimit, showLimitLines, averageVisible, priceLines, assetType])

  useEffect(() => {
    return () => {
      chartRef.current?.off('updateAxisPointer')
      chartRef.current?.off('globalout')
      if (priceDoubleClickHandlerRef.current) {
        chartRef.current?.getZr().off('dblclick', priceDoubleClickHandlerRef.current)
      }
      moRef.current?.disconnect()
      roRef.current?.disconnect()
      chartRef.current?.dispose()
      chartRef.current = null
      moRef.current = null
      roRef.current = null
    }
  }, [])

  const hovered = infoIdx >= 0 && infoIdx < data.length ? data[infoIdx] : null
  const d = hovered ?? summary
  const daily = dailySummary?.date === date ? dailySummary : undefined
  const ohlc = hovered ?? daily ?? summary
  const avg = d != null ? avgPrices[hovered ? infoIdx : data.length - 1] : null
  const chg = d && prevClose != null ? d.close - prevClose : null
  const isUp = chg != null ? chg > 0 : true
  const isFlat = chg != null ? chg === 0 : false
  const priceClr = isFlat ? '#A1A1AA' : isUp ? '#C74040' : '#2D9B65'

  return (
    <div className="w-full">
      {/* 按钮行: 切换式按钮组, 居右 */}
      {showLimitLines && assetType !== 'index' && <div className="flex items-center justify-end px-1 pb-0.5">
        <div className="inline-flex items-center rounded bg-elevated overflow-hidden">
          <button
            onClick={() => setYMode('adaptive')}
            className={`px-2.5 py-0.5 text-[10px] font-mono cursor-pointer transition-colors ${
              yMode === 'adaptive'
                ? 'bg-accent/20 text-accent'
                : 'text-muted hover:text-secondary'
            }`}
          >
            自适应
          </button>
          <div className="w-px h-3 bg-border/40" />
          <button
            onClick={() => setYMode('limit')}
            className={`px-2.5 py-0.5 text-[10px] font-mono cursor-pointer transition-colors ${
              yMode === 'limit'
                ? 'bg-accent/20 text-accent'
                : 'text-muted hover:text-secondary'
            }`}
          >
            涨跌停
          </button>
        </div>
      </div>}
      <div style={{ backgroundColor: ct.infoBarBg }}>
        {/* 第一行: 日期 + OHLC */}
        <div className="flex items-center gap-x-2 px-2 font-mono text-[11px] select-none flex-wrap" style={{ minHeight: 20 }}>
          {!d && <span className="text-muted">—</span>}
          {ohlc && (
            <>
              {date && <span className="text-muted">{date}</span>}
              <span className="text-muted">{hovered ? `${formatMinuteTime(hovered.datetime)} 分钟` : daily ? '日K（前复权）' : '分时汇总'}</span>
              <span className="text-muted">开</span>
              <span style={{ color: priceClr }}>{ohlc.open != null ? ohlc.open.toFixed(digits) : '—'}</span>
              <span className="text-muted">高</span>
              <span style={{ color: priceClr }}>{ohlc.high.toFixed(digits)}</span>
              <span className="text-muted">低</span>
              <span style={{ color: priceClr }}>{ohlc.low.toFixed(digits)}</span>
              <span className="text-muted">收</span>
              <span style={{ color: priceClr }} className="font-semibold">{ohlc.close.toFixed(digits)}</span>
            </>
          )}
        </div>
        {/* 第二行: 价格+均价+量+额 */}
        <div className="flex flex-wrap items-center gap-x-2 px-2 font-mono text-[11px] select-none" style={{ minHeight: 20 }}>
          {d && (
            <>
              <span className="flex items-center gap-x-1">
                <span style={{ display: 'inline-block', width: 14, height: 2, background: priceClr }} />
                <span style={{ color: priceClr }}>{d.close.toFixed(digits)}</span>
              </span>
              {averageVisible && <span className="flex items-center gap-x-1">
                <span style={{ display: 'inline-block', width: 14, height: 2, background: THEME.avgLine }} />
                <span style={{ color: THEME.avgLine }}>{avg != null ? avg.toFixed(digits) : '—'}</span>
              </span>}
              <span className="text-muted">{hovered ? '量' : '累计量'}</span>
              <span className="text-secondary">{d.volume.toFixed(0)}</span>
              <span className="text-muted">{hovered ? '额' : '累计额'}</span>
              <span className="text-secondary">{fmtAmt(d.amount)}</span>
            </>
          )}
        </div>
      </div>
      <div ref={containerRef} className="w-full" style={{ height: height - 42, cursor: 'crosshair' }} />
    </div>
  )
}
