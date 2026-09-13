import { useEffect, useLayoutEffect, useMemo, useRef, useState, Suspense } from 'react'
import { NavLink, Outlet, useNavigate, useLocation } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { useQuoteStream, useQuoteStreamStatus } from '@/lib/useQuoteStream'
import { ToastContainer, toast } from '@/components/Toast'
import { AlertToastContainer } from '@/components/AlertToast'
import { AiAnalysisHost } from '@/components/financials/AiAnalysisHost'
import { AiReportBubble } from '@/components/financials/AiReportBubble'
import { StockAnalysisHost } from '@/components/stock-analysis/StockAnalysisHost'
import { StockAnalysisBubble } from '@/components/stock-analysis/StockAnalysisBubble'
import {
  useCapabilityMatrix,
  useSettings,
  usePreferences,
  useQuoteStatus,
  useVersion,
} from '@/lib/useSharedQueries'
import {
  useToggleRealtimeQuotes,
} from '@/lib/useSharedMutations'
import { QK } from '@/lib/queryKeys'
import {
  Siren,
  Star,
  ScanSearch,
  History,
  Sigma,
  FileText,
  Settings,
  DatabaseZap,
  Database,
  Loader2,
  LayoutDashboard,
  Tags,
  TrendingUp,
  Flame,
  BarChart3,
  Gauge,
  Sparkles,
  Layers2,
  Layers3,
  Zap,
  Landmark,
  RadioTower,
  CheckCircle2,
  BookOpenCheck,
  ChevronRight,
  ChevronDown,
  Sun,
  Moon,
  X,
  WifiOff,
  Menu,
  PanelLeft,
  PanelLeftClose,
  PanelLeftOpen,
} from 'lucide-react'
import { Logo } from './Logo'
import { api, type CapabilityMatrix, type IndexQuote } from '@/lib/api'
import { cn } from '@/lib/cn'
import { useIsDesktop } from '@/lib/useMediaQuery'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'
import { resolveWatchlistGroupColor } from '@/lib/watchlist-group-colors'
import { computeGroupPcts, groupPctColor, groupPctTitle } from '@/lib/watchlistGroupStats'
import { fmtPct } from '@/lib/format'
import { findDataSource } from '@/lib/dataSources'
import { toggleTheme, useTheme } from '@/lib/theme'
import { setCurrentTotal as setAlertTotal, useUnreadAlerts } from '@/lib/monitorBadge'
import { ExtensionSlot } from '@/extensions/ExtensionSlot'
import { getFrontendExtensionNavigation } from '@/extensions/registry'

// 品牌色 — 只用于 logo / brand 区域,不影响功能语义色
const BRAND = '#8B5CF6'

// 核心四只指数 — 与后端 index_const.py 单一权威对齐 (前端展示层固定清单)
export const CORE_INDEXES = [
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
  { to: '/factors',    label: '因子', icon: Sigma },
  { to: '/backtest',   label: '回测', icon: History },
  { to: '/stock-analysis',    label: '个股分析', icon: TrendingUp },
  { to: '/limit-ladder', label: '连板梯队', icon: Flame },
  { to: '/concept-analysis', label: '概念分析', icon: Layers3 },
  { to: '/industry-analysis', label: '行业分析', icon: Landmark },
  { to: '/financials', label: '财务分析', icon: FileText },
  { to: '/monitor', label: '监控中心', icon: RadioTower },
  { to: '/regime', label: '市场环境', icon: Gauge },
  { to: '/abnormal', label: '异动监控', icon: Siren },
  { to: '/lots',       label: '持仓提醒', icon: Layers2 },
  { to: '/signals',    label: '信号库',   icon: Zap },
  { to: '/review',      label: '复盘',   icon: BookOpenCheck },
  { to: '/indices', label: '指数', icon: BarChart3 },
  { to: '/data',       label: '数据',   icon: Database },
] as const

/** 亮/暗主题切换 — 状态存 localStorage, 生效见 lib/theme.ts */
function ThemeToggle() {
  const theme = useTheme()
  const dark = theme === 'dark'
  return (
    <button
      onClick={() => toggleTheme()}
      className="flex items-center justify-center rounded-btn p-2 text-foreground/80 transition-colors duration-150 ease-smooth hover:bg-elevated hover:text-foreground cursor-pointer"
      title={dark ? '切换到亮色模式' : '切换到暗色模式'}
    >
      {dark ? <Sun className="h-4 w-4 shrink-0" /> : <Moon className="h-4 w-4 shrink-0" />}
    </button>
  )
}

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

function SidebarIndexQuotes({ rows, items }: { rows: IndexQuote[] | undefined; items: readonly CoreIndex[] }) {
  if (items.length === 0) return null
  const quoteBySymbol = new Map((rows ?? []).map(q => [q.symbol, q]))
  return (
    <div className="mt-2 grid grid-cols-2 gap-1.5 border-t border-border/60 pt-2">
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
            <div className={`mt-0.5 truncate font-mono text-[10px] ${indexPctClass(pct)}`}>
              {fmtIndexValue(value)}
            </div>
          </NavLink>
        )
      })}
    </div>
  )
}

// ===== 数据源能力健康卡 =====
// 能力路由架构下的侧栏状态: 不再展示「主数据源 + TickFlow 档位」(单源时代遗留 —
// 五个能力各自路由, 拿日K的源代表全局是随意的), 改为回答「各能力当前是否都有源在供」。
// 档位/订阅信息归设置页 TickFlow 介绍卡 (档位词仅出现在 TickFlow 专属界面的设计规则)。
// 单能力方格: 可用=绿 / 日K缺失=红 / 其他缺失=琥珀 (与悬浮卡中同色, 一眼对应)
function capSquareCls(c: { id: string; usable: boolean }) {
  return c.usable ? 'bg-accent' : c.id === 'daily' ? 'bg-danger' : 'bg-warning/80'
}

