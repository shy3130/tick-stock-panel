import type { NavigateFunction } from 'react-router-dom'
import { NavLink } from 'react-router-dom'
import {
  Settings,
  Key,
  Database,
  Loader2,
  Sparkles,
  CheckCircle2,
  ExternalLink,
  X,
} from 'lucide-react'
import { Logo } from '@/components/Logo'
import type { IndexQuote } from '@/lib/api'
import { cn } from '@/lib/cn'
import { useUnreadAlerts } from '@/lib/monitorBadge'
import { NAV_GROUP_ORDER, useNavItems, type NavBadge, type NavItem } from '@/lib/navRegistry'
import { SidebarGroup } from './SidebarGroup'
import { BRAND_NAME, BRAND_COLOR } from '@/lib/brand'

// 品牌色 — 只用于 logo / brand 区域,不影响功能语义色
const BRAND = BRAND_COLOR
const TICKFLOW_REGISTER_URL = 'https://tickflow.org/auth/register?ref=V3KDKGXPEA'

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
    <div className="mt-2 grid grid-cols-2 gap-1.5">
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
  )
}

// ===== 档位卡片 =====
function TierBadge({ label, hasKey }: { label: string; hasKey?: boolean }) {
  const base = label.split(' ')[0].split('+')[0].toLowerCase()
  const isNone = base === 'none'

  const tierConfig: Record<string, {
    desc: string
    tagBg: React.CSSProperties
    dotStyle: React.CSSProperties
    labelTextStyle: React.CSSProperties
  }> = {
    none: {
      desc: '未配置 Key · 仅历史日K',
      tagBg: { background: 'rgba(113,113,122,0.15)' },
      dotStyle: { background: '#52525b' },
      labelTextStyle: { color: '#71717a' },
    },
    free: {
      desc: '基础日K · 自选实时',
      tagBg: { background: 'rgba(113,113,122,0.3)' },
      dotStyle: { background: '#71717a' },
      labelTextStyle: { color: '#a1a1aa' },
    },
    starter: {
      desc: '批量同步 · 行情池',
      tagBg: { background: 'rgba(59,130,246,0.2)' },
      dotStyle: { background: '#3b82f6' },
      labelTextStyle: { color: '#60a5fa' },
    },
    pro: {
      desc: '分钟K · 实时行情 · 盘口',
      tagBg: { background: 'linear-gradient(135deg, rgba(168,85,247,0.2), rgba(124,58,237,0.15))' },
      dotStyle: { background: 'linear-gradient(135deg, #a855f7, #7c3aed)' },
      labelTextStyle: { background: 'linear-gradient(135deg, #c084fc, #a855f7)', WebkitBackgroundClip: 'text', backgroundClip: 'text', color: 'transparent' },
    },
    expert: {
      desc: 'WebSocket · 财务数据',
      tagBg: { background: 'linear-gradient(135deg, rgba(59,130,246,0.2), rgba(168,85,247,0.2), rgba(245,158,11,0.2))' },
      dotStyle: { background: 'linear-gradient(135deg, #3b82f6, #a855f7, #f59e0b)' },
      labelTextStyle: { background: 'linear-gradient(135deg, #60a5fa, #c084fc, #fbbf24)', WebkitBackgroundClip: 'text', backgroundClip: 'text', color: 'transparent' },
    },
  }

  const t = tierConfig[base] || tierConfig.none
  // none 档显示英文「None」,无 label 时也显示「None」
  const displayLabel = isNone ? 'None' : (label || 'None')

  return (
    <NavLink
      to="/settings?tab=account"
      className="mt-2.5 group block -mx-2.5"
      title="API 设置"
    >
      <div className="relative overflow-hidden rounded-lg border border-blue-400/20 bg-gradient-to-br from-blue-500/[0.12] via-surface to-surface px-3 py-2 transition-all hover:border-blue-400/35 hover:from-blue-500/[0.16]">
        <div className="absolute -right-5 -top-6 h-14 w-14 rounded-full bg-blue-500/10 blur-2xl" />
        <div className="relative flex items-center gap-2">
          <div className="flex h-6 w-6 items-center justify-center rounded-md bg-blue-400/10 text-blue-300 ring-1 ring-blue-400/20">
            <Key className="h-3.5 w-3.5" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <span className="text-xs font-medium text-foreground">TickFlow</span>
              <span
                className="h-1.5 w-1.5 rounded-full"
                style={{ ...t.dotStyle, ...(base === 'expert' ? { animation: 'pulse 2s infinite' } : {}) }}
              />
            </div>
            <div className="mt-0.5 truncate text-[10px] leading-tight text-muted">
              {isNone && !hasKey ? '配置 Key 解锁更多能力' : t.desc}
            </div>
          </div>
          <span
            className="inline-flex h-[18px] max-w-[68px] shrink-0 items-center overflow-hidden rounded px-1.5 text-[10px] font-bold font-mono leading-none"
            style={t.tagBg}
          >
            <span className="truncate" style={t.labelTextStyle}>{displayLabel}</span>
          </span>
          <Settings className="h-3 w-3 shrink-0 text-muted group-hover:text-blue-300 transition-colors" />
        </div>

      </div>
    </NavLink>
  )
}

