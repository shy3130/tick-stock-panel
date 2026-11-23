import { useEffect, useLayoutEffect, useMemo, useRef, useState, Suspense } from 'react'
import { createPortal } from 'react-dom'
import { Link, NavLink, Outlet, useNavigate, useLocation } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { useQuoteStream, useQuoteStreamStatus } from '@/lib/useQuoteStream'
import { ToastContainer, toast } from '@/components/Toast'
import { AlertToastContainer } from '@/components/AlertToast'
import { AiAnalysisHost } from '@/components/financials/AiAnalysisHost'
import { AiReportBubble } from '@/components/financials/AiReportBubble'
import { DailyDataHealthNotice } from '@/components/data/DailyDataHealthNotice'
import { StockAnalysisHost } from '@/components/stock-analysis/StockAnalysisHost'
import { StockPreviewHost } from '@/components/StockPreviewHost'
import { StockAnalysisBubble } from '@/components/stock-analysis/StockAnalysisBubble'
import {
  useCapabilityMatrix,
  useSettings,
  usePreferences,
  useQuoteStatus,
  useVersion,
} from '@/lib/useSharedQueries'
import { useUpdateCheck } from '@/lib/updateCheck'
import { useMigrateStockInfoBarLayout } from '@/lib/useStockInfoBarLayout'
import {
  useToggleRealtimeQuotes,
} from '@/lib/useSharedMutations'
import { QK, MINUTE_DATA_QUERY_PREFIXES } from '@/lib/queryKeys'
import { setUiPref } from '@/lib/uiPrefs'
import { useStrategyLibrarySync } from '@/lib/strategyLibrary'
import { getPageScale } from '@/lib/pageZoom'
import { setKlineBatchLimits } from '@/lib/klineBatchLimits'
import { seedHiddenForNewUser } from '@/lib/navOrder'
import { navPageIds, resolveNavLayout, visibleNavLayout, type NavGroup } from '@/lib/navLayout'
import { getNavigationEntries, type NavigationEntry } from '@/lib/navigation'
import { NavFolder } from './navigation/NavFolder'
import {
  Activity,
  Settings,
  DatabaseZap,
  Loader2,
  Sparkles,
  CheckCircle2,
  ChevronRight,
  Sun,
  Moon,
  X,
  WifiOff,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  Download,
  HardDriveDownload,
} from 'lucide-react'
import { Logo } from './Logo'
import { api, type CapabilityMatrix, type IndexQuote } from '@/lib/api'
import { cn } from '@/lib/cn'
import { useIsDesktop } from '@/lib/useMediaQuery'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'
import { resolveWatchlistGroupColor } from '@/lib/watchlist-group-colors'
import { computeGroupPcts, groupPctTitle } from '@/lib/watchlistGroupStats'
import { fmtPct } from '@/lib/format'
import { findDataSource } from '@/lib/dataSources'
import { toggleTheme, useTheme } from '@/lib/theme'
import { setCurrentTotal as setAlertTotal, useUnreadAlerts } from '@/lib/monitorBadge'
import { ExtensionSlot } from '@/extensions/ExtensionSlot'
import './navigation/sidebar.css'

// 核心四只指数 — 与后端 index_const.py 单一权威对齐 (前端展示层固定清单)
export const CORE_INDEXES = [
  { symbol: '000001.SH', name: '上证指数' },
  { symbol: '399001.SZ', name: '深证成指' },
  { symbol: '399006.SZ', name: '创业板指' },
  { symbol: '000680.SH', name: '科创综指' },
] as const

type CoreIndex = (typeof CORE_INDEXES)[number]

/** 亮/暗主题切换 — 状态存 localStorage, 生效见 lib/theme.ts */
function ThemeToggle() {
  const theme = useTheme()
  const dark = theme === 'dark'
  return (
    <button
      type="button"
      onClick={() => toggleTheme()}
      className="flex cursor-pointer items-center justify-center rounded-btn p-2 text-foreground/80 transition-colors duration-150 ease-smooth hover:bg-elevated hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent"
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
  if (v == null || Number.isNaN(Number(v))) return 'tsp-sidebar-note'
  const n = Number(v)
  if (n === 0) return 'text-foreground'
  return n > 0 ? 'tsp-sidebar-rise' : 'tsp-sidebar-fall'
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
    <span className="tsp-sidebar-count">
      <span className="sr-only">未读告警 </span>
      {unread > 99 ? '99+' : unread}
    </span>
  )
}

