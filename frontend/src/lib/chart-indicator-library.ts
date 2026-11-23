import { api, ApiError } from './api'
import { DEFAULT_VOLUME_COMPARE, normalizeVolumeCompare } from './chart-indicators'
import { storage } from './storage'
import { hasVolumeOutput, type ChartIndicatorLibrary, type IndicatorLibraryResponse, type VolumeCompareSettings } from './custom-chart-indicators'

interface LoadedLibrary extends IndicatorLibraryResponse { volumeCompareMigrationError?: string }

export function legacyVolumeCompareSettings(): VolumeCompareSettings {
  const legacy = normalizeVolumeCompare(storage.stockVolumeCompare.get(DEFAULT_VOLUME_COMPARE))
  return { days: legacy.days, show_labels: legacy.enabled, show_readout: legacy.enabled }
}

export function migrateVolumeCompare(library: ChartIndicatorLibrary): ChartIndicatorLibrary {
  const volumeIds = new Set(library.definitions.filter(hasVolumeOutput).map(d => d.id))
  const missing = library.views.daily.some(s => volumeIds.has(s.id) && s.volume_compare == null)
  if (!missing) return library
  const settings = legacyVolumeCompareSettings()
  return { ...library, views: { ...library.views, daily: library.views.daily.map(selection =>
    volumeIds.has(selection.id) && selection.volume_compare == null ? { ...selection, volume_compare: { ...settings } } : selection) } }
}

/** 旧设置只存在本机；首次打开将其按 revision 迁入指标方案，后续以方案为准。 */
export async function loadChartIndicatorLibrary(): Promise<LoadedLibrary> {
  let current = await api.chartIndicatorLibrary()
  for (let attempt = 0; attempt < 2; attempt++) {
    const library = migrateVolumeCompare(current.library)
    if (library === current.library) return current
    try { return await api.saveChartIndicatorLibrary(library, current.revision) }
    catch (cause) {
      if (!(cause instanceof ApiError) || cause.status !== 409 || attempt > 0) {
        // 迁移写入失败仍允许编辑旧定义（例如修复此前手动写入的无效公式）。
        return { ...current, library, volumeCompareMigrationError: cause instanceof Error ? cause.message : '保存失败，请重试' }
      }
      current = await api.chartIndicatorLibrary()
    }
  }
  return current
}
