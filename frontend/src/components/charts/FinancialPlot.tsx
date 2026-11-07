import { useEffect, useRef } from 'react'
import { createChart, CandlestickSeries, LineSeries, AreaSeries, BaselineSeries, HistogramSeries, createSeriesMarkers, ColorType, CrosshairMode,
  type IChartApi, type ISeriesApi, type ISeriesMarkersPluginApi, type SeriesType, type Time, type SeriesMarker, type MouseEventParams } from 'lightweight-charts'
import { useChartTheme } from '@/lib/theme'
import { fmtBigNum } from '@/lib/format'
import { CHART_LAYOUT } from '@/lib/chart-layout'
import { formatAxisPercent } from '@/lib/intraday-scale'
import { intradayTimeLabel, timeKey, type PlotSeries, type PlotData } from '@/lib/lightweight-model'
import { axisLabelCoordinate, ChartAnnotations, type AnnotationData } from './ChartAnnotations'
import { DailyVolumeSeries } from './DailyVolumeSeries'

const PRICE_MARGINS = { daily: { top: 0.08, bottom: 0.06 }, intraday: { top: 0, bottom: 0 } }
const markersForView = (markers: SeriesMarker<Time>[] = [], compact: boolean) => compact
  ? markers.map(marker => marker.shape === 'arrowUp' || marker.shape === 'arrowDown' ? marker
    : { ...marker, text: '', shape: 'circle' as const, size: marker.size === 0 ? 0.5 : Math.min(marker.size ?? 0.65, 0.65) })
  : markers

interface Props {
  scope: string
  series: PlotSeries[]
  height: number
  paneHeights: number[]
  visibleBars?: number | 'all'
  intraday?: boolean
  markers?: SeriesMarker<Time>[]
  annotations?: AnnotationData
  volumeLabels?: AnnotationData['labels']
  paneTitles?: string[]
  paneReadouts?: AnnotationData['readouts'][]
  linkedPrice?: number | null
  onHover?: (time: string | null) => void
  /** 十字线价格与像素比例(仅主价格 pane)，供价位命中判断使用 */
  onCrosshairMove?: (point: { time: string; price: number; pixelsPerPrice: number } | null) => void
  onSelect?: (time: string) => void
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  currentPrice?: number
  pricePrecision?: number
  /** 右侧预留空白带宽度(px)，给价位文字标签留位置 */
  rightOffsetPixels?: number
}

export function updatePlotData(series: ISeriesApi<SeriesType>, previous: PlotData[], next: PlotData[]) {
  // 相同前缀且仅尾部变化才走 update；历史修补必须完整替换，不能漏掉较早的更正。
  const tailOnly = previous.length > 0 && next.length >= previous.length && next.length <= previous.length + 1
    && previous.slice(0, -1).every((row, i) => JSON.stringify(row) === JSON.stringify(next[i]))
  if (!tailOnly) { series.setData(next); return }
  if (JSON.stringify(previous.at(-1)) !== JSON.stringify(next[previous.length - 1])) series.update(next[previous.length - 1])
  if (next.length > previous.length) series.update(next.at(-1)!)
}

