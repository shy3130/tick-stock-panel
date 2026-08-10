import { useEffect, useMemo, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { useQuoteStream } from '@/lib/useQuoteStream'
import { ToastContainer } from '@/components/Toast'
import { AlertToastContainer } from '@/components/AlertToast'
import { AiAnalysisHost } from '@/components/financials/AiAnalysisHost'
import { AiReportBubble } from '@/components/financials/AiReportBubble'
import { StockAnalysisHost } from '@/components/stock-analysis/StockAnalysisHost'
import { StockAnalysisBubble } from '@/components/stock-analysis/StockAnalysisBubble'
import {
  usePreferences,
  useQuoteStatus,
  useVersion,
} from '@/lib/useSharedQueries'
import {
  useToggleRealtimeQuotes,
} from '@/lib/useSharedMutations'
import { QK } from '@/lib/queryKeys'
import {
  Star,
  ScanSearch,
  ListFilter,
  History,
  FileText,
  Settings,
  Database,
  Loader2,
  LayoutDashboard,
  Tags,
  TrendingUp,
  Flame,
  BarChart3,
  PieChart,
  Activity,
  Landmark,
  Cable,
  RadioTower,
  CheckCircle2,
  BookOpenCheck,
  BookOpen,
  NotebookPen,
  Bot,
  Eye,
  FlaskConical,
  Target,
  Network,

  Sparkles,
  Globe,
} from 'lucide-react'
import { api, resolveQuoteDataState, quoteDataStateText, quoteSnapshotText, indexQuotesDegraded, indexFallbackReasonText, type IndexQuotesResponse } from '@/lib/api'
import { cn } from '@/lib/cn'
import { setCurrentTotal as setAlertTotal, useUnreadAlerts } from '@/lib/monitorBadge'

// 品牌色 — 只用于 logo / brand 区域,不影响功能语义色
const BRAND = '#8B5CF6'

const CORE_INDEXES = [
  { symbol: '000001.SH', name: '上证指数' },
  { symbol: '399001.SZ', name: '深证成指' },
  { symbol: '399006.SZ', name: '创业板指' },
  { symbol: '000680.SH', name: '科创综指' },
] as const

type CoreIndex = (typeof CORE_INDEXES)[number]

const nav = [
  { to: '/',                label: '看板',     icon: LayoutDashboard },
  { to: '/watchlist',  label: '自选',   icon: Star },
  { to: '/screener',   label: '策略',   icon: ScanSearch },
  { to: '/condition-screener', label: '条件选股', icon: ListFilter },
  { to: '/backtest',   label: '回测',   icon: History },
  { to: '/optimizer', label: '组合优化', icon: PieChart },
  { to: '/stock-analysis',    label: '个股分析', icon: TrendingUp },
  { to: '/limit-ladder', label: '连板梯队', icon: Flame },
  { to: '/industry-analysis', label: '行业分析', icon: Landmark },
  { to: '/regime', label: '市场环境', icon: Activity },
  { to: '/financials', label: '财务分析', icon: FileText },
  { to: '/monitor', label: '监控中心', icon: RadioTower },
  { to: '/review',      label: '复盘',   icon: BookOpenCheck },
  { to: '/agent', label: 'AI 助手', icon: Bot },
  { to: '/journal', label: '交易复盘', icon: NotebookPen },
  { to: '/research', label: '研究中心', icon: FlaskConical },
  { to: '/signal-scorecard', label: '信号记分卡', icon: Target },
  { to: '/cross-section', label: '横截面分析', icon: Network },

  { to: '/indices', label: '指数', icon: BarChart3 },
  { to: '/trading', label: '交易', icon: Cable },
  { to: '/data',       label: '数据',   icon: Database },
] as const

// ── 导航分组: 查看 / AI 分析 两个 tab ──

type NavCategory = 'view' | 'analysis'

const NAV_CATEGORY: Record<string, NavCategory> = {
  '/': 'view',
  '/watchlist': 'view',
  '/limit-ladder': 'view',
  '/monitor': 'view',
  '/indices': 'view',
  '/trading': 'view',
  '/data': 'view',
  '/screener': 'analysis',
  '/condition-screener': 'analysis',
  '/backtest': 'analysis',
  '/optimizer': 'analysis',
  '/stock-analysis': 'analysis',
  '/concept-analysis': 'analysis',
  '/industry-analysis': 'analysis',
  '/regime': 'analysis',
  '/financials': 'analysis',
  '/review': 'analysis',
  '/agent': 'analysis',
  '/journal': 'analysis',
  '/research': 'analysis',
  '/signal-scorecard': 'analysis',
  '/cross-section': 'analysis',

}

/** 扩展分析菜单 (/analysis/*) 一律归入 AI 分析; 未知内置项默认查看。 */
function navCategory(to: string): NavCategory {
  if (to.startsWith('/analysis/')) return 'analysis'
  return NAV_CATEGORY[to] ?? 'view'
}

const NAV_TAB_KEY = 'sidebar_nav_tab'

const NAV_TABS: { key: NavCategory; label: string; icon: typeof Eye }[] = [
  { key: 'view', label: '查看', icon: Eye },
  { key: 'analysis', label: 'AI 分析', icon: Sparkles },
]

function fmtIndexValue(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return '--'
  return Number(v).toFixed(2)
}

function fmtIndexPct(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return '--'
  return `${Number(v) >= 0 ? '+' : ''}${Number(v).toFixed(2)}%`
}

function indexPctClass(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return 'text-muted'
  const n = Number(v)
  if (n === 0) return 'text-foreground'
  return n > 0 ? 'text-bull' : 'text-bear'
}

/** 监控中心未读徽标 — 仅在非监控页且有未读时显示。 */
function MonitorBadge({ active }: { active: boolean }) {
  const unread = useUnreadAlerts()
  // 尊重用户设置: 可在菜单设置里关闭数字提示
  const badgeEnabled = (() => {
    try { return localStorage.getItem('monitor_badge_enabled') !== '0' } catch { return true }
  })()
  if (active || unread <= 0 || !badgeEnabled) return null
  return (
    <span className="inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-danger px-1 text-[9px] font-bold text-white animate-pulse">
      {unread > 99 ? '99+' : unread}
    </span>
  )
}

function SidebarIndexQuotes({ data, items }: { data: IndexQuotesResponse | undefined; items: CoreIndex[] }) {
  if (items.length === 0) return null
  const rows = data?.rows
  // 外部源降级标识: 仅 degraded=true / source=fallback_external / 行 source=tencent_quote 时显示;
  // 本地实时与日线兜底(index_daily)一律不误标
  const degraded = indexQuotesDegraded(data)
  const reasonText = indexFallbackReasonText(data?.fallback_reason)
  const quoteBySymbol = new Map((rows ?? []).map(q => [q.symbol, q]))
  return (
    <div className="mt-2">
      {degraded && (
        <div className="mb-1">
          <span
            className="inline-flex cursor-help items-center gap-1 rounded border border-warning/30 bg-warning/10 px-1.5 py-0.5 text-[9px] leading-none text-warning/80"
            title={`指数行情来自腾讯公共行情（${reasonText ?? '本地快照不可用'}），为外部源降级数据，仅供展示；不会写入本地行情库，也不参与选股、监控、回测。`}
          >
            <Globe className="h-2.5 w-2.5" aria-hidden />
            外部源·降级数据
          </span>
        </div>
      )}
      <div className="grid grid-cols-2 gap-1.5">
        {items.map(item => {
          const q = quoteBySymbol.get(item.symbol)
          const value = q?.last_price ?? q?.close
          const pct = q?.change_pct
          return (
            <NavLink
              key={item.symbol}
              to={`/indices?symbol=${encodeURIComponent(item.symbol)}`}
              className="block rounded bg-elevated/60 px-2 py-1.5 transition-colors hover:bg-elevated"
              title={`${item.name} ${item.symbol}`}
            >
              <div className="flex items-center justify-between gap-1">
                <span className="text-[10px] text-secondary">{item.name}</span>
                <span className={`text-[10px] font-mono ${indexPctClass(pct)}`}>{fmtIndexPct(pct)}</span>
              </div>
              <div className="mt-0.5 truncate font-mono text-[10px] text-foreground/80">
                {fmtIndexValue(value)}
              </div>
            </NavLink>
          )
        })}
      </div>
    </div>
  )
}

export function Layout() {
  // ===== 共享 hooks (替代内联 useQuery) =====
  const { data: versionData } = useVersion()
  const { data: prefs } = usePreferences()
  // poll=true: 全局唯一开启条件轮询 (非交易时段 60s 兜底, 交易时段靠 SSE)
  const { data: quoteStatus } = useQuoteStatus({ poll: true })
  const { data: analysisMenus } = useQuery({
    queryKey: QK.analysisMenus,
    queryFn: api.analysisMenus,
  })

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

  const toggleQuote = useToggleRealtimeQuotes()
  const isTrading = quoteStatus?.is_trading_hours ?? false
  // 数据健康: 仅 ready 才算行情真的在更新, 线程存活不算
  const quoteDataState = resolveQuoteDataState(quoteStatus)
  const isQuoteReady = quoteDataState === 'ready'
  const quoteSnapshot = quoteSnapshotText(quoteStatus?.source_as_of)
  const isQuoteLive = isQuoteReady && !quoteSnapshot
  const quoteMode = quoteStatus?.mode ?? 'none'
  const realtimeAllowed = quoteStatus?.realtime_allowed ?? quoteMode !== 'none'
  const isNoneTier = !realtimeAllowed
  const isWatchlistMode = quoteMode === 'watchlist'
  const realtimeModeLabel = isWatchlistMode ? '自选股' : '全市场'

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

  // 合并内置页面 + 可见的扩展分析菜单
  const analysisNav = useMemo(
    () => (analysisMenus?.items ?? [])
      .filter(m => m.visible)
      .map(m => ({ to: `/analysis/${m.id}`, label: m.label, icon: m.icon === 'tags' ? Tags : BarChart3 })),
    [analysisMenus],
  )

  const allNav = useMemo(() => [...nav, ...analysisNav], [analysisNav])
  const savedOrder = prefs?.nav_order ?? []

  const navItems = savedOrder.length > 0
    ? (() => {
        const byTo = new Map(allNav.map(n => [n.to, n]))
        const ordered = savedOrder
          .map(id => byTo.get(id) ?? byTo.get(`/analysis/${id}`))
          .filter(Boolean)
        const seen = new Set(ordered.map(n => n!.to))
        return [...ordered as typeof allNav, ...allNav.filter(n => !seen.has(n.to))]
      })()
    : allNav

  const hiddenIds = new Set(prefs?.nav_hidden ?? [])
  const visibleNavItems = navItems.filter(n => !hiddenIds.has(n.to) && !hiddenIds.has(n.to.replace(/^\/analysis\//, '')))

  // ── 查看 / AI 分析 分组 tab ──
  const location = useLocation()
  const [navTab, setNavTab] = useState<NavCategory>(() => {
    try {
      return localStorage.getItem(NAV_TAB_KEY) === 'analysis' ? 'analysis' : 'view'
    } catch {
      return 'view'
    }
  })
  const switchNavTab = (tab: NavCategory) => {
    setNavTab(tab)
    try { localStorage.setItem(NAV_TAB_KEY, tab) } catch { /* ignore */ }
  }
  // 路由落在某个导航项上时自动切到对应分组，保证激活项始终可见
  useEffect(() => {
    const pathname = location.pathname
    const hit = allNav.find(n =>
      n.to === '/' ? pathname === '/' : pathname === n.to || pathname.startsWith(`${n.to}/`),
    )
    if (!hit) return
    const cat = navCategory(hit.to)
    setNavTab(prev => {
      if (prev === cat) return prev
      try { localStorage.setItem(NAV_TAB_KEY, cat) } catch { /* ignore */ }
      return cat
    })
  }, [location.pathname, allNav])

  const tabNavItems = visibleNavItems.filter(n => navCategory(n.to) === navTab)
  // 监控未读: 用户在 AI 分析 tab 时用红点提示「查看」tab 有未读告警
  const unreadAlerts = useUnreadAlerts()
  const monitorBadgeEnabled = (() => {
    try { return localStorage.getItem('monitor_badge_enabled') !== '0' } catch { return true }
  })()

  const handleToggle = async (enabled: boolean) => {
    // 开启时重新校验后端实时行情模式，后端处理数据源 capability。
    if (enabled) {
      const freshStatus = await qc.fetchQuery({
        queryKey: QK.quoteStatus,
        queryFn: api.quoteStatus,
      })
      if (!freshStatus.realtime_allowed || freshStatus.mode === 'none') return
      if (freshStatus.mode === 'watchlist' && (prefs?.realtime_watchlist_symbols?.length ?? 0) === 0) {
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
    <div className="h-screen grid grid-cols-[14rem_1fr] bg-base text-foreground overflow-hidden">
      <aside className="border-r border-border bg-surface flex flex-col h-full min-h-0 overflow-hidden">
        <div className="px-5 py-5 border-b border-border shrink-0">
          {/* Brand block — FM wordmark */}
          <div
            className="font-mono font-bold text-[22px] tracking-[0.12em] leading-tight"
            style={{ color: BRAND, textShadow: `0 0 12px ${BRAND}55` }}
          >
            FM
          </div>

          <div className="mt-1.5 text-[10px] uppercase tracking-[0.22em] text-secondary">
            Quant · Terminal
          </div>

          <div
            className="mt-3 h-px"
            style={{ background: `linear-gradient(90deg, ${BRAND}88, transparent 80%)` }}
          />

        </div>

        {/* 查看 / AI 分析 分组切换 */}
        <div className="px-3 pt-3 shrink-0">
          <div className="grid grid-cols-2 gap-1 rounded-lg bg-elevated/50 p-1">
            {NAV_TABS.map(({ key, label, icon: TabIcon }) => {
              const active = navTab === key
              const showUnreadDot = key === 'view' && !active && unreadAlerts > 0 && monitorBadgeEnabled
              return (
                <button
                  key={key}
                  onClick={() => switchNavTab(key)}
                  className={cn(
                    'relative flex items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-xs font-medium transition-colors duration-150 ease-smooth',
                    active
                      ? 'bg-elevated text-foreground shadow-sm'
                      : 'text-secondary hover:text-foreground',
                  )}
                >
                  <TabIcon className="h-3.5 w-3.5 shrink-0" />
                  <span>{label}</span>
                  {showUnreadDot && (
                    <span className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-danger animate-pulse" />
                  )}
                </button>
              )
            })}
          </div>
        </div>

        <nav className="flex-1 min-h-0 overflow-y-auto px-2 pb-3 pt-2 space-y-0.5">
          {tabNavItems.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-3 px-3 py-2 rounded-btn text-sm transition-colors duration-150 ease-smooth',
                  isActive
                    ? 'bg-elevated text-foreground font-medium'
                    : 'text-foreground/80 hover:bg-elevated hover:text-foreground',
                )
              }
            >
              {({ isActive }) => (
                <>
                  <Icon className="h-4 w-4 shrink-0" />
                  <span className="flex-1">{label}</span>
                  {/* 个股分析 Beta 标识 */}
                  {(to === '/stock-analysis' || to === '/review') && (
                    <span className="inline-flex items-center rounded-full border border-amber-400/30 bg-amber-400/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-amber-400 shrink-0">
                      Beta
                    </span>
                  )}
                  {/* 数据同步状态: 同步中转圈, 刚完成显示绿色对勾闪烁 3 秒 */}
                  {to === '/data' && isDataSyncing && (
                    <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-accent" />
                  )}
                  {to === '/data' && !isDataSyncing && dataSyncJustDone && (
                    <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-bull animate-pulse" />
                  )}
                  {/* 监控中心徽标: 仅非监控页且有未读时显示 */}
                  {to === '/monitor' && <MonitorBadge active={isActive} />}
                </>
              )}
            </NavLink>
          ))}
        </nav>

        {/* 全局行情开关 */}
        <div className="border-t border-border px-3 py-2.5 shrink-0">
          {isNoneTier ? (
            <div>
              <div className="flex items-center justify-between">
                <span className="text-xs text-secondary truncate">实时行情</span>
                <span className="text-[10px] text-accent/70 font-medium bg-accent/10 px-1.5 py-0.5 rounded">
                  Free+
                </span>
              </div>
              <div className="mt-1.5 text-[10px] leading-snug text-muted">
                当前数据源未提供实时行情能力
              </div>
            </div>
          ) : (
            /* Starter+ — 开关 + 跳转设置 */
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 min-w-0">
                <span className={`inline-block h-1.5 w-1.5 rounded-full shrink-0 ${
                  realtimeEnabled && isQuoteLive
                    ? 'bg-accent animate-pulse'
                    : realtimeEnabled
                      ? 'bg-warning/60'
                      : 'bg-muted'
                }`} />
                <span className="text-xs text-secondary truncate">
                  实时行情 · {realtimeModeLabel}
                </span>
                <button
                  onClick={() => navigate('/settings?tab=monitoring')}
                  className="text-secondary hover:text-foreground transition-colors shrink-0"
                  title="实时监控设置"
                >
                  <Settings className="h-3 w-3" />
                </button>
              </div>
              <button
                onClick={() => handleToggle(!realtimeEnabled)}
                disabled={toggleQuote.isPending}
                className={`relative inline-flex h-4 w-7 items-center rounded-full shrink-0 transition-colors duration-200 ${
                  realtimeEnabled
                    ? 'bg-accent shadow-[0_0_6px_rgba(59,130,246,0.3)]'
                    : 'bg-elevated'
                } ${toggleQuote.isPending ? 'opacity-50' : 'cursor-pointer'}`}
              >
                <span className={`inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform duration-200 ${
                  realtimeEnabled ? 'translate-x-[14px]' : 'translate-x-0.5'
                }`} />
              </button>
            </div>
          )}

          {/* 状态提示 — 仅 ready 视为行情在更新, 其余状态给出真实原因 */}
          {realtimeEnabled && !isNoneTier && (
            <div className="mt-1.5 text-[10px] leading-snug">
              {isQuoteLive ? (
                <span className="text-accent">行情运行中</span>
              ) : isQuoteReady ? (
                <span className="text-warning/70">轮询中，本地快照可用</span>
              ) : !isTrading ? (
                <span className="text-warning/70">非交易时段，将在交易时间自动开启</span>
              ) : quoteDataState ? (
                <span className={quoteDataState === 'error' ? 'text-danger/80' : 'text-warning/70'}>
                  {quoteDataStateText(quoteDataState)}
                </span>
              ) : null}
              {quoteSnapshot && (
                <div className="text-muted">{quoteSnapshot}</div>
              )}
            </div>
          )}
          {showSidebarQuotes && !isWatchlistMode && !isNoneTier && (
            <SidebarIndexQuotes data={sidebarIndexQuotes} items={sidebarIndexes} />
          )}
        </div>

        <div className="border-t border-border px-2 py-3 space-y-0.5 shrink-0">
          <NavLink
            to="/guide"
            className={({ isActive }) =>
              cn(
                'flex items-center gap-3 px-3 py-2 rounded-btn text-sm transition-colors duration-150 ease-smooth',
                isActive
                  ? 'bg-elevated text-foreground font-medium'
                  : 'text-foreground/80 hover:bg-elevated hover:text-foreground',
              )
            }
          >
            <BookOpen className="h-4 w-4 shrink-0" />
            <span>功能说明</span>
          </NavLink>
          <NavLink
            to="/settings"
            className={({ isActive }) =>
              cn(
                'flex items-center justify-between gap-3 px-3 py-2 rounded-btn text-sm transition-colors duration-150 ease-smooth',
                isActive
                  ? 'bg-elevated text-foreground font-medium'
                  : 'text-foreground/80 hover:bg-elevated hover:text-foreground',
              )
            }
          >
            <span className="flex items-center gap-3">
              <Settings className="h-4 w-4 shrink-0" />
              <span>设置</span>
            </span>
            <span className="font-mono text-[10px] text-muted/50 select-none">
              {version ?? ''}
            </span>
          </NavLink>
        </div>
      </aside>

      <motion.main
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
        className="h-full overflow-auto scrollbar-gutter-stable"
      >
        <Outlet />
      </motion.main>
      <ToastContainer />
      <AlertToastContainer />
      <AiAnalysisHost />
      <AiReportBubble />
      <StockAnalysisHost />
      <StockAnalysisBubble />
    </div>
  )
}
