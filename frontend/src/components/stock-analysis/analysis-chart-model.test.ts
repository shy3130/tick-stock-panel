import { describe, expect, it } from 'vitest'
import {
  ANALYSIS_LABEL_BAND_PX,
  buildAnalysisChartModel,
  hitTestAnalysis,
  levelKey,
  type LevelType,
  type PriceLevel,
} from './analysis-chart-model'

const DATES = ['2026-10-29', '2026-10-30', '2026-11-02']
const rows = DATES.map((date, index) => ({
  date,
  open: 10 + index,
  high: 11 + index,
  low: 9 + index,
  close: 10.5 + index,
  volume: 1000 + index,
}))

function levels(patch: Partial<Record<LevelType, PriceLevel[]>> = {}): Record<LevelType, PriceLevel[]> {
  return {
    sr: [{ value: 12.4, label: '成交密集区', type: 'sr', side: 'resistance', strength: 'strong' }],
    pivot: [
      { value: 10.2, label: '枢轴位 P', type: 'pivot', side: 'neutral', rank: 0 },
      { value: 10.6, label: '压力位 R1', type: 'pivot', side: 'resistance', rank: 1 },
      { value: 11.1, label: '压力位 R2', type: 'pivot', side: 'resistance', rank: 2 },
      { value: 9.8, label: '支撑位 S1', type: 'pivot', side: 'support', rank: 1 },
    ],
    extreme: [],
    boll: [],
    keltner_s: [],
    keltner_m: [],
    keltner_l: [],
    atr_stop: [],
    gap: [],
    fib: [],
    round: [],
    ...patch,
  }
}

const series = {
  boll: { upper: [12.1, 12.2, 12.3], lower: [9.5, 9.4, 9.3], mid: [10.8, 10.8, 10.8] },
  atr: { stop_loss: [9.0, 9.1, 9.2], take_profit: [12.5, 12.6, 12.7] },
}

