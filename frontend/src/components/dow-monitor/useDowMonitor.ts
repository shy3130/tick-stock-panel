import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

import type { DowMonitorMarket, DowTimeframe } from './types'

const POLL_INTERVAL_MS = 15_000
const DOW_MONITOR_OVERVIEW_KEY = ['dow-monitor', 'overview'] as const
const DOW_MONITOR_NOTIFICATIONS_KEY = ['dow-monitor', 'notifications'] as const
const DOW_MONITOR_SYMBOLS_KEY = ['dow-monitor', 'symbols'] as const

export function useDowMonitorSymbols() {
  return useQuery({
    queryKey: QK.dowMonitorSymbols,
    queryFn: () => api.dowMonitorSymbols(),
    placeholderData: keepPreviousData,
  })
}

export function useDowMonitorOverview(
  market: DowMonitorMarket,
  realtimeActive = false,
) {
  void realtimeActive
  return useQuery({
    queryKey: QK.dowMonitorOverview(market),
    queryFn: () => api.dowMonitorOverview(market),
    refetchInterval: POLL_INTERVAL_MS,
    placeholderData: keepPreviousData,
  })
}

export function useDowNotifications(market: DowMonitorMarket) {
  return useQuery({
    queryKey: QK.dowMonitorNotifications(market),
    queryFn: () => api.dowMonitorNotifications(market),
    refetchInterval: POLL_INTERVAL_MS,
    placeholderData: keepPreviousData,
  })
}

export function useDowMonitorStatus() {
  return useQuery({
    queryKey: QK.dowMonitorStatus,
    queryFn: () => api.dowMonitorStatus(),
    refetchInterval: POLL_INTERVAL_MS,
    placeholderData: keepPreviousData,
  })
}

export function useDowMonitorDetail(
  symbol: string,
  timeframe: DowTimeframe,
  enabled = true,
) {
  return useQuery({
    queryKey: QK.dowMonitorDetail(symbol, timeframe),
    queryFn: () => api.dowMonitorDetail(symbol, timeframe),
    enabled: enabled && !!symbol,
    refetchInterval: POLL_INTERVAL_MS,
    placeholderData: keepPreviousData,
  })
}

export function useDowMonitorAiHistory(
  symbol: string,
  tradeDate: string,
  enabled: boolean,
) {
  return useQuery({
    queryKey: QK.dowMonitorAiHistory(symbol, tradeDate),
    queryFn: () => api.dowMonitorAiHistory(symbol, tradeDate),
    enabled: enabled && !!symbol && !!tradeDate,
  })
}

export function useDowMonitorAiDetail(
  symbol: string,
  analysisId: string,
  enabled: boolean,
) {
  return useQuery({
    queryKey: QK.dowMonitorAiDetail(symbol, analysisId),
    queryFn: () => api.dowMonitorAiDetail(symbol, analysisId),
    enabled: enabled && !!symbol && !!analysisId,
  })
}

export function useDowMonitorAiRerunStatus(
  symbol: string,
  analysisId: string,
  enabled: boolean,
) {
  return useQuery({
    queryKey: QK.dowMonitorAiRerun(symbol, analysisId),
    queryFn: () => api.dowMonitorAiRerunStatus(symbol, analysisId),
    enabled: enabled && !!symbol && !!analysisId,
    refetchInterval: query => {
      const status = query.state.data?.request?.status
      return status === 'queued' || status === 'running' ? 2_000 : false
    },
  })
}

export function useRerunDowMonitorAi() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ symbol, analysisId }: { symbol: string; analysisId: string }) =>
      api.rerunDowMonitorAi(symbol, analysisId),
    onSuccess: (data, { symbol, analysisId }) => {
      queryClient.setQueryData(
        QK.dowMonitorAiRerun(symbol, analysisId),
        { request: data.request },
      )
    },
  })
}

export function useAddDowMonitorSymbol() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ symbol, enabled = true }: { symbol: string; enabled?: boolean }) =>
      api.addDowMonitorSymbol(symbol, enabled),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: DOW_MONITOR_SYMBOLS_KEY })
      return queryClient.invalidateQueries({ queryKey: DOW_MONITOR_OVERVIEW_KEY })
    },
  })
}

export function useRemoveDowMonitorSymbol() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (symbol: string) => api.removeDowMonitorSymbol(symbol),
    onSuccess: (_, symbol) => {
      queryClient.removeQueries({ queryKey: ['dow-monitor', 'detail', symbol] })
      void queryClient.invalidateQueries({ queryKey: DOW_MONITOR_SYMBOLS_KEY })
      return queryClient.invalidateQueries({ queryKey: DOW_MONITOR_OVERVIEW_KEY })
    },
  })
}

export function useSetDowMonitorEnabled() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ symbol, enabled }: { symbol: string; enabled: boolean }) =>
      api.setDowMonitorEnabled(symbol, enabled),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: DOW_MONITOR_SYMBOLS_KEY })
      return queryClient.invalidateQueries({ queryKey: DOW_MONITOR_OVERVIEW_KEY })
    },
  })
}

export function useMarkDowNotificationRead() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (notificationId: string) => api.markDowNotificationRead(notificationId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: DOW_MONITOR_NOTIFICATIONS_KEY })
      return queryClient.invalidateQueries({ queryKey: DOW_MONITOR_OVERVIEW_KEY })
    },
  })
}
