import { Settings2, TrendingDown, RadioTower } from 'lucide-react'
import { motion } from 'framer-motion'
import { storage } from '@/lib/storage'
import { cn } from '@/lib/cn'
import { BADGE_TONE } from '@/components/ui/Primitives'

// ===== 卡片尺寸 =====

export type CardSize = 'mini' | 'normal' | 'large' | 'hidden'

export function loadCardSize(): CardSize {
  const v = storage.screenerCardSize.get('normal')
  if (v === 'mini' || v === 'normal' || v === 'large' || v === 'hidden') return v
  return 'normal'
}

const CARD_STYLES: Record<CardSize, {
  wrap: string
  card: string
  name: string
  count: string
  desc: string
  icon: string
}> = {
  mini: {
    wrap: 'gap-1',
    card: 'inline-flex items-center gap-1 px-2 py-0.5 rounded-full',
    name: 'text-[10px]',
    count: 'text-[11px]',
    desc: '',
    icon: 'h-3 w-3',
  },
  normal: {
    wrap: 'gap-2',
    card: 'relative inline-flex items-center gap-2 pl-3 pr-12 py-1.5 rounded-lg',
    name: 'text-xs',
    count: 'text-xs',
    desc: 'text-[10px] text-muted leading-tight mt-0.5 line-clamp-1 max-w-[120px]',
    icon: 'h-3.5 w-3.5',
  },
  large: {
    wrap: 'gap-2',
    card: 'relative inline-flex flex-col items-start pl-3.5 pr-12 py-2.5 rounded-btn min-w-[100px]',
    name: 'text-xs',
    count: 'text-lg font-mono font-bold tabular-nums',
    desc: 'text-[10px] text-muted leading-tight mt-0.5 line-clamp-2 max-w-[140px]',
    icon: 'h-3.5 w-3.5',
  },
  hidden: {
    wrap: '',
    card: '',
    name: '',
    count: '',
    desc: '',
    icon: '',
  },
}

export { CARD_STYLES }

/** 获取卡片容器的 flex-wrap gap 样式 */
export function cardWrapCls(size: CardSize): string {
  return `flex flex-wrap ${CARD_STYLES[size].wrap}`
}

// ===== 来源标签 — 静态 semantic token，无 purple/amber =====

const SRC_MAP: Record<string, string> = { builtin: '内置', custom: '自定义', ai: 'AI', screen: '方案' }
const BADGE_CLS_MAP: Record<string, string> = {
  builtin: BADGE_TONE.neutral,
  ai: BADGE_TONE.accent,
  custom: BADGE_TONE.info,
  screen: BADGE_TONE.accent,
}

// ===== 策略卡片 =====

interface StrategyCardProps {
  name: string
  description?: string
  source?: string
  active: boolean
  count?: number
  /** 今日曾命中总数 */
  everMatched?: number
  /** 今日已失效数 (曾命中 - 当前命中) */
  expiredCount?: number
  loading: boolean
  cardSize: CardSize
  onRun: () => void
  disabled: boolean
  onSettings: () => void
  /** 是否已加入策略监控 */
  monitored?: boolean
  /** 切换策略监控 (点击 RadioTower 图标) */
  onToggleMonitor?: () => void
}

