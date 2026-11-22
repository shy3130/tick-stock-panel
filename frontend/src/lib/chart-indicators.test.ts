import { expect, it } from 'vitest'
import { normalizeVolumeCompare, volumeRatioAt, volumeRatioLabels, volumeRatioReadout } from './chart-indicators'
import { type IndicatorResult } from './custom-chart-indicators'
import { legacyChartIndicator as newChartIndicator } from './chart-indicator-fixtures'

const rows = [100, 200, 300, 600].map((volume, index) => ({ date: `2026-11-0${index + 3}`, open: 10, high: 12, low: 9, close: 11, volume }))
const volume: IndicatorResult = { ...newChartIndicator('blank'), id: 'volume', format: 'volume',
  outputs: [{ key: 'BARS', label: '量', style: 'volume', color: '#C74040', values: [100, 200, 300, 600] }] }

it('compares only the preceding trading rows, including a one-day period and excluding the current day', () => {
  expect(volumeRatioAt(rows, 3, 1)).toBe(2)
  expect(volumeRatioAt(rows, 3, 2)).toBe(2.4)
  expect(volumeRatioAt(rows, 3, 3)).toBe(3)
  expect(volumeRatioAt(rows, 1, 2)).toBeNull()
  expect(normalizeVolumeCompare({ enabled: false, days: 22.5 })).toEqual({ enabled: false, days: 20 })
})

it('omits labels for missing data, zero denominators and invalid output anchors without fabricating a ratio', () => {
  const settings = { days: 1, show_labels: true, show_readout: true }
  const missing = rows.map((row, index) => ({ ...row, volume: index === 2 ? NaN : row.volume }))
  expect(volumeRatioReadout(missing, 3, settings)).toEqual([{ text: '量比1 —' }])
  expect(volumeRatioAt(rows.map(row => ({ ...row, volume: 0 })), 3, 2)).toBeNull()
  expect(volumeRatioLabels(rows, rows.map(row => row.date), { ...volume,
    outputs: [{ ...volume.outputs[0], values: [null, null, NaN, null] }] }, settings)).toEqual([])
  expect(volumeRatioLabels(rows, rows.map(row => row.date), volume, { ...settings, show_labels: false })).toEqual([])
  expect(volumeRatioReadout(rows, 3, { ...settings, show_readout: false })).toEqual([])
})