function DataSourceHealthBadge({ matrix }: { matrix: CapabilityMatrix | undefined }) {
  const caps = matrix?.capabilities ?? []
  const loading = caps.length === 0
  const usableCount = caps.filter(c => c.usable).length
  const down = caps.filter(c => !c.usable)
  // 日K是核心能力 (其他一切派生于它): 挂了用危险色; 一般缺项琥珀; 全可用绿
  const level = loading
    ? 'loading'
    : down.length === 0 ? 'ok' : down.some(c => c.id === 'daily') ? 'danger' : 'warn'
  const countCls = level === 'ok' ? 'text-accent/80'
    : level === 'danger' ? 'text-danger'
    : level === 'warn' ? 'text-warning'
    : 'text-muted'

  // 悬浮卡: 侧栏 aside 是 overflow-hidden, 用 fixed 定位逃逸裁剪 (坐标取自徽标实时位置)。
  // 徽标靠近屏幕顶部时居中定位会把卡片上半截推出视口 → 渲染后按实际高度钳制进视口。
  const linkRef = useRef<HTMLAnchorElement>(null)
  const popRef = useRef<HTMLDivElement>(null)
  const closeTimer = useRef<number | undefined>(undefined)
  const [popPos, setPopPos] = useState<{ left: number; top: number } | null>(null)
  const openPop = () => {
    window.clearTimeout(closeTimer.current)
    const rect = linkRef.current?.getBoundingClientRect()
    if (rect) setPopPos({ left: rect.right, top: rect.top + rect.height / 2 })
  }
  const closePop = () => {
    closeTimer.current = window.setTimeout(() => setPopPos(null), 80)
  }
  useEffect(() => () => window.clearTimeout(closeTimer.current), [])
  useLayoutEffect(() => {
    if (!popPos || !popRef.current) return
    const h = popRef.current.offsetHeight
    const margin = 8
    const minCenter = margin + h / 2
    const maxCenter = window.innerHeight - margin - h / 2
    const clamped = Math.min(maxCenter, Math.max(minCenter, popPos.top))
    if (clamped !== popPos.top) setPopPos({ ...popPos, top: clamped })
  }, [popPos])

  return (
    <>
      <NavLink
        ref={linkRef}
        to="/settings?tab=data-sources"
        aria-label={`数据源能力 ${usableCount}/${caps.length || 5} 可用, 点击前往数据源配置`}
        onMouseEnter={openPop}
        onMouseLeave={closePop}
        onFocus={openPop}
        onBlur={closePop}
        onKeyDown={e => { if (e.key === 'Escape') setPopPos(null) }}
        className="group relative flex items-center gap-2 overflow-hidden rounded-md py-1.5 pl-2.5 pr-2 transition-colors duration-150 hover:bg-elevated/70"
      >
        <span className="pointer-events-none absolute inset-y-1.5 left-0 w-[2px] rounded-full bg-accent/50 transition-colors group-hover:bg-accent" />
        <DatabaseZap className="h-3.5 w-3.5 shrink-0 text-muted group-hover:text-accent transition-colors" />
        {/* 能力方格 (按注册顺序: 实时/日K/分钟/除权/财务), 与悬浮卡逐格同色对应 */}
        <span className="flex items-center gap-1 shrink-0">
          {loading
            ? Array.from({ length: 5 }, (_, i) => (
                <span key={i} className="h-2 w-2 rounded-[2px] bg-muted animate-pulse" />
              ))
            : caps.map(c => (
                <span key={c.id} className={`h-2 w-2 rounded-[2px] ${capSquareCls(c)}`} />
              ))}
        </span>
        {!loading && (
          <span className={`ml-auto text-[10px] font-mono font-bold leading-none shrink-0 ${countCls}`}>
            {usableCount}/{caps.length}
          </span>
        )}
      </NavLink>
      {popPos && (
        <div
          ref={popRef}
          className="fixed z-50 -translate-y-1/2 pl-3"
          style={{ left: popPos.left, top: popPos.top }}
          onMouseEnter={() => window.clearTimeout(closeTimer.current)}
          onMouseLeave={closePop}
        >
          <motion.div
            initial={{ opacity: 0, x: -6 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
            className="w-64 rounded-md border border-border bg-surface py-2.5 pl-3 pr-3.5 shadow-2xl shadow-black/40"
          >
            <div className="mb-2 flex items-center justify-between">
              <span className="flex items-center gap-1.5 text-xs font-medium text-foreground">
                <DatabaseZap className="h-3.5 w-3.5 text-accent" />
                数据源能力
              </span>
              <span className={`text-[10px] font-mono font-bold ${countCls}`}>
                {loading ? '获取中…' : `${usableCount}/${caps.length} 可用`}
              </span>
            </div>
            <div className="space-y-1.5 border-t border-border/60 pt-2">
              {loading ? (
                <div className="py-0.5 text-[11px] text-muted">正在获取能力路由状态…</div>
              ) : caps.map(c => (
                <div key={c.id} className="flex min-w-0 items-center gap-2">
                  <span className={`h-2 w-2 shrink-0 rounded-[2px] ${capSquareCls(c)}`} />
                  <span className="shrink-0 text-xs font-medium text-secondary">{c.label}</span>
                  <span className="ml-auto flex min-w-0 shrink items-center gap-1.5">
                    {c.usable ? (
                      <>
                        <span className="truncate text-[11px] text-muted">{c.effective_display}</span>
                        <CheckCircle2 className="h-3 w-3 shrink-0 text-accent" />
                      </>
                    ) : (
                      <span className="text-[11px] text-muted/70">未接入</span>
                    )}
                  </span>
                </div>
              ))}
            </div>
            {/* 分时有分钟K功能替身 (intraday_monitor_support 三路可达), 不单独占能力格, 在此备注 */}
            <div className="mt-1.5 text-[10px] leading-relaxed text-muted/70">
              分时信号监控可由分钟 K 数据驱动，不单独设能力格
            </div>
            <div className="mt-2 flex items-center gap-1 border-t border-border/60 pt-1.5 text-[10px] text-muted">
              点击前往数据源配置
              <ChevronRight className="h-3 w-3" />
            </div>
          </motion.div>
        </div>
      )}
    </>
  )
}

function AIConfigBadge({ configured, model }: { configured?: boolean; model?: string }) {
  const descText = configured ? (model || '已接入模型') : '接入策略生成模型'
  return (
    <NavLink
      to="/settings?tab=ai"
      className="group relative flex items-center gap-2 overflow-hidden rounded-md py-1.5 pl-2.5 pr-2 transition-colors duration-150 hover:bg-elevated/70"
      title={`AI 配置 — ${descText}`}
    >
      <span className="pointer-events-none absolute inset-y-1.5 left-0 w-[2px] rounded-full bg-purple-400/50 transition-colors group-hover:bg-purple-400" />
      <Sparkles className="h-3.5 w-3.5 shrink-0 text-muted group-hover:text-purple-400 transition-colors" />
      {configured ? (
        <span className="truncate text-[11px] font-medium text-secondary group-hover:text-foreground transition-colors">
          {model || '已接入模型'}
        </span>
      ) : (
        <>
          <span className="text-[11px] text-secondary group-hover:text-foreground transition-colors">AI 配置</span>
          <span className="ml-auto text-[11px] font-mono leading-none text-muted">未配置</span>
        </>
      )}
      <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${configured ? 'bg-bear' : 'bg-warning'}`} />
    </NavLink>
  )
}

// 侧边栏桌面三态: expanded(14rem) / rail(3.5rem 图标条) / hidden(0 + 左缘悬浮按钮)。
// 移动端 (<768px) 不参与三态 — aside 以抽屉呈现 (见 Layout 内 drawerOpen)。
type NavState = 'expanded' | 'rail' | 'hidden'

export function Layout() {
  // ===== 共享 hooks (替代内联 useQuery) =====
  const { data: settingsState } = useSettings()
  const { data: matrix } = useCapabilityMatrix()
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
  const { data: analysisMenus } = useQuery({
    queryKey: QK.analysisMenus,
    queryFn: api.analysisMenus,
  })

  // 自选分组 — 仅当用户开启「显示在侧边栏」时拉取
  const groupsInNav = prefs?.watchlist_groups_in_nav ?? false
  const location = useLocation()
  const { data: watchlistGroupsData } = useQuery({
    queryKey: QK.watchlistGroups,
    queryFn: api.watchlistGroups,
    enabled: groupsInNav,
    staleTime: 60_000,
  })
  const watchlistGroups = watchlistGroupsData?.groups ?? []
  // 自选二级菜单展开状态 — 默认当前在自选页时展开
  const [watchlistNavExpanded, setWatchlistNavExpanded] = useState(location.pathname === '/watchlist')

  // 侧边栏三态 — expanded(14rem) / rail(3.5rem 图标条) / hidden(0, 左缘悬浮按钮唤出)。
  // 仅桌面 (≥768px) 参与三态; 移动端 aside 以抽屉呈现, 由 drawerOpen 控制, 恒渲染完整形态。
  // 持久化到 localStorage; 迁移旧两态键 tf-nav-collapsed (收起 → 图标条)。
  const [navState, setNavState] = useState<NavState>(() => {
    try {
      const v = localStorage.getItem('tf-nav-state')
      if (v === 'expanded' || v === 'rail' || v === 'hidden') return v
      return localStorage.getItem('tf-nav-collapsed') === '1' ? 'rail' : 'expanded'
    } catch { return 'expanded' }
  })
  const [drawerOpen, setDrawerOpen] = useState(false)
  const isDesktop = useIsDesktop()
  // 桌面 hidden 态的左缘悬浮按钮: hover 1s 以 overlay 预览 (不挤压主区), 点击固定展开 (push)
  const [overlayPreview, setOverlayPreview] = useState(false)
  const overlayTimer = useRef<number | undefined>(undefined)
  const setNavStatePersist = (s: NavState) => {
    setNavState(s)
    try { localStorage.setItem('tf-nav-state', s) } catch {}
  }
  // 图标条形态仅桌面 rail 态成立 (移动端抽屉与 overlay 预览恒为完整形态)
  const railMode = isDesktop && navState === 'rail'
  // 路由跳转/切回桌面时关抽屉; ESC 同样关闭
  useEffect(() => { setDrawerOpen(false) }, [location.pathname, isDesktop])
  useEffect(() => {
    if (!drawerOpen) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setDrawerOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [drawerOpen])

  // 分组等权平均涨跌幅 — 复用 watchlist/enriched 查询缓存(与自选页同 key,
  // 盘中随 SSE 刷新)。可见性门控: 子菜单实际可见(桌面展开 + 二级菜单展开, 或
  // 移动端抽屉打开) 时才拉取, 收起/隐藏/抽屉关闭下不为隐藏 UI 发请求。
  const sidebarFullyVisible = navState === 'expanded' && (isDesktop || drawerOpen)
  const navGroupPctVisible = groupsInNav && sidebarFullyVisible && watchlistNavExpanded
  const { data: navWatchlist } = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
    enabled: navGroupPctVisible,
    staleTime: 60_000,
  })
  const { data: navEnriched } = useQuery({
    queryKey: QK.watchlistEnriched(undefined),
    queryFn: () => api.watchlistEnriched(),
    enabled: navGroupPctVisible,
    staleTime: 60_000,
  })
  const navGroupPcts = useMemo(
    () => computeGroupPcts(
      navWatchlist?.symbols ?? [],
      new Map((navEnriched?.rows ?? []).map((r: any) => [r.symbol as string, r])),
    ),
    [navWatchlist, navEnriched],
  )

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
  // 自选实时模式限制提示: 可手动关闭, 不持久化 (刷新后恢复显示)
  const [dismissFreeHint, setDismissFreeHint] = useState(false)
  // 开启实时行情时若存在排队中的挖掘任务 → 确认弹窗 (实时落盘会让排队任务开跑即失败)
  const [miningQueuedWarning, setMiningQueuedWarning] = useState<number | null>(null)
  const miningWarnBackdrop = useDialogBackdrop(() => setMiningQueuedWarning(null))
  // 三态循环切换 (仅桌面): 展开 → 图标条 → 隐藏 → 展开
  const toggleNavCollapsed = () => {
    setNavStatePersist(navState === 'expanded' ? 'rail' : navState === 'rail' ? 'hidden' : 'expanded')
  }
  // 指数条: 固定核心四只 (产品契约, 不再可配置), 常驻显示
  const sidebarIndexes = CORE_INDEXES
  const { data: sidebarIndexQuotes } = useQuery({
    queryKey: [...QK.indexQuotes, 'sidebar', 'core'] as const,
    queryFn: () => api.indexQuotes(sidebarIndexes.map(p => p.symbol)),
    enabled: sidebarIndexes.length > 0,
    placeholderData: (prev) => prev,
  })

  // SSE: 行情更新时自动刷新相关 queries + 告警通知
  useQuoteStream(realtimeEnabled, prefs?.sse_refresh_pages)
  // 实时 SSE 连接状态 — 断开时底部显示提示, 提示可能漏策略告警
  const streamStatus = useQuoteStreamStatus()

  const toggleQuote = useToggleRealtimeQuotes()
  const isRunning = quoteStatus?.running ?? false
  const isTrading = quoteStatus?.is_trading_hours ?? false
  // 管道/数据修正运行期间实时行情被临时暂停 — 此时禁止开启
  const isPaused = quoteStatus?.paused ?? false
  // 实时模式以 quote_status 为准 (数据源无关): none=不可用 / watchlist=自选实时 / full_market=全市场
  const quoteMode = quoteStatus?.mode ?? 'none'
  const realtimeUnavailable = quoteMode === 'none'
  const isWatchlistMode = quoteMode === 'watchlist'
  const realtimeModeLabel = isWatchlistMode ? '自选股' : '全市场'
  // 当前实时行情数据源名称 (插件/自定义源显示源名, tickflow 不显示)
  const realtimeProvider = prefs?.realtime_data_provider
  const realtimeProviderName = realtimeProvider && realtimeProvider !== 'tickflow'
    ? (findDataSource(dataSources, realtimeProvider)?.display_name || realtimeProvider)
    : null
  const realtimeToggleDisabled = toggleQuote.isPending || isPaused
  const realtimeActive = realtimeEnabled && isRunning && isTrading
  const realtimeStatusLabel = toggleQuote.isPending
    ? '正在更新'
    : isPaused
      ? '同步期间暂停'
      : realtimeActive
        ? '运行中'
        : realtimeEnabled
          ? (isTrading ? '正在连接' : '等待交易时段')
          : '已关闭'
  const realtimeStatusClass = realtimeActive
    ? 'text-accent'
    : realtimeEnabled || isPaused
      ? 'text-warning/80'
      : 'text-muted'
  const realtimeIndicatorClass = realtimeActive
    ? 'bg-accent animate-pulse'
    : realtimeEnabled || isPaused
      ? 'bg-warning/70'
      : 'bg-muted'
  const realtimeToggleTitle = isPaused
    ? '数据同步运行中，实时行情已临时暂停'
    : toggleQuote.isPending
      ? '正在更新实时行情设置'
      : realtimeEnabled
        ? '关闭实时行情'
        : '开启实时行情'

  // 轮询触发记录总数 → 更新监控中心徽标 (每 15 秒; 后台标签页由 SSE 事件驱动, 不轮询)
  const alertsTotalQuery = useQuery({
    queryKey: ['alerts-total'],
    queryFn: () => api.alertsList({ days: 7, limit: 1 }),
    refetchInterval: 15000,
    select: (data) => data.total,
  })
  // 只在拿到真实总数时同步徽标 (避免 data=undefined 时传 0 重置 lastSeen)
  const alertsTotal = alertsTotalQuery.data
  useEffect(() => {
    if (alertsTotal != null) setAlertTotal(alertsTotal)
  }, [alertsTotal])

  // 合并内置页面 + 可见的扩展分析菜单
  type NavItem = { to: string; label: string; icon: typeof Gauge; badge?: string }
  const analysisNav: NavItem[] = (analysisMenus?.items ?? [])
    .filter(m => m.visible)
    .map(m => ({ to: `/analysis/${m.id}`, label: m.label, icon: m.icon === 'tags' ? Tags : BarChart3 }))
  const extensionNav: NavItem[] = getFrontendExtensionNavigation().map(item => ({
    to: item.route.path,
    label: item.label,
    icon: item.icon,
    badge: item.badge,
  }))

  const allNav: NavItem[] = [...nav, ...analysisNav, ...extensionNav]
  const savedOrder = prefs?.nav_order ?? []

  const navItems = savedOrder.length > 0
    ? (() => {
        const byTo = new Map(allNav.map(n => [n.to, n]))
        const ordered = (savedOrder
          .map(id => byTo.get(id) ?? byTo.get(`/analysis/${id}`))
          .filter(Boolean)) as typeof allNav
        const seen = new Set(ordered.map(n => n.to))
        const merged = [...ordered]
        for (const item of allNav) {
          if (seen.has(item.to)) continue
          // 未保存过排序的新条目: 内置页插回默认位置(排在已保存的默认前驱之后),
          // 分析/扩展菜单仍追加到末尾
          const defaultIndex = nav.findIndex(n => n.to === item.to)
          let anchor = -1
          if (defaultIndex > 0) {
            for (let i = defaultIndex - 1; i >= 0 && anchor < 0; i -= 1) {
              anchor = merged.findIndex(n => n.to === nav[i].to)
            }
          }
          if (anchor >= 0) merged.splice(anchor + 1, 0, item)
          else if (defaultIndex >= 0) merged.unshift(item)
          else merged.push(item)
        }
        return merged
      })()
    : allNav

  const hiddenIds = new Set(prefs?.nav_hidden ?? [])
  const visibleNavItems = navItems.filter(n => !hiddenIds.has(n.to) && !hiddenIds.has(n.to.replace(/^\/analysis\//, '')))

  const doEnableRealtime = async () => {
    await toggleQuote.mutateAsync(true)
    // 仅在交易时段立即获取一次行情
    if (isTrading) {
      api.intradayRefresh().catch(() => {})
    }
  }

  const handleToggle = async (enabled: boolean) => {
    // 开启时重新校验实时权限 (以 quote_status 的数据源无关判定为准)
    if (!enabled) {
      await toggleQuote.mutateAsync(false)
      return
    }
    const fresh = await qc.fetchQuery({
      queryKey: QK.quoteStatus,
      queryFn: api.quoteStatus,
    })
    if (!fresh.realtime_allowed) {
      toast('当前数据源无实时行情能力, 请先配置数据源', 'error')
      return
    }
    // 有排队中的挖掘任务时确认: 实时落盘会让排队任务开跑即失败
    // (data generation changed); 运行中的任务会自动跟随新数据, 不受影响。
    try {
      const runs = await qc.fetchQuery({
        queryKey: QK.miningRuns,
        queryFn: api.miningRuns,
        staleTime: 5_000,
      })
      const queued = (runs?.items ?? []).filter(r => r.status === 'queued').length
      if (queued > 0) {
        setMiningQueuedWarning(queued)
        return
      }
    } catch {
      // 挖掘运行历史查询失败不阻塞开关实时行情
    }
    await doEnableRealtime()
  }

  return (
    <div
      className="h-screen grid bg-base text-foreground overflow-hidden transition-[grid-template-columns] duration-200 ease-smooth"
      style={{ gridTemplateColumns: isDesktop && !overlayPreview ? (navState === 'expanded' ? '14rem 1fr' : navState === 'rail' ? '3.5rem 1fr' : '0 1fr') : '1fr' }}
    >
      {/* 移动端抽屉遮罩 */}
      {!isDesktop && drawerOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/50"
          onClick={() => setDrawerOpen(false)}
          aria-hidden="true"
        />
      )}
      {/* 移动端汉堡按钮 / 桌面 hidden 态左缘悬浮按钮 (hover 1s overlay 预览, 点击固定展开) */}
      {(!isDesktop || navState === 'hidden') && !overlayPreview && (
        <button
          onClick={() => {
            window.clearTimeout(overlayTimer.current)
            setOverlayPreview(false)
            if (isDesktop) setNavStatePersist('expanded')
            else setDrawerOpen(true)
          }}
          onMouseEnter={() => {
            if (!isDesktop) return
            window.clearTimeout(overlayTimer.current)
            overlayTimer.current = window.setTimeout(() => setOverlayPreview(true), 1000)
          }}
          onMouseLeave={() => window.clearTimeout(overlayTimer.current)}
          className={cn(
            'fixed z-30 rounded-btn border border-border bg-surface/90 text-muted shadow-lg backdrop-blur-sm',
            'hover:text-foreground hover:bg-elevated transition-colors duration-150 ease-smooth',
            isDesktop ? 'left-1.5 top-1/2 -translate-y-1/2 p-2' : 'left-3 top-3 p-2',
          )}
          title={isDesktop ? '展开菜单' : '打开菜单'}
        >
          {isDesktop
            ? <PanelLeftOpen className="h-4 w-4 shrink-0" />
            : <Menu className="h-4 w-4 shrink-0" />}
        </button>
      )}
      <aside
        onMouseLeave={() => { if (overlayPreview) setOverlayPreview(false) }}
        className={cn(
          'bg-surface flex flex-col min-h-0 overflow-hidden',
          isDesktop
            ? cn('h-full', navState === 'hidden' && !overlayPreview ? 'border-r-0' : 'border-r border-border')
            : cn(
                'fixed inset-y-0 left-0 z-50 w-[80vw] max-w-[320px] border-r border-border shadow-2xl',
                'transition-transform duration-200 ease-smooth',
                drawerOpen ? 'translate-x-0' : '-translate-x-full',
              ),
          overlayPreview && 'fixed inset-y-0 left-0 z-50 w-56 shadow-2xl border-r border-border',
        )}
      >
        <div className={cn('border-b border-border shrink-0', railMode ? 'px-2 pt-3 pb-2' : 'px-4 pt-4 pb-3')}>
          {/* Brand block — 收起时只显 logo 居中 */}
          <div className={cn('flex', railMode ? 'flex-col items-center gap-2' : 'items-center gap-2')}>
            <Logo
              size={railMode ? 24 : 26}
              className="shrink-0 drop-shadow-[0_0_8px_rgba(139,92,246,0.4)]"
              style={{ color: BRAND }}
            />
            {!railMode && (
              <div
                className="font-bold text-[11px] uppercase tracking-[0.14em] text-foreground whitespace-nowrap"
                style={{ textShadow: `0 0 10px ${BRAND}44` }}
              >
                Tick Stock Panel
              </div>
            )}
            {/* 收起/展开 按钮 (桌面三态循环) / 移动端抽屉关闭按钮 */}
            {isDesktop ? (
              <button
                onClick={toggleNavCollapsed}
                className={cn(
                  'flex items-center rounded-btn text-muted hover:text-foreground hover:bg-elevated/60 transition-colors duration-150 ease-smooth',
                  railMode ? 'justify-center p-1.5' : 'ml-auto p-1.5',
                )}
                title={railMode ? '隐藏菜单 (再点击左缘按钮可唤出)' : '收起菜单'}
              >
                {railMode
                  ? <PanelLeft className="h-3.5 w-3.5 shrink-0" />
                  : <PanelLeftClose className="h-3.5 w-3.5 shrink-0" />
                }
              </button>
            ) : (
              <button
                onClick={() => setDrawerOpen(false)}
                className="ml-auto flex items-center rounded-btn p-1.5 text-muted hover:text-foreground hover:bg-elevated/60 transition-colors duration-150 ease-smooth"
                title="关闭菜单"
              >
                <X className="h-4 w-4 shrink-0" />
              </button>
            )}
          </div>

            {/* 状态卡 — 收起时隐藏 */}
            {!railMode && (
              <div className="mt-2.5 border-t border-border/60 pt-1">
                <DataSourceHealthBadge matrix={matrix} />
              <div className="mx-2 border-t border-border/45" aria-hidden="true" />
              <AIConfigBadge
                configured={settingsState?.ai_configured ?? settingsState?.has_ai_key}
                model={settingsState?.ai_model}
              />
            </div>
          )}
        </div>

        <nav className="flex-1 min-h-0 overflow-y-auto px-2 py-3 space-y-0.5">
          {visibleNavItems.map(({ to, label, icon: Icon, badge }) => {
            // 「自选」项 — 开启分组侧栏且未整体收起时, 渲染为可展开父项 + 二级分组
            const isWatchlistExpandable = to === '/watchlist' && groupsInNav && !railMode && watchlistGroups.length > 0
            return (
              <div key={to}>
                {isWatchlistExpandable ? (
                  /* 可展开的自选父项 — 点击切换展开, 不直接跳页 */
                  <button
                    onClick={() => setWatchlistNavExpanded(v => !v)}
                    className={cn(
                      'group relative flex w-full items-center gap-3 rounded-btn px-3 py-2 text-sm transition-all duration-150 ease-smooth',
                      location.pathname === '/watchlist'
                        ? 'bg-elevated text-foreground font-medium'
                        : 'text-foreground/75 hover:bg-elevated/70 hover:text-foreground',
                    )}
                  >
                    <span
                      className={cn(
                        'pointer-events-none absolute left-0 top-1/2 h-4 -translate-y-1/2 w-[2.5px] rounded-full bg-accent transition-opacity duration-150',
                        location.pathname === '/watchlist' ? 'opacity-100 shadow-[0_0_8px_rgba(59,130,246,0.6)]' : 'opacity-0',
                      )}
                    />
                    <Icon className={cn('h-4 w-4 shrink-0 transition-colors', location.pathname === '/watchlist' ? 'text-accent' : 'text-foreground/60 group-hover:text-foreground/85')} />
                    <span className="flex-1 text-left">{label}</span>
                    {watchlistNavExpanded
                      ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted" />
                      : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted" />
                    }
                  </button>
                ) : (
                  /* 普通菜单项 */
                  <NavLink
                    to={to}
                    title={railMode ? label : undefined}
                    className={({ isActive }) =>
                      cn(
                        'group relative flex items-center rounded-btn text-sm transition-all duration-150 ease-smooth',
                        railMode ? 'justify-center px-0 py-2' : 'gap-3 px-3 py-2',
                        isActive
                          ? 'bg-elevated text-foreground font-medium'
                          : 'text-foreground/75 hover:bg-elevated/70 hover:text-foreground',
                      )
                    }
                  >
                    {({ isActive }) => (
                      <>
                        {/* active 左侧 accent 竖条指示 */}
                        <span
                          className={cn(
                            'pointer-events-none absolute left-0 top-1/2 h-4 -translate-y-1/2 w-[2.5px] rounded-full bg-accent transition-opacity duration-150',
                            isActive ? 'opacity-100 shadow-[0_0_8px_rgba(59,130,246,0.6)]' : 'opacity-0',
                          )}
                        />
                        <Icon className={cn('h-4 w-4 shrink-0 transition-colors', isActive ? 'text-accent' : 'text-foreground/60 group-hover:text-foreground/85')} />
                        {!railMode && <span className="flex-1">{label}</span>}
                        {!railMode && badge && (
                          <span className="ml-auto inline-flex items-center rounded-full border border-amber-400/30 bg-amber-400/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-amber-400 shrink-0">
                            {badge}
                          </span>
                        )}
                        {/* 数据同步状态: 同步中转圈, 刚完成显示绿色对勾闪烁 3 秒 */}
                        {to === '/data' && isDataSyncing && !railMode && (
                          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-accent" />
                        )}
                        {to === '/data' && !isDataSyncing && dataSyncJustDone && !railMode && (
                          <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-bull animate-pulse" />
                        )}
                        {/* 监控中心徽标: 仅非监控页且有未读时显示 */}
                        {to === '/monitor' && !railMode && <MonitorBadge active={isActive} />}
                      </>
                    )}
                  </NavLink>
                )}

                {/* 自选分组二级子菜单 — 展开时显示 */}
                {isWatchlistExpandable && watchlistNavExpanded && (
                  <div className="mt-0.5 space-y-0.5">
                    <NavLink
                      to="/watchlist"
                      className={({ isActive }) => cn(
                        'flex items-center gap-2 rounded-btn py-1.5 pl-9 pr-3 text-[12px] transition-colors duration-150 ease-smooth',
                        isActive && !location.search
                          ? 'text-accent font-medium'
                          : 'text-foreground/60 hover:text-foreground hover:bg-elevated/50',
                      )}
                    >
                      <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-muted" />
                      <span>全部</span>
                      {(() => {
                        const info = navGroupPcts['all']
                        return info && info.pct != null ? (
                          <span className={`ml-auto font-mono text-[10px] tabular-nums ${groupPctColor(info.pct)}`} title={groupPctTitle(info)}>
                            {fmtPct(info.pct)}
                          </span>
                        ) : null
                      })()}
                    </NavLink>
                    {watchlistGroups.map(group => {
                      const color = resolveWatchlistGroupColor(group.color)
                      const groupPath = `/watchlist?group=${group.id}`
                      const isGroupActive = location.pathname === '/watchlist' && location.search === `?group=${group.id}`
                      const pctInfo = navGroupPcts[group.id]
                      return (
                        <NavLink
                          key={group.id}
                          to={groupPath}
                          className={cn(
                            'flex items-center gap-2 rounded-btn py-1.5 pl-9 pr-3 text-[12px] transition-colors duration-150 ease-smooth',
                            isGroupActive
                              ? 'text-accent font-medium'
                              : 'text-foreground/60 hover:text-foreground hover:bg-elevated/50',
                          )}
                        >
                          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${color.dot}`} />
                          <span className="truncate">{group.name}</span>
                          {pctInfo && pctInfo.pct != null && (
                            <span className={`ml-auto font-mono text-[10px] tabular-nums ${groupPctColor(pctInfo.pct)}`} title={groupPctTitle(pctInfo)}>
                              {fmtPct(pctInfo.pct)}
                            </span>
                          )}
                        </NavLink>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}
          <ExtensionSlot
            name="layout.navigation.extra"
            context={{ collapsed: railMode, pathname: location.pathname }}
            compact
          />
        </nav>

        {/* 全局行情开关 — 收起时只显示状态指示点 */}
        {railMode ? (
          <div className="border-t border-border px-2 py-2.5 shrink-0 flex justify-center">
            <button
              onClick={() => handleToggle(!realtimeEnabled)}
              disabled={realtimeToggleDisabled}
              aria-label={realtimeToggleTitle}
              aria-busy={toggleQuote.isPending}
              title={realtimeToggleTitle}
              className="flex items-center justify-center rounded-btn p-1.5 transition-colors hover:bg-elevated/70 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <span className={`inline-block h-2 w-2 rounded-full ${realtimeIndicatorClass}`} />
            </button>
          </div>
        ) : (
        <div className="border-t border-border px-3 py-2.5 shrink-0">
          {realtimeUnavailable && !realtimeProviderName ? (
            <div>
              <div className="flex items-center justify-between">
                <span className="text-xs text-secondary truncate">实时行情</span>
                <span className="text-[10px] text-muted/80 bg-elevated px-1.5 py-0.5 rounded">
                  不可用
                </span>
              </div>
              <div className="mt-1.5 text-[10px] leading-snug text-muted">
                当前数据源无实时行情权限,
                <button
                  type="button"
                  onClick={() => navigate('/settings?tab=data-sources&highlight=data-sources')}
                  className="mx-0.5 text-accent/80 hover:text-accent hover:underline"
                >
                  去配置数据源
                </button>
              </div>
            </div>
          ) : (
            /* 实时可用 — 开关 + 跳转设置 */
            <div className="flex items-center gap-2">
              <div className="flex min-w-0 flex-1 items-center gap-2">
                <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${realtimeIndicatorClass}`} />
                <div className="min-w-0">
                  <div className="text-xs font-medium leading-none text-foreground">实时行情</div>
                  <div className="mt-1 flex min-w-0 items-center gap-1 text-[10px] leading-none">
                    <span className="truncate text-muted">{realtimeProviderName || realtimeModeLabel}</span>
                    <span className="shrink-0 text-border" aria-hidden="true">·</span>
                    <span className={`shrink-0 ${realtimeStatusClass}`}>{realtimeStatusLabel}</span>
                  </div>
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <button
                  onClick={() => navigate('/settings?tab=monitoring&highlight=quotes')}
                  aria-label="打开实时监控设置"
                  className="flex h-7 w-7 items-center justify-center rounded-btn text-muted transition-colors hover:bg-elevated hover:text-foreground"
                  title="实时监控设置"
                >
                  <Settings className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  role="switch"
                  aria-checked={realtimeEnabled}
                  aria-label={realtimeToggleTitle}
                  aria-busy={toggleQuote.isPending}
                  onClick={() => handleToggle(!realtimeEnabled)}
                  disabled={realtimeToggleDisabled}
                  title={realtimeToggleTitle}
                  className={cn(
                    'relative inline-flex h-5 w-9 items-center rounded-full border transition-all duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 focus-visible:ring-offset-1 focus-visible:ring-offset-surface',
                    realtimeEnabled
                      ? 'border-accent/50 bg-accent shadow-[0_0_6px_rgba(59,130,246,0.25)]'
                      : 'border-border bg-elevated hover:border-muted',
                    realtimeToggleDisabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer',
                  )}
                >
                  <span className={cn(
                    'inline-block h-3.5 w-3.5 rounded-full border border-black/5 bg-white shadow-sm transition-transform duration-200',
                    realtimeEnabled ? 'translate-x-[18px]' : 'translate-x-0.5',
                  )} />
                </button>
              </div>
            </div>
          )}

          {/* 状态提示 */}
          {realtimeEnabled
            && (!realtimeUnavailable || realtimeProviderName)
            && (isPaused || (isWatchlistMode && !dismissFreeHint && !realtimeProviderName))
            && (
              <div className="mt-1.5 text-[10px] leading-snug space-y-0.5">
                {isWatchlistMode && !dismissFreeHint && !realtimeProviderName && (
                  <div className="flex items-start gap-1 text-amber-400/80">
                    <span className="flex-1">自选实时模式监控前 5 只，全市场实时依赖数据源支持</span>
                    <button
                      onClick={() => setDismissFreeHint(true)}
                      className="text-amber-400/50 hover:text-amber-400 shrink-0 transition-colors"
                      title="关闭提示"
                    >
                      <X className="h-2.5 w-2.5" />
                    </button>
                  </div>
                )}
                {isPaused && (
                  <div className="text-warning/80">数据同步运行中，实时行情已临时暂停</div>
                )}
              </div>
            )}
          {!isWatchlistMode && (!realtimeUnavailable || !!realtimeProviderName) && (
            <SidebarIndexQuotes rows={sidebarIndexQuotes?.rows} items={sidebarIndexes} />
          )}
        </div>
        )}

        <div className={cn('border-t border-border py-3 shrink-0', railMode ? 'px-2 flex flex-col items-center gap-1' : 'px-2')}>
          <div className={railMode ? 'flex flex-col items-center gap-1' : 'flex items-center gap-1'}>
            <ThemeToggle />
            <NavLink
              to="/settings"
              title={railMode ? '设置' : undefined}
              className={({ isActive }) =>
                cn(
                  'group relative flex items-center rounded-btn text-sm transition-all duration-150 ease-smooth',
                  railMode ? 'justify-center px-0 py-2' : 'flex-1 gap-3 px-3 py-2',
                  isActive
                    ? 'bg-elevated text-foreground font-medium'
                    : 'text-foreground/75 hover:bg-elevated/70 hover:text-foreground',
                )
              }
            >
              {({ isActive }) => (
                <>
                  <span
                    className={cn(
                      'pointer-events-none absolute left-0 top-1/2 h-4 -translate-y-1/2 w-[2.5px] rounded-full bg-accent transition-opacity duration-150',
                      isActive ? 'opacity-100 shadow-[0_0_8px_rgba(59,130,246,0.6)]' : 'opacity-0',
                    )}
                  />
                  <Settings className={cn('h-4 w-4 shrink-0 transition-colors', isActive ? 'text-accent' : 'text-foreground/60 group-hover:text-foreground/85')} />
                  {!railMode && <span>设置</span>}
                  {!railMode && version && (
                    <span className="ml-auto font-mono text-[10px] text-muted/50 select-none shrink-0">
                      {version}
                    </span>
                  )}
                </>
              )}
            </NavLink>
          </div>
        </div>
      </aside>

      <motion.main
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
        className="h-full overflow-auto scrollbar-gutter-stable"
      >
        {streamStatus === 'reconnecting' && (
          <div
            role="status"
            aria-live="polite"
            className="fixed bottom-4 left-1/2 z-[9998] flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-warning/30 bg-warning/10 px-2.5 py-1 text-[11px] font-medium text-warning shadow-lg backdrop-blur-md"
          >
            <WifiOff className="h-3 w-3 shrink-0 animate-pulse" />
            与服务连接已断开 · 正在重连
          </div>
        )}
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
      <ToastContainer />
      <AlertToastContainer />
      <AiAnalysisHost />
      <AiReportBubble />
      <StockAnalysisHost />
      <StockAnalysisBubble />

      {/* 开启实时行情 + 排队中的挖掘任务 → 冲突确认 */}
      {miningQueuedWarning != null && (
        <div {...miningWarnBackdrop} className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50 p-4">
          <div onClick={e => e.stopPropagation()} className="w-full max-w-sm rounded-2xl border border-border bg-surface p-5 shadow-2xl">
            <div className="text-sm font-semibold text-foreground">
              有挖掘任务正在排队
            </div>
            <p className="mt-2 text-xs leading-5 text-secondary">
              当前有 {miningQueuedWarning} 个挖掘任务排队等待执行。开启实时行情后盘中数据会持续落盘，
              排队中的任务启动时可能因数据更新校验而失败（需重新开始挖掘）；已开始运行的任务不受影响。
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button type="button" onClick={() => setMiningQueuedWarning(null)} className="h-8 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated">取消</button>
              <button type="button" onClick={() => { setMiningQueuedWarning(null); void doEnableRealtime() }} className="h-8 rounded-btn bg-accent px-3 text-xs font-semibold text-white hover:opacity-90">仍要开启</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
