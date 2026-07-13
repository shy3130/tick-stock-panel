import { NavLink } from 'react-router-dom'
import {
  Settings,
  Loader2,
  CheckCircle2,
} from 'lucide-react'
import { Logo } from '@/components/Logo'
import type { IndexQuote } from '@/lib/api'
import { cn } from '@/lib/cn'
import { useUnreadAlerts } from '@/lib/monitorBadge'
import { NAV_GROUP_ORDER, useNavItems, type NavBadge, type NavItem } from '@/lib/navRegistry'
import { SidebarGroup } from './SidebarGroup'
import { BRAND_NAME, BRAND_COLOR, BRAND_PRODUCT } from '@/lib/brand'

// 品牌色 — 只用于 logo / brand 区域,不影响功能语义色
const BRAND = BRAND_COLOR

export const CORE_INDEXES = [
  { symbol: '000001.SH', name: '上证指数' },
  { symbol: '399001.SZ', name: '深证成指' },
  { symbol: '399006.SZ', name: '创业板指' },
  { symbol: '000680.SH', name: '科创综指' },
] as const

export type CoreIndex = (typeof CORE_INDEXES)[number]

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

/**
 * 导航项右侧徽标 —— 由 navRegistry 里的 item.badge 字段驱动,替代原来散落的
 * `to === '/xxx'` 字符串判断。实时数据(同步中/未读数)由 Layout 已有的 query
 * 提供,通过 props 传入。
 */
function NavBadgeSlot({ badge, isActive, isDataSyncing, dataSyncJustDone }: {
  badge?: NavBadge
  isActive: boolean
  isDataSyncing: boolean
  dataSyncJustDone: boolean
}) {
  if (badge === 'beta') {
    return (
      <span className="inline-flex items-center rounded-full border border-amber-400/30 bg-amber-400/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-amber-400 shrink-0">
        Beta
      </span>
    )
  }
  if (badge === 'data-sync') {
    if (isDataSyncing) return <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-accent" />
    if (dataSyncJustDone) return <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-bull animate-pulse" />
    return null
  }
  if (badge === 'monitor-unread') {
    return <MonitorBadge active={isActive} />
  }
  return null
}

function SidebarIndexQuotes({ rows, items }: { rows: IndexQuote[] | undefined; items: CoreIndex[] }) {
  if (items.length === 0) return null
  const quoteBySymbol = new Map((rows ?? []).map(q => [q.symbol, q]))
  return (
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
            <div className={`mt-0.5 truncate font-mono text-[10px] ${indexPctClass(pct)}`}>
              {fmtIndexValue(value)}
            </div>
          </NavLink>
        )
      })}
    </div>
  )
}

export interface SidebarProps {
  collapsed: boolean
  version?: string
  isDataSyncing: boolean
  dataSyncJustDone: boolean
  isNoneTier: boolean
  isWatchlistMode: boolean
  // 大盘指数卡片
  showSidebarQuotes: boolean
  sidebarIndexQuotesRows: IndexQuote[] | undefined
  sidebarIndexes: CoreIndex[]
}

