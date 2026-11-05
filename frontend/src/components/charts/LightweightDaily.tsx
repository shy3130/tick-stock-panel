import { useMemo, useState } from 'react'
import type { SeriesMarker, Time } from 'lightweight-charts'
import type { ChartMarker, DailyChartProps, OHLC } from '@/lib/chart-types'
import { SUB_CHART_META, volMaN, volumeRatioAt } from '@/lib/chart-indicators'
import { CHART_COLORS as C, lineData, validDailyRows, type PlotSeries } from '@/lib/lightweight-model'
import { fmtVolume, fmtPct, pricePrecision } from '@/lib/format'
import { CHART_LAYOUT, plotPaneHeights } from '@/lib/chart-layout'
import { useTheme, type Theme } from '@/lib/theme'
import { FinancialPlot } from './FinancialPlot'
import type { AnnotationData } from './ChartAnnotations'

const fields = {
  macd: [['macd_dif', 'DIF', 'yellow'], ['macd_dea', 'DEA', 'purple']],
  rsi: [['rsi_6', 'RSI6', 'yellow'], ['rsi_14', 'RSI14', 'blue'], ['rsi_24', 'RSI24', 'purple']],
  kdj: [['kdj_k', 'K', 'yellow'], ['kdj_d', 'D', 'blue'], ['kdj_j', 'J', 'purple']],
} as const
const LINE_COLORS = {
  yellow: { dark: '#FACC15', light: '#A16207' },
  blue: { dark: '#3B82F6', light: '#2563EB' },
  purple: { dark: '#8B5CF6', light: '#7C3AED' },
  gray: { dark: '#A1A1AA', light: '#71717A' },
  orange: { dark: '#F97316', light: '#C2410C' },
  pink: { dark: '#D58BDD', light: '#A21CAF' },
  cyan: { dark: '#22D3EE', light: '#0E7490' },
  up: { dark: C.up, light: C.up },
  down: { dark: C.down, light: '#15803D' },
} as const
const maFields = [['ma5', 'gray'], ['ma10', 'blue'], ['ma20', 'orange'], ['ma60', 'purple']] as const
const bollFields = [['boll_upper', '上轨', 'pink'], ['ma20', '中轨', 'orange'], ['boll_lower', '下轨', 'cyan']] as const

const price = (v: number | null | undefined, digits = 2) => v != null && Number.isFinite(v)
  ? (Number(v.toFixed(digits)) === 0 ? 0 : v).toFixed(digits) : '—'
const volume = (v: number | null | undefined) => v != null && Number.isFinite(v) && v >= 0 ? fmtVolume(v) : '—'
const EVENT_COLORS = {
  limit_up: { body: { dark: '#FACC15', light: '#CA8A04' }, label: { dark: '#FACC15', light: '#A16207' } },
  limit_down: { body: { dark: '#22D3EE', light: '#0891B2' }, label: { dark: '#67E8F9', light: '#0E7490' } },
  limit_down_recovery: { body: { dark: '#60A5FA', light: '#2563EB' }, bullBody: { dark: '#2DD4BF', light: '#0D9488' },
    label: { dark: '#93C5FD', light: '#1D4ED8' }, bullLabel: { dark: '#5EEAD4', light: '#0F766E' } },
  broken_limit_up: { body: { dark: '#8B5CF6', light: '#7C3AED' }, bullBody: { dark: '#E879F9', light: '#C026D3' },
    label: { dark: '#A78BFA', light: '#7C3AED' } },
} as const
type EventMarker = ChartMarker & { event: NonNullable<ChartMarker['event']> }
const isEvent = (marker: ChartMarker): marker is EventMarker => marker.event === 'limit_up' || marker.event === 'broken_limit_up' || marker.event === 'limit_down' || marker.event === 'limit_down_recovery'
const EVENT_PRIORITY = { limit_down: 3, broken_limit_up: 2, limit_up: 1, limit_down_recovery: 0 } as const
const eventBodyColor = (event: EventMarker, row: OHLC, theme: Theme) => (event.event === 'broken_limit_up' || event.event === 'limit_down_recovery') && row.close > row.open
  ? EVENT_COLORS[event.event].bullBody[theme] : EVENT_COLORS[event.event].body[theme]
