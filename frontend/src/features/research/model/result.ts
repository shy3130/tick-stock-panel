import { asArray, asBoolean, asNumber, asRecord, asString } from './parse'
import { parseResultProfile, type ResultProfile } from './status'

export interface ArmRow {
  id: string
  title: string
  baseline: string | null
  samples: number | null
  oos_samples: number | null
  metrics: Record<string, number | string | null>
  verdict: string | null
  notes: string | null
}

export interface HorizonRow {
  horizon: string
  arm_id: string | null
  metrics: Record<string, number | string | null>
}

export interface RiskBlock {
  max_drawdown: number | null
  volatility: number | null
  sharpe: number | null
  sortino: number | null
  calmar: number | null
  extra: Record<string, number | null>
}

export interface ShapeBin {
  label: string
  count: number
  share: number | null
}

export interface RetrievalItem {
  id: string
  title: string
  score: number | null
  rank: number | null
  extra: Record<string, string | number | null>
}

export interface CalendarWindow {
  id: string
  title: string
  start: string | null
  end: string | null
  effect: number | null
  samples: number | null
  extra: Record<string, string | number | null>
}

export interface EventRow {
  id: string
  symbol: string | null
  date: string | null
  arm: string | null
  qualified: boolean | null
  reachable: boolean | null
  censor_code: string | null
  extra: Record<string, string | number | boolean | null>
}

export interface SeriesPoint {
  t: string
  equity: number | null
  baseline: number | null
  increment: number | null
  drawdown: number | null
}

export type NormalizedResearchResult =
  | { profile: 'arm_comparison'; arms: ArmRow[]; horizons: HorizonRow[]; risk: RiskBlock | null; summary: Record<string, unknown> }
  | { profile: 'event_signal'; preview: EventRow[]; summary: Record<string, unknown> }
  | { profile: 'shape_distribution'; bins: ShapeBin[]; summary: Record<string, unknown> }
  | { profile: 'retrieval'; items: RetrievalItem[]; summary: Record<string, unknown> }
  | { profile: 'calendar_effect'; windows: CalendarWindow[]; summary: Record<string, unknown> }

const ARM_METRIC_SKIP = [
  'id', 'arm_id', 'name', 'title', 'label', 'baseline', 'strongest_baseline',
  'samples', 'n', 'sample_count', 'oos_samples', 'oos_n', 'verdict', 'notes', 'note',
  'horizon', 'h', 'arm', 'metrics',
]

function recordsFrom(value: unknown): Record<string, unknown>[] {
  if (Array.isArray(value)) {
    return value.map((item, index) => {
      const rec = asRecord(item)
      return rec ?? { value: item, id: String(index + 1) }
    })
  }
  const rec = asRecord(value)
  if (!rec) return []
  return Object.entries(rec).map(([id, item]) => {
    const row = asRecord(item)
    if (row) return { id: asString(row.id ?? row.arm_id ?? row.name) ?? id, ...row }
    return { id, value: item }
  })
}

export function parseArmRows(value: unknown): ArmRow[] {
  return recordsFrom(value).map((rec, index) => {
    const id = asString(rec.id ?? rec.arm_id ?? rec.name) ?? `arm-${index + 1}`
    const verdictRec = asRecord(rec.verdict)
    return {
      id,
      title: asString(rec.title ?? rec.name ?? rec.label) ?? id,
      baseline: asString(rec.baseline ?? rec.strongest_baseline),
      samples: asNumber(rec.samples ?? rec.n ?? rec.sample_count),
      oos_samples: asNumber(rec.oos_samples ?? rec.oos_n),
      metrics: pickMetrics(rec),
      verdict: asString(rec.verdict) ?? asString(verdictRec?.verdict ?? verdictRec?.value),
      notes: asString(rec.notes ?? rec.note),
    }
  })
}

export function parseHorizonRows(value: unknown): HorizonRow[] {
  const rec = asRecord(value)
  if (rec && !Array.isArray(value)) {
    return Object.entries(rec).flatMap(([horizon, cell]) => {
      if (Array.isArray(cell)) {
        return parseHorizonRows(cell).map((row) => ({ ...row, horizon: row.horizon || horizon }))
      }
      const metricsRec = asRecord(cell)
      if (!metricsRec) return []
      return [{ horizon, arm_id: asString(metricsRec.arm_id), metrics: pickMetrics(metricsRec) }]
    })
  }
  return asArray(value).map((item) => {
    const row = asRecord(item)
    if (!row) return null
    return {
      horizon: String(asString(row.horizon ?? row.h ?? row.name) ?? ''),
      arm_id: asString(row.arm_id ?? row.arm),
      metrics: pickMetrics(row),
    }
  }).filter((item): item is HorizonRow => item !== null)
}