export function Sidebar(props: SidebarProps) {
  const {
    collapsed, version,
    isDataSyncing, dataSyncJustDone,
    isNoneTier, isWatchlistMode,
    showSidebarQuotes, sidebarIndexQuotesRows, sidebarIndexes,
  } = props

  // 单一导航数据源 (见 lib/navRegistry.ts), 按 group 分桶渲染
  const { visibleItems } = useNavItems()
  const pinnedItems = visibleItems.filter(n => n.pinned)
  const restItems = visibleItems.filter(n => !n.pinned)
  const itemsByGroup = new Map(NAV_GROUP_ORDER.map(g => [g, [] as NavItem[]]))
  for (const item of restItems) {
    itemsByGroup.get(item.group)?.push(item)
  }

  const renderBadge = (item: NavItem, isActive: boolean) => (
    <NavBadgeSlot
      badge={item.badge}
      isActive={isActive}
      isDataSyncing={isDataSyncing}
      dataSyncJustDone={dataSyncJustDone}
    />
  )

  return (
    <aside
      className={cn(
        'border-r border-border bg-surface flex flex-col h-full min-h-0 overflow-hidden shrink-0 transition-[width] duration-200 ease-smooth',
        collapsed ? 'w-16' : 'w-56',
      )}
    >
      <div className={cn('shrink-0 border-b border-border', collapsed ? 'px-2 py-3' : 'px-4 py-4')}>
        <NavLink
          to="/"
          aria-label="返回市场看板"
          title={collapsed ? `${BRAND_NAME} ${BRAND_PRODUCT}` : undefined}
          className={cn(
            'flex items-center rounded-btn focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
            collapsed ? 'justify-center' : 'gap-3',
          )}
        >
          <span
            className={cn(
              'flex shrink-0 items-center justify-center rounded-[7px] border',
              collapsed ? 'h-9 w-9' : 'h-10 w-10',
            )}
            style={{
              color: BRAND,
              borderColor: `color-mix(in srgb, ${BRAND} 32%, transparent)`,
              backgroundColor: `color-mix(in srgb, ${BRAND} 10%, transparent)`,
              boxShadow: 'inset 0 1px 0 hsl(var(--fg-primary) / 0.06)',
            }}
          >
            <Logo size={collapsed ? 25 : 28} />
          </span>
          {!collapsed && (
            <div className="min-w-0 leading-tight">
              <div className="text-[15px] font-semibold text-foreground">{BRAND_NAME}</div>
              <div className="mt-1 truncate text-[10px] font-medium text-secondary">{BRAND_PRODUCT}</div>
            </div>
          )}
        </NavLink>
      </div>

      <nav className={cn('flex-1 min-h-0 overflow-y-auto py-3', collapsed ? 'px-2' : 'px-2')}>
        {pinnedItems.length > 0 && (
          collapsed ? (
            <div className="pb-1">
              {pinnedItems.map((item) => {
                const Icon = item.icon
                return (
                  <NavLink key={item.to} to={item.to} title={item.label} aria-label={item.label} className="flex h-11 items-center justify-center">
                    {({ isActive }) => (
                      <span
                        className="relative flex h-9 w-9 items-center justify-center rounded-md transition-colors duration-150 ease-smooth"
                        style={{
                          color: 'hsl(var(--g-core))',
                          backgroundColor: isActive ? 'color-mix(in srgb, hsl(var(--g-core)) 22%, transparent)' : 'color-mix(in srgb, hsl(var(--g-core)) 10%, transparent)',
                        }}
                      >
                        <Icon className="h-4 w-4 shrink-0" />
                        <span className="absolute -right-1 -top-1 scale-75 origin-top-right">
                          {renderBadge(item, isActive)}
                        </span>
                      </span>
                    )}
                  </NavLink>
                )
              })}
              <div className="mx-2 my-1 h-px bg-border" />
            </div>
          ) : (
            <div className="pb-1">
              <div className="px-3 pb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">常用</div>
              {pinnedItems.map((item) => {
                const Icon = item.icon
                return (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    className={({ isActive }) =>
                      cn(
                        'mx-0 mb-1 flex items-center gap-2.5 rounded-btn border-l-2 px-2.5 py-1.5 text-sm transition-colors duration-150 ease-smooth',
                        isActive ? 'border-[hsl(var(--g-core))] bg-elevated-2 font-medium text-foreground' : 'border-transparent text-foreground/80 hover:bg-elevated',
                      )
                    }
                  >
                    {({ isActive }) => (
                      <>
                        <span
                          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md"
                          style={{ backgroundColor: 'color-mix(in srgb, hsl(var(--g-core)) 18%, transparent)', color: 'hsl(var(--g-core))' }}
                        >
                          <Icon className="h-4 w-4" />
                        </span>
                        <span className="flex-1">{item.label}</span>
                        {renderBadge(item, isActive)}
                      </>
                    )}
                  </NavLink>
                )
              })}
              <div className="mx-2 my-1.5 h-px bg-border" />
            </div>
          )
        )}
        {NAV_GROUP_ORDER.map(group => (
          <SidebarGroup
            key={group}
            group={group}
            items={itemsByGroup.get(group) ?? []}
            collapsed={collapsed}
            renderBadge={renderBadge}
          />
        ))}
      </nav>

      {!collapsed && showSidebarQuotes && !isWatchlistMode && !isNoneTier && (
        <div className="border-t border-border px-3 py-2.5 shrink-0">
          <SidebarIndexQuotes rows={sidebarIndexQuotesRows} items={sidebarIndexes} />
        </div>
      )}

      <div className={cn('border-t border-border py-3 shrink-0', collapsed ? 'px-2' : 'px-2')}>
        <NavLink
          to="/settings"
          title="设置"
          aria-label={collapsed ? '设置' : undefined}
          className={({ isActive }) =>
            cn(
              'flex items-center rounded-btn text-sm transition-colors duration-150 ease-smooth',
              collapsed ? 'justify-center py-2' : 'justify-between gap-3 px-3 py-2',
              isActive
                ? 'bg-elevated text-foreground font-medium'
                : 'text-foreground/80 hover:bg-elevated hover:text-foreground',
            )
          }
        >
          <span className="flex items-center gap-3">
            <Settings className="h-4 w-4 shrink-0" />
            {!collapsed && <span>设置</span>}
          </span>
          {!collapsed && (
            <span className="font-mono text-[10px] text-muted/50 select-none">
              {version ?? ''}
            </span>
          )}
        </NavLink>
      </div>
    </aside>
  )
}
