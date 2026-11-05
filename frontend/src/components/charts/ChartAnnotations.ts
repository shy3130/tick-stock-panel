import type { IChartApi, ISeriesApi, ISeriesPrimitive, IPrimitivePaneView, IPrimitivePaneRenderer, ISeriesPrimitiveAxisView, SeriesType, Time, SeriesAttachedParameter } from 'lightweight-charts'
import type { ChartRange, ChartPriceLine } from '@/lib/chart-types'
import type { PlotAxisTick } from '@/lib/lightweight-model'

export interface AnnotationData {
  dates: string[]
  ranges?: ChartRange[]
  priceLines?: (ChartPriceLine & { axisLabel?: boolean; axisLabelTextColor?: string; opacity?: number })[]
  axisTicks?: PlotAxisTick[]
  gridTicks?: number[]
  gridColor?: string
  precision?: number
  verticals?: { time: Time; label?: string; color?: string }[]
  labels?: { time: Time; value: number; text: string }[]
  textColor?: string
  title?: string
  readouts?: { text: string; color?: string }[]
}

export function axisLabelCoordinate(series: ISeriesApi<SeriesType> | undefined, value: number) {
  if (!series || !Number.isFinite(value)) return undefined
  const y = series.priceToCoordinate(value), height = series.getPane().getHeight()
  if (y == null || !Number.isFinite(y) || y < -0.5 || y > height + 0.5 || height < 20) return undefined
  return Math.max(10, Math.min(height - 10, y))
}

/** 标注随原生画布缩放，避免用绝对定位 DOM 在滚动后与蜡烛错位。 */
export class ChartAnnotations implements ISeriesPrimitive<Time> {
  private chart?: IChartApi
  private series?: ISeriesApi<SeriesType>
  private requestUpdate?: () => void
  private data: AnnotationData = { dates: [] }
  private compact = false
  private hoverY?: number
  private axisViews: ISeriesPrimitiveAxisView[] = []
  private view: IPrimitivePaneView = {
    zOrder: () => 'top',
    renderer: () => ({ draw: target => this.draw(target) }),
  }

  attached({ chart, series, requestUpdate }: SeriesAttachedParameter<Time>) {
    this.chart = chart; this.series = series; this.requestUpdate = requestUpdate
    this.update(this.data)
  }
  detached() { this.chart = undefined; this.series = undefined; this.requestUpdate = undefined; this.axisViews = []; this.hoverY = undefined }
  paneViews() { return [this.view] }
  priceAxisViews() { return this.axisViews }
  update(data: AnnotationData) {
    this.data = data
    const ticks: PlotAxisTick[] = [...(data.axisTicks ?? []), ...(data.priceLines ?? []).filter(line => line.axisLabel
      && Number.isFinite(line.value) && line.value > 0
      && !(data.dates.length && ((line.start && line.start > data.dates.at(-1)!) || (line.end && line.end < data.dates[0]))))
      .map(line => ({ value: line.value, text: line.value.toFixed(data.precision ?? 2), color: line.axisLabelTextColor ?? line.color, priority: 2 }))]
    this.axisViews = this.series ? ticks.map((tick, index) => ({
        // 价签跟随虚线，仅在绘图区边缘向内收，避免文字被裁切。
        coordinate: () => -1e9,
        fixedCoordinate: () => axisLabelCoordinate(this.series, tick.value),
        text: () => tick.text,
        textColor: () => tick.color ?? this.data.textColor ?? '#A1A1AA',
        backColor: () => 'transparent',
        visible: () => {
          const y = axisLabelCoordinate(this.series, tick.value)
          return y != null && (this.hoverY == null || Math.abs(y - this.hoverY) >= 18) && !ticks.some((other, i) => {
            const otherY = axisLabelCoordinate(this.series, other.value)
            return i !== index && otherY != null && Math.abs(otherY - y) < 20
              && ((other.priority ?? 0) > (tick.priority ?? 0) || ((other.priority ?? 0) === (tick.priority ?? 0) && i < index))
          })
        },
        tickVisible: () => false,
      })) : []
    this.requestUpdate?.()
  }
  setCompact(value: boolean) { this.compact = value; this.requestUpdate?.() }
  setHoverY(value?: number) { this.hoverY = value; this.requestUpdate?.() }

