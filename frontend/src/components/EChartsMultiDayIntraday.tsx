import { useEffect, useMemo, useRef, useState } from 'react'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import type { MinuteKlineRow, MinuteKlineSession } from '@/lib/api'
import { computeIntradayAverage, formatMinuteTime, FULL_DAY_TIMES } from '@/lib/intraday-chart'
import { useChartTheme } from '@/lib/theme'

const COLORS = {
  up: '#C74040',
  down: '#2D9B65',
  flat: '#A1A1AA',
  average: '#F59E0B',
  volumeUp: 'rgba(240,68,56,0.58)',
  volumeDown: 'rgba(18,183,106,0.58)',
  volumeFlat: 'rgba(161,161,170,0.45)',
}

interface Props {
  sessions: MinuteKlineSession[]
  height?: number
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  priceLines?: { value: number; label?: string; color?: string }[]
}

interface InfoPoint {
  date: string
  row: MinuteKlineRow
  average: number | null
  prevClose: number | null
}

function formatAmount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  if (value >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(2)}亿`
  if (value >= 10_000) return `${(value / 10_000).toFixed(0)}万`
  return value.toFixed(0)
}

function priceColor(close: number, prevClose: number | null): string {
  if (prevClose == null || close === prevClose) return COLORS.flat
  return close > prevClose ? COLORS.up : COLORS.down
}

function buildModel(sessions: MinuteKlineSession[]) {
  const categories: string[] = []
  const volumeData: ({ value: number; itemStyle: { color: string } } | null)[] = []
  const dayLabelByIndex = new Map<number, string>()
  const dayStartIndexes: number[] = []
  const pointByIndex = new Map<number, InfoPoint>()
  const dayRanges: {
    start: number
    session: MinuteKlineSession
    values: (number | null)[]
    averages: (number | null)[]
  }[] = []
  const priceValues: number[] = []

  const labelStep = Math.max(1, Math.ceil(sessions.length / 10))
  for (let sessionIndex = 0; sessionIndex < sessions.length; sessionIndex++) {
    const session = sessions[sessionIndex]
    const start = categories.length
    dayStartIndexes.push(start)
    if (sessionIndex % labelStep === 0 || sessionIndex === sessions.length - 1) {
      dayLabelByIndex.set(start + Math.floor(FULL_DAY_TIMES.length / 2), session.date.slice(5))
    }

    const averagePrices = computeIntradayAverage(session.rows)
    const rowsByTime = new Map<string, { row: MinuteKlineRow; average: number | null }>()
    session.rows.forEach((row, index) => {
      rowsByTime.set(formatMinuteTime(row.datetime), {
        row,
        average: averagePrices[index],
      })
    })

    const dayValues: (number | null)[] = []
    const dayAverages: (number | null)[] = []
    // 量柱着色基准: 前一分钟 close; 当日第一根用 session 昨收。
    // 不用 row.open — 部分分钟源历史日无真实 open(为 null), close-vs-open 会全偏。
    let prevRef: number | null = session.prev_close
    for (const time of FULL_DAY_TIMES) {
      const point = rowsByTime.get(time)
      const index = categories.length
      categories.push(`${session.date} ${time}`)
      if (!point) {
        dayValues.push(null)
        dayAverages.push(null)
        volumeData.push(null)
        continue
      }

      const { row, average } = point
      dayValues.push(row.close)
      dayAverages.push(average)
      volumeData.push({
        value: row.volume,
        itemStyle: {
          color: prevRef == null
            ? COLORS.volumeFlat
            : row.close > prevRef
              ? COLORS.volumeUp
              : row.close < prevRef
                ? COLORS.volumeDown
                : COLORS.volumeFlat,
        },
      })
      prevRef = row.close
      priceValues.push(row.low, row.high)
      if (average != null) priceValues.push(average)
      pointByIndex.set(index, {
        date: session.date,
        row,
        average,
        prevClose: session.prev_close,
      })
    }

    dayRanges.push({
      start,
      session,
      values: dayValues,
      averages: dayAverages,
    })

    if (sessionIndex < sessions.length - 1) {
      categories.push(`${session.date} gap`)
      volumeData.push(null)
    }
  }

  return {
    categories,
    volumeData,
    dayLabelByIndex,
    dayStartIndexes,
    pointByIndex,
    dayRanges,
    priceValues,
    latest: pointByIndex.size > 0
      ? Array.from(pointByIndex.values())[pointByIndex.size - 1]
      : null,
  }
}

export function EChartsMultiDayIntraday({
  sessions,
  height = 420,
  onPriceDoubleClick,
  priceLines = [],
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ECharts | null>(null)
  const resizeObserverRef = useRef<ResizeObserver | null>(null)
  const priceDoubleClickHandlerRef = useRef<((event: { offsetX: number; offsetY: number }) => void) | null>(null)
  const onPriceDoubleClickRef = useRef(onPriceDoubleClick)
  onPriceDoubleClickRef.current = onPriceDoubleClick
  const model = useMemo(() => buildModel(sessions), [sessions])
  const modelRef = useRef(model)
  modelRef.current = model
  const [info, setInfo] = useState<InfoPoint | null>(model.latest)
  const theme = useChartTheme()

  useEffect(() => {
    setInfo(model.latest)
  }, [model])

  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    let chart = chartRef.current
    if (!chart) {
      chart = echarts.init(container, undefined, { renderer: 'canvas' })
      chartRef.current = chart
      resizeObserverRef.current = new ResizeObserver(() => chart?.resize())
      resizeObserverRef.current.observe(container)

      chart.on('updateAxisPointer', (event: any) => {
        const axisInfo = event.axesInfo?.find((item: any) => item.axisDim === 'x' && item.axisIndex === 0)
          ?? event.axesInfo?.[0]
        const rawValue = axisInfo?.value
        const current = modelRef.current
        const index = typeof rawValue === 'number'
          ? rawValue
          : current.categories.indexOf(String(rawValue))
        const point = current.pointByIndex.get(index)
        if (point) setInfo(point)
      })
      chart.on('globalout', () => setInfo(modelRef.current.latest))

      const handlePriceDoubleClick = (event: { offsetX: number; offsetY: number }) => {
        const pixel: [number, number] = [event.offsetX, event.offsetY]
        if (!chart!.containPixel({ gridIndex: 0 }, pixel)) return
        const coordinate = chart!.convertFromPixel({ xAxisIndex: 0, yAxisIndex: 0 }, pixel)
        const price = Array.isArray(coordinate) ? Number(coordinate[1]) : NaN
        const currentPrice = modelRef.current.latest?.row.close
        if (
          Number.isFinite(price)
          && price > 0
          && typeof currentPrice === 'number'
          && Number.isFinite(currentPrice)
          && currentPrice > 0
        ) {
          onPriceDoubleClickRef.current?.(price, currentPrice)
        }
      }
      priceDoubleClickHandlerRef.current = handlePriceDoubleClick
      chart.getZr().on('dblclick', handlePriceDoubleClick)
    }

    const monitoredPrices = priceLines
      .map(line => line.value)
      .filter(value => Number.isFinite(value) && value > 0)
    const allPriceValues = [...model.priceValues, ...monitoredPrices]
    const minPrice = allPriceValues.length > 0 ? Math.min(...allPriceValues) : 0
    const maxPrice = allPriceValues.length > 0 ? Math.max(...allPriceValues) : 1
    const padding = Math.max((maxPrice - minPrice) * 0.08, maxPrice * 0.002)
    const totalLength = model.categories.length
    const priceSeries: any[] = model.dayRanges.map(({ start, session, values }) => {
      const data = new Array(totalLength).fill(null) as (number | null)[]
      for (let index = 0; index < values.length; index++) data[start + index] = values[index]
      const last = session.rows[session.rows.length - 1]
      const color = last ? priceColor(last.close, session.prev_close) : COLORS.flat
      return {
        name: session.date,
        type: 'line',
        data,
        symbol: 'none',
        smooth: false,
        connectNulls: true,
        lineStyle: { width: 1.2, color },
        areaStyle: { color, opacity: 0.08 },
        emphasis: { disabled: true },
      }
    })

    const boundaryData = model.dayStartIndexes.slice(1).map(index => ({
      xAxis: model.categories[index],
      lineStyle: { color: theme.grid, width: 1 },
      label: { show: false },
    }))
    const monitorLineData = priceLines.flatMap(line => {
      if (!Number.isFinite(line.value) || line.value <= 0) return []
      return [{
        yAxis: line.value,
        lineStyle: { color: line.color ?? theme.text, type: 'dashed', width: 1, opacity: 0.92 },
        label: {
          show: !!line.label,
          formatter: line.label ?? '',
          position: 'insideEndTop',
          color: line.color ?? theme.text,
          backgroundColor: theme.tooltipBg,
          borderRadius: 4,
          padding: [2, 6],
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
        },
      }]
    })
    const markLineData = [...boundaryData, ...monitorLineData]
    if (priceSeries.length > 0 && markLineData.length > 0) {
      priceSeries[0].markLine = {
        symbol: 'none',
        silent: true,
        animation: false,
        data: markLineData,
      }
    }
    const averageSeries: any[] = model.dayRanges.map(({ start, session, averages }) => {
      const data = new Array(totalLength).fill(null) as (number | null)[]
      for (let index = 0; index < averages.length; index++) data[start + index] = averages[index]
      return {
        name: `${session.date} 均价`,
        type: 'line',
        data,
        symbol: 'none',
        connectNulls: true,
        lineStyle: { width: 1, color: COLORS.average },
        emphasis: { disabled: true },
      }
    })
    const option: EChartsOption = {
      animation: false,
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'transparent',
        borderWidth: 0,
        formatter: () => '',
        axisPointer: {
          type: 'cross',
          label: {
            show: true,
            backgroundColor: theme.tooltipBg,
            borderColor: theme.tooltipBorder,
            borderWidth: 1,
            color: theme.tooltipText,
            fontFamily: 'JetBrains Mono, monospace',
            fontSize: 10,
          },
          crossStyle: { color: theme.crosshair, type: 'dashed', width: 1 },
          lineStyle: { color: theme.crosshair, type: 'dashed', width: 1 },
        },
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [
        { left: 58, right: 18, top: 16, bottom: '34%' },
        { left: 58, right: 18, top: '69%', bottom: 22 },
      ],
      xAxis: [
        {
          type: 'category',
          data: model.categories,
          boundaryGap: false,
          axisLine: { lineStyle: { color: theme.grid } },
          axisTick: { show: false },
          splitLine: { show: false },
          axisLabel: {
            color: theme.text,
            fontFamily: 'JetBrains Mono, monospace',
            fontSize: 10,
            interval: 0,
            hideOverlap: true,
            formatter: (_value: string, index: number) => model.dayLabelByIndex.get(index) ?? '',
          },
          axisPointer: {
            label: {
              formatter: (params: any) => {
                const value = String(params.value ?? '')
                return value.endsWith(' gap') ? '' : value.slice(5)
              },
            },
          },
        },
        {
          type: 'category',
          gridIndex: 1,
          data: model.categories,
          boundaryGap: false,
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { show: false },
          splitLine: { show: false },
        },
      ],
      yAxis: [
        {
          type: 'value',
          min: minPrice - padding,
          max: maxPrice + padding,
          scale: true,
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { lineStyle: { color: theme.grid } },
          axisLabel: {
            color: theme.text,
            fontFamily: 'JetBrains Mono, monospace',
            fontSize: 10,
            formatter: (value: number) => value.toFixed(2),
          },
        },
        {
          type: 'value',
          gridIndex: 1,
          scale: true,
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { show: false },
          axisLabel: { show: false },
        },
      ],
      dataZoom: [{
        type: 'inside',
        xAxisIndex: [0, 1],
        start: 0,
        end: 100,
        minValueSpan: FULL_DAY_TIMES.length,
        filterMode: 'none',
      }],
      series: [
        ...priceSeries,
        ...averageSeries,
        {
          name: '成交量',
          type: 'bar',
          data: model.volumeData,
          xAxisIndex: 1,
          yAxisIndex: 1,
        },
      ],
    }
    chart.setOption(option, true)
  }, [height, model, priceLines, theme])

  useEffect(() => () => {
    chartRef.current?.off('updateAxisPointer')
    chartRef.current?.off('globalout')
    if (priceDoubleClickHandlerRef.current) {
      chartRef.current?.getZr().off('dblclick', priceDoubleClickHandlerRef.current)
    }
    resizeObserverRef.current?.disconnect()
    chartRef.current?.dispose()
    chartRef.current = null
  }, [])

  const changePct = info?.prevClose
    ? (info.row.close - info.prevClose) / info.prevClose * 100
    : null
  const infoColor = info ? priceColor(info.row.close, info.prevClose) : COLORS.flat
  const rowCount = sessions.reduce((total, session) => total + session.rows.length, 0)

  return (
    <div className="w-full overflow-hidden">
      <div className="flex min-h-10 flex-wrap items-center justify-between gap-x-4 gap-y-1 px-2 py-1 font-mono text-[11px]" style={{ backgroundColor: theme.infoBarBg }}>
        <div className="flex min-w-0 flex-wrap items-center gap-x-2">
          {info ? (
            <>
              <span className="text-muted">{info.date} {formatMinuteTime(info.row.datetime)}</span>
              <span className="text-muted">开</span><span style={{ color: infoColor }}>{info.row.open != null ? info.row.open.toFixed(2) : '—'}</span>
              <span className="text-muted">高</span><span style={{ color: infoColor }}>{info.row.high.toFixed(2)}</span>
              <span className="text-muted">低</span><span style={{ color: infoColor }}>{info.row.low.toFixed(2)}</span>
              <span className="text-muted">收</span><span className="font-semibold" style={{ color: infoColor }}>{info.row.close.toFixed(2)}</span>
              {changePct != null && (
                <span style={{ color: infoColor }}>{changePct >= 0 ? '+' : ''}{changePct.toFixed(2)}%</span>
              )}
              <span className="text-muted">均价</span><span style={{ color: COLORS.average }}>{info.average != null ? info.average.toFixed(2) : '—'}</span>
              <span className="text-muted">量</span><span className="text-secondary">{info.row.volume.toFixed(0)}</span>
              <span className="text-muted">额</span><span className="text-secondary">{formatAmount(info.row.amount)}</span>
            </>
          ) : <span className="text-muted">—</span>}
        </div>
        <div className="shrink-0 text-[10px] text-muted">{sessions.length} 个交易日 · {rowCount} 分钟</div>
      </div>
      <div ref={containerRef} className="w-full" style={{ height: height - 40, cursor: 'crosshair' }} />
    </div>
  )
}
