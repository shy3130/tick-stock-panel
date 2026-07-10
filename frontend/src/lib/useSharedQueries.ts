/**
 * 共享 query hooks — 消除多页面重复的 useQuery 调用。
 *
 * 实时数据走 SSE invalidation，无需前端轮询。
 * 只有管线进度等非 SSE 数据才用 refetchInterval。
 */
import { useQuery } from '@tanstack/react-query'
import { api } from './api'
import { QK } from './queryKeys'

// ===== 全局共享 =====

/** 能力检测 — Layout / Data / Keys 共用 */
export function useCapabilities() {
  return useQuery({
    queryKey: QK.capabilities,
    queryFn: api.capabilities,
  })
}

/** 设置状态 — Layout / Data / Keys 共用 */
export function useSettings() {
  return useQuery({
    queryKey: QK.settings,
    queryFn: api.settings,
  })
}

/** 用户偏好 — Layout / Data / Intraday 共用 */
export function usePreferences() {
  return useQuery({
    queryKey: QK.preferences,
    queryFn: api.preferences,
  })
}

/** 行情状态 — SSE quotes_updated 自动刷新。

 * poll=true 时启用 60s 状态轮询兜底, 用于在交易时段边界
 * (11:30午休 / 13:00开盘 / 15:00收盘) 同步 quote status。
 * SSE 会在行情更新时即时刷新, 轮询负责没有 SSE 的休盘边界。
 * 只应在全局唯一挂载处 (Layout) 传 poll=true, 避免多页面重复轮询;
 * 其他调用方共享同一 queryKey 缓存, 无需自行轮询。
 */
export function useQuoteStatus(opts?: { enabled?: boolean; poll?: boolean }) {
  return useQuery({
    queryKey: QK.quoteStatus,
    queryFn: api.quoteStatus,
    enabled: opts?.enabled ?? true,
    refetchInterval: opts?.poll ? 60_000 : false,
  })
}

/** 行情间隔 — Layout / Data 共用 */
export function useQuoteInterval() {
  return useQuery({
    queryKey: QK.quoteInterval,
    queryFn: api.quoteInterval,
  })
}

/** 版本号 — Layout 专用 */
export function useVersion() {
  return useQuery({
    queryKey: QK.version,
    queryFn: api.version,
    staleTime: Infinity,
  })
}

/** 数据状态 — Data / Screener 共用 */
export function useDataStatus(opts?: {
  staleTime?: number
  refetchInterval?: number | false | ((query: any) => number | false | undefined)
}) {
  return useQuery({
    queryKey: QK.dataStatus,
    queryFn: api.dataStatus,
    staleTime: opts?.staleTime,
    refetchInterval: opts?.refetchInterval,
  })
}
