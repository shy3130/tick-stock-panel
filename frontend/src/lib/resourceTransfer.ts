import { api, type ResourceRef, type ResourceTransferEntry } from './api'

export async function downloadResourceBundle(resources: ResourceRef[], filename: string, revision?: string): Promise<void> {
  const blob = await api.resourcesExport(resources, revision)
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export const resourceKey = (item: ResourceRef) => `${item.kind}:${item.id}`

/** 选择的是主资源, 依赖由服务端提供的关系图自动关联。 */
export function resourceClosure(entries: ResourceTransferEntry[], selected: ReadonlySet<string>): Set<string> {
  const byKey = new Map(entries.map(entry => [entry.key, entry]))
  const included = new Set<string>()
  const visit = (key: string) => {
    if (included.has(key) || !byKey.has(key)) return
    included.add(key)
    byKey.get(key)!.dependencies.forEach(visit)
  }
  selected.forEach(visit)
  return included
}

export function toggleResourceSelection(selected: ReadonlySet<string>, keys: string[]): Set<string> {
  const next = new Set(selected)
  const remove = keys.length > 0 && keys.every(key => next.has(key))
  keys.forEach(key => remove ? next.delete(key) : next.add(key))
  return next
}
