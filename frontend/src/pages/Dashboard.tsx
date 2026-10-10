import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { ArrowUpRight, Check, Database, Gauge, GripVertical, Info, Loader2, Play, RefreshCw, RotateCcw, Sparkles, Timer } from 'lucide-react'
import { DatePicker } from '@/components/DatePicker'
import { api, type AlertEvent } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { DimensionMembersDialog, type DimensionMembersTarget } from '@/components/DimensionMembersDialog'
import { useDataStatus, useCapabilities, useSettings, usePreferences } from '@/lib/useSharedQueries'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import type { NavItem } from '@/lib/listNav'
import { SettingsModal } from '@/components/data/SettingsModal'
import { useAdjFactorSyncGate } from '@/components/AdjFactorSyncGate'
import { STAGE_LABELS } from '@/components/data/ActiveJobCard'
import { scoreColor, quoteAge } from '@/components/dashboard/shared'
import { DashboardGrid } from '@/components/dashboard/DashboardGrid'
import { AddWidgetPanel } from '@/components/dashboard/AddWidgetPanel'
import { useDashboardLayout } from '@/components/dashboard/useDashboardLayout'
import { DEFAULT_LAYOUT, widgetDef, type WidgetCtx } from '@/components/dashboard/registry'
import { cloneItems, GRID_COLS, type WidgetType } from '@/components/dashboard/layout'

/** 打开个股预览的来源榜 (用于行高亮与切股导航列表) */
type PreviewSource = 'gain' | 'loss' | 'amount' | 'active' | 'concept' | 'industry' | 'alert'

