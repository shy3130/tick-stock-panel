/**
 * SSE 配置 — 运行时参数。
 *
 * 目前只保留 SSE 重连延迟，其他数据刷新全部走 SSE invalidation。
 * 存储在 localStorage。
 */
import { storage } from '@/lib/storage'

// ===== 配置结构 =====

export interface QueryConfig {
  /** SSE 配置 */
  sse: {
    reconnectDelay: number
  }
}

export const DEFAULT_QUERY_CONFIG: QueryConfig = {
  sse: {
    reconnectDelay: 5_000,
  },
}

// ===== localStorage 持久化 =====

function loadConfig(): QueryConfig {
  const raw = storage.queryConfig.get(null) as QueryConfig | null
  if (!raw) return DEFAULT_QUERY_CONFIG
  return {
    sse: { ...DEFAULT_QUERY_CONFIG.sse, ...raw.sse },
  }
}

/**
 * 轻量版：只读取当前配置。
 * 供 useQuoteStream 使用。
 */
export function getQueryConfig(): QueryConfig {
  return loadConfig()
}