describe('buildAnalysisChartModel', () => {
  it('aligns channel curves to the chart rows by date and keeps missing days blank', () => {
    const model = buildAnalysisChartModel({
      rows,
      levels: levels(),
      series,
      seriesDates: ['2026-10-28', '2026-10-30', '2026-11-02'],
      activeTypes: ['boll'],
    })
    const upper = model.series.find(item => item.id === 'curve:boll_upper')!
    expect(upper.data).toEqual([
      { time: '2026-10-29' },
      { time: '2026-10-30', value: 12.2 },
      { time: '2026-11-02', value: 12.3 },
    ])
    expect(model.dateIndex.get('2026-10-30')).toBe(1)
  })

  it('keeps horizontal levels out of channel groups, filters pivot ranks and shows curve values in the right band', () => {
    const model = buildAnalysisChartModel({
      rows,
      levels: levels(),
      series,
      seriesDates: DATES,
      activeTypes: ['sr', 'pivot', 'boll', 'atr_stop'],
      pivotRank: 1,
    })
    const horizontal = model.annotations.priceLines!.filter(line => !line.noLine)
    expect(horizontal.map(line => line.value)).toEqual([12.4, 10.2, 10.6, 9.8])
    expect(horizontal[0]).toMatchObject({ label: '成交密集区 12.40', color: '#F97316', opacity: 0.7, width: 1 })
    expect(horizontal.some(line => line.value === 11.1)).toBe(false)

    const curveLabels = model.annotations.priceLines!.filter(line => line.noLine)
    expect(curveLabels.map(line => line.label)).toEqual([
      '布林上轨 12.30', '布林下轨 9.30', '布林中轨 10.80', 'ATR下轨 9.20', 'ATR上轨 12.70',
    ])

    expect(model.series.map(item => item.id)).toEqual([
      'price', 'volume', 'curve:boll_upper', 'curve:boll_lower', 'curve:boll_mid',
      'curve:atr_stop', 'curve:atr_tp',
    ])
    expect(model.series[0].type).toBe('Candlestick')
    expect(model.series[1]).toMatchObject({ type: 'Volume', pane: 1, priceFormat: 'volume' })
    expect(model.series[2].color).toBe('#F97316CC')
    expect(horizontal[0]).toMatchObject({ labelPriority: 2 })
    expect(curveLabels.every(line => line.labelPriority === 1)).toBe(true)
    expect(model.hitLines.map(line => line.key)).toEqual([
      'sr-12.40', 'pivot-10.20', 'pivot-10.60', 'pivot-9.80',
    ])
    expect(ANALYSIS_LABEL_BAND_PX).toBeGreaterThanOrEqual(120)
  })

  it('dims unrelated levels and thickens the hovered one, drawing it last', () => {
    const hoveredKey = levelKey('pivot', 10.2)
    const model = buildAnalysisChartModel({
      rows,
      levels: levels(),
      series,
      seriesDates: DATES,
      activeTypes: ['sr', 'pivot', 'boll'],
      pivotRank: 1,
      hoveredKey,
    })
    const lines = model.annotations.priceLines!.filter(line => !line.noLine)
    expect(lines.find(line => line.value === 10.2)).toMatchObject({ width: 2, opacity: 1, labelPriority: 3 })
    expect(lines.find(line => line.value === 12.4)).toMatchObject({ width: 1, opacity: 0.12 })
    expect(model.series.find(item => item.id === 'curve:boll_upper')!.color).toBe('#F973161F')
    const labels = model.annotations.priceLines!.filter(line => line.noLine)
    expect(labels.find(line => line.label === '布林上轨 12.30')!.color).toBe('#F973161F')
    expect(model.annotations.priceLines!.at(-1)).toMatchObject({ value: 10.2, width: 2, labelPriority: 3 })
    expect(lines.find(line => line.value === 12.4)).toMatchObject({ labelPriority: 2 })
    expect(model.hoverKeys.has(hoveredKey)).toBe(true)
    expect(model.hoverKeys.has('boll')).toBe(true)
  })

  it('maps reserved markers and event ranges into annotations without mutating rows', () => {
    const input = [
      rows[2],
      { date: 'not-a-date', open: 1, high: 1, low: 1, close: 1, volume: 1 },
      rows[0],
      rows[1],
    ]
    const model = buildAnalysisChartModel({
      rows: input,
      levels: levels(),
      activeTypes: ['sr'],
      markers: [{ date: '2026-10-30', label: '涨停', color: '#FACC15', above: true }],
      ranges: [{ start: '2026-10-29', end: '2026-10-30', label: '事件', color: 'rgba(0,0,0,0.1)' }],
    })
    expect(model.rows.map(row => row.date)).toEqual(DATES)
    expect(model.annotations.labels).toEqual([
      { time: '2026-10-30', value: 12, text: '涨停', color: '#FACC15' },
    ])
    expect(model.annotations.ranges).toEqual([
      { start: '2026-10-29', end: '2026-10-30', label: '事件', color: 'rgba(0,0,0,0.1)' },
    ])
    expect(rows[0].date).toBe('2026-10-29')
  })
})

describe('hitTestAnalysis', () => {
  const model = buildAnalysisChartModel({
    rows,
    levels: levels(),
    series,
    seriesDates: DATES,
    activeTypes: ['sr', 'pivot', 'boll'],
    pivotRank: 1,
  })

  it('returns the nearest level within the pixel tolerance', () => {
    expect(hitTestAnalysis(model, 12.42, 0.01, 1)).toBe('sr-12.40')
    expect(hitTestAnalysis(model, 10.2, 0.01, 1)).toBe('pivot-10.20')
    expect(hitTestAnalysis(model, 12.46, 0.01, 1, 10)).toBe('sr-12.40')
  })

  it('hits an active channel curve at the hovered date', () => {
    expect(hitTestAnalysis(model, 12.31, 0.01, 2)).toBe('boll')
    expect(hitTestAnalysis(model, 12.31, 0.01, 0)).toBeNull()
  })

  it('ignores levels that are off, out of tolerance, or reached through invalid input', () => {
    expect(hitTestAnalysis(model, 11.0, 0.01, 1)).toBeNull()
    expect(hitTestAnalysis(model, 10.2, 0, 1)).toBeNull()
    expect(hitTestAnalysis(model, 10.2, 0.01, 9)).toBeNull()
  })
})