export function Dashboard() {
  const qc = useQueryClient()
  const [selectedDate, setSelectedDate] = useState<string | undefined>()
  const [manualFetching, setManualFetching] = useState(false)
  const [previewStock, setPreviewStock] = useState<{
    symbol: string
    name?: string
    alert?: AlertEvent
    /** 打开来源榜: 仅高亮来源榜的行 */
    source?: PreviewSource
    /** 切股导航列表 (来自来源榜) */
    navList?: NavItem[]
  } | null>(null)
  // 板块成分股弹窗 (概念/行业热度卡片行点击)
  const [dimensionTarget, setDimensionTarget] = useState<DimensionMembersTarget | null>(null)
  // 自定义网格布局(持久化 hook: 后端偏好加载 + 本地改动防抖落盘);
  // 注意必须在早退 return 之前 — Hooks 顺序不可随数据加载状态变化。
  const { items: dashItems, setItems: setDashItems } = useDashboardLayout()
  // 布局编辑态: 入口与控制组(添加组件/恢复默认/完成)在头部「重载」右侧
  const [dashEditing, setDashEditing] = useState(false)
  const placedTypes = useMemo(() => new Set(dashItems.map(it => it.t)), [dashItems])
  const resetDashLayout = useCallback(() => {
    setDashItems(cloneItems(DEFAULT_LAYOUT))
  }, [setDashItems])
  /** 追加组件: 放到当前布局最底部; ext-link 用唯一 id 支持多实例 */
  const addDashWidget = useCallback((t: WidgetType, props?: Record<string, string>) => {
    const def = widgetDef(t)
    if (!def) return
    // 内置组件单实例: 已存在则忽略
    if (t !== 'ext-link' && dashItems.some(it => it.t === t)) return
    const maxY = dashItems.reduce((m, it) => Math.max(m, it.y + it.h), 0)
    const id = t === 'ext-link' ? `ext-link-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}` : t
    setDashItems([...dashItems, { i: id, t, x: 0, y: maxY, w: Math.min(def.defW, GRID_COLS), h: def.defH, p: props }])
  }, [dashItems, setDashItems])
  // 首次使用(无数据 + 未完成引导)自动弹窗: 同一会话只弹一次
  const [showWelcomeModal, setShowWelcomeModal] = useState(false)
  const dataStatus = useDataStatus({ staleTime: 60_000 })
  const overview = useQuery({
    queryKey: QK.overviewMarket(selectedDate),
    queryFn: () => api.overviewMarket(selectedDate),
    staleTime: 5_000,
    placeholderData: (prev) => prev,
  })
  const data = overview.data
  const caps = useCapabilities()
  const settings = useSettings()
  const hasDepth = !!caps.data?.capabilities?.['depth5.batch']
  const sealedReady = !!data?.limit?.sealed_ready
  const isSealedDegrade = !hasDepth || !sealedReady
  // 空态引导文案按当前数据源分流: TickFlow 源提"免费服务器", 其他源提"当前数据源",
  // 弱化与默认 TickFlow 的隐式绑定 (None 档/免费 Key 等 TickFlow 概念仅在其被选中时出现)
  const prefs = usePreferences()
  const dataSourceList = useQuery({
    queryKey: QK.dataSources,
    queryFn: api.dataSources,
    staleTime: 60_000,
  })
  const activeProvider = prefs.data?.daily_data_provider || 'tickflow'
  const isTickflowProvider = activeProvider === 'tickflow'
  const providerLabel = [
    ...(dataSourceList.data?.builtin ?? []),
    ...(dataSourceList.data?.plugins ?? []),
    ...(dataSourceList.data?.custom ?? []),
  ].find(s => s.name === activeProvider)?.display_name
    ?.replace(/（.*?）|\(.*?\)/g, '').trim() || activeProvider
  // 无本地数据(enriched/daily 都没有)→ 常驻引导卡片
  // 注: 后端 status 的 rows 为性能刻意返回 0, 用 trading_days 判断是否有数据
  const ds = dataStatus.data
  const hasNoData = !!ds
    && (ds.enriched?.trading_days ?? 0) === 0
    && (ds.daily?.trading_days ?? 0) === 0

  // ===== 盘后管道触发(看板内一键获取数据) =====
  const [fetchJobId, setFetchJobId] = useState<string | null>(null)
  const fetchStatus = useQuery({
    queryKey: QK.pipelineJob(fetchJobId ?? ''),
    queryFn: () => api.pipelineJob(fetchJobId!),
    enabled: !!fetchJobId,
    refetchInterval: (q: any) => {
      const j = q.state.data
      return j && (j.status === 'succeeded' || j.status === 'failed') ? false : 1_000
    },
  })
  const startFetch = useMutation({
    mutationFn: api.pipelineRun,
    onSuccess: ({ job_id }) => setFetchJobId(job_id),
  })
  const isFetching = startFetch.isPending
    || fetchStatus.data?.status === 'running'
    || fetchStatus.data?.status === 'pending'
  const fetchFailed = fetchStatus.data?.status === 'failed'
  const fetchSucceeded = fetchStatus.data?.status === 'succeeded'

  // 首次使用且无数据 → 自动弹一次引导弹窗(同会话只弹一次)
  // 「开始获取」前若无除权因子能力, 先弹前置确认 (adjGate.guard)
  const adjGate = useAdjFactorSyncGate()
  useEffect(() => {
    if (!hasNoData) return
    if (settings.data?.onboarding_completed === false) return  // 还在引导流程中,不重复弹
    if (sessionStorage.getItem('tf_welcome_shown')) return
    sessionStorage.setItem('tf_welcome_shown', '1')
    setShowWelcomeModal(true)
  }, [hasNoData, settings.data?.onboarding_completed])

  // 同步完成后刷新看板数据
  useEffect(() => {
    if (fetchSucceeded) {
      qc.invalidateQueries({ queryKey: QK.dataStatus })
      qc.invalidateQueries({ queryKey: QK.overviewMarket(undefined) })
    }
  }, [fetchSucceeded, qc])

  // 组件重新挂载时(从其他页面切回)恢复正在运行的同步任务进度。
  // 原因: fetchJobId 是组件内状态, 切走页面时组件卸载、状态丢失, 切回后进度卡片消失。
  // 修复: 挂载时若无本地数据且未跟踪任何 job, 查一次后端是否有 active job, 有则接管。
  const resumeTriedRef = useRef(false)
  useEffect(() => {
    if (resumeTriedRef.current) return
    if (!hasNoData) return
    if (fetchJobId) return
    resumeTriedRef.current = true
    api.pipelineJobs(1).then(({ active_id }) => {
      if (active_id) setFetchJobId(active_id)
    }).catch(() => { /* 查询失败不阻塞, 用户仍可手动点击获取 */ })
  }, [hasNoData, fetchJobId])

  // 手动刷新: 先重建后端 Polars 缓存(解决跨天残留), 再重新拉看板数据
  const handleRefresh = () => {
    setManualFetching(true)
    api.refreshCache()
      .then(() => qc.invalidateQueries({ queryKey: ['overview-market'] }))
      .finally(() => {
        overview.refetch().finally(() => setManualFetching(false))
      })
  }

  if (overview.isLoading && !data) {
    return (
      <div className="flex h-full items-center justify-center bg-base">
        <div className="flex items-center gap-2 text-sm text-muted">
          <Loader2 className="h-4 w-4 animate-spin" /> 加载市场看板…
        </div>
      </div>
    )
  }

  if (!data) {
    return (
      <div className="flex h-full items-center justify-center bg-base p-6">
        <div className="rounded-card border border-border bg-surface p-6 text-center">
          <div className="text-sm text-danger">看板加载失败</div>
          <button onClick={() => overview.refetch()} className="mt-3 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-white">重试</button>
        </div>
      </div>
    )
  }

  const score = data.emotion?.score ?? 50
  const latestDate = dataStatus.data?.enriched?.latest_date ?? null
  const currentDate = selectedDate ?? data.as_of ?? ''
  const quoteRunning = (!selectedDate || selectedDate === latestDate) && data.quote_status?.running
  // 实时模式: none / watchlist / full_market。
  // watchlist 模式仅自选 ≤5 只实时, 看板呈现的大盘数据实为盘后快照, 需提示避免误读。
  const quoteMode = data.quote_status?.mode as ('none' | 'watchlist' | 'full_market') | undefined

  // 网格组件渲染上下文: 数据切片 + 交互回调统一由页面层供给
  const widgetCtx: WidgetCtx = {
    data,
    score,
    hasDepth,
    sealedReady,
    isSealedDegrade,
    activeSymbol: source => (previewStock?.source === source ? previewStock.symbol : undefined),
    openStock: (source, symbol, name, navList) =>
      setPreviewStock({ symbol, name, navList, source: source as PreviewSource }),
    openDimension: setDimensionTarget,
    openAlert: (event, navList) => {
      if (event.symbol) setPreviewStock({ symbol: event.symbol, name: event.name ?? undefined, alert: event, source: 'alert', navList })
    },
  }

  return (
    <div className="min-h-full bg-base p-1.5">
      {/* 无本地数据常驻引导卡片 —— 一键触发盘后管道获取数据(无 Key 也可) */}
      {hasNoData && (
        <FetchDataCard
          isFetching={isFetching}
          isStarting={startFetch.isPending}
          fetchFailed={fetchFailed}
          stage={fetchStatus.data?.stage}
          fetchPct={fetchStatus.data?.progress}
          onStart={() => startFetch.mutate()}
          isTickflowProvider={isTickflowProvider}
          providerLabel={providerLabel}
        />
      )}
      {/* 首次使用自动弹窗(同会话仅一次) */}
      <AnimatePresence>
        {showWelcomeModal && (
          <WelcomeFetchModal
            isTickflowProvider={isTickflowProvider}
            providerLabel={providerLabel}
            onClose={() => setShowWelcomeModal(false)}
            onStart={() => {
              adjGate.guard(() => {
                startFetch.mutate()
                setShowWelcomeModal(false)
              })
            }}
          />
        )}
      </AnimatePresence>
      {/* 无除权因子能力时的同步前置确认 */}
      {adjGate.dialog}
      <div className="relative mb-1.5 flex flex-wrap items-center justify-between gap-2 overflow-hidden rounded-card border border-border bg-gradient-to-r from-surface/90 to-surface/70 px-3 py-1.5 shadow-[0_1px_3px_hsl(var(--border)/0.4)] backdrop-blur-sm">
        <div className="pointer-events-none absolute left-0 top-0 h-full w-1 bg-gradient-to-b from-accent to-accent/20" aria-hidden />
        <div className="flex items-center gap-2">
          <Gauge className="h-4 w-4 text-accent" />
          <h1 className="text-[16px] leading-6 font-semibold text-foreground">市场看板</h1>
          <span
            className="rounded-full border px-2 py-0.5 text-[10px] font-medium"
            style={{
              color: scoreColor(score),
              borderColor: `${scoreColor(score)}40`,
              background: `${scoreColor(score)}14`,
            }}
          >
            {data.emotion.label} · {score}
          </span>
        </div>
        <div className="flex items-center gap-3 text-[11px] text-muted">
          {currentDate ? (
            <DatePicker
              value={currentDate}
              onChange={setSelectedDate}
              min={dataStatus.data?.enriched?.earliest_date ?? undefined}
              max={latestDate ?? undefined}
              className="w-32"
            />
          ) : (
            <span className="font-mono text-secondary">—</span>
          )}
          <span className="flex items-center gap-1"><Timer className="h-3 w-3" />{quoteAge(data.quote_status?.quote_age_ms)}</span>
          <span className={quoteRunning ? 'text-accent' : 'text-warning'}>{quoteRunning ? '实时' : '非实时'}</span>
          <button
            onClick={handleRefresh}
            disabled={manualFetching}
            className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground disabled:opacity-50"
          >
            <RefreshCw className={`h-3 w-3 ${manualFetching ? 'animate-spin' : ''}`} />重载
          </button>
          {!dashEditing ? (
            <button
              onClick={() => setDashEditing(true)}
              title="拖拽调整组件位置与宽高"
              className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground hover:border-accent/40"
            >
              <GripVertical className="h-3 w-3" />自定义布局
            </button>
          ) : (
            <>
              <AddWidgetPanel placedTypes={placedTypes} onAdd={addDashWidget} />
              <button
                onClick={resetDashLayout}
                className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground"
              >
                <RotateCcw className="h-3 w-3" />恢复默认
              </button>
              <button
                onClick={() => setDashEditing(false)}
                className="inline-flex items-center gap-1 rounded-btn bg-accent px-2.5 py-1 text-[11px] font-medium text-white transition-colors hover:bg-accent/90"
              >
                <Check className="h-3 w-3" />完成
              </button>
            </>
          )}
        </div>
      </div>

      {/* 自选实时模式提示: 大盘看板为盘后数据, 仅自选实时。避免用户误读为全市场实时。 */}
      {quoteMode === 'watchlist' && (
        <div className="mb-1.5 flex items-start gap-2 rounded-card border border-amber-500/30 bg-amber-500/8 px-3 py-1.5 text-[11px] leading-relaxed">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
          <div className="min-w-0 flex-1 text-secondary">
            当前为「自选实时」模式,看板展示的大盘数据为<strong className="text-foreground">盘后快照</strong>(最新有数据日),并非盘中实时;
            仅自选({data.quote_status?.watchlist_symbol_count ?? 0} 只)支持实时监控。
            <span className="ml-1 text-accent">全市场实时依赖数据源支持</span>
          </div>
        </div>
      )}

      <DashboardGrid ctx={widgetCtx} items={dashItems} onItemsChange={setDashItems} editing={dashEditing} />

      <StockPreviewDialog
        symbol={previewStock?.symbol ?? null}
        name={previewStock?.name}
        triggerInfo={previewStock?.alert ? {
          price: previewStock.alert.price ?? null,
          changePct: previewStock.alert.change_pct ?? null,
          ts: previewStock.alert.ts,
          signals: previewStock.alert.signals,
          message: previewStock.alert.message,
        } : null}
        navList={previewStock?.navList}
        onNavigate={(sym, n) => setPreviewStock(prev => prev ? { ...prev, symbol: sym, name: n, alert: undefined } : prev)}
        onClose={() => setPreviewStock(null)}
      />
      <DimensionMembersDialog
        target={dimensionTarget}
        onClose={() => setDimensionTarget(null)}
        onStockClick={(symbol, name) => {
          setDimensionTarget(null)
          setPreviewStock({ symbol, name })
        }}
      />
    </div>
  )
}