function SidebarIndexQuotes({ rows, items }: { rows: IndexQuote[] | undefined; items: readonly CoreIndex[] }) {
  const { pathname, search } = useLocation()
  const selected = pathname === '/indices' ? new URLSearchParams(search).get('symbol') || items[0]?.symbol : null
  const quoteBySymbol = new Map((rows ?? []).map(q => [q.symbol, q]))
  return (
    <div className="tsp-sidebar-indices" aria-label="四大指数" role="group">
      {items.map(item => {
        const q = quoteBySymbol.get(item.symbol)
        const value = q?.last_price ?? q?.close
        const pct = q?.change_pct
        return (
          <Link
            key={item.symbol}
            to={`/indices?symbol=${encodeURIComponent(item.symbol)}`}
            aria-current={selected === item.symbol ? 'page' : undefined}
            className="tsp-sidebar-index"
            title={`${item.name} ${item.symbol}`}
          >
            <span className="truncate text-secondary">{item.name}</span>
            <span className="truncate text-right font-mono text-[10px] tabular-nums text-foreground">{fmtIndexValue(value)}</span>
            <span className={cn('truncate text-right font-mono text-[10px] tabular-nums', indexPctClass(pct))}>{fmtIndexPct(pct)}</span>
          </Link>
        )
      })}
    </div>
  )
}

