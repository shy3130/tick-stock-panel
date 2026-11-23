import type { IChartApi, ISeriesApi, SeriesType, Time } from 'lightweight-charts'
import type { PlotDrawing } from '@/lib/custom-chart-indicators'
import { timeKey } from '@/lib/lightweight-model'

const comparable = (time: Time): string | number => typeof time === 'object' ? timeKey(time) : time

/** All shapes use the same native time/price scales as their indicator series. */
export function paintIndicatorDrawings(ctx: CanvasRenderingContext2D, chart: IChartApi, series: ISeriesApi<SeriesType>, drawings: PlotDrawing[], width: number, height: number, header = 0) {
  if (!drawings.length) return
  const scale = chart.timeScale(), range = scale.getVisibleRange?.(), spacing = scale.options().barSpacing
  const lower = range ? comparable(range.from) : null, upper = range ? comparable(range.to) : null
  for (const drawing of drawings) {
    let points = drawing.points
    if (range && drawing.kind !== 'fixed_text') {
      const bound = (value: string | number, inclusive: boolean) => {
        let lo = 0, hi = points.length
        while (lo < hi) { const mid = (lo + hi) >>> 1; if (comparable(points[mid].time) < value || (inclusive && comparable(points[mid].time) === value)) lo = mid + 1; else hi = mid }
        return lo
      }
      points = points.slice(Math.max(0, bound(lower!, false) - 1), bound(upper!, true) + 1)
    }
    ctx.save()
    ctx.lineWidth = drawing.line_width ?? 1
    ctx.font = '11px "JetBrains Mono", monospace'
    ctx.textBaseline = 'bottom'
    if (drawing.empty === -1 || drawing.dotted) ctx.setLineDash([3, 3])
    let previous: { x: number; y: number; bottom?: number } | undefined
    const text = (value: string, x: number, y: number, above = false) => {
      const lines = value.split('&')
      ctx.textBaseline = 'top'
      lines.forEach((line, index) => ctx.fillText(line, x, y + index * 14 - (above ? lines.length * 14 : 0)))
    }
    for (const point of points) {
      if (point.break_before) previous = undefined
      ctx.fillStyle = point.color ?? drawing.color
      ctx.strokeStyle = ctx.fillStyle
      if (drawing.kind === 'fixed_text') {
        const x = (drawing.x ?? 0) * width, y = header + 6 + (drawing.y ?? 0) * Math.max(0, height - header - 6)
        ctx.textAlign = drawing.align ? 'right' : 'left'; ctx.textBaseline = 'top'
        text(point.text ?? '', x, y); continue
      }
      const x = scale.timeToCoordinate(point.time), y = series.priceToCoordinate(point.value)
      if (x == null || y == null) { previous = undefined; continue }
      const end = point.end_value == null ? null : series.priceToCoordinate(point.end_value)
      if (drawing.kind === 'line' && previous) {
        ctx.beginPath(); ctx.moveTo(previous.x, previous.y); ctx.lineTo(x, y); ctx.stroke()
      } else if (drawing.kind === 'band' && previous?.bottom != null && end != null) {
        ctx.globalAlpha = 0.2
        ctx.beginPath(); ctx.moveTo(previous.x, previous.y); ctx.lineTo(x, y)
        ctx.lineTo(x, end); ctx.lineTo(previous.x, previous.bottom); ctx.closePath(); ctx.fill()
        ctx.globalAlpha = 1
      } else if (drawing.kind === 'stick' && end != null) {
        const size = Math.max(1, Math.min(spacing, spacing * (drawing.width ?? 2) / 4))
        const top = Math.min(y, end), span = Math.max(1, Math.abs(end - y))
        if (drawing.empty) ctx.strokeRect(x - size / 2, top, size, span)
        else ctx.fillRect(x - size / 2, top, size, span)
      } else if (drawing.kind === 'text') {
        text(point.text ?? '', x, y, drawing.above)
      } else if (drawing.kind === 'icon') {
        const direction = point.icon === 2 ? -1 : 1
        const tip = y + (drawing.above ? -14 : 0) + (direction < 0 ? 14 : 0)
        ctx.beginPath(); ctx.moveTo(x, tip); ctx.lineTo(x - 5, tip + direction * 8); ctx.lineTo(x + 5, tip + direction * 8); ctx.closePath(); ctx.fill()
        ctx.fillRect(x - 1.5, Math.min(tip + direction * 8, tip + direction * 14), 3, 6)
      } else if (drawing.kind === 'circles') {
        ctx.beginPath(); ctx.arc(x, y, Math.max(2, drawing.line_width ?? 2), 0, Math.PI * 2); ctx.stroke()
      } else if (drawing.kind === 'crosses') {
        const radius = Math.max(2, drawing.line_width ?? 2)
        ctx.beginPath(); ctx.moveTo(x - radius, y - radius); ctx.lineTo(x + radius, y + radius)
        ctx.moveTo(x - radius, y + radius); ctx.lineTo(x + radius, y - radius); ctx.stroke()
      } else if (drawing.kind === 'candle' && end != null && point.open != null && point.close != null) {
        const open = series.priceToCoordinate(point.open), close = series.priceToCoordinate(point.close)
        if (open != null && close != null) {
          ctx.fillStyle = point.color ?? (point.close >= point.open ? '#C74040' : '#2D9B65'); ctx.strokeStyle = ctx.fillStyle
          ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x, end); ctx.stroke()
          const size = Math.max(1, spacing * 0.7)
          ctx.fillRect(x - size / 2, Math.min(open, close), size, Math.max(1, Math.abs(open - close)))
        }
      }
      previous = { x, y, bottom: end ?? undefined }
    }
    ctx.restore()
  }
}