// ===== 无数据常驻引导卡片: 一键触发盘后管道获取行情数据(无 Key 也可) =====
function FetchDataCard({
  isFetching, isStarting, fetchFailed, stage, fetchPct, onStart,
  isTickflowProvider, providerLabel,
}: {
  isFetching: boolean
  isStarting: boolean
  fetchFailed: boolean
  stage?: string
  fetchPct?: number
  onStart: () => void
  isTickflowProvider: boolean
  providerLabel: string
}) {
  const stageText = stage ? (STAGE_LABELS[stage] ?? stage) : '正在同步行情数据…'
  return (
    <div className="mb-3 rounded-card border border-border bg-surface/85 p-3.5">
      <div className="flex items-start gap-3">
        <div className="rounded-lg bg-accent/10 p-2 shrink-0">
          <Database className="h-4 w-4 text-accent" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium text-foreground">当前暂无数据</div>
          <p className="mt-1 text-xs text-secondary leading-relaxed">
            首次使用需获取行情数据后才能查看看板。{isTickflowProvider
              ? '可通过 TickFlow 免费服务器拉取近 1 年全 A 股日K'
              : `将从当前数据源「${providerLabel}」拉取近 1 年全 A 股日K`}(约 5500 只),预计 1-3 分钟,期间可继续浏览其他页面。
          </p>
          <p className="mt-1 text-[11px] text-warning/80 leading-relaxed">
            ⓘ 获取数据后即可进行策略定制、回测验证、策略扫描等本地分析功能。
          </p>
          <p className="mt-1 text-[11px] text-muted leading-relaxed">
            💡 配置 fuyao(同花顺 REST) Key 可解锁财务四表 / 龙虎榜 / 盘前风向标 / 竞价异动:
            <Link to="/settings?tab=data-sources" className="text-accent hover:text-accent/80 transition-colors">前往设置 →</Link>
          </p>

          {isFetching ? (
            <div className="mt-3">
              <div className="flex items-center justify-between text-[11px] text-muted mb-1.5">
                <span className="inline-flex items-center gap-1.5">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  {isStarting ? '正在启动同步任务…' : stageText}
                </span>
                <span className="font-mono tabular">
                  {typeof fetchPct === 'number' ? `${Math.round(fetchPct)}%` : ''}
                </span>
              </div>
              <div className="h-1.5 rounded-full bg-elevated overflow-hidden">
                <motion.div
                  className="h-full bg-accent"
                  initial={{ width: 0 }}
                  animate={{ width: `${Math.max(2, Math.min(100, fetchPct ?? 0))}%` }}
                  transition={{ duration: 0.4, ease: 'easeOut' }}
                />
              </div>
            </div>
          ) : fetchFailed ? (
            <div className="mt-3 flex items-center gap-2">
              <span className="text-xs text-danger">同步失败,请重试</span>
              <button
                onClick={onStart}
                className="inline-flex items-center gap-1.5 px-3 h-8 rounded-btn bg-accent text-white text-xs font-medium hover:bg-accent/90 transition-colors"
              >
                <Play className="h-3.5 w-3.5" />重新获取
              </button>
            </div>
          ) : (
            <div className="mt-3 flex items-center gap-3">
              <button
                onClick={onStart}
                className="inline-flex items-center gap-1.5 px-4 h-8 rounded-btn bg-accent text-white text-xs font-medium hover:bg-accent/90 transition-colors"
              >
                <Play className="h-3.5 w-3.5" />立即获取数据
              </button>
              <Link
                to="/data"
                className="inline-flex items-center gap-0.5 text-xs text-secondary hover:text-accent transition-colors"
              >
                前往数据页
                <ArrowUpRight className="h-3 w-3 self-center" />
              </Link>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ===== 首次使用自动弹窗: 询问用户后触发盘后管道 =====
function WelcomeFetchModal({
  onClose, onStart, isTickflowProvider, providerLabel,
}: {
  isTickflowProvider: boolean
  providerLabel: string
  onClose: () => void
  onStart: () => void
}) {
  return (
    <SettingsModal title="欢迎首次使用 · 获取行情数据" onClose={onClose}>
      <div className="text-center">
        <motion.div
          initial={{ scale: 0.85, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
          className="mx-auto w-fit rounded-2xl bg-accent/10 p-3.5"
        >
          <Sparkles className="h-7 w-7 text-accent" />
        </motion.div>
        <h3 className="mt-4 text-[16px] leading-6 font-semibold text-foreground">首次使用,需先获取行情数据</h3>
        <p className="mt-2 text-xs text-secondary leading-relaxed">
          {isTickflowProvider
            ? '可通过 TickFlow 免费服务器拉取近 1 年全 A 股日K'
            : `将从当前数据源「${providerLabel}」拉取近 1 年全 A 股日K`}(约 5500 只),预计 1-3 分钟。
          同步期间可继续浏览其他页面,完成后看板自动刷新。
        </p>
        <div className="mx-auto mt-4 max-w-md rounded-btn bg-elevated/60 px-4 py-3 text-left">
          <div className="text-[11px] font-medium text-secondary">获取完成后的推荐步骤</div>
          <ol className="mt-1.5 space-y-1 text-[11px] text-muted leading-relaxed">
            <li>1. <span className="text-secondary">配置 fuyao(同花顺 REST) Key</span> — 解锁财务四表 / 龙虎榜 / 盘前风向标 / 竞价异动</li>
            <li>2. <span className="text-secondary">分钟数据落盘(可选)</span> — 分钟策略回测与板块分时走势需要</li>
            <li>3. <span className="text-secondary">开始研究</span> — 自选加标的 → 策略扫描 → 回测验证</li>
          </ol>
          <Link
            to="/settings?tab=data-sources"
            onClick={onClose}
            className="mt-2 inline-flex items-center gap-0.5 text-[11px] text-accent hover:text-accent/80 transition-colors"
          >
            前往设置 → 数据源
            <ArrowUpRight className="h-3 w-3 self-center" />
          </Link>
        </div>
        <div className="mt-5 flex items-center justify-center gap-2.5">
          <button
            onClick={onClose}
            className="px-4 h-9 rounded-btn text-sm text-secondary hover:text-foreground hover:bg-elevated transition-colors"
          >
            稍后再说
          </button>
          <button
            onClick={onStart}
            className="inline-flex items-center gap-2 px-5 h-9 rounded-xl bg-accent text-white text-sm font-semibold shadow-lg shadow-accent/20 hover:bg-accent/90 transition-all"
          >
            <Play className="h-4 w-4" />开始获取
          </button>
        </div>
      </div>
    </SettingsModal>
  )
}