export function StrategyCard({
  name, description, source, active, count, expiredCount,
  loading, cardSize,
  onRun, disabled, onSettings, monitored, onToggleMonitor,
}: StrategyCardProps) {
  const cs = CARD_STYLES[cardSize]
  const activeCls = active
    ? 'border-accent/50 bg-accent/10'
    : 'border-border bg-surface hover:border-accent/40 hover:bg-accent/[0.03]'
  const countCls = count === 0
    ? 'text-muted'
    : 'text-accent font-semibold'
  const srcLabel = cardSize === 'mini' ? (SRC_MAP[source ?? ''] ?? '内') : (SRC_MAP[source ?? ''] ?? '内置')
  const badgeCls = BADGE_CLS_MAP[source ?? 'builtin'] ?? BADGE_CLS_MAP.builtin

  // 失效数 > 0 时显示
  const hasExpired = expiredCount != null && expiredCount > 0

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: 0.12, ease: [0.16, 1, 0.3, 1] }}
      className={cn(cs.card, 'group border text-left transition-all duration-150', activeCls)}
    >
      {cardSize === 'large' ? (
        <>
          <button onClick={onRun} disabled={disabled}
            className="flex w-full cursor-pointer flex-col items-start disabled:cursor-wait disabled:opacity-50">
            <div className="flex max-w-full items-center gap-1.5">
              <span className={cn('shrink-0 rounded border px-1 py-px text-[9px] font-medium leading-tight', badgeCls)}>{srcLabel}</span>
              <span className="truncate text-xs font-medium text-foreground">{name}</span>
            </div>
            {description && (
              <span className="mt-0.5 line-clamp-1 text-[10px] leading-tight text-muted">{description}</span>
            )}
            {count != null && (
              <div className="mt-1.5 flex items-center gap-2">
                <div className="flex items-center gap-1">
                  <span className={cn('font-mono text-sm font-bold tabular-nums', countCls)}>{count}</span>
                  <span className="text-[10px] text-muted">只</span>
                </div>
                {hasExpired && (
                  <div className="flex items-center gap-0.5 rounded border border-danger/15 bg-danger/8 px-1.5 py-0.5">
                    <TrendingDown className="h-2.5 w-2.5 text-danger" />
                    <span className="font-mono text-[10px] font-medium text-danger">{expiredCount}</span>
                  </div>
                )}
              </div>
            )}
            {loading && <div className="mt-1 h-4 w-10 animate-pulse rounded bg-elevated" />}
          </button>
          <button onClick={(e) => { e.stopPropagation(); onSettings() }}
            className="absolute right-1.5 top-1.5 cursor-pointer rounded p-0.5 transition-colors hover:bg-elevated" title="策略设置">
            <Settings2 className="h-3 w-3 text-muted transition-colors hover:text-accent" />
          </button>
          {onToggleMonitor && (
            <button onClick={(e) => { e.stopPropagation(); onToggleMonitor() }}
              className="absolute right-7 top-1.5 cursor-pointer rounded p-0.5 transition-colors hover:bg-elevated" title={monitored ? '取消策略监控' : '开启策略监控'}>
              <RadioTower className={cn('relative h-3 w-3 transition-colors', monitored ? 'text-accent' : 'text-muted hover:text-accent')} />
              {monitored && <span className="absolute inset-0 animate-ping rounded bg-accent/20" />}
            </button>
          )}
        </>
      ) : cardSize === 'normal' ? (
        <>
          <button onClick={onRun} disabled={disabled}
            className="flex min-w-0 cursor-pointer flex-col items-start disabled:cursor-wait disabled:opacity-50">
            <div className="flex min-w-0 items-center gap-1.5">
              <span className={cn('shrink-0 rounded border px-1 py-px text-[9px] font-medium leading-tight', badgeCls)}>{srcLabel}</span>
              <span className="truncate text-xs font-medium text-foreground">{name}</span>
              {count != null && (
                <span className={cn('shrink-0 font-mono text-xs font-bold tabular-nums', countCls)}>{count}</span>
              )}
              {loading && <span className="h-3 w-5 shrink-0 animate-pulse rounded bg-elevated" />}
            </div>
            <div className="mt-0.5 flex items-center gap-1.5">
              {description && (
                <span className="line-clamp-1 max-w-[120px] text-[10px] leading-tight text-muted">{description}</span>
              )}
              {hasExpired && (
                <span className="font-mono text-[9px] text-danger/80">{'-' + expiredCount}</span>
              )}
            </div>
          </button>
          <button onClick={(e) => { e.stopPropagation(); onSettings() }}
            className="absolute right-1.5 top-1.5 cursor-pointer rounded p-0.5 transition-colors hover:bg-elevated" title="策略设置">
            <Settings2 className="h-3 w-3 text-muted transition-colors hover:text-accent" />
          </button>
          {onToggleMonitor && (
            <button onClick={(e) => { e.stopPropagation(); onToggleMonitor() }}
              className="absolute right-7 top-1.5 cursor-pointer rounded p-0.5 transition-colors hover:bg-elevated" title={monitored ? '取消策略监控' : '开启策略监控'}>
              <RadioTower className={cn('relative h-3 w-3 transition-colors', monitored ? 'text-accent' : 'text-muted hover:text-accent')} />
              {monitored && <span className="absolute inset-0 animate-ping rounded bg-accent/20" />}
            </button>
          )}
        </>
      ) : (
        /* mini */
        <>
          <button onClick={onRun} disabled={disabled}
            className="flex cursor-pointer items-center gap-1 disabled:cursor-wait disabled:opacity-50">
            <span className={cn('rounded border px-0.5 text-[8px] font-medium leading-tight', BADGE_TONE.neutral)}>{srcLabel}</span>
            <span className="whitespace-nowrap text-[10px] font-medium text-foreground">{name}</span>
            {count != null && (
              <span className={cn('font-mono text-xs font-bold tabular-nums', countCls)}>{count}</span>
            )}
            {hasExpired && (
              <span className="font-mono text-[9px] text-danger/70">{'-' + expiredCount}</span>
            )}
            {loading && <span className="h-2.5 w-4 animate-pulse rounded bg-elevated" />}
          </button>
          {onToggleMonitor && (
            <button onClick={(e) => { e.stopPropagation(); onToggleMonitor() }}
              className="relative cursor-pointer rounded p-0.5 transition-colors hover:bg-elevated" title={monitored ? '取消策略监控' : '开启策略监控'}>
              <RadioTower className={cn('h-3 w-3 transition-colors', monitored ? 'text-accent' : 'text-muted hover:text-accent')} />
              {monitored && <span className="absolute inset-0 animate-ping rounded bg-accent/20" />}
            </button>
          )}
          <button onClick={(e) => { e.stopPropagation(); onSettings() }}
            className="cursor-pointer rounded p-0.5 transition-colors hover:bg-elevated" title="策略设置">
            <Settings2 className="h-3 w-3 text-muted transition-colors hover:text-accent" />
          </button>
        </>
      )}
    </motion.div>
  )
}
