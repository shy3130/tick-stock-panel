import type { IPanePrimitive, IPanePrimitivePaneView, PaneAttachedParameter, Time } from 'lightweight-charts'
import { intradayTimeLabel, timeKey } from '@/lib/lightweight-model'

interface AxisTheme { text: string; crosshairLabelBg: string }
interface AxisTick { time: Time; logical: number; text: string; priority: number }

export function timeAxisTicks(times: Time[], intraday: boolean): AxisTick[] {
  const ordered = [...new Map(times.map(time => [timeKey(time), time])).values()]
    .sort((a, b) => timeKey(a).localeCompare(timeKey(b)))
  const singleDay = timeKey(ordered[0] ?? '').slice(0, 10) === timeKey(ordered.at(-1) ?? '').slice(0, 10)
  return ordered.flatMap((time, index): AxisTick[] => {
    const key = timeKey(time), previous = index ? timeKey(ordered[index - 1]) : ''
    if (!intraday) return [{ time, logical: index, text: previous && key.slice(0, 4) !== previous.slice(0, 4) ? key.slice(0, 4) : key.slice(5, 10),
      priority: previous && key.slice(0, 4) !== previous.slice(0, 4) ? 4 : key.slice(0, 7) !== previous.slice(0, 7) ? 3 : 0 }]
    const clock = intradayTimeLabel(time).slice(11)
    const dayStart = key.slice(0, 10) !== previous.slice(0, 10)
    if (!dayStart && index !== ordered.length - 1 && !clock.includes('/') && Number(key.slice(14, 16)) % 15 !== 0) return []
    return [{ time, logical: index, text: dayStart && !singleDay ? key.slice(5, 10) : clock,
      priority: dayStart || (singleDay && index === ordered.length - 1) ? 4
        : clock.includes('/') ? 3 : clock.endsWith(':00') ? 2 : clock.endsWith(':30') ? 1 : 0 }]
  })
}

/** 使用同一个原生 timeScale 绘制主图下方横轴，副图不再另占日期／时间栏。 */
export class ChartTimeAxis implements IPanePrimitive<Time> {
  private chart?: PaneAttachedParameter<Time>['chart']
  private requestUpdate?: () => void
  private ticks: AxisTick[] = []
  private intraday = false
  private theme: AxisTheme = { text: '#A1A1AA', crosshairLabelBg: '#333' }
  private hoverTime?: Time
  private views: IPanePrimitivePaneView[] = [{ zOrder: () => 'top', renderer: () => ({ draw: target => {
    const chart = this.chart
    if (!chart) return
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize: { width, height } }) => {
      const scale = chart.timeScale(), range = scale.getVisibleLogicalRange()
      if (!range) return
      ctx.save()
      ctx.font = '11px "JetBrains Mono", monospace'
      ctx.textBaseline = 'middle'
      const candidates = this.ticks
        .filter(tick => tick.logical >= range.from - 1 && tick.logical <= range.to + 1)
        .flatMap(tick => {
          const x = scale.timeToCoordinate(tick.time)
          // fitContent 的首尾柱中心可能在边缘外半像素，仍需保留开收盘刻度。
          if (x == null || x < -1 || x > width + 1) return []
          const size = ctx.measureText(tick.text).width
          const left = Math.max(3, Math.min(x - size / 2, width - size - 3))
          return [{ ...tick, x, left, right: left + size }]
        }).sort((a, b) => b.priority - a.priority || a.x - b.x)
      const placed: { left: number; right: number }[] = []
      ctx.fillStyle = this.theme.text
      for (const tick of candidates) {
        if (placed.some(other => tick.left < other.right + 12 && tick.right + 12 > other.left)) continue
        placed.push(tick)
        ctx.fillRect(Math.round(tick.x), 0, 1, 3)
        ctx.fillText(tick.text, tick.left, height / 2 + 1)
      }
      if (this.hoverTime != null) {
        const x = scale.timeToCoordinate(this.hoverTime)
        if (x != null && x >= -1 && x <= width + 1) {
          const text = this.intraday ? intradayTimeLabel(this.hoverTime) : timeKey(this.hoverTime)
          const size = Math.min(width, ctx.measureText(text).width + 12)
          const left = Math.max(0, Math.min(x - size / 2, width - size))
          ctx.fillStyle = this.theme.crosshairLabelBg
          ctx.fillRect(left, 1, size, height - 2)
          ctx.fillStyle = '#FFF'
          ctx.fillText(text, left + 6, height / 2 + 1)
        }
      }
      ctx.restore()
    })
  } }) }]

  attached({ chart, requestUpdate }: PaneAttachedParameter<Time>) { this.chart = chart; this.requestUpdate = requestUpdate }
  detached() { this.chart = undefined; this.requestUpdate = undefined; this.hoverTime = undefined }
  paneViews() { return this.views }
  update(times: Time[], intraday: boolean, theme: AxisTheme) {
    this.ticks = timeAxisTicks(times, intraday)
    this.intraday = intraday; this.theme = theme
    this.requestUpdate?.()
  }
  setHoverTime(time?: Time) { this.hoverTime = time; this.requestUpdate?.() }
}
