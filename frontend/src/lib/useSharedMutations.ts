/**
 * 共享 mutation hooks — 消除多页面重复的 useMutation 调用。
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from './api'
import { QK } from './queryKeys'

/** 切换实时行情 — Layout / Data 共用 */
export function useToggleRealtimeQuotes() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (enabled: boolean) => api.updateRealtimeQuotes(enabled),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.preferences })
      qc.invalidateQueries({ queryKey: QK.quoteStatus })
    },
  })
}

/** 更新行情轮询间隔 — Layout / Data 共用 */
export function useUpdateQuoteInterval() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (v: number) => api.updateQuoteInterval(v),
    onSuccess: (data) => {
      qc.setQueryData(QK.quoteInterval, data)
      qc.invalidateQueries({ queryKey: QK.quoteStatus })
    },
  })
}

/** 批量添加自选 — Screener / 截图导入 共用; tags 对新增标的生效, 已在自选合并 */
export function useWatchlistBatchAdd() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ symbols, tags = [] }: { symbols: string[]; tags?: string[] }) =>
      api.watchlistBatchAdd(symbols, tags),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.watchlist })
      // 前缀匹配: 实际 key 为 ['watchlist-enriched', extColumnsParam],
      // 不能用 QK.watchlistEnriched()(= undefined) 精确匹配, 否则列表不刷新。
      qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })
    },
  })
}
