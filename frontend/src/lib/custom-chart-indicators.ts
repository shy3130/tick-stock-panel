import type { Time } from 'lightweight-charts'
import type { PlotData, PlotSeries } from './lightweight-model'
import { minuteTime } from './lightweight-model'
import { FULL_DAY_TIMES } from './intraday-chart'

export type IndicatorView = 'daily' | 'minute'
export interface IndicatorParameter { name: string; value: number; min: number; max: number; integer: boolean }
export interface IndicatorOutput {
  key: string; label: string; formula: string; style: 'line' | 'histogram' | 'volume' | 'line_stick'; color: string
  hidden?: boolean; color_mode?: 'fixed' | 'sign' | 'candle'; down_color?: string
  nodraw?: boolean; line_width?: number; line_style?: 'solid' | 'dotted' | 'points' | 'circles' | 'crosses'
}
export interface ChartIndicatorDefinition {
  id: string; name: string; pane: 'main' | 'sub'; format: 'price' | 'number' | 'percent' | 'volume'
  precision: number; reset_daily: boolean; parameters: IndicatorParameter[]; outputs: IndicatorOutput[]
  syntax?: 'expression' | 'tdx'; source?: string
}
export interface IndicatorDrawingPoint { time: string; value: number; end_value?: number; open?: number; close?: number; text?: string; icon?: number; color?: string; break_before?: boolean }
export interface IndicatorDrawing {
  kind: 'stick' | 'text' | 'icon' | 'band' | 'candle' | 'fixed_text' | 'line' | 'crosses' | 'circles'
  points: IndicatorDrawingPoint[]; color: string; line_width?: number; dotted?: boolean; above?: boolean; width?: number; empty?: number; x?: number; y?: number; align?: number
}
export interface PlotDrawing extends Omit<IndicatorDrawing, 'points'> { points: (Omit<IndicatorDrawingPoint, 'time'> & { time: Time })[] }
export interface VolumeCompareSettings { days: number; show_labels: boolean; show_readout: boolean }
export interface IndicatorSelection { id: string; parameters: Record<string, number>; visible?: boolean; volume_compare?: VolumeCompareSettings | null }

export function hasVolumeOutput(definition: ChartIndicatorDefinition | IndicatorResult) {
  return definition.pane === 'sub' && (definition.outputs.some(output => output.style === 'volume' && !output.hidden && !output.nodraw)
    || (definition.syntax === 'tdx' && /,\s*VOLSTICK\b/i.test(definition.source ?? '')))
}
export interface ChartIndicatorLibrary {
  version: 1; presets_version?: 1 | null; definitions: ChartIndicatorDefinition[]; views: Record<IndicatorView, IndicatorSelection[]>
}
export interface IndicatorLibraryResponse { library: ChartIndicatorLibrary; revision: string; presets?: ChartIndicatorDefinition[] }
export interface IndicatorPackage { format: 'tick-chart-indicators'; version: 1; definitions: ChartIndicatorDefinition[] }
export interface IndicatorImportPreview { package: IndicatorPackage; revision: string; entries: { id: string; name: string; action: 'create' | 'reuse' | 'conflict'; local?: ChartIndicatorDefinition | null }[] }
export const MAX_ACTIVE_INDICATORS = 10
export const MAX_INDICATOR_DEFINITIONS = 48
export interface IndicatorBar {
  time: string; close: number; open?: number | null; high?: number | null; low?: number | null
  volume?: number | null; amount?: number | null; turnover_rate?: number | null; raw_close?: number | null; prev_close?: number | null
}
export interface IndicatorResult extends Omit<ChartIndicatorDefinition, 'parameters' | 'outputs'> {
  outputs: (Omit<IndicatorOutput, 'formula'> & { values: (number | null)[]; colors?: string[] })[]
  drawings?: IndicatorDrawing[]
}
export interface IndicatorEvaluation {
  times: string[]; results: IndicatorResult[]; errors: { id: string; name: string; message: string }[]
  warnings: string[]; cache_hit: boolean; elapsed_ms: number
}
export interface IndicatorEvaluationRequest { symbol: string; timeframe: IndicatorView; rows: IndicatorBar[]; definitions: ChartIndicatorDefinition[] }