function AIConfigBadge({ configured, model }: { configured?: boolean; model?: string }) {
  return (
    <NavLink
      to="/settings?tab=ai"
      className="mt-2 group block -mx-2.5"
      title="AI 配置"
    >
      <div className="relative overflow-hidden rounded-lg border border-purple-400/20 bg-gradient-to-br from-purple-500/[0.12] via-surface to-surface px-3 py-2 transition-all hover:border-purple-400/35 hover:from-purple-500/[0.16]">
        <div className="absolute -right-5 -top-6 h-14 w-14 rounded-full bg-purple-500/10 blur-2xl" />
        <div className="relative flex items-center gap-2">
          <div className="flex h-6 w-6 items-center justify-center rounded-md bg-purple-400/10 text-purple-300 ring-1 ring-purple-400/20">
            <Sparkles className="h-3.5 w-3.5" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <span className="text-xs font-medium text-foreground">AI 配置</span>
              <span className={`h-1.5 w-1.5 rounded-full ${configured ? 'bg-bear' : 'bg-warning'}`} />
            </div>
            <div className="mt-0.5 truncate text-[10px] leading-tight text-muted">
              {configured ? (model || '已接入模型') : '接入策略生成模型'}
            </div>
          </div>
          <Settings className="h-3 w-3 text-muted group-hover:text-purple-300 transition-colors" />
        </div>
      </div>
    </NavLink>
  )
}

export interface SidebarProps {
  collapsed: boolean
  navigate: NavigateFunction
  version?: string
  tierLabel: string
  hasApiKey: boolean
  aiConfigured?: boolean
  aiModel?: string
  isDataSyncing: boolean
  dataSyncJustDone: boolean
  // 实时行情开关区
  isNoneTier: boolean
  isWatchlistMode: boolean
  realtimeEnabled: boolean
  isRunning: boolean
  isTrading: boolean
  realtimeModeLabel: string
  realtimeProviderName: string | null
  dismissFreeHint: boolean
  onDismissFreeHint: () => void
  onToggleRealtime: (enabled: boolean) => void
  toggleRealtimePending: boolean
  // 数据源状态条
  activeProviderName: string
  activeProviderDatasets: string[]
  isCustomActive: boolean
  // 大盘指数卡片
  showSidebarQuotes: boolean
  sidebarIndexQuotesRows: IndexQuote[] | undefined
  sidebarIndexes: CoreIndex[]
}

