import { useSyncExternalStore } from 'react'

/**
 * 监控中心未读触发记录徽标 — 全局 store + localStorage 持久化。
 *
 * 核心逻辑:
 *   - 在监控中心页面时, 每次收到新推送都同步更新 lastSeen (看到=已读)
 *   - 离开监控中心后, 新推送才计入未读
 *   - 刷新页面从 localStorage 恢复 lastSeen, 未读 = 期间新增
 */

const STORAGE_KEY = 'monitor_last_seen_total'

let currentTotal = 0
let lastSeenTotal = readSeen()
let onMonitorPage = false    // 当前是否在监控中心页面
let pendingSeen = false      // Monitor mount 请求 markSeen, 等 currentTotal 就绪
const listeners = new Set<() => void>()

function readSeen(): number {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    if (v === null) return -1  // 从未设置过 → 未初始化
    return parseInt(v, 10) || 0
  } catch {
    return -1
  }
}

function writeSeen(v: number) {
  try { localStorage.setItem(STORAGE_KEY, String(v)) } catch { /* ignore */ }
}

function syncSeen() {
  if (lastSeenTotal !== currentTotal) {
    lastSeenTotal = currentTotal
    writeSeen(currentTotal)
  }
}

function emit() {
  listeners.forEach(fn => fn())
}

function subscribe(fn: () => void) {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

function getSnapshot() {
  return Math.max(0, currentTotal - Math.max(0, lastSeenTotal))
}

/** 轮询更新最新总数 (Layout 层调用)。 */
export function setCurrentTotal(total: number): void {
  if (total < 0) return

  // 首次初始化: lastSeen < 0 (从未设置) → 把已读基线设为当前总数
  // 否则 lastSeen=0 + total=1 会被误算成"1条未读" (首次进入就显示徽标的 bug)
  if (lastSeenTotal < 0) {
    lastSeenTotal = total
    writeSeen(total)
  }
  // 总数减少 (清空) → 同步重置
  if (total < lastSeenTotal) {
    lastSeenTotal = total
    writeSeen(total)
  }

  const changed = total !== currentTotal
  currentTotal = total

  // 消费 pending markSeen
  if (pendingSeen) {
    pendingSeen = false
    syncSeen()
  }
  // ★ 在监控中心页面期间: 收到新推送立即同步 (看到=已读, 不计入未读)
  else if (onMonitorPage && changed) {
    syncSeen()
  }

  emit()
}

/** 进入监控页时调用。 */
export function markSeen(): void {
  onMonitorPage = true
  if (currentTotal > 0) {
    syncSeen()
    emit()
  } else {
    pendingSeen = true
  }
}

/** 离开监控页时调用 (停止同步, 之后新增才计入未读)。 */
export function leaveMonitorPage(): void {
  onMonitorPage = false
  pendingSeen = false
  // 不在此处 syncSeen — lastSeen 保持页面期间最后一次同步的值即可
  // (避免 currentTotal 此刻还没刷新到最新, 写入偏小的值)
}

/** 记录被清空时调用。 */
export function resetBadge(): void {
  currentTotal = 0
  lastSeenTotal = 0
  pendingSeen = false
  writeSeen(0)
  emit()
}

/** 读取当前未读数。 */
export function useUnreadAlerts(): number {
  return useSyncExternalStore(subscribe, getSnapshot, () => 0)
}