export function parseRisk(value: unknown): RiskBlock | null {
  const rec = asRecord(value)
  if (!rec) return null
  const extra: Record<string, number | null> = {}
  for (const [key, val] of Object.entries(rec)) {
    if (key === 'max_drawdown' || key === 'volatility' || key === 'sharpe' || key === 'sortino' || key === 'calmar') continue
    const num = asNumber(val)
    if (num != null) extra[key] = num
  }
  return {
    max_drawdown: asNumber(rec.max_drawdown ?? rec.mdd),
    volatility: asNumber(rec.volatility ?? rec.vol),
    sharpe: asNumber(rec.sharpe),
    sortino: asNumber(rec.sortino),
    calmar: asNumber(rec.calmar),
    extra,
  }
}

export function parseEventRows(value: unknown): EventRow[] {
  return recordsFrom(value).map((rec, index) => {
    const extra: EventRow['extra'] = {}
    const skip = new Set(['id', 'event_id', 'symbol', 'date', 'event_date', 'signal_date', 'ts', 'arm', 'arm_id', 'qualified', 'reachable', 'censor_code', 'censor', 'detail'])
    for (const [key, val] of Object.entries(rec)) {
      if (skip.has(key)) continue
      if (typeof val === 'string' || typeof val === 'number' || typeof val === 'boolean' || val == null) {
        extra[key] = val as string | number | boolean | null
      }
    }
    const detail = asRecord(rec.detail)
    if (detail) {
      for (const [key, val] of Object.entries(detail)) {
        if (extra[key] != null) continue
        if (typeof val === 'string' || typeof val === 'number' || typeof val === 'boolean' || val == null) {
          extra[key] = val as string | number | boolean | null
        }
      }
    }
    return {
      id: asString(rec.id ?? rec.event_id) ?? `event-${index + 1}`,
      symbol: asString(rec.symbol),
      date: asString(rec.date ?? rec.event_date ?? rec.signal_date ?? rec.ts),
      arm: asString(rec.arm ?? rec.arm_id),
      qualified: asBoolean(rec.qualified),
      reachable: asBoolean(rec.reachable),
      censor_code: asString(rec.censor_code ?? rec.censor ?? extra.code),
      extra,
    }
  })
}

export function parseSeriesPoints(payload: unknown): SeriesPoint[] {
  const rec = asRecord(payload)
  const source = rec ? rec.items ?? rec.points ?? rec.series ?? rec : payload
  const byTime = new Map<string, SeriesPoint>()
  const write = (point: SeriesPoint | null) => {
    if (!point) return
    const prev = byTime.get(point.t) ?? { t: point.t, equity: null, baseline: null, increment: null, drawdown: null }
    byTime.set(point.t, {
      t: point.t,
      equity: point.equity ?? prev.equity,
      baseline: point.baseline ?? prev.baseline,
      increment: point.increment ?? prev.increment,
      drawdown: point.drawdown ?? prev.drawdown,
    })
  }
  if (Array.isArray(source)) {
    source.forEach((item) => write(parsePoint(item)))
  } else {
    const bag = asRecord(source)
    if (bag) {
      for (const kind of ['equity', 'baseline', 'increment', 'drawdown'] as const) {
        const rows = asArray(bag[kind])
        if (rows.length === 0) continue
        rows.forEach((item) => write(parsePoint(item, kind)))
      }
      if (byTime.size === 0) {
        Object.entries(bag).forEach(([kind, rows]) => {
          asArray(rows).forEach((item) => write(parsePoint(item, kind)))
        })
      }
    }
  }
  return Array.from(byTime.values())
}

export function parseNormalizedResult(value: unknown, fallbackProfile: ResultProfile | null): NormalizedResearchResult | null {
  const rec = asRecord(value)
  if (!rec) return null
  const payload = asRecord(rec.payload) ?? rec
  const profile = parseResultProfile(rec.profile ?? rec.result_profile ?? payload.profile) ?? fallbackProfile
  if (!profile) return null
  const summary = asRecord(rec.summary) ?? asRecord(payload.summary) ?? {}
  if (profile === 'arm_comparison') {
    return {
      profile,
      arms: parseArmRows(payload.arms ?? rec.arms),
      horizons: parseHorizonRows(payload.horizons ?? rec.horizons ?? rec.horizon),
      risk: parseRisk(payload.risk ?? rec.risk),
      summary,
    }
  }
  if (profile === 'event_signal') {
    return {
      profile,
      preview: parseEventRows(payload.signals ?? payload.events ?? rec.events ?? rec.preview ?? rec.items),
      summary,
    }
  }
  if (profile === 'shape_distribution') {
    return {
      profile,
      bins: parseBins(payload.factors ?? payload.bins ?? rec.bins ?? rec.distribution ?? rec.shapes ?? payload.symbol_audit),
      summary,
    }
  }
  if (profile === 'retrieval') {
    return {
      profile,
      items: parseRetrieval(payload.routing ?? payload.items ?? rec.items ?? rec.routes ?? rec.candidates),
      summary,
    }
  }
  return {
    profile,
    windows: parseWindows(payload.legs ?? payload.windows ?? rec.windows ?? rec.effects ?? rec.calendar ?? payload.sensitivity),
    summary,
  }
}

