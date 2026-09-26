/**
 * 看板布局持久化 hook — 后端偏好加载 + 本地改动防抖落盘。
 *
 * 返回 { items, setItems }:
 * - items: 本地有改动取本地, 否则取后端保存布局(规范化), 再退内置默认。
 * - setItems: 本地改动入口; 800ms 防抖保存到 PUT /preferences/dashboard-layout,
 *   与内置默认布局完全一致时存 null(清除), 让代码内置默认可随版本演进。
 */
import { useEffect, useMemo, useState } from 'react'
import { api } from '@/lib/api'
import { usePreferences } from '@/lib/useSharedQueries'
import { DEFAULT_LAYOUT } from './registry'
import { normalizeDashboardLayout } from './DashboardGrid'
import { toBlob, type DashboardItem } from './layout'

const SAVE_DEBOUNCE_MS = 800

export function useDashboardLayout(): {
  items: DashboardItem[]
  setItems: (next: DashboardItem[]) => void
} {
  const { data: prefs } = usePreferences()
  const saved = useMemo(
    () => normalizeDashboardLayout(prefs?.dashboard_layout ?? undefined),
    [prefs?.dashboard_layout],
  )
  const [local, setLocal] = useState<DashboardItem[] | null>(null)

  useEffect(() => {
    if (local === null) return
    const blob = toBlob(local)
    const isDefault = JSON.stringify(blob.items) === JSON.stringify(DEFAULT_LAYOUT)
    const timer = window.setTimeout(() => {
      api.saveDashboardLayout(isDefault ? null : blob)
        .catch(() => { /* 保存失败静默: 布局仍在本会话生效, 下次刷新回退上次保存 */ })
    }, SAVE_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [local])

  return { items: local ?? saved, setItems: setLocal }
}
