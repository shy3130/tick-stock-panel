// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { loadChartIndicatorLibrary, migrateVolumeCompare } from './chart-indicator-library'
import { type ChartIndicatorLibrary, type IndicatorLibraryResponse } from './custom-chart-indicators'
import { legacyChartIndicator as newChartIndicator } from './chart-indicator-fixtures'

const calls = vi.hoisted(() => ({ get: vi.fn(), save: vi.fn() }))
vi.mock('./api', async original => {
  const module = await original<typeof import('./api')>()
  return { ...module, api: { ...module.api, chartIndicatorLibrary: calls.get, saveChartIndicatorLibrary: calls.save } }
})
let initial: IndicatorLibraryResponse
beforeEach(() => {
  localStorage.clear(); calls.get.mockReset(); calls.save.mockReset()
  const volume = { ...newChartIndicator('blank'), id: 'volume', outputs: [
    { key: 'BARS', label: '量', formula: 'V', style: 'volume' as const, color: '#C74040' },
  ] }
  initial = { revision: 'old', library: { version: 1, presets_version: 1, definitions: [volume],
    views: { daily: [{ id: volume.id, parameters: { N: 5 }, visible: false }], minute: [{ id: volume.id, parameters: { N: 8 } }] } } }
  calls.get.mockImplementation(async () => initial)
  calls.save.mockImplementation(async (library: ChartIndicatorLibrary) => ({ library, revision: 'new' }))
})

it('persists a migrated period and separate display flags with revision protection before returning the shared library', async () => {
  localStorage.setItem('stock_volume_compare', JSON.stringify({ enabled: false, days: 20 }))
  const result = await loadChartIndicatorLibrary()
  expect(calls.save).toHaveBeenCalledWith(expect.objectContaining({ views: {
    daily: [{ id: 'volume', parameters: { N: 5 }, visible: false, volume_compare: { days: 20, show_labels: false, show_readout: false } }],
    minute: initial.library.views.minute,
  } }), 'old')
  expect(result.revision).toBe('new')
  expect(initial.library.views.daily[0].volume_compare).toBeUndefined()
})

it('preserves explicit indicator settings even when local legacy preferences differ', () => {
  initial.library.views.daily[0].volume_compare = { days: 7, show_labels: true, show_readout: false }
  localStorage.setItem('stock_volume_compare', JSON.stringify({ enabled: false, days: 1 }))
  expect(migrateVolumeCompare(initial.library)).toBe(initial.library)
  initial.library.definitions[0].outputs[0].style = 'line'
  initial.library.views.daily[0].volume_compare = undefined
  expect(migrateVolumeCompare(initial.library)).toBe(initial.library)
})

it('reloads after a conflict and preserves the other window’s migrated setting without overwriting it', async () => {
  const latest = JSON.parse(JSON.stringify(initial)) as IndicatorLibraryResponse
  latest.revision = 'remote'
  latest.library.views.daily[0].volume_compare = { days: 10, show_labels: false, show_readout: true }
  calls.get.mockResolvedValueOnce(initial).mockResolvedValueOnce(latest)
  calls.save.mockRejectedValueOnce(new ApiError('配置已更新', 409))
  expect(await loadChartIndicatorLibrary()).toBe(latest)
  expect(calls.save).toHaveBeenCalledOnce()
})

it('leaves the old library intact when migration cannot be saved so it can be retried safely', async () => {
  calls.save.mockRejectedValueOnce(new Error('保存失败'))
  const unsaved = await loadChartIndicatorLibrary()
  expect(unsaved.volumeCompareMigrationError).toBe('保存失败')
  expect(unsaved.library.views.daily[0].volume_compare).toEqual({ days: 1, show_labels: true, show_readout: true })
  expect(initial.library.views.daily[0].volume_compare).toBeUndefined()
  const result = await loadChartIndicatorLibrary()
  expect(result.library.views.daily[0].volume_compare).toEqual({ days: 1, show_labels: true, show_readout: true })
})
