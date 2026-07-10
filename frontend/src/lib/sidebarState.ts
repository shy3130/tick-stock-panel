// 侧栏折叠 / 分组展开状态 —— 模式对齐 lib/theme.ts:
//   - localStorage 持久化 + CustomEvent 通知本页其他订阅者 + storage 事件同步跨标签页
//
// 两类独立状态:
//   1. useSidebarCollapsed(): 侧栏整体折叠成图标条(窄屏/用户主动收起)
//   2. useGroupOpen(group):   单个导航分组的展开/收起(仅侧栏未整体折叠时有意义)
//
// 分组展开状态存"已收起的分组集合"(而非"已展开的"), 这样以后 navRegistry 里
// 新增分组时, 默认就是展开的, 不需要同步更新这里的默认值列表。
import { useEffect, useState } from 'react'
import type { NavGroup } from './navRegistry'

const COLLAPSED_KEY = 'tf-sidebar-collapsed'
const COLLAPSED_EVENT = 'tf-sidebar-collapsed-change'
const CLOSED_GROUPS_KEY = 'tf-sidebar-closed-groups'
const CLOSED_GROUPS_EVENT = 'tf-sidebar-closed-groups-change'

// ===== 整体折叠(图标条) =====

export function getSidebarCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === '1'
  } catch {
    return false
  }
}

export function setSidebarCollapsed(collapsed: boolean) {
  try { localStorage.setItem(COLLAPSED_KEY, collapsed ? '1' : '0') } catch { /* ignore */ }
  window.dispatchEvent(new CustomEvent(COLLAPSED_EVENT, { detail: collapsed }))
}

export function useSidebarCollapsed(): [boolean, (v: boolean) => void] {
  const [collapsed, set] = useState<boolean>(getSidebarCollapsed)
  useEffect(() => {
    const onChange = () => set(getSidebarCollapsed())
    window.addEventListener(COLLAPSED_EVENT, onChange)
    window.addEventListener('storage', onChange)
    return () => {
      window.removeEventListener(COLLAPSED_EVENT, onChange)
      window.removeEventListener('storage', onChange)
    }
  }, [])
  return [collapsed, setSidebarCollapsed]
}

// ===== 窄屏检测(仅影响视觉折叠, 不写入用户偏好) =====

function getIsNarrowViewport(breakpointPx: number) {
  try {
    return window.matchMedia(`(max-width: ${breakpointPx - 1}px)`).matches
  } catch {
    return false
  }
}

export function useIsNarrowViewport(breakpointPx = 1024): boolean {
  const [isNarrow, setIsNarrow] = useState(() => getIsNarrowViewport(breakpointPx))

  useEffect(() => {
    let media: MediaQueryList
    try {
      media = window.matchMedia(`(max-width: ${breakpointPx - 1}px)`)
    } catch {
      setIsNarrow(false)
      return
    }

    const onChange = () => setIsNarrow(media.matches)
    onChange()
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [breakpointPx])

  return isNarrow
}

// ===== 单个分组展开/收起 =====

function getClosedGroups(): Set<NavGroup> {
  try {
    const raw = localStorage.getItem(CLOSED_GROUPS_KEY)
    if (!raw) return new Set()
    const arr = JSON.parse(raw)
    return Array.isArray(arr) ? new Set(arr) : new Set()
  } catch {
    return new Set()
  }
}

function setClosedGroups(groups: Set<NavGroup>) {
  try { localStorage.setItem(CLOSED_GROUPS_KEY, JSON.stringify([...groups])) } catch { /* ignore */ }
  window.dispatchEvent(new CustomEvent(CLOSED_GROUPS_EVENT))
}

/** 某个分组当前是否展开, 以及切换它的方法。默认展开(未记录 = 展开)。 */
export function useGroupOpen(group: NavGroup): [boolean, () => void] {
  const [closed, setClosed] = useState<Set<NavGroup>>(getClosedGroups)
  useEffect(() => {
    const onChange = () => setClosed(getClosedGroups())
    window.addEventListener(CLOSED_GROUPS_EVENT, onChange)
    window.addEventListener('storage', onChange)
    return () => {
      window.removeEventListener(CLOSED_GROUPS_EVENT, onChange)
      window.removeEventListener('storage', onChange)
    }
  }, [])

  const toggle = () => {
    const next = new Set(closed)
    if (next.has(group)) next.delete(group)
    else next.add(group)
    setClosedGroups(next)
  }

  return [!closed.has(group), toggle]
}