  private draw(target: Parameters<IPrimitivePaneRenderer['draw']>[0]) {
    const chart = this.chart, series = this.series
    if (!chart || !series) return
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const ts = chart.timeScale(), width = mediaSize.width, height = mediaSize.height
      const coordinate = (date: string, end = false) => {
        const index = this.data.dates.findIndex(d => d >= date)
        const i = index < 0 ? this.data.dates.length : index
        return ts.logicalToCoordinate((end && this.data.dates[i] !== date ? i - 1 : i) as never)
      }
      ctx.save(); ctx.beginPath(); ctx.rect(0, 0, width, height); ctx.clip()
      ctx.font = '10px "JetBrains Mono", monospace'
      for (const value of this.data.gridTicks ?? []) {
        const y = series.priceToCoordinate(value)
        if (y == null) continue
        ctx.strokeStyle = this.data.gridColor ?? '#27272A'; ctx.lineWidth = 0.7
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke()
      }
      let headerBottom = this.data.title ? 14 : 0
      if (this.data.title) { ctx.fillStyle = this.data.textColor ?? '#A1A1AA'; ctx.fillText(this.data.title, 8, 14) }
      if (this.data.readouts?.length) {
        const titleWidth = this.data.title ? ctx.measureText(this.data.title).width + 12 : 0
        let row: { text: string; color?: string; width: number }[] = [], rowWidth = 0, y = 14
        const flush = () => {
          let x = width - 8 - rowWidth
          for (const item of row) {
            ctx.fillStyle = item.color ?? this.data.textColor ?? '#A1A1AA'
            ctx.fillText(item.text, x, y)
            x += item.width + 12
          }
          headerBottom = y; row = []; rowWidth = 0; y += 12
        }
        for (const item of this.data.readouts) {
          const itemWidth = ctx.measureText(item.text).width
          const available = width - 16 - (y === 14 ? titleWidth : 0)
          if (row.length && rowWidth + 12 + itemWidth > available) flush()
          rowWidth += (row.length ? 12 : 0) + itemWidth
          row.push({ ...item, width: itemWidth })
        }
        if (row.length) flush()
      }
      for (const range of this.data.ranges ?? []) {
        if (!this.data.dates.length || range.start > this.data.dates.at(-1)! || range.end < this.data.dates[0]) continue
        const x1 = coordinate(range.start), x2 = coordinate(range.end, true)
        if (x1 == null || x2 == null || x2 < x1) continue
        ctx.fillStyle = range.color ?? 'rgba(59,130,246,0.08)'
        ctx.fillRect(x1, 0, Math.max(1, x2 - x1), height)
        if (range.label) { ctx.fillStyle = this.data.textColor ?? '#A1A1AA'; ctx.fillText(range.label, Math.max(x1 + 5, 5), 15) }
      }
      for (const line of this.data.priceLines ?? []) {
        if (this.data.dates.length && ((line.start && line.start > this.data.dates.at(-1)!) || (line.end && line.end < this.data.dates[0]))) continue
        const y = series.priceToCoordinate(line.value)
        if (y == null || !Number.isFinite(line.value) || line.value <= 0) continue
        const start = line.start ? coordinate(line.start) : 0
        const end = line.end ? coordinate(line.end, true) : width
        if (start == null || end == null || end < start) continue
        ctx.globalAlpha = line.opacity ?? 1
        ctx.strokeStyle = line.color ?? '#3B82F6'; ctx.lineWidth = 1; ctx.setLineDash([4, 4])
        ctx.beginPath(); ctx.moveTo(start, y); ctx.lineTo(end, y); ctx.stroke(); ctx.setLineDash([])
        ctx.globalAlpha = 1
        if (line.label) { ctx.fillStyle = ctx.strokeStyle; ctx.fillText(line.label, Math.max(5, Math.min(end - ctx.measureText(line.label).width - 5, width - 10)), y - 5) }
      }
      let verticalLabelRight = -Infinity
      for (const line of this.data.verticals ?? []) {
        const x = ts.timeToCoordinate(line.time)
        if (x == null || x < 0 || x > width) continue
        ctx.strokeStyle = line.color ?? '#3B82F6'; ctx.setLineDash([3, 4])
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, height); ctx.stroke(); ctx.setLineDash([])
        if (line.label) {
          const textWidth = ctx.measureText(line.label).width
          const left = Math.max(5, Math.min(x + 5, width - textWidth - 5))
          if (left >= verticalLabelRight + 6) {
            ctx.fillStyle = ctx.strokeStyle; ctx.fillText(line.label, left, 15)
            verticalLabelRight = left + textWidth
          }
        }
      }
      let labelRight = -Infinity
      if (!this.compact) for (const label of this.data.labels ?? []) {
        const x = ts.timeToCoordinate(label.time), y = series.priceToCoordinate(label.value)
        if (x == null || y == null || x < 0 || x > width) continue
        const textWidth = ctx.measureText(label.text).width
        if (x - textWidth / 2 < labelRight + 4) continue
        if (this.data.readouts?.length && y - 4 < headerBottom + 12) continue
        ctx.fillStyle = this.data.textColor ?? '#A1A1AA'
        ctx.fillText(label.text, x - textWidth / 2, Math.max(28, y - 4))
        labelRight = x + textWidth / 2
      }
      ctx.restore()
    })
  }
}
