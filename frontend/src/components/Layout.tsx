import { Suspense, useEffect, useRef, useState } from 'react'
import { Outlet, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { Loader2 } from 'lucide-react'
import { useQuoteStream, useQuoteStreamStatus } from '@/lib/useQuoteStream'
import { ToastContainer } from '@/components/Toast'
import { AlertToastContainer } from '@/components/AlertToast'
import { AiAnalysisHost } from '@/components/financials/AiAnalysisHost'
import { AiReportBubble } from '@/components/financials/AiReportBubble'
import { StockAnalysisHost } from '@/components/stock-analysis/StockAnalysisHost'
import { StockAnalysisBubble } from '@/components/stock-analysis/StockAnalysisBubble'
import {
  useCapabilities,
  useSettings,
  usePreferences,
  useQuoteStatus,
  useVersion,
} from '@/lib/useSharedQueries'
import {
  useToggleRealtimeQuotes,
} from '@/lib/useSharedMutations'
import { QK } from '@/lib/queryKeys'
import { tierRank } from '@/lib/capability-labels'
import { api } from '@/lib/api'
import { useIsNarrowViewport, useSidebarCollapsed } from '@/lib/sidebarState'
import { setCurrentTotal as setAlertTotal } from '@/lib/monitorBadge'
import { Sidebar, CORE_INDEXES } from '@/components/layout/Sidebar'
import { TopBar } from '@/components/layout/TopBar'

/**
 * Layout —— 应用外壳: 侧栏(Sidebar) + 顶栏(TopBar) + 内容区(Outlet) + 全局浮层。
 *
 * 这里只保留"跨页面共享的业务数据查询/状态", 具体怎么画交给 Sidebar/TopBar,
 * 拆分详见 components/layout/。侧栏导航项本身的合并/排序/隐藏逻辑统一在
 * lib/navRegistry.ts, 这里不重复。
 */
export function Layout() {
  // ===== 共享 hooks (替代内联 useQuery) =====
  const { data: caps } = useCapabilities()
  const { data: settingsState } = useSettings()
  const { data: versionData } = useVersion()
  const { data: prefs } = usePreferences()
  // 数据源列表 (用于实时行情状态显示当前数据源名称)
  const { data: dataSources } = useQuery({
    queryKey: QK.dataSources,
    queryFn: api.dataSources,
    staleTime: 60_000,
  })
  // poll=true: 全局唯一开启条件轮询 (非交易时段 60s 兜底, 交易时段靠 SSE)
  const { data: quoteStatus } = useQuoteStatus({ poll: true })

  // 数据同步状态轮询: 有活跃 job 时「数据」菜单项显示转圈
  const { data: pipelineJobs } = useQuery({
    queryKey: QK.pipelineJobs,
    queryFn: () => api.pipelineJobs(1),
    refetchInterval: (query) => (query.state.data?.active_id ? 2000 : 15000),
    refetchIntervalInBackground: true,
  })
  const isDataSyncing = !!pipelineJobs?.active_id

  // 数据同步完成的"瞬时反馈": isDataSyncing 从 true→false 时显示绿色对勾,
  // 闪烁约 3 秒后自动消失。
  const [dataSyncJustDone, setDataSyncJustDone] = useState(false)
  const prevSyncingRef = useRef(false)
  useEffect(() => {
    // 仅在"刚结束"(true→false)且非首次挂载时触发
    if (prevSyncingRef.current && !isDataSyncing) {
      setDataSyncJustDone(true)
      const t = setTimeout(() => setDataSyncJustDone(false), 3000)
      prevSyncingRef.current = isDataSyncing
      return () => clearTimeout(t)
    }
    prevSyncingRef.current = isDataSyncing
  }, [isDataSyncing])

  const qc = useQueryClient()
  const navigate = useNavigate()
  const version = versionData?.version
  const realtimeEnabled = prefs?.realtime_quotes_enabled ?? false
  // Free 档监控限制提示: 可手动关闭, 不持久化 (刷新后恢复显示)
  const [dismissFreeHint, setDismissFreeHint] = useState(false)
  const indicesPinned = prefs?.indices_nav_pinned ?? true
  const sidebarIndexSymbols = prefs?.sidebar_index_symbols ?? CORE_INDEXES.map(p => p.symbol)
  const sidebarIndexes = CORE_INDEXES.filter(item => sidebarIndexSymbols.includes(item.symbol))
  // 卡片数据：固定显示时也拉取（即使实时行情关闭）
  const showSidebarQuotes = indicesPinned || realtimeEnabled
  const { data: sidebarIndexQuotes } = useQuery({
    queryKey: [...QK.indexQuotes, 'sidebar', sidebarIndexSymbols.join(',')] as const,
    queryFn: () => api.indexQuotes(sidebarIndexes.map(p => p.symbol)),
    enabled: showSidebarQuotes && sidebarIndexes.length > 0,
    placeholderData: (prev) => prev,
  })

  // SSE: 行情更新时自动刷新相关 queries + 告警通知
  useQuoteStream(realtimeEnabled, prefs?.sse_refresh_pages)
  const streamStatus = useQuoteStreamStatus()

  const toggleQuote = useToggleRealtimeQuotes()
  const isRunning = quoteStatus?.running ?? false
  const isTrading = quoteStatus?.is_trading_hours ?? false
  const isPaused = quoteStatus?.paused ?? false
  const tier = tierRank(caps?.label ?? '')
  const isNoneTier = tier < 0
  const isWatchlistMode = tier === 0
  const realtimeModeLabel = isWatchlistMode ? '自选股' : '全市场'
  // 当前实时行情数据源名称 (custom 时显示源名, tickflow 时不显示)
  const realtimeProvider = prefs?.realtime_data_provider
  const realtimeProviderName = realtimeProvider && realtimeProvider !== 'tickflow'
    ? (dataSources?.custom?.find(s => s.name === realtimeProvider)?.display_name || realtimeProvider)
    : null

  // 当前主数据源 (用于菜单底部状态条)
  const activeProvider = prefs?.daily_data_provider || 'tickflow'
  const activeProviderName = activeProvider === 'tickflow'
    ? 'TickFlow'
    : (dataSources?.custom?.find(s => s.name === activeProvider)?.display_name || activeProvider)
  const activeProviderDatasets = activeProvider === 'tickflow'
    ? ['daily', 'adj_factor', 'realtime', 'minute']
    : (dataSources?.custom?.find(s => s.name === activeProvider)?.datasets || [])
  const isCustomActive = activeProvider !== 'tickflow'

  // 轮询触发记录总数 → 更新监控中心徽标 (每 15 秒)
  const alertsTotalQuery = useQuery({
    queryKey: ['alerts-total'],
    queryFn: () => api.alertsList({ days: 7, limit: 1 }),
    refetchInterval: 15000,
    refetchIntervalInBackground: true,
    select: (data) => data.total,
  })
  // 只在拿到真实总数时同步徽标 (避免 data=undefined 时传 0 重置 lastSeen)
  const alertsTotal = alertsTotalQuery.data
  useEffect(() => {
    if (alertsTotal != null) setAlertTotal(alertsTotal)
  }, [alertsTotal])

  const [collapsed, setCollapsed] = useSidebarCollapsed()
  const isNarrow = useIsNarrowViewport()
  const effectiveCollapsed = collapsed || isNarrow

  const handleToggle = async (enabled: boolean) => {
    // 开启时重新校验档位
    if (enabled) {
      const fresh = await qc.fetchQuery({
        queryKey: QK.capabilities,
        queryFn: api.capabilities,
      })
      const freshTier = tierRank(fresh.label ?? '')
      if (freshTier < 0) return
      if (freshTier === 0 && (prefs?.realtime_watchlist_symbols?.length ?? 0) === 0) {
        navigate('/watchlist')
        return
      }
    }
    await toggleQuote.mutateAsync(enabled)
    // 仅在交易时段立即获取一次行情
    if (enabled && isTrading) {
      api.intradayRefresh().catch(() => {})
    }
  }

  return (
    <div className="h-screen flex bg-base text-foreground overflow-hidden">
      <Sidebar
        collapsed={effectiveCollapsed}
        navigate={navigate}
        version={version}
        tierLabel={caps?.label ?? ''}
        hasApiKey={settingsState?.mode !== 'none'}
        aiConfigured={settingsState?.ai_configured ?? settingsState?.has_ai_key}
        aiModel={settingsState?.ai_model}
        isDataSyncing={isDataSyncing}
        dataSyncJustDone={dataSyncJustDone}
        isNoneTier={isNoneTier}
        isWatchlistMode={isWatchlistMode}
        realtimeEnabled={realtimeEnabled}
        isRunning={isRunning}
        isTrading={isTrading}
        isPaused={isPaused}
        realtimeModeLabel={realtimeModeLabel}
        realtimeProviderName={realtimeProviderName}
        dismissFreeHint={dismissFreeHint}
        onDismissFreeHint={() => setDismissFreeHint(true)}
        onToggleRealtime={handleToggle}
        toggleRealtimePending={toggleQuote.isPending}
        activeProviderName={activeProviderName}
        activeProviderDatasets={activeProviderDatasets}
        isCustomActive={isCustomActive}
        showSidebarQuotes={showSidebarQuotes}
        sidebarIndexQuotesRows={sidebarIndexQuotes?.rows}
        sidebarIndexes={sidebarIndexes}
      />

      <div className="flex-1 min-w-0 flex flex-col h-full">
        <TopBar
          collapsed={effectiveCollapsed}
          forcedByViewport={isNarrow}
          reconnecting={streamStatus === 'reconnecting'}
          onToggleCollapsed={() => setCollapsed(!collapsed)}
        />
        <motion.main
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
          className="flex-1 min-h-0 overflow-auto scrollbar-gutter-stable"
        >
          <Suspense
            fallback={
              <div className="flex items-center justify-center py-24">
                <Loader2 className="h-5 w-5 animate-spin text-muted" />
              </div>
            }
          >
            <Outlet />
          </Suspense>
        </motion.main>
      </div>

      <ToastContainer />
      <AlertToastContainer />
      <AiAnalysisHost />
      <AiReportBubble />
      <StockAnalysisHost />
      <StockAnalysisBubble />
    </div>
  )
}
