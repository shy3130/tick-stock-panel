/**
 * 唯一导航数据源 —— 内置菜单 + 动态扩展分析菜单(analysisMenus) + 用户排序/隐藏偏好。
 *
 * 背景: 原来 Layout.tsx(nav[]/analysisNav/allNav/navItems/visibleNavItems) 和
 * MenuSettings.tsx(BUILTIN_PAGES[]/allEntries/orderedEntries) 各自独立维护了一份
 * "内置菜单 + 扩展菜单 + 排序 + 隐藏" 的合并逻辑,容易出现两处不同步的 bug。
 * 现在两处都从这里的 useNavItems() 取数据,只维护一份。
 *
 * 重要: 扩展分析菜单来自后端 /api/analysis-menus,用户在设置页(ExtPages)配置扩展
 * 数据源后会自动在这里出现 —— 新增 UI 结构时必须继续消费这个动态数据源,不能把
 * 导航退化成写死的列表,否则会漏掉作者/用户后续新增的扩展页面。
 */
import type { ReactNode } from 'react'
import {
  FileText,
  Tags,
  BarChart3,
  Layers3,
  Landmark,
  BookOpenCheck,
} from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import {
  BacktestIcon,
  DashboardIcon,
  DataIcon,
  IndicesIcon,
  LimitLadderIcon,
  MonitorIcon,
  StockAnalysisIcon,
  StrategyIcon,
  TradingIcon,
  WatchlistIcon,
} from '@/components/icons/customNavIcons'
import { api, type AnalysisMenu } from './api'
import { QK } from './queryKeys'
import { usePreferences } from './useSharedQueries'

/** 侧栏分组 —— 只在这里声明归属,渲染层按 group 分桶展示,不再写死 if/else */
export type NavGroup = 'core' | 'strategy' | 'research' | 'system'

export const NAV_GROUP_ORDER: NavGroup[] = ['core', 'strategy', 'research', 'system']

export const NAV_GROUP_LABEL: Record<NavGroup, string> = {
  core: '概览与自选',
  strategy: '策略与回测',
  research: '行情研究',
  system: '数据与系统',
}

export const NAV_GROUP_COLOR: Record<NavGroup, string> = {
  core: 'hsl(var(--g-core))',
  strategy: 'hsl(var(--g-strategy))',
  research: 'hsl(var(--g-research))',
  system: 'hsl(var(--g-system))',
}

/** 声明式徽标标记 —— 具体怎么画(Beta标签/转圈/未读红点)留给渲染层,这里只标记"这项需要哪种" */
export type NavBadge = 'beta' | 'data-sync' | 'monitor-unread'

export type NavIconComponent = (props: { className?: string }) => ReactNode

export interface NavItem {
  /** 与 prefs.nav_order / nav_hidden 存的 key 一致(内置项为路由 path,扩展项为 analysisMenus 的 id) */
  id: string
  to: string
  label: string
  icon: NavIconComponent
  group: NavGroup
  badge?: NavBadge
  pinned?: boolean
  /** 来自用户在设置页配置的扩展分析页(analysisMenus),内置项恒为 false */
  extension: boolean
  /**
   * 仅扩展页有意义: 对应 AnalysisMenu.visible(用户在扩展页设置里的总开关)。
   * 内置项恒为 undefined(视为 true)。侧栏渲染需据此过滤掉已禁用的扩展页;
   * 但 MenuSettings 管理列表不按此过滤 —— 已禁用的扩展页仍要列出来方便管理,
   * 这是原 Layout.tsx 与 MenuSettings.tsx 就存在的行为差异,这里保留。
   */
  menuVisible?: boolean
}

