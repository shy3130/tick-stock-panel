/**
 * 共享 mutation hooks — 消除多页面重复的 useMutation 调用。
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from './api'
import { QK } from './queryKeys'

/** 切换实时行情 — Watchlist / Data 共用 */
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

/** 批量添加自选 — Screener / Intraday 共用 */
export function useWatchlistBatchAdd() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (symbols: string[]) => api.watchlistBatchAdd(symbols),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.watchlist })
      qc.invalidateQueries({ queryKey: QK.watchlistEnriched() })
    },
  })
}