function parseBins(value: unknown): ShapeBin[] {
  return recordsFrom(value).map((rec) => ({
    label: asString(rec.label ?? rec.shape ?? rec.name ?? rec.id) ?? '—',
    count: asNumber(rec.count ?? rec.n) ?? 0,
    share: asNumber(rec.share ?? rec.pct ?? rec.ratio),
  }))
}

function parseRetrieval(value: unknown): RetrievalItem[] {
  return recordsFrom(value).map((rec, index) => {
    const extra: RetrievalItem['extra'] = {}
    for (const [key, val] of Object.entries(rec)) {
      if (key === 'id' || key === 'title' || key === 'name' || key === 'score' || key === 'rank') continue
      extra[key] = asNumber(val) ?? asString(val)
    }
    const id = asString(rec.id ?? rec.route_id) ?? `item-${index + 1}`
    return {
      id,
      title: asString(rec.title ?? rec.name ?? rec.route) ?? id,
      score: asNumber(rec.score),
      rank: asNumber(rec.rank) ?? index + 1,
      extra,
    }
  })
}

function parseWindows(value: unknown): CalendarWindow[] {
  return recordsFrom(value).map((rec, index) => {
    const extra: CalendarWindow['extra'] = {}
    for (const [key, val] of Object.entries(rec)) {
      if (key === 'id' || key === 'title' || key === 'name' || key === 'start' || key === 'end' || key === 'effect' || key === 'samples' || key === 'n') continue
      extra[key] = asNumber(val) ?? asString(val)
    }
    const id = asString(rec.id) ?? `window-${index + 1}`
    return {
      id,
      title: asString(rec.title ?? rec.name) ?? id,
      start: asString(rec.start),
      end: asString(rec.end),
      effect: asNumber(rec.effect ?? rec.mean ?? rec.ret),
      samples: asNumber(rec.samples ?? rec.n),
      extra,
    }
  })
}

function pickMetrics(rec: Record<string, unknown>): Record<string, number | string | null> {
  const nested = asRecord(rec.metrics)
  const source = nested ?? rec
  const metrics: Record<string, number | string | null> = {}
  for (const [key, val] of Object.entries(source)) {
    if (!nested && ARM_METRIC_SKIP.includes(key)) continue
    const num = asNumber(val)
    if (num != null) metrics[key] = num
    else if (typeof val === 'string') metrics[key] = val
    else if (val == null) metrics[key] = null
  }
  return metrics
}

function parsePoint(value: unknown, kind?: string): SeriesPoint | null {
  if (Array.isArray(value) && value.length >= 2) {
    return applyKind({ t: String(value[0]), equity: asNumber(value[1]), baseline: null, increment: null, drawdown: null }, kind)
  }
  const rec = asRecord(value)
  const t = rec ? asString(rec.t ?? rec.date ?? rec.ts ?? rec.time) : null
  if (!rec || !t) return null
  const inferred = asString(rec.kind) ?? kind
  return applyKind({
    t,
    equity: asNumber(rec.equity),
    baseline: asNumber(rec.baseline),
    increment: asNumber(rec.increment ?? rec.alpha),
    drawdown: asNumber(rec.drawdown ?? rec.dd),
  }, inferred, asNumber(rec.value ?? rec.v))
}

function applyKind(point: SeriesPoint, kind?: string | null, value?: number | null): SeriesPoint {
  if (!kind) {
    if (value != null && point.equity == null) point.equity = value
    return point
  }
  if (kind === 'equity') point.equity = value ?? point.equity
  else if (kind === 'baseline') point.baseline = value ?? point.baseline
  else if (kind === 'increment') point.increment = value ?? point.increment
  else if (kind === 'drawdown') point.drawdown = value ?? point.drawdown
  else if (value != null && point.equity == null) point.equity = value
  return point
}


export function metricKeys(rows: { metrics: Record<string, number | string | null> }[]): string[] {
  const keys: string[] = []
  for (const row of rows) {
    for (const key of Object.keys(row.metrics)) {
      if (!keys.includes(key)) keys.push(key)
    }
  }
  return keys
}

export function formatMetric(value: number | string | null | undefined): string {
  if (value == null || value === '') return '—'
  if (typeof value === 'string') return value
  if (!Number.isFinite(value)) return '—'
  const abs = Math.abs(value)
  if (abs !== 0 && abs < 0.01) return value.toFixed(4)
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 4 })
}