export function FinancialPlot(props: Props) {
  const host = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef(new Map<string, { api: ISeriesApi<SeriesType>; data: PlotData[]; pane: number; type: PlotSeries['type']; scale?: string }>())
  const propsRef = useRef(props); propsRef.current = props
  const scopeRef = useRef<string | null>(null)
  const markerRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null)
  const annotationRef = useRef(new ChartAnnotations())
  const attachedMain = useRef<ISeriesApi<SeriesType> | null>(null)
  const leftAnnotation = useRef(new ChartAnnotations())
  const attachedLeft = useRef<ISeriesApi<SeriesType> | null>(null)
  const paneAnnotations = useRef(new Map<number, { api: ISeriesApi<SeriesType>; annotation: ChartAnnotations; volume: boolean }>())
  const ct = useChartTheme()
  const themeRef = useRef(ct); themeRef.current = ct
  const compactRef = useRef(false)

  useEffect(() => {
    if (!host.current) return
    const theme = themeRef.current
    const chart = createChart(host.current, {
      autoSize: true,
      handleScale: !propsRef.current.intraday,
      handleScroll: !propsRef.current.intraday,
      layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: theme.text,
        // 归属声明与链接统一放在设置的关于页，避免每个画布重复显示角标。
        fontFamily: 'JetBrains Mono, monospace', fontSize: 11, attributionLogo: false,
        panes: { separatorColor: theme.border, separatorHoverColor: theme.crosshair, enableResize: !propsRef.current.intraday } },
      grid: { vertLines: { visible: false }, horzLines: { color: theme.grid } },
      crosshair: { mode: CrosshairMode.Normal,
        vertLine: { color: theme.crosshair, labelBackgroundColor: theme.crosshairLabelBg },
        horzLine: { color: theme.crosshair, labelBackgroundColor: theme.crosshairLabelBg } },
      rightPriceScale: { borderVisible: false, minimumWidth: 58, entireTextOnly: !!propsRef.current.intraday,
        scaleMargins: propsRef.current.intraday ? PRICE_MARGINS.intraday : PRICE_MARGINS.daily },
      timeScale: { borderColor: theme.border, minimumHeight: CHART_LAYOUT.timeAxis,
        // 右侧像素留白与 fixRightEdge 互斥: 固定右缘会钳制 rightOffset。
        rightOffset: 0, fixLeftEdge: true, fixRightEdge: !propsRef.current.intraday && !propsRef.current.rightOffsetPixels,
        timeVisible: !!propsRef.current.intraday,
        ...(propsRef.current.rightOffsetPixels ? { rightOffsetPixels: propsRef.current.rightOffsetPixels } : {}),
        lockVisibleTimeRangeOnResize: propsRef.current.visibleBars === 'all',
        // 20 日约 4820 个分钟位置，窄屏也要能完整适配所选时段。
        minBarSpacing: propsRef.current.intraday ? 0.01 : 0.5,
        tickMarkFormatter: (time: Time, kind: number) => {
          const key = timeKey(time)
          return typeof time === 'number' && kind >= 3 ? intradayTimeLabel(time).slice(11) : key.slice(5, 10)
        } },
      localization: { locale: 'zh-CN', timeFormatter: (time: Time) => propsRef.current.intraday ? intradayTimeLabel(time) : timeKey(time) },
    })
    chartRef.current = chart
    let frame = 0, resizeFrame = 0
    const hover = (event: MouseEventParams<Time>) => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        if (propsRef.current.intraday) {
          const y = event.paneIndex === 0 ? event.point?.y : undefined
          annotationRef.current.setHoverY(y); leftAnnotation.current.setHoverY(y)
        }
        const time = event.time != null ? timeKey(event.time) : null
        propsRef.current.onHover?.(time)
        const reportCrosshair = propsRef.current.onCrosshairMove
        if (reportCrosshair) {
          const primarySpec = propsRef.current.series.find(s => s.pane === 0 && s.visible !== false && s.priceScaleId !== 'left')
          const primary = primarySpec && seriesRef.current.get(primarySpec.id)
          const point = event.point
          if (event.paneIndex === 0 && point && time && primary) {
            const price = primary.api.coordinateToPrice(point.y)
            const upper = primary.api.coordinateToPrice(1)
            const lower = primary.api.coordinateToPrice(0)
            const step = upper != null && lower != null ? upper - lower : null
            reportCrosshair(price != null && Number.isFinite(price) && step != null && Number.isFinite(step) && step !== 0
              ? { time, price, pixelsPerPrice: Math.abs(step) } : null)
          } else reportCrosshair(null)
        }
      })
    }
    const select = (event: MouseEventParams<Time>) => {
      if (event.paneIndex === 0 && event.time != null) propsRef.current.onSelect?.(timeKey(event.time))
    }
    const doubleClick = (event: MouseEventParams<Time>) => {
      if (event.paneIndex !== 0 || !event.point || event.point.x > chart.timeScale().width()) return
      const primarySpec = propsRef.current.series.find(s => s.pane === 0 && s.visible !== false && s.priceScaleId !== 'left')
      const primary = primarySpec && seriesRef.current.get(primarySpec.id)
      const price = primary?.api.coordinateToPrice(event.point.y)
      const current = propsRef.current.currentPrice
      if (price != null && Number.isFinite(price) && price > 0 && current != null && current > 0) propsRef.current.onPriceDoubleClick?.(price, current)
    }
    const zoom = () => {
      const range = chart.timeScale().getVisibleLogicalRange()
      const compact = !!range && range.to - range.from > 60
      compactRef.current = compact
      annotationRef.current.setCompact(compact)
      paneAnnotations.current.forEach(item => item.annotation.setCompact(compact))
      markerRef.current?.setMarkers(markersForView(propsRef.current.markers, compact))
    }
    const resize = () => {
      if (!propsRef.current.intraday) return
      cancelAnimationFrame(resizeFrame)
      resizeFrame = requestAnimationFrame(() => {
        if (chartRef.current === chart && propsRef.current.intraday) chart.timeScale().fitContent()
      })
    }
    chart.subscribeCrosshairMove(hover); chart.subscribeClick(select); chart.subscribeDblClick(doubleClick)
    chart.timeScale().subscribeVisibleLogicalRangeChange(zoom)
    chart.timeScale().subscribeSizeChange(resize)
    return () => {
      cancelAnimationFrame(frame)
      cancelAnimationFrame(resizeFrame)
      chart.unsubscribeCrosshairMove(hover); chart.unsubscribeClick(select); chart.unsubscribeDblClick(doubleClick)
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(zoom)
      chart.timeScale().unsubscribeSizeChange(resize)
      markerRef.current?.detach(); markerRef.current = null
      chart.remove(); chartRef.current = null; seriesRef.current.clear(); scopeRef.current = null
      paneAnnotations.current.clear(); attachedMain.current = null; attachedLeft.current = null
    }
  }, [])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    chart.applyOptions({ layout: { textColor: ct.text, panes: { separatorColor: ct.border } },
      grid: { horzLines: { color: ct.grid, visible: !propsRef.current.annotations?.gridTicks } }, timeScale: { borderColor: ct.border },
      crosshair: { vertLine: { color: ct.crosshair, labelBackgroundColor: ct.crosshairLabelBg }, horzLine: { color: ct.crosshair, labelBackgroundColor: ct.crosshairLabelBg } } })
  }, [ct])

  useEffect(() => {
    const intraday = !!props.intraday
    // 分时固定完整时段；不能锁到最后一根实值，否则尚未到达的时段会被挤到左侧。
    chartRef.current?.applyOptions({
      handleScale: !intraday, handleScroll: !intraday,
      layout: { panes: { enableResize: !intraday } },
      rightPriceScale: { ...(intraday ? { autoScale: true } : {}), entireTextOnly: intraday,
        scaleMargins: intraday ? PRICE_MARGINS.intraday : PRICE_MARGINS.daily },
      timeScale: { timeVisible: intraday, fixRightEdge: !intraday && !props.rightOffsetPixels, rightOffset: 0,
        minBarSpacing: intraday ? 0.01 : 0.5, lockVisibleTimeRangeOnResize: props.visibleBars === 'all' },
    })
  }, [props.intraday, props.visibleBars])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const reset = scopeRef.current !== props.scope
    const paneChanged = props.series.some(spec => {
      const old = seriesRef.current.get(spec.id)
      return old && (old.pane !== spec.pane || old.type !== spec.type || old.scale !== spec.priceScaleId)
    })
    chart.applyOptions({
      leftPriceScale: { visible: props.series.some(s => s.priceScaleId === 'left'), borderVisible: false, minimumWidth: 64,
        autoScale: true, alignLabels: false, scaleMargins: PRICE_MARGINS.intraday },
      rightPriceScale: { alignLabels: !props.annotations?.axisTicks },
      grid: { horzLines: { visible: !props.annotations?.gridTicks } },
    })
    // 原生移除空 pane 会重排索引；拓扑变化时统一重建系列，图表实例与视口仍保留。
    const view = reset || props.intraday ? null : chart.timeScale().getVisibleLogicalRange()
    if (reset || paneChanged) {
      markerRef.current?.detach(); markerRef.current = null
      for (const { api } of [...seriesRef.current.values()].reverse()) chart.removeSeries(api)
      seriesRef.current.clear()
      paneAnnotations.current.clear()
    }
    const wanted = new Set(props.series.map(s => s.id))
    for (const [id, item] of [...seriesRef.current].reverse()) if (!wanted.has(id)) {
      if (id === props.series[0]?.id) { markerRef.current?.detach(); markerRef.current = null }
      chart.removeSeries(item.api); seriesRef.current.delete(id)
    }
    for (const spec of props.series) {
      const options = { ...(spec.color ? { color: spec.color } : {}), lineWidth: spec.lineWidth ?? 1, lastValueVisible: false, priceLineVisible: false,
        ...(spec.priceScaleId ? { priceScaleId: spec.priceScaleId, lineVisible: false } : {}),
        crosshairMarkerVisible: false, visible: spec.visible ?? true,
        priceFormat: spec.priceFormat === 'volume' ? { type: 'custom' as const, formatter: fmtBigNum, minMove: 1 }
          : props.intraday && spec.pane === 0 ? {
            type: 'custom' as const, minMove: 10 ** -(props.pricePrecision ?? 2),
            formatter: spec.priceFormat === 'percent' ? formatAxisPercent : (value: number) => value.toFixed(props.pricePrecision ?? 2),
            tickmarksFormatter: (values: number[]) => {
              if (spec.axisTicks || propsRef.current.annotations?.axisTicks) return values.map(() => '')
              const main = propsRef.current.series.find(s => s.pane === 0 && s.visible !== false && s.priceScaleId !== 'left')
              const api = main && seriesRef.current.get(main.id)?.api
              const anchors = (propsRef.current.annotations?.priceLines ?? []).filter(line => line.axisLabel)
                .map(line => axisLabelCoordinate(api, line.value)).filter((y): y is NonNullable<typeof y> => y != null)
              return values.map(value => {
                const y = api?.priceToCoordinate(value)
                // 价签附近的普通刻度整项隐藏，避免被遮成半截数字。
                return y != null && anchors.some(anchor => Math.abs(anchor - y) < 20) ? '' : value.toFixed(propsRef.current.pricePrecision ?? 2)
              })
            },
          } : { type: 'price' as const, precision: spec.precision ?? (spec.pane === 0 ? props.pricePrecision : undefined) ?? 2,
            minMove: 10 ** -(spec.precision ?? (spec.pane === 0 ? props.pricePrecision : undefined) ?? 2) },
        ...(spec.type === 'Area' ? { lineColor: spec.color, topColor: `${spec.color}${props.intraday ? '52' : '28'}`,
          bottomColor: `${spec.color}${props.intraday ? '05' : '02'}` } : {}),
        ...(spec.type === 'Baseline' ? { baseValue: { type: 'price' as const, price: spec.baseValue! },
          // 固定到价格 pane；按曲线极值定位渐变会在全日高于昨收时产生绿色矩形。
          topLineColor: spec.color, bottomLineColor: spec.color, relativeGradient: false,
          topFillColor1: 'rgba(199,64,64,0.32)', topFillColor2: 'rgba(199,64,64,0.02)',
          bottomFillColor1: 'rgba(45,155,101,0.02)', bottomFillColor2: 'rgba(45,155,101,0.32)' } : {}),
        ...(spec.type === 'Candlestick' ? { upColor: '#C74040', downColor: '#2D9B65', borderVisible: false, wickUpColor: '#C74040', wickDownColor: '#2D9B65' } : {}),
        ...(spec.range ? { autoscaleInfoProvider: () => ({ priceRange: { minValue: spec.range!.min, maxValue: spec.range!.max } }) } : { autoscaleInfoProvider: undefined }),
      }
      let item = seriesRef.current.get(spec.id)
      if (!item) {
        const definition = spec.type === 'Candlestick' ? CandlestickSeries : spec.type === 'Area' ? AreaSeries : spec.type === 'Baseline' ? BaselineSeries : spec.type === 'Histogram' ? HistogramSeries : LineSeries
        const api = (spec.type === 'Volume' ? chart.addCustomSeries(new DailyVolumeSeries(), options, spec.pane)
          : chart.addSeries(definition, options, spec.pane)) as ISeriesApi<SeriesType>
        item = { api, data: [], pane: spec.pane, type: spec.type, scale: spec.priceScaleId }; seriesRef.current.set(spec.id, item)
        api.setData(spec.data)
      } else {
        item.api.applyOptions(options)
        updatePlotData(item.api, item.data, spec.data)
      }
      item.data = spec.data
      if (spec.pane > 0) item.api.priceScale().applyOptions({
        entireTextOnly: !!props.intraday,
        scaleMargins: spec.id === 'volume' ? { top: props.intraday ? 0.22 : 0.3, bottom: 0 } : { top: 0.28, bottom: 0.08 },
      })
    }
    const primarySpec = props.series.find(s => s.pane === 0 && s.visible !== false && s.priceScaleId !== 'left')
    const primary = primarySpec && seriesRef.current.get(primarySpec.id)?.api
    if (primary) {
      if (attachedMain.current !== primary) { primary.attachPrimitive(annotationRef.current); attachedMain.current = primary }
      if (props.markers && !markerRef.current) markerRef.current = createSeriesMarkers(primary, [])
    }
    const leftSpec = props.series.find(s => s.priceScaleId === 'left')
    const left = leftSpec && seriesRef.current.get(leftSpec.id)?.api
    if (left && attachedLeft.current !== left) { left.attachPrimitive(leftAnnotation.current); attachedLeft.current = left }
    for (const pane of new Set(props.series.map(s => s.pane).filter(p => p > 0))) {
      const spec = props.series.find(s => s.pane === pane)!
      const api = seriesRef.current.get(spec.id)!.api
      if (paneAnnotations.current.get(pane)?.api !== api) {
        const annotation = new ChartAnnotations(); api.attachPrimitive(annotation)
        paneAnnotations.current.set(pane, { api, annotation, volume: spec.id === 'volume' })
      }
    }
    props.paneHeights.forEach((value, i) => chart.panes()[i]?.setStretchFactor(value))
    if (props.intraday) chart.timeScale().fitContent()
    else if (reset) {
      const count = Math.max(...props.series.map(s => s.data.length), 0)
      if (props.visibleBars === 'all') chart.timeScale().fitContent()
      else if (count > 0) {
        // 默认每格约 12 px；宽窗不画粗柱，窄窗也保留可读的实体和间隔。
        const bars = props.visibleBars ?? Math.max(32, Math.min(90, Math.round(chart.timeScale().width() / 12)))
        const offsetPixels = props.rightOffsetPixels
        if (offsetPixels && chart.timeScale().width() > offsetPixels) {
          // 右侧像素留白按比例换算成额外的空白 bar，保证标签带出现后可见蜡烛数不变。
          const extra = offsetPixels * bars / (chart.timeScale().width() - offsetPixels)
          chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, count - bars - extra), to: count - 1 })
          chart.timeScale().applyOptions({ rightOffsetPixels: offsetPixels })
        } else {
          chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, count - bars), to: count - 1 })
        }
      }
    } else if (view) chart.timeScale().setVisibleLogicalRange(view)
    if (reset) props.onHover?.(null)
    scopeRef.current = props.scope
  }, [props.scope, props.series, props.paneHeights, props.intraday, props.visibleBars, props.pricePrecision])

  useEffect(() => {
    const lines = [...(props.annotations?.priceLines ?? [])]
    if (props.linkedPrice != null) lines.push({ value: props.linkedPrice, color: ct.crosshair })
    annotationRef.current.update({ ...props.annotations, dates: props.annotations?.dates ?? [], priceLines: lines, textColor: ct.text, gridColor: ct.grid,
      precision: props.pricePrecision })
    leftAnnotation.current.update({ dates: [], axisTicks: props.series.find(s => s.priceScaleId === 'left')?.axisTicks, textColor: ct.text })
    paneAnnotations.current.forEach((item, pane) => item.annotation.update({ dates: [],
      labels: item.volume ? props.volumeLabels : undefined, title: props.paneTitles?.[pane], readouts: props.paneReadouts?.[pane], textColor: ct.text }))
    markerRef.current?.setMarkers(markersForView(props.markers, compactRef.current))
  }, [props.annotations, props.markers, props.volumeLabels, props.paneTitles, props.paneReadouts, props.linkedPrice, props.series, props.pricePrecision, ct])

  return <div ref={host} role="group" aria-label={props.intraday ? '分时价格与成交量图' : '日 K 与技术指标图'} data-chart-engine="lightweight" className="w-full min-w-0 [&_canvas]:!cursor-crosshair" style={{ height: props.height }} />
}
