import type { QueryClient } from '@tanstack/react-query'
import { packToExtension } from '@/extensions/runtime'
import { QK } from './queryKeys'

export const RESOURCE_INVALIDATE_KEYS = [
  QK.resourceCatalog, QK.definitions, QK.customSignals, QK.customSignalsOptions, QK.factorColumns,
  QK.factorLibrary('all').slice(0, 1), QK.screenerStrategies().slice(0, 1), QK.strategyDetail('').slice(0, 1),
  QK.screenerCached().slice(0, 1), QK.screener, QK.monitorRuleOptions, QK.watchlist, QK.strategyLinkOptions().slice(0, 1),
]

export async function refreshResourceQueries(qc: QueryClient) {
  await Promise.allSettled(RESOURCE_INVALIDATE_KEYS.map(queryKey => qc.invalidateQueries({ queryKey })))
}

export async function inspectPageModule(url: string, expectedId?: string) {
  const module = await import(/* @vite-ignore */ url)
  const extension = packToExtension(module.default)
  if (expectedId && extension.id !== expectedId) throw new Error('页面与配套后端的扩展 ID 不一致')
  return { label: extension.navigation?.[0]?.label ?? extension.id, path: extension.routes?.[0]?.path ?? '' }
}

export async function inspectPageBlob(blob: Blob, expectedId?: string) {
  const url = URL.createObjectURL(new Blob([blob], { type: 'text/javascript' }))
  try { return await inspectPageModule(url, expectedId) }
  finally { URL.revokeObjectURL(url) }
}
