import { useEffect, useMemo, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence, useReducedMotion } from 'framer-motion'
import { useQuoteStream } from '@/lib/useQuoteStream'
import { ToastContainer } from '@/components/Toast'
import { AlertToastContainer } from '@/components/AlertToast'
import { AiAnalysisHost } from '@/components/financials/AiAnalysisHost'
import { AiReportBubble } from '@/components/financials/AiReportBubble'
import { StockAnalysisHost } from '@/components/stock-analysis/StockAnalysisHost'
import { StockAnalysisBubble } from '@/components/stock-analysis/StockAnalysisBubble'
import { ThemeToggle } from '@/components/ThemeToggle'
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
  FlaskConical,
  Target,
  Network,
  Globe,
  ChevronDown,
  Menu,
  X,
  LineChart,
  Crosshair,
  Cpu,
} from 'lucide-react'
import { api, resolveQuoteDataState, quoteDataStateText, quoteSnapshotText, indexQuotesDegraded, indexFallbackReasonText, type IndexQuotesResponse } from '@/lib/api'
import { cn } from '@/lib/cn'
import { setCurrentTotal as setAlertTotal, useUnreadAlerts } from '@/lib/monitorBadge'

const CORE_INDEXES = [
  { symbol: '000001.INDEX', name: '上证指数' },
  { symbol: '399001.INDEX', name: '深证成指' },
  { symbol: '399006.INDEX', name: '创业板指' },
  { symbol: '000680.INDEX', name: '科创综指' },
] as const

type CoreIndex = (typeof CORE_INDEXES)[number]

