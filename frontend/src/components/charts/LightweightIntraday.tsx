import { useMemo, useState } from 'react'
import type { MinuteKlineRow, MinuteKlineSession, PriceLimitInfo } from '@/lib/api'
import type { ChartPriceLine } from '@/lib/chart-types'
import { buildIntradayModel, CHART_COLORS as C, minuteTime, type PlotSeries } from '@/lib/lightweight-model'
import { getLimitPrices, summarizeMinutes, type DailySummary } from '@/lib/intraday-chart'
import { pricePrecision } from '@/lib/format'
import { fmtBigNum, fmtPct } from '@/lib/format'
import { CHART_LAYOUT, plotPaneHeights } from '@/lib/chart-layout'
import { formatAxisPercent, intradayScale } from '@/lib/intraday-scale'
import { FinancialPlot } from './FinancialPlot'
import type { AnnotationData } from './ChartAnnotations'

interface Props {
  symbol: string
  assetType?: 'stock' | 'etf' | 'index'
  data?: MinuteKlineRow[]
  sessions?: MinuteKlineSession[]
  date?: string
  prevClose?: number
  dailySummary?: DailySummary
  height?: number
  priceLimit?: PriceLimitInfo
  showLimitLines?: boolean
  showAvgLine?: boolean
  currentPrice?: number
  priceLines?: ChartPriceLine[]
  onPriceHover?: (price: number | null) => void
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
}
export function LightweightIntraday({ symbol, assetType, data, sessions: supplied, date, prevClose, dailySummary, height = 420,
  showLimitLines = true, showAvgLine = true, priceLimit, currentPrice, priceLines, onPriceHover, onPriceDoubleClick }: Props) {
  const [yMode, setYMode] = useState<'adaptive' | 'limit'>('adaptive')
  const precision = pricePrecision(assetType)
  const fmt = (v: number | null | undefined) => v != null && Number.isFinite(v) ? v.toFixed(precision) : '—'
  const sessions = useMemo(() => supplied ?? [{ date: date ?? data?.[0]?.datetime.slice(0, 10) ?? '', prev_close: prevClose ?? null, rows: data ?? [] }], [supplied, data, date, prevClose])
  const scope = `${symbol}:${sessions.map(s => s.date).join(',')}`
  const model = useMemo(() => buildIntradayModel(sessions.filter(s => s.date), showAvgLine, assetType), [sessions, showAvgLine, assetType])
  const [hover, setHover] = useState<{ scope: string; time: string } | null>(null)
  const hovered = hover?.scope === scope ? model.points.get(hover.time) : null
  const point = hovered ?? model.latest
  const sourceTimes = hovered || supplied ? point?.sourceTimes : undefined
  const summary = useMemo(() => !supplied && data ? summarizeMinutes(data) : null, [data, supplied])
  const daily = !supplied && dailySummary?.date === date ? dailySummary : null
  const ohlc = hovered?.row ?? daily ?? summary ?? point?.row
  const shown = hovered?.row ?? summary ?? point?.row
  const change = point?.prevClose && shown ? shown.close / point.prevClose - 1 : null
  const reference = sessions.length === 1 && sessions[0].prev_close != null && Number.isFinite(sessions[0].prev_close) && sessions[0].prev_close > 0
    ? sessions[0].prev_close : undefined
  const limits = useMemo(() => assetType !== 'index' && reference && priceLimit && !priceLimit.no_limit
    ? getLimitPrices(reference, priceLimit, assetType) : undefined, [reference, priceLimit, assetType])
  const mode = limits && showLimitLines ? yMode : 'adaptive'
  const scale = useMemo(() => {
    const values = [
      ...model.series.filter(s => s.type === 'Area').flatMap(s => s.data.flatMap(p => 'value' in p ? [p.value] : [])),
      ...[...model.points.values()].flatMap(p => [p.row.low, p.row.high]),
      ...(mode === 'limit' && limits ? [limits.limitDown, limits.limitUp] : []),
    ]
    return intradayScale(values, reference, precision, limits ? { min: limits.limitDown, max: limits.limitUp } : undefined, mode)
  }, [reference, precision, mode, limits, model.points, model.series])
  const tickColor = (percent?: number) => percent == null || Math.abs(percent) < 1e-8 ? undefined : percent > 0 ? C.up : C.down
  const plotSeries = useMemo<PlotSeries[]>(() => {
    const result = model.series.map(s => s.pane === 0 ? { ...s, range: scale?.range,
      ...(s.type === 'Area' && reference ? { type: 'Baseline' as const, baseValue: reference } : {}),
    } : s)
    const timeline = model.series.find(s => s.id === 'timeline')?.data
    if (scale && reference && timeline?.length) result.push({ id: 'percent-axis', type: 'Line', pane: 0,
      priceScaleId: 'left', priceFormat: 'percent', color: 'transparent',
      // 仅建立百分比坐标基准，不合成任何行情点，绘制始终关闭。
      data: [{ time: timeline[0].time, value: 0 }, { time: timeline.at(-1)!.time, value: 0 }],
      range: { min: (scale.range.min / reference - 1) * 100, max: (scale.range.max / reference - 1) * 100 },
      axisTicks: [
        ...scale.ticks.map(t => ({ value: t.percent!, text: formatAxisPercent(t.percent!), color: tickColor(t.percent), priority: t.percent === 0 ? 3 : 0 })),
        ...(limits ? [{ value: limits.upPct, text: formatAxisPercent(limits.upPct), color: C.up, priority: 2 },
          { value: limits.downPct, text: formatAxisPercent(limits.downPct), color: C.down, priority: 2 }] : []),
      ],
    })
    return result
  }, [model.series, scale, reference, limits])
  const plotHeight = Math.max(250, height - CHART_LAYOUT.toolbar - CHART_LAYOUT.readout)
  const paneHeights = useMemo(() => plotPaneHeights(plotHeight, 1), [plotHeight])
  const annotations = useMemo(() => {
    // A daily holding range selects which minute session may show its fill
    // prices. Within that session the lines span the minute axis, not day indices.
    const replayLines = sessions.length === 1 ? (priceLines ?? [])
      .filter(line => (!line.start || line.start <= sessions[0].date) && (!line.end || line.end >= sessions[0].date))
      .map(({ start: _start, end: _end, ...line }) => line) : (priceLines ?? [])
    const lines: NonNullable<AnnotationData['priceLines']> = [...replayLines]
    if (reference) lines.push({ value: reference, color: '#A1A1AA', opacity: 0.8 })
    if (limits && showLimitLines) {
      lines.push({ value: limits.limitUp, color: C.up, opacity: 0.35 },
        { value: limits.limitDown, color: C.down, opacity: 0.35 })
    }
    return { dates: sessions.map(s => s.date), priceLines: lines, precision,
      axisTicks: scale ? [
        ...scale.ticks.map(t => ({ value: t.price, text: t.price.toFixed(precision), color: tickColor(t.percent), priority: t.percent === 0 ? 3 : 0 })),
        ...(limits ? [{ value: limits.limitUp, text: limits.limitUp.toFixed(precision), color: C.up, priority: 2 },
          { value: limits.limitDown, text: limits.limitDown.toFixed(precision), color: C.down, priority: 2 }] : []),
      ] : undefined,
      gridTicks: scale?.ticks.filter(t => t.percent !== 0).map(t => t.price),
      verticals: sessions.length > 1 ? sessions.slice(1).map(s => ({ time: minuteTime(`${s.date} 09:30`), label: s.date.slice(5), color: '#71717A' })) : [] }
  }, [priceLines, reference, limits, sessions, precision, scale, showLimitLines])
  const paneTitles = useMemo(() => ['', `成交量  ${fmtBigNum(shown?.volume)}`], [shown?.volume])

  return <div className="w-full min-w-0" data-testid="lightweight-intraday">
    <div className={`flex min-h-10 flex-wrap items-center justify-between gap-2 border-b border-border/50 bg-elevated/20 px-3 py-1.5 xl:h-10 xl:flex-nowrap xl:py-0 ${showLimitLines ? 'pr-10' : ''}`}>
      <span className="shrink-0 text-xs font-semibold text-foreground">{supplied && sessions.length > 1 ? '多日分时' : '分时走势'}
        <span className="ml-2 font-mono text-[10px] text-muted">{supplied ? `${sessions.length} 个交易日` : date}</span>
      </span>
      {sessions.length === 1 && scale && !reference && <span className="text-[10px] text-muted">昨收缺失 · 仅价格轴</span>}
      {showLimitLines && assetType === 'index' && <span className="text-[10px] text-muted" title="按分时最高、最低点自动适配">自适应</span>}
      {showLimitLines && assetType !== 'index' && <div className="inline-flex rounded border border-border/70 p-0.5" role="group" aria-label="分时纵轴范围">
        {(['adaptive', 'limit'] as const).map(option => <button key={option} type="button" aria-pressed={mode === option}
          disabled={option === 'limit' && !limits} title={option === 'limit' && !limits ? '当日无有效涨跌停价格' : undefined}
          onClick={() => setYMode(option)} className={`rounded px-2 py-0.5 text-[10px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:cursor-not-allowed disabled:opacity-40 ${mode === option ? 'bg-accent/15 text-accent' : 'text-muted hover:text-foreground'}`}>
          {option === 'adaptive' ? '自适应' : '涨跌停'}
        </button>)}
      </div>}
    </div>
    <div className="min-h-16 border-b border-border/50 px-3 py-2 font-mono text-[11px] tabular-nums xl:h-16">
      <div className="flex min-h-5 flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-medium text-foreground">{sourceTimes
          ? `${point?.date} ${sourceTimes.join(' / ')}`
          : hovered || supplied ? point?.row.datetime.slice(0, 16).replace('T', ' ') : point?.date ?? '—'}</span>
        <span className="text-muted">{sourceTimes ? '午间衔接' : hovered || supplied ? '分钟' : daily ? '日K（前复权）' : '分时汇总'}</span>
        {([['开', ohlc?.open], ['高', ohlc?.high], ['低', ohlc?.low], ['收', ohlc?.close]] as const).map(([label, value]) => <span key={label} className="text-muted">{label} <span className="text-secondary">{fmt(value)}</span></span>)}
      </div>
      <div className="mt-1 flex min-h-4 flex-wrap items-center gap-x-4 gap-y-1 text-[10px]">
        <span className="text-muted">分时价 <span style={{ color: change == null || change === 0 ? C.flat : change > 0 ? C.up : C.down }}>{fmt(shown?.close)}{change != null && ` ${fmtPct(change)}`}</span></span>
        {showAvgLine && assetType !== 'index' && <span style={{ color: C.average }}>均价 {fmt(point?.average)}</span>}
        <span className="text-muted">量 <span className="text-secondary">{fmtBigNum(shown?.volume)}</span></span>
        <span className="text-muted">额 <span className="text-secondary">{fmtBigNum(shown?.amount)}</span></span>
        {supplied && <span className="ml-auto text-muted">{[...model.points.values()].length} 分钟</span>}
      </div>
    </div>
    <FinancialPlot scope={scope} series={plotSeries} height={plotHeight} paneHeights={paneHeights}
      visibleBars="all" intraday annotations={annotations} paneTitles={paneTitles} pricePrecision={precision}
      currentPrice={currentPrice ?? model.latest?.row.close} onPriceDoubleClick={onPriceDoubleClick}
      onHover={time => { setHover(time ? { scope, time } : null); onPriceHover?.(time ? model.points.get(time)?.row.close ?? null : null) }} />
  </div>
}