export const EMPTY_INDICATOR_LIBRARY: ChartIndicatorLibrary = { version: 1, definitions: [], views: { daily: [], minute: [] } }
const identifier = () => `ci_${globalThis.crypto?.randomUUID?.() ?? `${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`}`

export function newChartIndicator(): ChartIndicatorDefinition {
  return { id: identifier(), name: '新指标', syntax: 'tdx', source: '', pane: 'sub', format: 'number',
    precision: 2, reset_daily: false, parameters: [], outputs: [] }
}

export function copyChartIndicator(definition: ChartIndicatorDefinition): ChartIndicatorDefinition {
  return { ...JSON.parse(JSON.stringify(definition)), id: identifier(), name: `${definition.name.slice(0, 36)} 副本` }
}

export function resolveChartIndicators(library: ChartIndicatorLibrary | undefined, view: IndicatorView): ChartIndicatorDefinition[] {
  if (!library) return []
  return library.views[view].flatMap(selection => {
    const definition = library.definitions.find(d => d.id === selection.id)
    return definition ? [{ ...definition, parameters: definition.parameters.map(p => ({ ...p, value: selection.parameters[p.name] ?? p.value })) }] : []
  })
}

const TRADING_TIMES = new Set(FULL_DAY_TIMES)
const VALUE_FIELDS = ['open', 'high', 'low', 'volume', 'amount', 'turnover_rate', 'raw_close', 'prev_close'] as const
export function indicatorBars(rows: readonly object[], view: IndicatorView): IndicatorBar[] {
  const mapped = new Map<string, IndicatorBar>()
  for (const input of rows) {
    const row = input as Record<string, unknown>
    const raw = view === 'daily' ? row.date : row.datetime
    if (typeof raw !== 'string') continue
    const time = view === 'daily' ? raw.slice(0, 10) : `${raw.slice(0, 16).replace('T', ' ')}:00`
    if (view === 'minute' && !TRADING_TIMES.has(time.slice(11, 16))) continue
    if (typeof row.close !== 'number' || !Number.isFinite(row.close) || row.close <= 0) continue
    if (view === 'daily' && ['open', 'high', 'low'].some(key => typeof row[key] !== 'number' || !Number.isFinite(row[key]) || Number(row[key]) <= 0)) continue
    const bar: IndicatorBar = { time, close: row.close }
    for (const key of VALUE_FIELDS) bar[key] = typeof row[key] === 'number' && Number.isFinite(row[key]) ? row[key] as number : null
    mapped.set(time, bar)
  }
  return [...mapped.values()].sort((a, b) => a.time.localeCompare(b.time))
}

/** Query data is immutable: a repaired earlier bar gets a new source identity,
 * including when the visible tail/date are unchanged. Weak keys retain no bars. */
const sourceIdentities = new WeakMap<object, number>()
const identitySession = `${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`
let identitySequence = 0
export function indicatorSourceIdentity(source: object): string {
  if (!sourceIdentities.has(source)) sourceIdentities.set(source, ++identitySequence)
  return `${identitySession}:${sourceIdentities.get(source)}`
}

export function indicatorPlotTime(time: string, view: IndicatorView): Time {
  if (view === 'daily') return time
  return minuteTime(time.slice(11, 16) === '11:30' ? `${time.slice(0, 10)} 13:00:00` : time)
}

export function indicatorPlotSeries(evaluation: IndicatorEvaluation | undefined, view: IndicatorView, firstSubPane = 1): PlotSeries[] {
  if (!evaluation) return []
  let nextPane = firstSubPane
  return evaluation.results.flatMap(indicator => {
    const pane = indicator.pane === 'main' ? 0 : nextPane++
    const plotted = indicator.outputs.filter(out => !out.hidden && !out.nodraw).flatMap(out => {
      const segments: PlotData[][] = []
      let segment: PlotData[] = [], previousDay = ''
      const flush = () => { if (segment.length) segments.push(segment); segment = [] }
      evaluation.times.forEach((sourceTime, index) => {
        const value = out.values[index]
        if (value == null || !Number.isFinite(value)) { flush(); return }
        const time = indicatorPlotTime(sourceTime, view), day = sourceTime.slice(0, 10)
        // Lunch endpoints may share x. Separate series preserve both original
        // calculations instead of feeding duplicate times to the chart engine.
        if (segment.length && (segment.at(-1)!.time >= time || (view === 'minute' && previousDay !== day))) flush()
        const color = out.color_mode === 'candle' ? out.colors?.[index] ?? out.color
          : out.color_mode === 'fixed' ? out.color : value < 0 ? out.down_color ?? '#2D9B65' : out.color
        segment.push({ time, value, ...(out.style !== 'line' ? { color } : {}) })
        previousDay = day
      })
      flush()
      if (!segments.length && evaluation.times.length) segments.push([...new Map(evaluation.times.map(time => [indicatorPlotTime(time, view), { time: indicatorPlotTime(time, view) }])).values()])
      return segments.flatMap((data, index): PlotSeries[] => {
        const base: PlotSeries = { id: `custom:${indicator.id}:${out.key}:${index}`, pane, data,
        type: out.style === 'volume' ? 'Volume' : out.style === 'histogram' ? 'Histogram' : 'Line', color: out.color, precision: indicator.precision,
        lineWidth: Math.max(1, Math.min(4, out.line_width ?? 1)) as 1 | 2 | 3 | 4,
        lineVisible: !(out.line_style === 'points' || out.line_style === 'circles' || out.line_style === 'crosses' || (out.line_width ?? 1) > 4),
        lineStyle: out.line_style === 'dotted' ? 2 : 0, pointMarkersVisible: out.line_style === 'points',
        ...(indicator.format === 'number' ? {} : { priceFormat: indicator.format }),
        }
        return out.style === 'line_stick' ? [base, { ...base, id: `${base.id}:histogram`, type: 'Histogram' }] : [base]
      })
    })
    const anchors = new Map<Time, { min: number; max: number }>()
    for (const drawing of indicator.drawings ?? []) if (drawing.kind !== 'fixed_text') for (const point of drawing.points) {
      const time = indicatorPlotTime(point.time, view)
      const values = [point.value, point.end_value, point.open, point.close].filter((value): value is number => value != null && Number.isFinite(value))
      if (!values.length) continue
      const previous = anchors.get(time)
      anchors.set(time, { min: Math.min(previous?.min ?? Infinity, ...values), max: Math.max(previous?.max ?? -Infinity, ...values) })
    }
    if (!plotted.length && !anchors.size) {
      // Fixed text still needs a native pane, without inventing a numeric series value.
      plotted.push({ id: `custom:${indicator.id}:empty`, type: 'Line', pane, lineVisible: false, data: [...new Set(evaluation.times.map(time => indicatorPlotTime(time, view)))].map(time => ({ time })) })
    }
    if (anchors.size) for (const key of ['min', 'max'] as const) plotted.push({ id: `custom:${indicator.id}:anchor:${key}`, type: 'Line', pane,
      lineVisible: false, data: [...anchors].sort(([a], [b]) => a < b ? -1 : 1).map(([time, value]) => ({ time, value: value[key] })) })
    return plotted
  })
}

export function indicatorPaneDrawings(evaluation: IndicatorEvaluation | undefined, view: IndicatorView, firstSubPane = 1): PlotDrawing[][] {
  const panes: PlotDrawing[][] = []
  let next = firstSubPane
  for (const indicator of evaluation?.results ?? []) {
    const pane = indicator.pane === 'main' ? 0 : next++
    const drawings: PlotDrawing[] = (indicator.drawings ?? []).map(drawing => ({ ...drawing, points: drawing.points.map((point, index) => ({ ...point, time: indicatorPlotTime(point.time, view),
      break_before: point.break_before || (drawing.kind === 'band' && view === 'minute' && index > 0 && point.time.slice(0, 10) !== drawing.points[index - 1].time.slice(0, 10)),
    })) }))
    for (const output of indicator.outputs) if (!output.hidden && !output.nodraw && (output.style === 'line' || output.style === 'line_stick') && (output.line_style === 'crosses' || output.line_style === 'circles' || ((output.line_width ?? 1) > 4 && output.line_style !== 'points'))) {
      let points: PlotDrawing['points'] = []
      const flush = () => { if (points.length) drawings.push({ kind: output.line_style === 'crosses' || output.line_style === 'circles' ? output.line_style : 'line', color: output.color, line_width: output.line_width, dotted: output.line_style === 'dotted', points }); points = [] }
      evaluation!.times.forEach((time, index) => {
        const value = output.values[index], native = indicatorPlotTime(time, view)
        if (value == null || !Number.isFinite(value)) { flush(); return }
        if (points.length && (points.at(-1)!.time >= native || (view === 'minute' && time.slice(0, 10) !== evaluation!.times[index - 1]?.slice(0, 10)))) flush()
        points.push({ time: native, value })
      })
      flush()
    }
    panes[pane] = [...(panes[pane] ?? []), ...drawings]
  }
  return panes
}

export function indicatorReadouts(evaluation: IndicatorEvaluation | undefined, time?: string, pane: 'main' | 'sub' = 'sub') {
  if (!evaluation) return []
  const found = time ? evaluation.times.indexOf(time) : -1
  const index = found >= 0 ? found : evaluation.times.length - 1
  return evaluation.results.filter(r => r.pane === pane).map(indicator => indicator.outputs.filter(o => !o.hidden).map(output => {
    const value = output.values[index]
    return { text: `${output.label} ${value == null || !Number.isFinite(value) ? '—' : value.toFixed(indicator.precision)}${indicator.format === 'percent' ? '%' : ''}`, color: output.color }
  }))
}