/** 唯一权威定义 —— 原来分别活在 Layout.tsx#nav[] 和 MenuSettings.tsx#BUILTIN_PAGES 里 */
export const BUILTIN_NAV: NavItem[] = [
  { id: '/',                  to: '/',                  label: '看板',     icon: DashboardIcon,     group: 'core',     extension: false, pinned: true },
  { id: '/watchlist',         to: '/watchlist',         label: '自选',     icon: WatchlistIcon,     group: 'core',     extension: false, pinned: true },
  { id: '/monitor',           to: '/monitor',           label: '监控中心', icon: MonitorIcon,       group: 'system',   extension: false, pinned: true, badge: 'monitor-unread' },
  { id: '/screener',          to: '/screener',          label: '策略',     icon: StrategyIcon,      group: 'strategy', extension: false },
  { id: '/backtest',          to: '/backtest',          label: '回测',     icon: BacktestIcon,      group: 'strategy', extension: false },
  { id: '/review',            to: '/review',            label: '复盘',     icon: BookOpenCheck,     group: 'strategy', extension: false },
  { id: '/indices',           to: '/indices',           label: '指数',     icon: IndicesIcon,       group: 'research', extension: false },
  { id: '/limit-ladder',      to: '/limit-ladder',      label: '连板梯队', icon: LimitLadderIcon,   group: 'research', extension: false },
  { id: '/concept-analysis',  to: '/concept-analysis',  label: '概念分析', icon: Layers3,         group: 'research', extension: false },
  { id: '/industry-analysis', to: '/industry-analysis', label: '行业分析', icon: Landmark,        group: 'research', extension: false },
  { id: '/stock-analysis',    to: '/stock-analysis',    label: '个股分析', icon: StockAnalysisIcon, group: 'research', extension: false, badge: 'beta' },
  { id: '/financials',        to: '/financials',        label: '财务分析', icon: FileText,        group: 'research', extension: false },
  { id: '/trading',           to: '/trading',           label: '交易',     icon: TradingIcon,       group: 'system',   extension: false },
  { id: '/data',              to: '/data',              label: '数据',     icon: DataIcon,          group: 'system',   extension: false, badge: 'data-sync' },
]

function analysisMenuToNavItem(m: AnalysisMenu): NavItem {
  return {
    id: `/analysis/${m.id}`,
    to: `/analysis/${m.id}`,
    label: m.label,
    icon: m.icon === 'tags' ? Tags : BarChart3,
    group: 'research', // 扩展页固定进"行情研究"分组; 以后要可配置再加字段
    extension: true,
    menuVisible: m.visible,
  }
}

function applyOrderAndVisibility(allItems: NavItem[], order: string[], hidden: Set<string>) {
  // order 里存的是 to (含 /analysis/ 前缀) 或历史遗留的裸 id, 两种都兼容匹配
  const byTo = new Map(allItems.map(n => [n.to, n]))
  const ordered = order.length > 0
    ? (() => {
        const head = order
          .map(id => byTo.get(id) ?? byTo.get(`/analysis/${id}`))
          .filter((n): n is NavItem => !!n)
        const seen = new Set(head.map(n => n.to))
        return [...head, ...allItems.filter(n => !seen.has(n.to))]
      })()
    : allItems

  const isHidden = (n: NavItem) => hidden.has(n.to) || hidden.has(n.to.replace(/^\/analysis\//, ''))
  return { ordered, isHidden }
}

/**
 * 单一数据源: 内置导航 + 扩展分析菜单(analysisMenus) + 用户排序/隐藏偏好,一次算好。
 * Sidebar(Layout.tsx) 和 MenuSettings.tsx 都从这里取数,不再各自维护一份合并逻辑。
 */
export function useNavItems() {
  const { data: menus, isLoading: menusLoading } = useQuery({
    queryKey: QK.analysisMenus,
    queryFn: api.analysisMenus,
  })
  const { data: prefs } = usePreferences()

  const analysisItems = (menus?.items ?? []).map(analysisMenuToNavItem)
  const allItems = [...BUILTIN_NAV, ...analysisItems]

  const savedOrder = prefs?.nav_order ?? []
  const hiddenIds = new Set(prefs?.nav_hidden ?? [])
  const { ordered, isHidden } = applyOrderAndVisibility(allItems, savedOrder, hiddenIds)

  return {
    /** 侧栏渲染用: 已排序、按 nav_hidden 过滤、且扩展页需 menuVisible !== false(与原 Layout.tsx 一致) */
    visibleItems: ordered.filter(n => !isHidden(n) && n.menuVisible !== false),
    /** 设置页(MenuSettings)用: 展示全部已配置页面(含已禁用的扩展页,与原 MenuSettings.tsx 一致),仅反映 nav_hidden 状态 */
    allItemsForSettings: ordered.map(n => ({ ...n, hidden: isHidden(n) })),
    /** 未做排序/过滤的原始扩展菜单原始数据,极少数场景(如需要 AnalysisMenu 原字段)才用 */
    rawAnalysisMenus: menus?.items ?? [],
    isLoading: menusLoading,
  }
}

export function getNavIconMeta(id: string): { icon: NavIconComponent; group: NavGroup } | undefined {
  const item = BUILTIN_NAV.find(n => n.id === id)
  if (!item) return undefined
  return { icon: item.icon, group: item.group }
}