// ===== 头部快捷行: 数据同步入口 (描边分段按钮, 与扩展快捷方式并排成组) =====
// 点击直达 /data; 同步中转圈 / 刚完成绿勾, 空闲无角标保持安静。
function QuickDataEntry({ syncing, justDone }: { syncing: boolean; justDone: boolean }) {
  return (
    <Link
      to="/data"
      title={syncing ? '数据同步中…' : '数据同步与画像'}
      className={cn(
        'tsp-sidebar-shortcut',
        syncing
          ? 'tsp-sidebar-shortcut-sync'
          : 'text-secondary',
      )}
    >
      {syncing
        ? <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
        : <HardDriveDownload className="h-3.5 w-3.5 shrink-0" />}
      <span className="truncate">{syncing ? '同步中' : '数据同步'}</span>
      {!syncing && justDone && <CheckCircle2 className="tsp-sidebar-success h-3 w-3 shrink-0" aria-label="同步完成" />}
    </Link>
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

function DataSourceHealthBadge({ matrix, failed, preview }: { matrix: CapabilityMatrix | undefined; failed: boolean; preview: boolean }) {
  const caps = matrix?.capabilities ?? []
  const hasCapabilities = caps.length > 0
  const usableCount = caps.filter(c => c.usable).length
  const down = caps.filter(c => !c.usable)
  // 日K是核心能力 (其他一切派生于它): 挂了用危险色; 一般缺项琥珀; 全可用绿
  const countCls = !hasCapabilities ? 'tsp-sidebar-note'
    : down.length === 0 ? 'tsp-sidebar-success'
    : down.some(c => c.id === 'daily') ? 'tsp-sidebar-rise'
    : 'tsp-sidebar-warning'
  const count = hasCapabilities ? `${usableCount}/${caps.length}` : failed ? '获取失败' : matrix ? '暂无状态' : '获取中'

  // 预览挂载到缩放层根节点，避开侧栏滚动区的裁剪。
  const linkRef = useRef<HTMLAnchorElement>(null)
  const popRef = useRef<HTMLDivElement>(null)
  const closeTimer = useRef<number | undefined>(undefined)
  const [popPos, setPopPos] = useState<{ left: number; top: number } | null>(null)
  const previewOpen = preview && popPos !== null
  const openPop = () => {
    if (!preview) return
    window.clearTimeout(closeTimer.current)
    const rect = linkRef.current?.getBoundingClientRect()
    // rect 是视觉坐标, fixed 定位值是 #root 布局坐标 (缩放层), 需 ÷ scale 换算。
    if (rect) {
      const z = getPageScale()
      setPopPos({ left: Math.min(rect.right / z, window.innerWidth / z - 272), top: (rect.top + rect.height / 2) / z })
    }
  }
  const closePop = () => {
    if (!preview) return
    window.clearTimeout(closeTimer.current)
    closeTimer.current = window.setTimeout(() => setPopPos(null), 120)
  }
  useEffect(() => () => window.clearTimeout(closeTimer.current), [])
  useEffect(() => {
    if (!previewOpen) return
    const dismiss = () => setPopPos(null)
    window.addEventListener('resize', dismiss)
    window.addEventListener('scroll', dismiss, true)
    return () => {
      window.removeEventListener('resize', dismiss)
      window.removeEventListener('scroll', dismiss, true)
    }
  }, [previewOpen])
  useLayoutEffect(() => {
    if (!popPos || !popRef.current) return
    const h = popRef.current.offsetHeight
    const margin = 8
    const minCenter = margin + h / 2
    const maxCenter = window.innerHeight / getPageScale() - margin - h / 2
    const clamped = Math.min(maxCenter, Math.max(minCenter, popPos.top))
    if (clamped !== popPos.top) setPopPos({ ...popPos, top: clamped })
  }, [popPos])

  return (
    <>
      <Link
        ref={linkRef}
        to="/settings?tab=data-sources"
        aria-label={`数据源能力 ${count}${hasCapabilities ? ' 可用' : ''}，前往数据源配置`}
        onMouseEnter={openPop}
        onMouseLeave={closePop}
        onFocus={openPop}
        onBlur={closePop}
        onKeyDown={e => { if (e.key === 'Escape') setPopPos(null) }}
        className="tsp-sidebar-status-row"
      >
        <DatabaseZap className="tsp-sidebar-note h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span className="shrink-0">数据源</span>
        {hasCapabilities && (
          <span className="ml-auto flex min-w-0 items-center gap-0.5" aria-hidden="true">
            {caps.map(c => <span key={c.id} className={`h-1.5 w-1.5 rounded-sm ${capSquareCls(c)}`} />)}
          </span>
        )}
        <span className={cn('tsp-sidebar-status-value shrink-0', !hasCapabilities && 'ml-auto', countCls)}>{count}</span>
      </Link>
      {preview && popPos && createPortal(
        <div
          ref={popRef}
          className="fixed z-50 -translate-y-1/2 pl-3"
          style={{ left: popPos.left, top: popPos.top }}
          onMouseEnter={() => window.clearTimeout(closeTimer.current)}
          onMouseLeave={closePop}
        >
          <div className="tsp-nav-flyout w-64 p-3" role="tooltip">
            <div className="mb-2 flex items-center justify-between">
              <span className="flex items-center gap-1.5 text-xs font-medium text-foreground">
                <DatabaseZap className="h-3.5 w-3.5 text-accent" />
                数据源能力
              </span>
              <span className={`text-[11px] font-semibold ${countCls}`}>
                {hasCapabilities ? `${count} 可用` : count}
              </span>
            </div>
            <div className="space-y-1.5 border-t border-border/60 pt-2">
              {!hasCapabilities ? (
                <div className="tsp-sidebar-note py-0.5 text-[11px]">{failed ? '暂时无法读取能力状态。' : count}</div>
              ) : caps.map(c => (
                <div key={c.id} className="flex min-w-0 items-center gap-2">
                  <span className={`h-2 w-2 shrink-0 rounded-[2px] ${capSquareCls(c)}`} />
                  <span className="shrink-0 text-xs font-medium text-secondary">{c.label}</span>
                  <span className="ml-auto flex min-w-0 shrink items-center gap-1.5">
                    {c.usable ? (
                      <>
                        <span className="tsp-sidebar-note truncate text-[11px]">{c.effective_display}</span>
                        <CheckCircle2 className="h-3 w-3 shrink-0 text-accent" />
                      </>
                    ) : (
                      <span className="tsp-sidebar-note text-[11px]">未接入</span>
                    )}
                  </span>
                </div>
              ))}
            </div>
            {/* 分时有分钟K功能替身 (intraday_monitor_support 三路可达), 不单独占能力格, 在此备注 */}
            <div className="tsp-sidebar-note mt-2 text-[11px] leading-relaxed">
              分时信号监控可由分钟 K 数据驱动，不单独设能力格
            </div>
            <div className="tsp-sidebar-note mt-2 flex items-center gap-1 border-t border-border/60 pt-2 text-[11px]">
              点击前往数据源配置
              <ChevronRight className="h-3 w-3" />
            </div>
          </div>
        </div>, document.getElementById('root') ?? document.body,
      )}
    </>
  )
}

function AIConfigBadge({ configured, model }: { configured?: boolean; model?: string }) {
  const descText = configured ? (model || '已接入模型') : '接入策略生成模型'
  return (
    <Link
      to="/settings?tab=ai"
      className="tsp-sidebar-status-row"
      title={`AI 配置：${descText}`}
    >
      <Sparkles className="tsp-sidebar-note h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <span className="shrink-0">AI 配置</span>
      <span className="tsp-sidebar-status-value tsp-sidebar-note ml-auto min-w-0 truncate">{configured ? descText : '未配置'}</span>
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${configured ? 'bg-bear' : 'bg-warning'}`} aria-hidden="true" />
    </Link>
  )
}

// 桌面在展开和图标条间切换，移动端独立使用抽屉。
type NavState = 'expanded' | 'rail'

export function Layout() {
  useStrategyLibrarySync()
  // ===== 共享 hooks (替代内联 useQuery) =====
  const { data: settingsState } = useSettings()
  const { data: matrix, isError: sourceHealthError } = useCapabilityMatrix()
  const { data: versionData } = useVersion()
  // 更新检查 (单例 store): 启动静默查一次 GitHub Releases, 供左下角版本号徽标
  const { status: updateStatus, info: updateInfo } = useUpdateCheck()
  const availableUpdate = updateStatus === 'found' ? updateInfo : null
  const { data: prefs } = usePreferences()
  useMigrateStockInfoBarLayout(prefs?.stock_info_bar_layout)
  // 批量端点单请求上限 = 前端分块大小 (与后端校验同源, 见 lib/klineBatchLimits)。
  // 在渲染期写入而非 effect: 分块助手在子组件 queryFn 内调用, 而 queryFn 早于
  // 父组件 effect 触发 —— 用 effect 会让首轮请求仍按默认值分块。幂等, 无状态更新。
  setKlineBatchLimits({
    daily: prefs?.daily_batch_symbols,
    minute: prefs?.minute_batch_symbols,
  })
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

  const allNav = getNavigationEntries(analysisMenus?.items ?? [])
  const navById = new Map(allNav.map(item => [item.id, item]))
  const layout = resolveNavLayout(prefs?.nav_layout, allNav, prefs?.nav_order)
  const visibleLayout = visibleNavLayout(layout, allNav, prefs?.nav_hidden ?? [])
  const [groupOpen, setGroupOpen] = useState<Record<string, boolean>>(() => {
    try {
      const value = JSON.parse(localStorage.getItem('tf-nav-groups') || '{}')
      return value && typeof value === 'object' && !Array.isArray(value)
        ? Object.fromEntries(Object.entries(value).filter((item): item is [string, boolean] => typeof item[1] === 'boolean')) : {}
    } catch { return {} }
  })
  const groupEdited = useRef(false)
  const groupRestored = useRef(false)
  const [railGroupId, setRailGroupId] = useState<string | null>(null)
  const activeGroupId = visibleLayout.items.find(node => node.type === 'group' && node.children.some(page =>
    location.pathname === page.id || (page.id !== '/' && location.pathname.startsWith(`${page.id}/`))))?.id
  useEffect(() => {
    setRailGroupId(null)
    if (activeGroupId) setGroupOpen(previous => ({ ...previous, [activeGroupId]: true }))
  }, [location.pathname, location.search, activeGroupId])
  const savedGroupOpen = prefs?.ui_prefs?.nav_groups
  useEffect(() => {
    if (groupRestored.current || groupEdited.current || typeof savedGroupOpen !== 'string') return
    groupRestored.current = true
    try {
      const value = JSON.parse(savedGroupOpen)
      if (value && typeof value === 'object' && !Array.isArray(value)) {
        const restored = Object.fromEntries(Object.entries(value).filter((item): item is [string, boolean] => typeof item[1] === 'boolean'))
        setGroupOpen(previous => ({ ...restored, ...previous }))
      }
    } catch { /* 非法展开偏好不影响页面入口 */ }
  }, [savedGroupOpen])
  const isGroupOpen = (group: NavGroup) => groupOpen[group.id] ?? group.id === activeGroupId
  const toggleGroup = (group: NavGroup) => {
    groupEdited.current = true
    const next = { ...groupOpen, [group.id]: !isGroupOpen(group) }
    setGroupOpen(next)
    setUiPref('tf-nav-groups', JSON.stringify(next))
  }

  const [navState, setNavState] = useState<NavState>(() => {
    try {
      const v = localStorage.getItem('tf-nav-state')
      if (v === 'expanded' || v === 'rail') return v
      // 旧版收起偏好仍映射到图标条。
      return localStorage.getItem('tf-nav-collapsed') === '1' ? 'rail' : 'expanded'
    } catch { return 'expanded' }
  })
  const [drawerOpen, setDrawerOpen] = useState(false)
  const isDesktop = useIsDesktop()
  const railMode = isDesktop && navState === 'rail'
  const drawerVisible = !isDesktop && drawerOpen
  const sidebarRef = useRef<HTMLElement>(null)
  const drawerOpener = useRef<HTMLButtonElement>(null)
  const drawerCloser = useRef<HTMLButtonElement>(null)
  // 选中当前页也应关闭抽屉，因此按导航记录而非路径变化收起。
  useEffect(() => { setDrawerOpen(false) }, [location.key, isDesktop])
  useEffect(() => { if (!railMode) setRailGroupId(null) }, [railMode])
  useEffect(() => {
    if (!drawerVisible) return
    drawerCloser.current?.focus({ preventScroll: true })
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return
      if (event.key === 'Escape') {
        setDrawerOpen(false)
        return
      }
      const sidebar = sidebarRef.current
      if (event.key !== 'Tab' || !sidebar?.contains(document.activeElement)) return
      const controls = Array.from(sidebar.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]',
      )).filter(el => el.tabIndex >= 0 && el.getClientRects().length > 0)
      const first = controls[0]
      const last = controls[controls.length - 1]
      if (first && last && document.activeElement === (event.shiftKey ? first : last)) {
        event.preventDefault()
        const next = event.shiftKey ? last : first
        next.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      drawerOpener.current?.focus({ preventScroll: true })
    }
  }, [drawerVisible])

  // 分组等权平均涨跌幅 — 复用 watchlist/enriched 查询缓存(与自选页同 key,
  // 盘中随 SSE 刷新)。可见性门控: 子菜单实际可见(桌面展开 + 二级菜单展开, 或
  // 移动端抽屉打开) 时才拉取, 收起/隐藏/抽屉关闭下不为隐藏 UI 发请求。
  const sidebarFullyVisible = isDesktop ? !railMode : drawerVisible
  const watchlistParent = visibleLayout.items.find(node => node.type === 'group' && navPageIds(node).includes('/watchlist'))
  const watchlistVisible = visibleLayout.items.some(node => navPageIds(node).includes('/watchlist'))
  const watchlistParentOpen = !watchlistParent || (watchlistParent.type === 'group' &&
    (railMode ? railGroupId === watchlistParent.id : isGroupOpen(watchlistParent)))
  // 图标模式仅在包含自选的分类弹层中显示分组统计。
  const navGroupPctVisible = groupsInNav && watchlistVisible && watchlistParentOpen
    && (sidebarFullyVisible || (railMode && railGroupId === watchlistParent?.id)) && watchlistNavExpanded
  const { data: navWatchlist } = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
    enabled: navGroupPctVisible,
    staleTime: 60_000,
  })
  const { data: navEnriched } = useQuery({
    // 无参 → key 归一为 ['watchlist-enriched', ''], 与自选页 (无 ext 列时) 共享缓存,
    // 消除侧栏与自选页各拉一份整表的双份内存
    queryKey: QK.watchlistEnriched(),
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

  // 数据同步状态轮询: 有活跃 job 时「数据」菜单项显示转圈。
  // 标签页隐藏时 TanStack 默认暂停轮询 — 转圈提示只在页面可见时有意义, 不再强制后台轮询
  const { data: pipelineJobs } = useQuery({
    queryKey: QK.pipelineJobs,
    queryFn: () => api.pipelineJobs(1),
    refetchInterval: (query) => (query.state.data?.active_id ? 2000 : 15000),
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
  const latestDataJob = pipelineJobs?.jobs?.[0]
  const refreshedMinuteJob = useRef<string | null>(null)
  useEffect(() => {
    if (!latestDataJob || !['succeeded', 'failed'].includes(latestDataJob.status)) return
    if (latestDataJob.result?.minute_rows == null && !['sync_minute', 'extend_minute'].includes(latestDataJob.stage)) return
    if (refreshedMinuteJob.current === latestDataJob.id) return
    refreshedMinuteJob.current = latestDataJob.id
    // 复用常驻的任务轮询, 离开数据页后补拉结束也会刷新; 取消/失败可能已写入部分数据。
    // 清掉列表增量游标，让下一次查询也能读回上次末根之前补入的早盘。
    qc.resetQueries({ queryKey: QK.minuteBatch(), exact: true })
    qc.invalidateQueries({ predicate: q => q.queryKey[0] !== QK.minuteBatch()[0] && MINUTE_DATA_QUERY_PREFIXES.has(String(q.queryKey[0])) })
    qc.invalidateQueries({ queryKey: QK.dataStatus })
  }, [latestDataJob?.id, latestDataJob?.status, qc])
  const navigate = useNavigate()
  const version = versionData?.version
  const realtimeEnabled = prefs?.realtime_quotes_enabled ?? false
  // 自选实时模式限制提示: 可手动关闭, 不持久化 (刷新后恢复显示)
  const [dismissFreeHint, setDismissFreeHint] = useState(false)
  // 开启实时行情时若存在排队中的挖掘任务 → 确认弹窗 (实时落盘会让排队任务开跑即失败)
  const [miningQueuedWarning, setMiningQueuedWarning] = useState<number | null>(null)
  const miningWarnBackdrop = useDialogBackdrop(() => setMiningQueuedWarning(null))
  // 两态切换 (仅桌面): 展开 <-> 图标条
  const toggleNavCollapsed = () => {
    const next = railMode ? 'expanded' : 'rail'
    setNavState(next)
    setUiPref('tf-nav-state', next)
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
  const realtimeModeLabel = isWatchlistMode ? '自选' : '全市场'
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
    ? 'tsp-sidebar-success'
    : realtimeEnabled || isPaused
      ? 'tsp-sidebar-warning'
      : 'tsp-sidebar-note'
  const realtimeIndicatorClass = realtimeActive
    ? 'bg-bear'
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

  // 轮询触发记录总数 → 更新监控中心徽标 (每 15 秒; 后台标签页由 SSE 事件驱动, 不轮询)。
  // 非交易时段无新告警, 降为 2 分钟兜底
  const alertsTotalQuery = useQuery({
    queryKey: ['alerts-total'],
    queryFn: () => api.alertsList({ days: 7, limit: 1 }),
    refetchInterval: () => (isTrading ? 15000 : 120000),
    select: (data) => data.total,
  })
  // 只在拿到真实总数时同步徽标 (避免 data=undefined 时传 0 重置 lastSeen)
  const alertsTotal = alertsTotalQuery.data
  useEffect(() => {
    if (alertsTotal != null) setAlertTotal(alertsTotal)
  }, [alertsTotal])

  // 新装用户一次性种子: 扩展域页面默认隐藏, 菜单设置里可逐个开启。
  // 老用户 (升级前已完成 onboarding) 只写标记不动偏好, 升级前后侧栏一致。
  // 种子标记存 localStorage (每设备一次, 保存成功后才写); 隐藏偏好本体持久化在后端 nav_hidden。
  const extSeedRef = useRef(false)
  useEffect(() => {
    if (!prefs || !settingsState || extSeedRef.current) return
    let seeded = false
    try { seeded = localStorage.getItem('tf-ext-pages-seed') === '1' } catch { return }
    if (seeded) { extSeedRef.current = true; return }
    const next = seedHiddenForNewUser(settingsState.onboarding_completed, prefs.nav_hidden)
    if (!next) {
      extSeedRef.current = true
      try { localStorage.setItem('tf-ext-pages-seed', '1') } catch {}
      return
    }
    extSeedRef.current = true
    api.saveNavHidden(next)
      .then(() => { try { localStorage.setItem('tf-ext-pages-seed', '1') } catch {} })
      .then(() => qc.invalidateQueries({ queryKey: QK.preferences }))
      .catch(() => { /* 保存失败不写标记, 下次启动重试; 结果集幂等, 重复触发无害 */ })
  }, [prefs, settingsState, qc])

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
      // 刚切换到 fuyao 等源时, 全局 staleTime 内仍可能缓存旧源的权限。
      staleTime: 0,
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

  const selectedWatchlistGroup = location.pathname === '/watchlist'
    ? new URLSearchParams(location.search).get('group') ?? 'all'
    : null

  const renderNavItem = (entry: NavigationEntry, nested = false) => {
    const itemRail = railMode && !nested
    const { to, label, icon: Icon, badge, arch } = entry
    const isWatchlistExpandable = to === '/watchlist' && groupsInNav && !itemRail && watchlistGroups.length > 0
    return (
      <div key={to}>
        {isWatchlistExpandable ? (
          <button
            type="button"
            aria-expanded={watchlistNavExpanded}
            aria-controls="tsp-sidebar-watchlist"
            data-active={selectedWatchlistGroup !== null}
            className="tsp-nav-row"
            onClick={() => setWatchlistNavExpanded(v => !v)}
          >
            <span className="tsp-nav-icon" aria-hidden="true"><Icon className="h-4 w-4" /></span>
            <span className="tsp-nav-label">{label}</span>
            <ChevronRight className="tsp-nav-chevron" aria-hidden="true" />
          </button>
        ) : (
          <NavLink
            to={to}
            aria-label={itemRail ? label : undefined}
            title={arch === 'ext' ? `${label}（扩展页面）` : label}
            data-rail={itemRail}
            className="tsp-nav-row"
          >
            {({ isActive }) => (
              <>
                <span className="tsp-nav-icon" aria-hidden="true"><Icon className="h-4 w-4" /></span>
                {!itemRail && (
                  <>
                    <span className="tsp-nav-label">{label}</span>
                    {arch === 'ext' && <span className="tsp-nav-tag tsp-sidebar-note font-mono" aria-hidden="true">EXT</span>}
                    {badge && <span className="tsp-nav-tag tsp-sidebar-warning">{badge}</span>}
                    {to === '/data' && isDataSyncing && (
                      <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-accent" aria-label="数据同步中" />
                    )}
                    {to === '/data' && !isDataSyncing && dataSyncJustDone && (
                      <CheckCircle2 className="tsp-sidebar-success h-3.5 w-3.5 shrink-0" aria-label="同步完成" />
                    )}
                    {to === '/monitor' && <MonitorBadge active={isActive} />}
                  </>
                )}
              </>
            )}
          </NavLink>
        )}

        {isWatchlistExpandable && watchlistNavExpanded && (
          <div id="tsp-sidebar-watchlist" className="tsp-nav-children">
            {[{ id: 'all', name: '全部', color: null }, ...watchlistGroups].map(group => {
              const dotColor = group.color ? resolveWatchlistGroupColor(group.color).dot : 'bg-muted'
              const pctInfo = navGroupPcts[group.id]
              return (
                <Link
                  key={group.id}
                  to={group.id === 'all' ? '/watchlist' : `/watchlist?group=${encodeURIComponent(group.id)}`}
                  aria-current={selectedWatchlistGroup === group.id ? 'page' : undefined}
                  className="tsp-nav-row tsp-nav-subitem"
                  title={group.name}
                >
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dotColor}`} aria-hidden="true" />
                  <span className="tsp-nav-label">{group.name}</span>
                  {pctInfo && pctInfo.pct != null && (
                    <span
                      className={cn('shrink-0 font-mono text-[10px] tabular-nums', indexPctClass(pctInfo.pct))}
                      title={groupPctTitle(pctInfo)}
                    >
                      {fmtPct(pctInfo.pct)}
                    </span>
                  )}
                </Link>
              )
            })}
          </div>
        )}
      </div>
    )
  }


  return (
    <div
      className="grid h-full overflow-hidden bg-base text-foreground transition-[grid-template-columns] duration-200 ease-smooth motion-reduce:transition-none"
      style={{ gridTemplateColumns: isDesktop ? `${railMode ? '4.25rem' : '15.5rem'} minmax(0, 1fr)` : 'minmax(0, 1fr)' }}
    >
      {drawerVisible && (
        <div className="tsp-sidebar-scrim" onClick={() => setDrawerOpen(false)} aria-hidden="true" />
      )}
      {!isDesktop && (
        <button
          ref={drawerOpener}
          type="button"
          onClick={() => setDrawerOpen(true)}
          className="tsp-sidebar-trigger"
          aria-expanded={drawerVisible}
          aria-controls="tsp-sidebar"
          tabIndex={drawerVisible ? -1 : undefined}
          title="打开菜单"
        >
          <Menu className="h-5 w-5" aria-hidden="true" />
        </button>
      )}
      <aside
        id="tsp-sidebar"
        ref={sidebarRef}
        className={cn('tsp-sidebar', !isDesktop && 'tsp-sidebar-drawer')}
        data-rail={railMode}
        data-open={drawerVisible}
        role={drawerVisible ? 'dialog' : undefined}
        aria-label="侧栏菜单"
        aria-modal={drawerVisible || undefined}
        aria-hidden={!isDesktop && !drawerVisible ? true : undefined}
        {...(!isDesktop && !drawerVisible ? { inert: '' } : {})}
      >
        <div className="tsp-sidebar-header">
          <div className="tsp-sidebar-brand">
            <Logo size={24} />
          </div>
          {!railMode && <span className="tsp-sidebar-name">Tick Stock Panel</span>}
          {isDesktop ? (
            <button
              type="button"
              onClick={toggleNavCollapsed}
              aria-expanded={!railMode}
              aria-controls="tsp-sidebar-navigation"
              className="tsp-sidebar-icon-button tsp-sidebar-density-button"
              title={railMode ? '展开菜单' : '收起菜单'}
            >
              {railMode
                ? <PanelLeftOpen className="h-4 w-4" aria-hidden="true" />
                : <PanelLeftClose className="h-4 w-4" aria-hidden="true" />}
            </button>
          ) : (
            <button
              ref={drawerCloser}
              type="button"
              onClick={() => setDrawerOpen(false)}
              className="tsp-sidebar-icon-button ml-auto"
              title="关闭菜单"
            >
              <X className="h-5 w-5" aria-hidden="true" />
            </button>
          )}
        </div>

        <div className="tsp-sidebar-scroller">
          {!railMode && (
            <div className="tsp-sidebar-tools">
              <DataSourceHealthBadge matrix={matrix} failed={sourceHealthError} preview={isDesktop} />
              <AIConfigBadge
                configured={settingsState?.ai_configured ?? settingsState?.has_ai_key}
                model={settingsState?.ai_model}
              />
              <div className="tsp-sidebar-quick">
                <QuickDataEntry syncing={isDataSyncing} justDone={dataSyncJustDone} />
                <ExtensionSlot name="layout.header.quick" context={{}} compact />
              </div>
            </div>
          )}

          <nav id="tsp-sidebar-navigation" aria-label="主导航" className="tsp-sidebar-nav">
            {visibleLayout.items.map(node => {
              if (node.type === 'page') {
                const entry = navById.get(node.id)
                return entry ? renderNavItem(entry) : null
              }
              const ids = navPageIds(node)
              return <NavFolder key={node.id} label={node.label} active={node.id === activeGroupId}
                expanded={isGroupOpen(node)} rail={railMode} popupOpen={railGroupId === node.id}
                onToggle={() => toggleGroup(node)} onPopupChange={open => setRailGroupId(open ? node.id : null)}
                indicators={<>
                  {ids.includes('/data') && isDataSyncing && <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" aria-label="数据同步中" />}
                  {ids.includes('/monitor') && <MonitorBadge active={location.pathname === '/monitor'} />}
                </>}>
                {node.children.length === 0 && <p className="tsp-sidebar-note px-3 py-2 text-xs leading-5">暂无子菜单，请在菜单管理中移入页面。</p>}
                {node.children.map(page => {
                  const entry = navById.get(page.id)
                  return entry ? renderNavItem(entry, true) : null
                })}
              </NavFolder>
            })}
            <ExtensionSlot
              name="layout.navigation.extra"
              context={{ collapsed: railMode, pathname: location.pathname }}
              compact
            />
          </nav>

          {railMode ? (
            <div className="tsp-sidebar-rail-market">
              <button
                type="button"
                role="switch"
                aria-checked={realtimeEnabled}
                onClick={() => handleToggle(!realtimeEnabled)}
                disabled={realtimeToggleDisabled}
                aria-label={realtimeToggleTitle}
                aria-busy={toggleQuote.isPending}
                title={realtimeToggleTitle}
                className="tsp-sidebar-icon-button relative"
              >
                <Activity className="h-4 w-4" aria-hidden="true" />
                <span className={`absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full ${realtimeIndicatorClass}`} aria-hidden="true" />
              </button>
            </div>
          ) : (
            <section className="tsp-sidebar-market" aria-label="实时行情">
              {realtimeUnavailable && !realtimeProviderName ? (
                <>
                  <div className="flex items-center justify-between gap-2">
                    <span className="tsp-sidebar-market-title">实时行情</span>
                    <span className="tsp-sidebar-note text-[11px]">不可用</span>
                  </div>
                  <Link
                    to="/settings?tab=data-sources&highlight=data-sources"
                    className="tsp-sidebar-configure"
                  >
                    配置实时行情数据源
                    <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
                  </Link>
                </>
              ) : (
                <div className="flex items-center gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${realtimeIndicatorClass}`} aria-hidden="true" />
                      <span className="tsp-sidebar-market-title">实时行情</span>
                    </div>
                    <div className="mt-1 flex min-w-0 items-center gap-1 text-[11px] leading-tight">
                      <span className="tsp-sidebar-note truncate" title={realtimeProviderName ?? undefined}>{realtimeProviderName || realtimeModeLabel}</span>
                      <span className="tsp-sidebar-note" aria-hidden="true">·</span>
                      <span className={cn('shrink-0', realtimeStatusClass)}>{realtimeStatusLabel}</span>
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => navigate('/settings?tab=monitoring&highlight=quotes')}
                    aria-label="打开实时监控设置"
                    className="tsp-sidebar-icon-button tsp-sidebar-market-config"
                    title="实时监控设置"
                  >
                    <Settings className="h-3.5 w-3.5" aria-hidden="true" />
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
                    className="tsp-sidebar-switch"
                  >
                    <span className="tsp-sidebar-switch-track" aria-hidden="true"><span /></span>
                  </button>
                </div>
              )}

              {realtimeEnabled
                && (!realtimeUnavailable || realtimeProviderName)
                && (isPaused || (isWatchlistMode && !dismissFreeHint && !realtimeProviderName))
                && (
                  <div className="tsp-sidebar-warning mt-2 space-y-1 text-[11px] leading-snug">
                    {isWatchlistMode && !dismissFreeHint && !realtimeProviderName && (
                      <div className="flex items-start gap-1">
                        <span className="flex-1">自选实时模式监控前 5 只，全市场实时依赖数据源支持</span>
                        <button
                          type="button"
                          onClick={() => setDismissFreeHint(true)}
                          className="tsp-sidebar-icon-button tsp-sidebar-dismiss"
                          title="关闭提示"
                        >
                          <X className="h-3 w-3" aria-hidden="true" />
                        </button>
                      </div>
                    )}
                    {isPaused && <div>数据同步运行中，实时行情已临时暂停</div>}
                  </div>
                )}
              {!isWatchlistMode && (!realtimeUnavailable || !!realtimeProviderName) && (
                <SidebarIndexQuotes rows={sidebarIndexQuotes?.rows} items={sidebarIndexes} />
              )}
            </section>
          )}
        </div>

        <div className="tsp-sidebar-footer">
          <ThemeToggle />
          <div className={cn('relative', !railMode && 'min-w-0 flex-1')}>
            <NavLink
              to="/settings"
              title={railMode ? '设置' : undefined}
              className={({ isActive }) => cn(
                'group relative flex items-center rounded-btn text-sm transition-colors duration-150 ease-smooth focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent',
                railMode ? 'h-8 w-8 justify-center' : 'gap-3 px-3 py-2',
                !railMode && availableUpdate && 'pr-[30px]',
                isActive
                  ? 'bg-elevated font-medium text-foreground'
                  : 'text-foreground/75 hover:bg-elevated/70 hover:text-foreground',
              )}
            >
              {({ isActive }) => (
                <>
                  {isActive && <span className="pointer-events-none absolute inset-y-1 left-0 w-0.5 rounded-full bg-accent" />}
                  <Settings
                    className={cn('h-4 w-4 shrink-0 transition-colors', isActive ? 'text-accent' : 'text-foreground/60 group-hover:text-foreground/85')}
                    aria-hidden="true"
                  />
                  {!railMode && <span className="whitespace-nowrap">设置</span>}
                  {!railMode && version && (
                    <span className="tsp-sidebar-note ml-auto min-w-0 select-none truncate font-mono text-[10px]" title={version}>
                      {version}
                    </span>
                  )}
                  {railMode && availableUpdate && (
                    <span
                      className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-accent shadow-[0_0_6px_rgba(59,130,246,0.8)]"
                      title="发现新版本"
                    />
                  )}
                </>
              )}
            </NavLink>
            {!railMode && availableUpdate && (
              <Link
                to="/settings?tab=about&autoupdate=1"
                title={`发现新版本 ${availableUpdate.latest}，点击检查更新`}
                className="absolute inset-y-0 right-1.5 flex w-6 items-center justify-center rounded-btn text-amber-700 focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent motion-safe:transition-transform motion-safe:hover:scale-110 dark:text-amber-400"
              >
                <Download className="animate-breath h-3 w-3 motion-reduce:!animate-none" aria-hidden="true" />
              </Link>
            )}
          </div>
        </div>
      </aside>


      <motion.main
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
        className="h-full overflow-auto scrollbar-gutter-stable"
        {...(drawerVisible ? { inert: '' } : {})}
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
        <DailyDataHealthNotice />
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
      <StockPreviewHost />

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
