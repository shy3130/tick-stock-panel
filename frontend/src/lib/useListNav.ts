import { useEffect, useMemo, useRef, useState } from 'react'
import { uniqueBy, wrapNavIndex } from '@/lib/listNav'

/** 首↔尾循环的弱提示展示时长 */
const WRAP_HINT_MS = 1500

export interface UseListNavOptions<T> {
  /** 有序候选列表 (可含重复 key, 内部会按 keyOf 去重) */
  items: T[]
  /** 从列表项取稳定 key; 需稳定引用 (模块级函数或 useCallback), 否则每次渲染都会重算 */
  keyOf: (item: T) => string
  /** 当前项 key; null 或不在列表中时导航不可用 */
  currentKey: string | null
  onNavigate: (item: T) => void
  /** 追加的方向键禁用条件 (如弹窗内点位监控/规则编辑器打开时) */
  blocked?: () => boolean
  /** 首↔尾循环时的弱提示文案 */
  wrapHints?: { head: string; tail: string }
}

export interface ListNav<T> {
  /** 当前项在去重序列中的位置 (-1 = 不在序列中) */
  navIdx: number
  navTotal: number
  navEnabled: boolean
  /** 前后切项; 返回是否真正导航 (供调用方判断是否 preventDefault) */
  go: (delta: 1 | -1) => boolean
  /** 首↔尾循环的弱提示, 自显 ~1.5s 后自动消失 */
  wrapMsg: string | null
  /** 当前项左右邻居 (首↔尾循环), 供调用方做邻项预取 */
  neighbors: T[]
}

/**
 * 列表内前后切换 (上一只股票 / 上一笔交易) 的共享逻辑:
 * 去重后的位置计数、首↔尾循环、方向键监听、循环弱提示、邻项预取。
 *
 * 只接管左右方向键; ESC 等其余按键由调用方自理。
 * 焦点在输入框/编辑器时方向键让位给光标, 不切项。
 */
export function useListNav<T>({
  items,
  keyOf,
  currentKey,
  onNavigate,
  blocked,
  wrapHints,
}: UseListNavOptions<T>): ListNav<T> {
  const navItems = useMemo(() => uniqueBy(items, keyOf), [items, keyOf])
  const navIdx = currentKey == null ? -1 : navItems.findIndex(n => keyOf(n) === currentKey)
  const navTotal = navItems.length
  const navEnabled = navTotal >= 2 && navIdx >= 0

  const [wrapMsg, setWrapMsg] = useState<string | null>(null)
  const wrapTimer = useRef<number | null>(null)
  useEffect(() => {
    return () => { if (wrapTimer.current) window.clearTimeout(wrapTimer.current) }
  }, [])

  const go = (delta: 1 | -1): boolean => {
    if (!navEnabled) return false
    const nextIdx = wrapNavIndex(navIdx, delta, navTotal)
    // 提示词描述切项后的落点 (而非起点)
    const hint = delta === 1 ? wrapHints?.head : wrapHints?.tail
    if (hint && nextIdx === (delta === 1 ? 0 : navTotal - 1)) {
      setWrapMsg(hint)
      if (wrapTimer.current) window.clearTimeout(wrapTimer.current)
      wrapTimer.current = window.setTimeout(() => setWrapMsg(null), WRAP_HINT_MS)
    }
    onNavigate(navItems[nextIdx])
    return true
  }

  const neighbors = useMemo(() => {
    if (!navEnabled) return []
    return [
      navItems[wrapNavIndex(navIdx, -1, navTotal)],
      navItems[wrapNavIndex(navIdx, 1, navTotal)],
    ]
  }, [navEnabled, navIdx, navTotal, navItems])

  // 键盘监听只注册一次, 经 ref 取本次渲染的 go/blocked: 调用方的 onNavigate/blocked 多为内联 lambda,
  // 直接进依赖会让监听每次父渲染都重建。
  const goRef = useRef(go)
  goRef.current = go
  const blockedRef = useRef(blocked)
  blockedRef.current = blocked

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return
      // 上层弹窗/编辑器打开时方向键不切项
      if (blockedRef.current?.()) return
      // 焦点在输入框/编辑器时方向键让位给光标/输入, 不切项
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return
      if (goRef.current(e.key === 'ArrowRight' ? 1 : -1)) {
        e.preventDefault()
        // 切项后 blur 掉残留的键盘焦点: 点过分时tab/外链等控件后方向键切项, 浏览器会给该控件
        // 显示 focus-visible 默认蓝色 outline。keydown 的 e.target 即聚焦元素 (已排除输入框/编辑器)。
        t?.blur()
      }
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [])

  return { navIdx, navTotal, navEnabled, go, wrapMsg, neighbors }
}