const eventTextColor = (event: EventMarker, row: OHLC, theme: Theme) => event.event === 'limit_down_recovery'
  ? EVENT_COLORS.limit_down_recovery[row.close > row.open ? 'bullLabel' : 'label'][theme]
  : event.event === 'limit_down' ? EVENT_COLORS.limit_down.label[theme] : eventBodyColor(event, row, theme)

export function LightweightDaily({ data, assetType, symbol = '', height = 480, activeIndicators = [], showMA = true, showInfoBar = true,
  markers = [], ranges, priceLines, showMarkers = true, markerStyle = 'label', volumeCompare = { enabled: true, days: 1 }, addedDate,
  linkedPrice, onDateClick, onPriceDoubleClick, visibleBars, stockInfo }: DailyChartProps) {
  const rows = useMemo(() => validDailyRows(data), [data])
  const precision = pricePrecision(assetType)
  const theme = useTheme()
  const color = (key: keyof typeof LINE_COLORS) => LINE_COLORS[key][theme]
  const volumeMAs = useMemo(() => ({ 5: volMaN(rows, 5), 10: volMaN(rows, 10) }), [rows])
  const scope = `${symbol}:${rows[0]?.date ?? ''}`
  const [hover, setHover] = useState<{ scope: string; time: string } | null>(null)
  const index = hover?.scope === scope ? rows.findIndex(d => d.date === hover.time) : -1
  const i = index >= 0 ? index : rows.length - 1
  const current = rows[i], previous = rows[i - 1]
  const events = useMemo(() => {
    const result = new Map<string, EventMarker>()
    for (const marker of markers) if (isEvent(marker)) {
      const existing = result.get(marker.date)
      if (!existing || EVENT_PRIORITY[marker.event] > EVENT_PRIORITY[existing.event]) result.set(marker.date, marker)
    }
    return result
  }, [markers])
  const currentEvent = showMarkers && markerStyle === 'candle' && current ? events.get(current.date) : undefined
  const selected = useMemo(() => activeIndicators.filter(key => SUB_CHART_META.some(d => d.key === key)), [activeIndicators])
  const model = useMemo(() => {
    const series: PlotSeries[] = [{ id: 'price', type: 'Candlestick', pane: 0, data: rows.map(r => {
      const event = showMarkers && markerStyle === 'candle' ? events.get(r.date) : undefined
      return { time: r.date, open: r.open, high: r.high, low: r.low, close: r.close,
        ...(event ? { color: eventBodyColor(event, r, theme) } : {}),
      }
    }) }]
    const addLine = (id: string, values: ReturnType<typeof lineData>, color: string, pane = 0,
      options: Pick<PlotSeries, 'range' | 'precision' | 'priceFormat'> = {}) => {
      series.push({ id, type: 'Line', pane, data: values, color, ...options })
    }
    if (showMA) for (const [key, token] of maFields) addLine(key, lineData(rows, key), color(token))
    if (activeIndicators.includes('boll')) for (const [key, , token] of bollFields) addLine(`boll:${key}`, lineData(rows, key), color(token))
    selected.forEach((key, offset) => {
      const pane = offset + 1
      if (key === 'vol') {
        series.push({ id: 'volume', type: 'Volume', pane, priceFormat: 'volume', data: rows.map(r => ({
          time: r.date, value: r.volume ?? 0, color: r.close >= r.open ? 'rgba(199,64,64,0.55)' : 'rgba(45,155,101,0.55)',
        })) })
        for (const [n, token] of [[5, 'yellow'], [10, 'purple']] as const) {
          const ma = volumeMAs[n]
          addLine(`vol${n}`, rows.map((r, i) => ma[i] == null ? { time: r.date } : { time: r.date, value: ma[i]! }), color(token), pane, { priceFormat: 'volume' })
        }
      } else {
        const lines = fields[key as keyof typeof fields]
        for (const [field, label, token] of lines) addLine(label, lineData(rows, field), color(token), pane,
          { range: key === 'rsi' ? { min: 0, max: 100 } : undefined, precision: key === 'macd' ? 3 : 1 })
        if (key === 'macd') series.push({ id: 'macd_hist', type: 'Histogram', pane, precision: 3, data: rows.map(r => r.macd_hist == null || !Number.isFinite(r.macd_hist) ? { time: r.date } : {
          time: r.date, value: r.macd_hist, color: `${color(r.macd_hist >= 0 ? 'up' : 'down')}99`,
        }) })
      }
    })
    return series
  }, [rows, showMA, selected, activeIndicators, events, markerStyle, showMarkers, theme, volumeMAs])
  const plotHeight = Math.max(260, height - (showInfoBar ? CHART_LAYOUT.readout : 0))
  const paneHeights = useMemo(() => plotPaneHeights(plotHeight, selected.length), [plotHeight, selected.length])
  const chartMarkers = useMemo<SeriesMarker<Time>[]>(() => showMarkers ? markers
    .filter(m => rows.some(r => r.date === m.date) && !(isEvent(m) && markerStyle === 'candle'))
    .sort((a, b) => a.date.localeCompare(b.date)).map(m => {
      const appearance: Pick<SeriesMarker<Time>, 'shape' | 'size'> = isEvent(m)
        ? { shape: 'circle', size: 0 }
        : { shape: m.kind === 'neutral' ? 'circle' : m.kind === 'buy' ? 'arrowUp' : 'arrowDown' }
      return {
        time: m.date, position: m.above || (m.kind === 'sell' && m.event !== 'limit_down' && m.event !== 'limit_down_recovery') ? 'aboveBar' : 'belowBar',
        color: isEvent(m) && (!m.color || m.color === EVENT_COLORS[m.event].body.dark)
          ? EVENT_COLORS[m.event].label[theme] : m.color ?? (m.kind === 'buy' ? C.up : m.kind === 'sell' ? C.down : '#F59E0B'),
        text: m.label ?? '', ...appearance,
      }
    }) : [], [markers, showMarkers, rows, markerStyle, theme])
  const annotations = useMemo(() => {
    const addedRow = addedDate ? rows.filter(r => r.date <= addedDate).at(-1) : null
    return { dates: rows.map(r => r.date), ranges, priceLines,
      verticals: addedRow ? [{ time: addedRow.date, label: '自选', color: '#3B82F6' }] : [],
    }
  }, [rows, ranges, priceLines, addedDate])
  const volumeLabels = useMemo(() => volumeCompare.enabled ? rows.flatMap((row, idx) => {
    const ratio = volumeRatioAt(rows, idx, volumeCompare.days)
    return ratio == null ? [] : [{ time: row.date, value: row.volume ?? 0, text: `${ratio.toFixed(1)}x` }]
  }) : [], [rows, volumeCompare.enabled, volumeCompare.days])
  const paneTitles = useMemo(() => ['', ...selected.map(key => SUB_CHART_META.find(d => d.key === key)!.label)], [selected])
  const paneReadouts = useMemo(() => [[], ...selected.map(key => {
    const result: NonNullable<AnnotationData['readouts']> = []
    if (key === 'vol') {
      result.push({ text: `量 ${volume(current?.volume)}`, color: current ? color(current.close >= current.open ? 'up' : 'down') : undefined })
      for (const [n, token] of [[5, 'yellow'], [10, 'purple']] as const) result.push({ text: `VOL${n} ${volume(volumeMAs[n][i])}`, color: color(token) })
      if (volumeCompare.enabled) {
        const ratio = volumeRatioAt(rows, i, volumeCompare.days)
        result.push({ text: `量比 ${ratio == null ? '—' : `${ratio.toFixed(2)}x`}` })
      }
    } else {
      for (const [field, label, token] of fields[key as keyof typeof fields]) result.push({ text: `${label} ${price(current?.[field], key === 'macd' ? 3 : 1)}`, color: color(token) })
      if (key === 'macd') result.push({ text: `MACD ${price(current?.macd_hist, 3)}`,
        color: current?.macd_hist == null || !Number.isFinite(current.macd_hist) ? undefined : color(current.macd_hist >= 0 ? 'up' : 'down') })
    }
    return result
  })], [selected, current, rows, i, volumeCompare.days, volumeCompare.enabled, theme, volumeMAs])
  const change = previous?.close ? current.close / previous.close - 1 : null
  const turnover = stockInfo?.float_shares && current?.volume != null ? current.volume * 100 / stockInfo.float_shares : null

  return <div className="min-w-0" data-testid="lightweight-daily">
    {showInfoBar && <div className="min-h-16 border-b border-border/50 px-3 py-2 text-[11px] font-mono tabular-nums xl:h-16">
      <div className="flex min-h-5 flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-medium text-foreground">{current?.date ?? '—'}</span>
        {currentEvent && <span style={{ color: eventTextColor(currentEvent, current, theme) }} title="阴阳按收盘价与开盘价判断，影线保留原来的涨跌色">
          {currentEvent.event === 'limit_down' ? '跌停' : currentEvent.event === 'broken_limit_up' || currentEvent.event === 'limit_down_recovery'
            ? `${currentEvent.event === 'broken_limit_up' ? '炸板' : '翘板'} · ${current.close > current.open ? '收阳' : current.close < current.open ? '收阴' : '开收同价'}`
            : currentEvent.label && /^\d+$/.test(currentEvent.label) ? `${currentEvent.label}连板` : '涨停'}
        </span>}
        {([['开', current?.open], ['高', current?.high], ['低', current?.low], ['收', current?.close]] as const).map(([name, value]) =>
          <span key={name} className="text-muted">{name} <span className="text-secondary">{price(value, precision)}</span></span>)}
        {change != null && <span style={{ color: change >= 0 ? C.up : C.down }}>{fmtPct(change)}</span>}
        {turnover != null && <span className="text-muted">换手 {fmtPct(turnover)}</span>}
      </div>
      <div className="mt-1 flex min-h-4 flex-wrap gap-x-4 gap-y-1 text-[10px]">
        {showMA && maFields.map(([field, token]) => <span key={field} style={{ color: color(token) }}>{field.toUpperCase()} {price(current?.[field], precision)}</span>)}
        {activeIndicators.includes('boll') && <span className="inline-flex flex-wrap gap-x-3" aria-label="BOLL 读数">
          <span className="text-muted">BOLL</span>
          {bollFields.map(([field, label, token]) => <span key={field} style={{ color: color(token) }}>{`${label} ${price(current?.[field], precision)}`}</span>)}
        </span>}
        {index >= 0 && previous && <span className="text-secondary">至今 <span style={{ color: rows.at(-1)!.close >= previous.close ? C.up : C.down }}>{fmtPct(rows.at(-1)!.close / previous.close - 1)}</span> · 周期 {rows.length - index}</span>}
      </div>
    </div>}
    <FinancialPlot scope={scope} series={model} height={plotHeight} paneHeights={paneHeights} pricePrecision={precision}
      markers={chartMarkers} annotations={annotations} volumeLabels={volumeLabels} paneTitles={paneTitles} paneReadouts={paneReadouts}
      linkedPrice={linkedPrice} visibleBars={visibleBars} currentPrice={rows.at(-1)?.close}
      onHover={time => setHover(time ? { scope, time } : null)} onSelect={onDateClick} onPriceDoubleClick={onPriceDoubleClick} />
  </div>
}
