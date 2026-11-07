import type { Time, UTCTimestamp, CandlestickData, LineData, HistogramData, WhitespaceData } from 'lightweight-charts'
import type { MinuteKlineSession } from './api'
import type { OHLC } from './chart-types'
import { computeIntradayAverage, formatMinuteTime, FULL_DAY_TIMES } from './intraday-chart'

export const CHART_COLORS = { up: '#C74040', down: '#2D9B65', flat: '#A1A1AA', average: '#F59E0B', blue: '#3B82F6', purple: '#8B5CF6' }
const SESSION_TIMES = new Set(FULL_DAY_TIMES)
// 午休不占用交易时间：11:30 / 13:00 共用中点，下一格直接进入 13:01。
const PLOT_DAY_TIMES = FULL_DAY_TIMES.filter(time => time !== '11:30')
export type PlotData = CandlestickData<Time> | LineData<Time> | HistogramData<Time> | WhitespaceData<Time>
export interface PlotAxisTick { value: number; text: string; color?: string; priority?: number }
export interface PlotSeries {
  id: string
  type: 'Candlestick' | 'Line' | 'Histogram' | 'Area' | 'Baseline' | 'Volume'
  pane: number
  data: PlotData[]
  color?: string
  /** 线宽(默认 1)；悬浮高亮时加粗 */
  lineWidth?: 1 | 2 | 3 | 4
  priceFormat?: 'price' | 'volume' | 'percent'
  priceScaleId?: 'left'
  axisTicks?: PlotAxisTick[]
  baseValue?: number
  precision?: number
  range?: { min: number; max: number }
  visible?: boolean
}

/** 画布专用北京墙钟键，不回传给 API，不受浏览器宿主时区影响。 */
export function minuteTime(value: string): UTCTimestamp {
  const match = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value)
  if (!match) throw new Error(`分钟时间格式异常: ${value}`)
  const [, y, m, d, h, min, sec] = match
  return (Date.UTC(+y, +m - 1, +d, +h, +min, +(sec ?? 0)) / 1000) as UTCTimestamp
}

export function timeKey(time: Time): string {
  if (typeof time === 'number') return new Date(time * 1000).toISOString().slice(0, 16).replace('T', ' ')
  if (typeof time === 'string') return time
  return `${time.year}-${String(time.month).padStart(2, '0')}-${String(time.day).padStart(2, '0')}`
}

export function intradayTimeLabel(time: Time): string {
  const key = timeKey(time)
  return key.endsWith(' 13:00') ? `${key.slice(0, 11)}11:30/13:00` : key
}

export function validDailyRows(rows: OHLC[]): OHLC[] {
  return [...new Map(rows.filter(row => /^\d{4}-\d{2}-\d{2}$/.test(row.date)
    && [row.open, row.high, row.low, row.close].every(v => Number.isFinite(v) && v > 0))
    .map(row => [row.date, row])).values()].sort((a, b) => a.date.localeCompare(b.date))
}

export function lineData(rows: OHLC[], field: keyof OHLC): PlotData[] {
  return rows.map(row => typeof row[field] === 'number' && Number.isFinite(row[field])
    ? { time: row.date, value: row[field] as number } : { time: row.date })
}

export interface IntradayPoint {
  date: string
  row: MinuteKlineSession['rows'][number]
  average: number | null
  prevClose: number | null
  /** 同一午休衔接位置内的真实源时间，仅双边都有记录时设置。 */
  sourceTimes?: string[]
}

export function buildIntradayModel(sessions: MinuteKlineSession[], showAverage = true, assetType?: 'stock' | 'etf' | 'index') {
  const series: PlotSeries[] = []
  const points = new Map<string, IntradayPoint>()
  const timeline: WhitespaceData<Time>[] = []
  const volume: PlotData[] = []
  for (const session of [...sessions].sort((a, b) => a.date.localeCompare(b.date))) {
    const rows = [...new Map(session.rows.map(row => [formatMinuteTime(row.datetime), row])).values()]
      .filter(row => row.datetime.slice(0, 10) === session.date && SESSION_TIMES.has(formatMinuteTime(row.datetime))
        && Number.isFinite(row.close) && row.close > 0)
      .sort((a, b) => a.datetime.localeCompare(b.datetime))
    const averages = computeIntradayAverage(rows, assetType)
    const byTime = new Map(rows.map((row, i) => [formatMinuteTime(row.datetime), { row, average: averages[i] }]))
    const lastPrice = rows.at(-1)?.close
    const color = session.prev_close == null || lastPrice == null ? CHART_COLORS.flat
      : lastPrice > session.prev_close ? CHART_COLORS.up : lastPrice < session.prev_close ? CHART_COLORS.down : CHART_COLORS.flat
    let priceSegment: PlotData[] = [], averageSegment: PlotData[] = [], segment = 0
    let previous = session.prev_close
    const flush = () => {
      if (priceSegment.length) series.push({ id: `${session.date}:price:${segment}`, type: 'Area', pane: 0, color, data: priceSegment })
      if (showAverage && assetType !== 'index' && averageSegment.length) series.push({ id: `${session.date}:average:${segment}`, type: 'Line', pane: 0, color: CHART_COLORS.average, data: averageSegment })
      priceSegment = []; averageSegment = []; segment++
    }
    for (const minute of PLOT_DAY_TIMES) {
      const time = minuteTime(`${session.date} ${minute}`)
      timeline.push({ time })
      const group = (minute === '13:00' ? [byTime.get('11:30'), byTime.get('13:00')] : [byTime.get(minute)])
        .filter((point): point is NonNullable<typeof point> => !!point)
      if (!group.length) { flush(); volume.push({ time }); continue }
      group.forEach(({ row, average }, index) => {
        // 两个真实价格分别保留在同一 x 的系列端点，避免同系列重复时间或覆盖午收价。
        if (index > 0) flush()
        priceSegment.push({ time, value: row.close })
        averageSegment.push(average != null ? { time, value: average } : { time })
      })
      const last = group[group.length - 1]
      const row = group.length === 1 ? last.row : {
        ...last.row,
        open: group[0].row.open,
        high: Math.max(...group.map(p => p.row.high)),
        low: Math.min(...group.map(p => p.row.low)),
        volume: group.every(p => Number.isFinite(p.row.volume) && p.row.volume >= 0)
          ? group.reduce((sum, p) => sum + p.row.volume, 0) : NaN,
        amount: group.every(p => p.row.amount != null && Number.isFinite(p.row.amount))
          ? group.reduce((sum, p) => sum + p.row.amount!, 0) : null,
      }
      volume.push(Number.isFinite(row.volume) && row.volume >= 0 ? {
        time, value: row.volume,
        color: previous == null || row.close === previous ? 'rgba(161,161,170,0.45)'
          : row.close > previous ? 'rgba(199,64,64,0.55)' : 'rgba(45,155,101,0.55)',
      } : { time })
      previous = row.close
      points.set(timeKey(time), { date: session.date, row, average: last.average, prevClose: session.prev_close,
        ...(group.length > 1 ? { sourceTimes: group.map(p => formatMinuteTime(p.row.datetime)) } : {}),
      })
    }
    flush()
  }
  series.unshift({ id: 'timeline', type: 'Line', pane: 0, data: timeline, visible: false })
  series.push({ id: 'volume', type: 'Histogram', pane: 1, data: volume, priceFormat: 'volume' })
  return { series, points, timeline, latest: [...points.values()].at(-1) ?? null }
}
