import { expect, it } from 'vitest'
import { copyChartIndicator, indicatorBars, indicatorPlotSeries, indicatorReadouts, indicatorSourceIdentity, resolveChartIndicators, type IndicatorEvaluation } from './custom-chart-indicators'
import { legacyChartIndicator as newChartIndicator } from './chart-indicator-fixtures'

it('keeps day/minute parameter choices separate and copies definitions without sharing state', () => {
  const definition = newChartIndicator()
  const library = { version: 1 as const, definitions: [definition], views: {
    daily: [{ id: definition.id, parameters: { N: 5 } }], minute: [{ id: definition.id, parameters: { N: 30 } }],
  } }
  expect(resolveChartIndicators(library, 'daily')[0].parameters[0].value).toBe(5)
  expect(resolveChartIndicators(library, 'minute')[0].parameters[0].value).toBe(30)
  expect(definition.parameters[0].value).toBe(20)
  const copy = copyChartIndicator(definition)
  expect(copy.id).not.toBe(definition.id)
  copy.outputs[0].formula = 'C'
  expect(definition.outputs[0].formula).toBe('MA(C,N)')
})

it('normalizes original bars without collapsing lunch endpoints or fabricating missing amounts', () => {
  const rows = [
    { datetime: '2026-01-01 11:30:00', close: 10 },
    { datetime: '2026-01-01 13:00:00', close: 20 },
    { datetime: '2026-01-01 09:25:00', close: 99 },
  ]
  const bars = indicatorBars(rows, 'minute')
  expect(bars.map(r => r.close)).toEqual([10, 20])
  expect(bars[0].amount).toBeNull()
  expect(rows).toHaveLength(3)
})

it('draws equal lunch x coordinates as separate series, keeps panes and null breaks', () => {
  const definition = newChartIndicator('macd')
  const evaluation: IndicatorEvaluation = { times: ['2026-01-01 11:29:00', '2026-01-01 11:30:00', '2026-01-01 13:00:00', '2026-01-01 13:01:00'],
    results: [{ ...definition, outputs: [{ ...definition.outputs[0], values: [1, 2, 3, null] }] }],
    errors: [], warnings: [], cache_hit: false, elapsed_ms: 1 }
  const series = indicatorPlotSeries(evaluation, 'minute', 2)
  expect(series).toHaveLength(2)
  expect(series.every(s => s.pane === 2)).toBe(true)
  expect(series[0].data).toHaveLength(2)
  expect(series[1].data).toHaveLength(1)
  expect(series[0].data.at(-1)!.time).toBe(series[1].data[0].time)
  const main = { ...evaluation, results: evaluation.results.map(r => ({ ...r, pane: 'main' as const })) }
  expect(indicatorPlotSeries(main, 'minute', 2)[0].pane).toBe(0)
})

it('invalidates changed earlier bars even when the last bar is identical', () => {
  const first = [{ close: 10 }, { close: 20 }]
  const corrected = [{ close: 11 }, first[1]]
  expect(indicatorSourceIdentity(first)).toBe(indicatorSourceIdentity(first))
  expect(indicatorSourceIdentity(corrected)).not.toBe(indicatorSourceIdentity(first))
})

it('copies server presets and draws candle-coloured volume without intermediate variables', () => {
  const preset = newChartIndicator('macd')
  preset.id = 'preset_volume'; preset.name = '成交量'; preset.format = 'volume'
  preset.outputs = [
    { key: 'BASE', label: '中间值', formula: 'V', style: 'line', color: '#abcdef', hidden: true },
    { key: 'BARS', label: '成交量', formula: 'BASE', style: 'volume', color: '#C74040', color_mode: 'candle' },
  ]
  const created = newChartIndicator('volume', [preset])
  expect(created.id).not.toBe(preset.id)
  created.name = '编辑副本'
  expect(preset.name).toBe('成交量')
  const evaluation: IndicatorEvaluation = { times: ['2026-01-01', '2026-01-02'], results: [{ ...created,
    outputs: created.outputs.map(o => ({ ...o, values: [10, 20], colors: ['#C74040', '#2D9B65'] })),
  }], errors: [], warnings: [], cache_hit: false, elapsed_ms: 0 }
  const series = indicatorPlotSeries(evaluation, 'daily')
  expect(series).toHaveLength(1)
  expect(series[0].type).toBe('Volume')
  expect(series[0].data).toEqual([{ time: '2026-01-01', value: 10, color: '#C74040' }, { time: '2026-01-02', value: 20, color: '#2D9B65' }])
  expect(indicatorReadouts(evaluation)[0]).toHaveLength(1)
})

it('creates only a blank source definition and preserves source verbatim when copied', async () => {
  const { newChartIndicator: createSource } = await import('./custom-chart-indicators')
  const d = createSource()
  expect(d).toMatchObject({ syntax: 'tdx', source: '', outputs: [], parameters: [] })
  d.source = '{源码}\n中文:MA(C,5),NODRAW;'
  expect(copyChartIndicator(d).source).toBe(d.source)
})

it('keeps NODRAW readouts, plots drawing extents on the native scale and never turns them into visible price lines', async () => {
  const { indicatorPaneDrawings } = await import('./custom-chart-indicators')
  const result: IndicatorEvaluation = { times: ['2026-01-01', '2026-01-02'], results: [{ ...newChartIndicator('blank'),
    outputs: [{ key: 'N', label: '读数', style: 'line', color: '#FF0000', nodraw: true, values: [10, 11] }],
    drawings: [{ kind: 'stick', color: '#FF0000', width: 2, points: [{ time: '2026-01-01', value: 15, end_value: 5 }] }],
  }], errors: [], warnings: [], cache_hit: false, elapsed_ms: 0 }
  const series = indicatorPlotSeries(result, 'daily', 2)
  expect(series).toHaveLength(2)
  expect(series.every(line => line.pane === 2 && line.lineVisible === false)).toBe(true)
  expect(series.map(line => line.data[0])).toEqual([{ time: '2026-01-01', value: 5 }, { time: '2026-01-01', value: 15 }])
  expect(indicatorReadouts(result)[0][0].text).toBe('读数 11.00')
  expect(indicatorPaneDrawings(result, 'daily', 2)[2][0].points[0]).toMatchObject({ value: 15, end_value: 5 })
})