export function Sidebar(props: SidebarProps) {
  const {
    collapsed, navigate, version, tierLabel, hasApiKey, aiConfigured, aiModel,
    isDataSyncing, dataSyncJustDone,
    isNoneTier, isWatchlistMode, realtimeEnabled, isRunning, isTrading,
    realtimeModeLabel, realtimeProviderName, dismissFreeHint, onDismissFreeHint,
    onToggleRealtime, toggleRealtimePending,
    activeProviderName, activeProviderDatasets, isCustomActive,
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
      <div className={cn('border-b border-border shrink-0', collapsed ? 'px-2 py-4' : 'px-5 py-5')}>
        {/* Brand block — 原创 logo + 等宽 wordmark(折叠态只留图标) */}
        <div className={cn('flex items-center', collapsed ? 'justify-center' : 'gap-2.5')}>
          <Logo
            size={28}
            className="shrink-0 drop-shadow-[0_0_8px_rgba(139,92,246,0.5)]"
            style={{ color: BRAND }}
          />
          {!collapsed && (
            <div
              className="font-mono font-bold text-[13px] tracking-[0.06em] text-foreground leading-tight"
              style={{ textShadow: `0 0 10px ${BRAND}44` }}
            >
              <div>{BRAND_NAME}</div>
              <div>workbench</div>
            </div>
          )}
        </div>

        {!collapsed && (
          <>
            <div className="mt-2.5 text-[10px] uppercase tracking-[0.22em] text-secondary">
              Quant · Terminal
            </div>

            <div
              className="mt-3 h-px"
              style={{ background: `linear-gradient(90deg, ${BRAND}88, transparent 80%)` }}
            />

            <TierBadge label={tierLabel} hasKey={hasApiKey} />
            <AIConfigBadge configured={aiConfigured} model={aiModel} />
          </>
        )}
      </div>

      <nav className={cn('flex-1 min-h-0 overflow-y-auto py-3', collapsed ? 'px-2' : 'px-2')}>
        {pinnedItems.length > 0 && (
          collapsed ? (
            <div className="pb-1">
              {pinnedItems.map(({ to, label, icon: Icon }) => (
                <NavLink key={to} to={to} title={label} aria-label={label} className="flex h-11 items-center justify-center">
                  {({ isActive }) => (
                    <span
                      className="flex h-9 w-9 items-center justify-center rounded-md transition-colors duration-150 ease-smooth"
                      style={{
                        color: 'hsl(var(--g-core))',
                        backgroundColor: isActive ? 'color-mix(in srgb, hsl(var(--g-core)) 22%, transparent)' : 'color-mix(in srgb, hsl(var(--g-core)) 10%, transparent)',
                      }}
                    >
                      <Icon className="h-4 w-4 shrink-0" />
                    </span>
                  )}
                </NavLink>
              ))}
              <div className="mx-2 my-1 h-px bg-border" />
            </div>
          ) : (
            <div className="pb-1">
              <div className="px-3 pb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">常用</div>
              {pinnedItems.map(({ to, label, icon: Icon }) => (
                <NavLink
                  key={to}
                  to={to}
                  className={({ isActive }) =>
                    cn(
                      'mx-0 mb-1 flex items-center gap-2.5 rounded-btn border-l-2 px-2.5 py-1.5 text-sm transition-colors duration-150 ease-smooth',
                      isActive ? 'border-[hsl(var(--g-core))] bg-elevated-2 font-medium text-foreground' : 'border-transparent text-foreground/80 hover:bg-elevated',
                    )
                  }
                >
                  <span
                    className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md"
                    style={{ backgroundColor: 'color-mix(in srgb, hsl(var(--g-core)) 18%, transparent)', color: 'hsl(var(--g-core))' }}
                  >
                    <Icon className="h-4 w-4" />
                  </span>
                  <span className="flex-1">{label}</span>
                </NavLink>
              ))}
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

      {!collapsed && (
        <>
          {/* 数据源状态条 */}
          <button
            onClick={() => navigate('/settings?tab=data-sources')}
            className="mx-2 mb-1 flex items-center gap-2 rounded-btn px-2.5 py-2 text-left transition-colors hover:bg-elevated/60 shrink-0 group"
            title="数据源设置"
          >
            <span className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-md ${
              isCustomActive ? 'bg-accent/15' : 'bg-elevated'
            }`}>
              <Database className={`h-3 w-3 ${isCustomActive ? 'text-accent' : 'text-muted'}`} />
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5">
                <span className="text-[11px] font-medium text-secondary truncate group-hover:text-foreground transition-colors">
                  {activeProviderName}
                </span>
                {isCustomActive && (
                  <span className="shrink-0 rounded bg-accent/15 px-1 py-px text-[8px] font-semibold uppercase tracking-wider text-accent">
                    自定义
                  </span>
                )}
              </div>
              <div className="mt-0.5 flex gap-0.5">
                {(['daily', 'adj_factor', 'realtime', 'minute'] as const).map(ds => {
                  const supported = ds === 'daily' || ds === 'adj_factor' || ds === 'realtime' || ds === 'minute'
                  const active = supported && (
                    isCustomActive ? activeProviderDatasets.includes(ds) : true
                  )
                  return (
                    <span
                      key={ds}
                      title={ds}
                      className={`h-1 flex-1 rounded-full transition-colors ${
                        active ? 'bg-accent/60' : 'bg-muted/20'
                      }`}
                    />
                  )
                })}
              </div>
            </div>
          </button>

          {/* 全局行情开关 */}
          <div className="border-t border-border px-3 py-2.5 shrink-0">
            {isNoneTier && !realtimeProviderName ? (
              <div>
                <div className="flex items-center justify-between">
                  <span className="text-xs text-secondary truncate">实时行情</span>
                  <span className="text-[10px] text-accent/70 font-medium bg-accent/10 px-1.5 py-0.5 rounded">
                    Free+
                  </span>
                </div>
                <div className="mt-1.5 text-[10px] leading-snug text-muted">
                  免费注册
                  <a
                    href={TICKFLOW_REGISTER_URL}
                    target="_blank"
                    rel="noreferrer"
                    className="mx-1 inline-flex items-baseline gap-0.5 text-accent/80 hover:text-accent hover:underline"
                  >
                    TickFlow
                    <ExternalLink className="h-2.5 w-2.5 self-center" />
                  </a>
                  开启个股监控
                </div>
              </div>
            ) : (
              /* Starter+ — 开关 + 跳转设置 */
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 min-w-0">
                  <span className={`inline-block h-1.5 w-1.5 rounded-full shrink-0 ${
                    realtimeEnabled && isRunning && isTrading
                      ? 'bg-accent animate-pulse'
                      : realtimeEnabled
                        ? 'bg-warning/60'
                        : 'bg-muted'
                  }`} />
                  <span className="text-xs text-secondary truncate">
                    实时行情 · {realtimeProviderName || realtimeModeLabel}
                  </span>
                  <button
                    onClick={() => navigate('/settings?tab=monitoring')}
                    className="text-secondary hover:text-foreground transition-colors shrink-0"
                    title="实时监控设置"
                    aria-label="实时监控设置"
                  >
                    <Settings className="h-3 w-3" />
                  </button>
                </div>
                <button
                  onClick={() => onToggleRealtime(!realtimeEnabled)}
                  disabled={toggleRealtimePending}
                  aria-label={realtimeEnabled ? '关闭实时行情' : '开启实时行情'}
                  className={`relative inline-flex h-4 w-7 items-center rounded-full shrink-0 transition-colors duration-200 ${
                    realtimeEnabled
                      ? 'bg-accent shadow-[0_0_6px_rgba(59,130,246,0.3)]'
                      : 'bg-elevated'
                  } ${toggleRealtimePending ? 'opacity-50' : 'cursor-pointer'}`}
                >
                  <span className={`inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform duration-200 ${
                    realtimeEnabled ? 'translate-x-[14px]' : 'translate-x-0.5'
                  }`} />
                </button>
              </div>
            )}

            {/* 状态提示 */}
            {realtimeEnabled && (!isNoneTier || realtimeProviderName) && (
              <div className="mt-1.5 text-[10px] leading-snug space-y-0.5">
                {isWatchlistMode && !dismissFreeHint && !realtimeProviderName && (
                  <div className="flex items-start gap-1 text-amber-400/80">
                    <span className="flex-1">监控自选股前 5 只，全市场监控需 Starter+</span>
                    <button
                      onClick={onDismissFreeHint}
                      className="text-amber-400/50 hover:text-amber-400 shrink-0 transition-colors"
                      title="关闭提示"
                      aria-label="关闭提示"
                    >
                      <X className="h-2.5 w-2.5" />
                    </button>
                  </div>
                )}
                {isRunning && isTrading ? (
                  <div className="text-accent">行情运行中</div>
                ) : realtimeEnabled && !isTrading ? (
                  <div className="text-warning/70">非交易时段，将在交易时间自动开启</div>
                ) : null}
              </div>
            )}
            {showSidebarQuotes && !isWatchlistMode && !isNoneTier && (
              <SidebarIndexQuotes rows={sidebarIndexQuotesRows} items={sidebarIndexes} />
            )}
          </div>
        </>
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