const nav = [
  { to: '/',                label: '看板',     icon: LayoutDashboard },
  { to: '/watchlist',  label: '自选',   icon: Star },
  { to: '/screener',   label: '策略',   icon: ScanSearch, title: '策略选股：跑内置/自定义策略' },
  { to: '/condition-screener', label: '条件选股', icon: ListFilter, title: '自己拼条件筛选' },
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

// ── 五领域导航: 市场 / 研究 / 策略 / 交易 / 系统 ──

type NavDomain = 'market' | 'research' | 'strategy' | 'trading' | 'system'

const NAV_DOMAIN: Record<string, NavDomain> = {
  '/': 'market',
  '/watchlist': 'market',
  '/limit-ladder': 'market',
  '/monitor': 'market',
  '/indices': 'market',
  '/stock-analysis': 'research',
  '/concept-analysis': 'research',
  '/industry-analysis': 'research',
  '/regime': 'research',
  '/financials': 'research',
  '/research': 'research',
  '/signal-scorecard': 'research',
  '/cross-section': 'research',
  '/screener': 'strategy',
  '/condition-screener': 'strategy',
  '/backtest': 'strategy',
  '/optimizer': 'strategy',
  '/trading': 'trading',
  '/review': 'trading',
  '/journal': 'trading',
  '/agent': 'research',
  '/data': 'system',
}

/** 扩展分析菜单 (/analysis/*) 归入研究; 未知内置项默认市场。 */
function navDomain(to: string): NavDomain {
  if (to.startsWith('/analysis/')) return 'research'
  return NAV_DOMAIN[to] ?? 'market'
}

const DOMAIN_ORDER: NavDomain[] = ['market', 'research', 'strategy', 'trading', 'system']

const DOMAIN_META: Record<NavDomain, { label: string; icon: typeof LineChart }> = {
  market: { label: '市场', icon: LineChart },
  research: { label: '研究', icon: FlaskConical },
  strategy: { label: '策略', icon: Crosshair },
  trading: { label: '交易', icon: Cable },
  system: { label: '系统', icon: Cpu },
}

const DOMAIN_EXPANDED_KEY = 'sidebar_domain_expanded'

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
      <div className="grid grid-cols-2 gap-1">
        {items.map(item => {
          const q = quoteBySymbol.get(item.symbol)
          const value = q?.last_price ?? q?.close
          const pct = q?.change_pct
          return (
            <NavLink
              key={item.symbol}
              to={`/indices?symbol=${encodeURIComponent(item.symbol)}`}
              className="block rounded-input bg-elevated/60 px-1.5 py-1 transition-colors hover:bg-elevated"
              title={`${item.name} ${item.symbol}`}
            >
              <div className="flex items-center justify-between gap-1">
                <span className="truncate text-[10px] text-secondary">{item.name}</span>
                <span className={`shrink-0 text-[10px] font-mono ${indexPctClass(pct)}`}>{fmtIndexPct(pct)}</span>
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

function pathTitle(pathname: string, navItems: { to: string; label: string }[]): string {
  if (pathname === '/settings' || pathname.startsWith('/settings/')) return '设置'
  if (pathname === '/guide' || pathname.startsWith('/guide/')) return '功能说明'
  const hit = navItems.find(n =>
    n.to === '/' ? pathname === '/' : pathname === n.to || pathname.startsWith(`${n.to}/`),
  )
  if (hit) return hit.label
  if (pathname.startsWith('/analysis/')) return '扩展分析'
  return '工作台'
}

export function Layout() {
  const reduceMotion = useReducedMotion()
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

  // ── 五领域折叠导航 ──
  const location = useLocation()
  const activeDomain = useMemo(() => {
    const pathname = location.pathname
    const hit = allNav.find(n =>
      n.to === '/' ? pathname === '/' : pathname === n.to || pathname.startsWith(`${n.to}/`),
    )
    if (hit) return navDomain(hit.to)
    if (pathname === '/settings' || pathname.startsWith('/settings/') || pathname === '/guide') return 'system'
    return navDomain(pathname)
  }, [location.pathname, allNav])

  const [expandedDomains, setExpandedDomains] = useState<Set<NavDomain>>(() => {
    try {
      const raw = localStorage.getItem(DOMAIN_EXPANDED_KEY)
      if (raw) {
        const parsed = JSON.parse(raw) as string[]
        const valid = parsed.filter((d): d is NavDomain => DOMAIN_ORDER.includes(d as NavDomain))
        if (valid.length) return new Set(valid)
      }
    } catch { /* ignore */ }
    return new Set<NavDomain>(['market'])
  })

  // 路由落在某领域时确保该领域展开
  useEffect(() => {
    setExpandedDomains(prev => {
      if (prev.has(activeDomain)) return prev
      const next = new Set(prev)
      next.add(activeDomain)
      try { localStorage.setItem(DOMAIN_EXPANDED_KEY, JSON.stringify([...next])) } catch { /* ignore */ }
      return next
    })
  }, [activeDomain])

  const toggleDomain = (domain: NavDomain) => {
    setExpandedDomains(prev => {
      const next = new Set(prev)
      if (next.has(domain)) next.delete(domain)
      else next.add(domain)
      try { localStorage.setItem(DOMAIN_EXPANDED_KEY, JSON.stringify([...next])) } catch { /* ignore */ }
      return next
    })
  }

  const domainGroups = useMemo(() => {
    const groups: Record<NavDomain, typeof visibleNavItems> = {
      market: [],
      research: [],
      strategy: [],
      trading: [],
      system: [],
    }
    for (const item of visibleNavItems) {
      groups[navDomain(item.to)].push(item)
    }
    return groups
  }, [visibleNavItems])

  const unreadAlerts = useUnreadAlerts()
  const monitorBadgeEnabled = (() => {
    try { return localStorage.getItem('monitor_badge_enabled') !== '0' } catch { return true }
  })()

  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const mobileNavTriggerRef = useRef<HTMLButtonElement>(null)
  const mobileNavDrawerRef = useRef<HTMLElement>(null)
  const mobileNavCloseRef = useRef<HTMLButtonElement>(null)


  // Close mobile drawer on route change
  useEffect(() => {
    setMobileNavOpen(false)
  }, [location.pathname])
  useEffect(() => {
    if (!mobileNavOpen) return

    const drawer = mobileNavDrawerRef.current
    mobileNavCloseRef.current?.focus()

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        setMobileNavOpen(false)
        return
      }
      if (event.key !== 'Tab' || !drawer) return

      const focusable = Array.from(
        drawer.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      )
      if (focusable.length === 0) {
        event.preventDefault()
        drawer.focus()
        return
      }

      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const active = document.activeElement
      if (event.shiftKey && (active === first || !drawer.contains(active))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (active === last || !drawer.contains(active))) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      mobileNavTriggerRef.current?.focus()
    }
  }, [mobileNavOpen])


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

  const currentTitle = pathTitle(location.pathname, visibleNavItems)
  const domainLabel = DOMAIN_META[activeDomain]?.label ?? ''

  const marketStatusLabel = isTrading ? '交易中' : '休市'
  const marketStatusState = isTrading ? 'live' : 'idle'

  let dataStatusLabel = '行情关闭'
  let dataStatusState: 'live' | 'warn' | 'error' | 'idle' | 'ready' | 'off' = 'idle'
  if (isNoneTier) {
    dataStatusLabel = '无实时源'
    dataStatusState = 'idle'
  } else if (!realtimeEnabled) {
    dataStatusLabel = '行情关闭'
    dataStatusState = 'off'
  } else if (isQuoteLive) {
    dataStatusLabel = `实时 · ${realtimeModeLabel}`
    dataStatusState = 'live'
  } else if (isQuoteReady) {
    dataStatusLabel = '快照可用'
    dataStatusState = 'warn'
  } else if (!isTrading) {
    dataStatusLabel = '非交易时段'
    dataStatusState = 'warn'
  } else if (quoteDataState === 'error') {
    dataStatusLabel = quoteDataStateText(quoteDataState) || '行情异常'
    dataStatusState = 'error'
  } else if (quoteDataState) {
    dataStatusLabel = quoteDataStateText(quoteDataState) || '等待数据'
    dataStatusState = 'warn'
  }

  const renderNavItem = ({ to, label, icon: Icon, ...rest }: (typeof visibleNavItems)[number]) => (
    <NavLink
      key={to}
      to={to}
      title={'title' in rest ? rest.title : undefined}
      aria-label={'title' in rest ? rest.title : undefined}
      className={({ isActive }) =>
        cn(
          'flex items-center gap-2 rounded-btn px-2 py-1.5 text-[13px] transition-colors duration-fast ease-smooth',
          isActive
            ? 'bg-elevated font-medium text-foreground'
            : 'text-secondary hover:bg-elevated/70 hover:text-foreground',
        )
      }
    >
      {({ isActive }) => (
        <>
          <Icon className="h-3.5 w-3.5 shrink-0 opacity-80" />
          <span className="min-w-0 flex-1 truncate">{label}</span>
          {(to === '/stock-analysis' || to === '/review') && (
            <span className="inline-flex shrink-0 items-center rounded border border-warning/30 bg-warning/10 px-1 py-px text-[9px] font-semibold uppercase tracking-wider text-warning">
              Beta
            </span>
          )}
          {to === '/data' && isDataSyncing && (
            <Loader2 className="h-3 w-3 shrink-0 animate-spin text-accent" />
          )}
          {to === '/data' && !isDataSyncing && dataSyncJustDone && (
            <CheckCircle2 className="h-3 w-3 shrink-0 animate-pulse text-success" />
          )}
          {to === '/monitor' && <MonitorBadge active={isActive} />}
        </>
      )}
    </NavLink>
  )

  const sidebarBody = (
    <>
      <div className="shrink-0 border-b border-border px-3 py-3">
        <div className="flex items-center gap-2.5">
          <div className="flex h-7 w-7 items-center justify-center rounded-btn border border-border bg-elevated font-mono text-[11px] font-bold tracking-wider text-accent">
            QR
          </div>
          <div className="min-w-0">
            <div className="truncate text-[13px] font-semibold tracking-tight text-foreground">
              Quant Research
            </div>
            <div className="truncate text-[10px] uppercase tracking-[0.14em] text-muted">
              Workbench
            </div>
          </div>
        </div>
      </div>

      <nav className="min-h-0 flex-1 space-y-1 overflow-y-auto px-2 py-2">
        {DOMAIN_ORDER.map((domain) => {
          const meta = DOMAIN_META[domain]
          const items = domainGroups[domain]
          if (items.length === 0) return null
          const expanded = expandedDomains.has(domain)
          const DomainIcon = meta.icon
          const isActiveDomain = activeDomain === domain
          const showUnreadDot =
            domain === 'market' &&
            !expanded &&
            unreadAlerts > 0 &&
            monitorBadgeEnabled &&
            items.some(i => i.to === '/monitor')

          return (
            <div key={domain} className="min-w-0">
              <button
                type="button"
                onClick={() => toggleDomain(domain)}
                className={cn(
                  'flex w-full items-center gap-2 rounded-btn px-2 py-1.5 text-left transition-colors duration-fast ease-smooth',
                  isActiveDomain
                    ? 'text-foreground'
                    : 'text-secondary hover:bg-elevated/50 hover:text-foreground',
                )}
                aria-expanded={expanded}
              >
                <DomainIcon className={cn('h-3.5 w-3.5 shrink-0', isActiveDomain && 'text-accent')} />
                <span className={cn('min-w-0 flex-1 text-[12px] font-medium tracking-wide', isActiveDomain && 'text-foreground')}>
                  {meta.label}
                </span>
                {showUnreadDot && (
                  <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-danger animate-pulse" />
                )}
                <ChevronDown
                  className={cn(
                    'h-3 w-3 shrink-0 text-muted transition-transform duration-fast ease-smooth',
                    expanded ? 'rotate-0' : '-rotate-90',
                  )}
                />
              </button>
              <AnimatePresence initial={false}>
                {expanded && (
                  <motion.div
                    key={`${domain}-items`}
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: 'auto', opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    transition={{ duration: reduceMotion ? 0 : 0.18, ease: [0.16, 1, 0.3, 1] }}
                    className="overflow-hidden"
                  >
                    <div className="mb-1 ml-1 space-y-0.5 border-l border-border/70 pl-1.5">
                      {items.map(renderNavItem)}
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          )
        })}
      </nav>

      {/* 全局行情开关 */}
      <div className="shrink-0 border-t border-border px-2.5 py-2">
        {isNoneTier ? (
          <div>
            <div className="flex items-center justify-between gap-2">
              <span className="truncate text-xs text-secondary">实时行情</span>
              <span className="rounded bg-accent/10 px-1.5 py-0.5 text-[10px] font-medium text-accent/80">
                Free+
              </span>
            </div>
            <div className="mt-1 text-[10px] leading-snug text-muted">
              当前数据源未提供实时行情能力
            </div>
          </div>
        ) : (
          <div className="flex items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-1.5">
              <span
                className={cn(
                  'status-dot',
                  realtimeEnabled && isQuoteLive && 'animate-pulse',
                )}
                data-state={
                  realtimeEnabled && isQuoteLive
                    ? 'live'
                    : realtimeEnabled
                      ? 'warn'
                      : 'off'
                }
              />
              <span className="truncate text-xs text-secondary">
                实时 · {realtimeModeLabel}
              </span>
              <button
                type="button"
                onClick={() => navigate('/settings?tab=monitoring')}
                className="shrink-0 text-secondary transition-colors hover:text-foreground"
                title="实时监控设置"
              >
                <Settings className="h-3 w-3" />
              </button>
            </div>
            <button
              type="button"
              onClick={() => handleToggle(!realtimeEnabled)}
              disabled={toggleQuote.isPending}
              className={cn(
                'relative inline-flex h-4 w-7 shrink-0 items-center rounded-full transition-colors duration-base',
                realtimeEnabled ? 'bg-accent' : 'bg-elevated',
                toggleQuote.isPending ? 'opacity-50' : 'cursor-pointer',
              )}
              aria-pressed={realtimeEnabled}
              aria-label="切换实时行情"
            >
              <span
                className={cn(
                  'inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform duration-base',
                  realtimeEnabled ? 'translate-x-[14px]' : 'translate-x-0.5',
                )}
              />
            </button>
          </div>
        )}

        {realtimeEnabled && !isNoneTier && (
          <div className="mt-1.5 text-[10px] leading-snug">
            {isQuoteLive ? (
              <span className="text-accent">行情运行中</span>
            ) : isQuoteReady ? (
              <span className="text-warning/80">轮询中，本地快照可用</span>
            ) : !isTrading ? (
              <span className="text-warning/80">非交易时段，将在交易时间自动开启</span>
            ) : quoteDataState ? (
              <span className={quoteDataState === 'error' ? 'text-danger/80' : 'text-warning/80'}>
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

      <div className="shrink-0 space-y-0.5 border-t border-border px-2 py-2">
        <NavLink
          to="/guide"
          className={({ isActive }) =>
            cn(
              'flex items-center gap-2 rounded-btn px-2 py-1.5 text-[13px] transition-colors duration-fast ease-smooth',
              isActive
                ? 'bg-elevated font-medium text-foreground'
                : 'text-secondary hover:bg-elevated/70 hover:text-foreground',
            )
          }
        >
          <BookOpen className="h-3.5 w-3.5 shrink-0" />
          <span>功能说明</span>
        </NavLink>
        <NavLink
          to="/settings"
          className={({ isActive }) =>
            cn(
              'flex items-center justify-between gap-2 rounded-btn px-2 py-1.5 text-[13px] transition-colors duration-fast ease-smooth',
              isActive
                ? 'bg-elevated font-medium text-foreground'
                : 'text-secondary hover:bg-elevated/70 hover:text-foreground',
            )
          }
        >
          <span className="flex items-center gap-2">
            <Settings className="h-3.5 w-3.5 shrink-0" />
            <span>设置</span>
          </span>
          <span className="select-none font-mono text-[10px] text-muted/60">
            {version ?? ''}
          </span>
        </NavLink>
      </div>
    </>
  )

  return (
    <div className="flex h-screen min-h-0 w-full overflow-hidden bg-base text-foreground">
      {/* Desktop sidebar */}
      <aside className="hidden h-full min-h-0 w-[13.5rem] shrink-0 flex-col border-r border-border bg-surface md:flex">
        {sidebarBody}
      </aside>

      {/* Mobile drawer */}
      <AnimatePresence>
        {mobileNavOpen && (
          <>
            <motion.div
              aria-hidden="true"
              className="fixed inset-0 z-40 bg-base/60 md:hidden"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: reduceMotion ? 0 : 0.15 }}
              onClick={() => setMobileNavOpen(false)}
            />
            <motion.aside
              ref={mobileNavDrawerRef}
              role="dialog"
              aria-modal="true"
              aria-labelledby="mobile-navigation-title"
              tabIndex={-1}
              className="fixed inset-y-0 left-0 z-50 flex h-full w-[15.5rem] max-w-[85vw] flex-col border-r border-border bg-surface md:hidden"
              initial={{ x: -20, opacity: 0 }}
              animate={{ x: 0, opacity: 1 }}
              exit={{ x: -12, opacity: 0 }}
              transition={{ duration: reduceMotion ? 0 : 0.18, ease: [0.16, 1, 0.3, 1] }}
            >
              <div className="flex items-center justify-between border-b border-border px-3 py-2">
                <span id="mobile-navigation-title" className="text-xs font-medium text-secondary">导航</span>
                <button
                  ref={mobileNavCloseRef}
                  type="button"
                  className="btn-ghost h-8 w-8 px-0"
                  onClick={() => setMobileNavOpen(false)}
                  aria-label="关闭"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              {sidebarBody}
            </motion.aside>
          </>
        )}
      </AnimatePresence>

      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        {/* Context bar */}
        <header className="flex h-10 shrink-0 items-center gap-2 border-b border-border bg-surface px-2 sm:px-3">
          <button
            ref={mobileNavTriggerRef}
            type="button"
            className="btn-ghost h-8 w-8 px-0 md:hidden"
            onClick={() => setMobileNavOpen(true)}
            aria-label="打开导航"
          >
            <Menu className="h-4 w-4" />
          </button>

          <div className="flex min-w-0 flex-1 items-center gap-2">
            <span className="hidden shrink-0 text-[11px] uppercase tracking-[0.12em] text-muted sm:inline">
              {domainLabel}
            </span>
            <span className="hidden text-border sm:inline" aria-hidden>
              /
            </span>
            <span className="truncate text-[13px] font-medium text-foreground">
              {currentTitle}
            </span>
          </div>

          <div className="flex shrink-0 items-center gap-1.5 sm:gap-2">
            <div
              className="hidden items-center gap-1.5 rounded-input border border-border bg-elevated/50 px-2 py-1 sm:flex"
              title="市场状态"
            >
              <span className="status-dot" data-state={marketStatusState} />
              <span className="text-[11px] text-secondary">{marketStatusLabel}</span>
            </div>
            <div
              className="flex max-w-[9.5rem] items-center gap-1.5 truncate rounded-input border border-border bg-elevated/50 px-2 py-1 sm:max-w-none"
              title={dataStatusLabel}
            >
              <span className="status-dot" data-state={dataStatusState} />
              <span className="truncate text-[11px] text-secondary">{dataStatusLabel}</span>
            </div>
            <ThemeToggle />
          </div>
        </header>

        <motion.main
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: reduceMotion ? 0 : 0.2, ease: [0.16, 1, 0.3, 1] }}
          className="min-h-0 min-w-0 flex-1 overflow-auto scrollbar-gutter-stable"
        >
          <Outlet />
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
