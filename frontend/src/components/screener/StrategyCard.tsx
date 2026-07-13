import { Settings2, TrendingDown, RadioTower } from 'lucide-react'
import { motion } from 'framer-motion'
import { storage } from '@/lib/storage'

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

// ===== 来源标签 =====

const SRC_MAP: Record<string, string> = { builtin: '内置', custom: '自定义', ai: 'AI' }
const BADGE_CLS_MAP: Record<string, string> = {
  builtin: 'bg-secondary/10 text-muted border-border',
  ai: 'bg-purple-500/10 text-purple-400 border-purple-500/30',
  custom: 'bg-amber-400/10 text-amber-400 border-amber-400/30',
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
    ? 'border-accent/50 bg-accent/10 shadow-[0_0_10px_rgba(59,130,246,0.1)]'
    : 'border-border bg-surface hover:border-accent/40 hover:bg-accent/[0.03]'
  const countCls = count === 0
    ? 'text-muted'
    : 'bg-gradient-to-r from-amber-400 to-orange-500 bg-clip-text text-transparent'
  const srcLabel = cardSize === 'mini' ? (SRC_MAP[source ?? ''] ?? '内') : (SRC_MAP[source ?? ''] ?? '内置')
  const badgeCls = BADGE_CLS_MAP[source ?? 'builtin'] ?? BADGE_CLS_MAP.builtin

  // 失效数 > 0 时显示
  const hasExpired = expiredCount != null && expiredCount > 0

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: 0.12, ease: [0.16, 1, 0.3, 1] }}
      className={`${cs.card} border transition-all duration-150 text-left group ${activeCls}`}
    >
      {cardSize === 'large' ? (
        <>
          <button onClick={onRun} disabled={disabled}
            className="flex flex-col items-start cursor-pointer disabled:opacity-50 disabled:cursor-wait w-full">
            <div className="flex items-center gap-1.5 max-w-full">
              <span className={`text-[9px] px-1 py-px rounded border font-medium leading-tight shrink-0 ${badgeCls}`}>{srcLabel}</span>
              <span className="text-xs font-medium truncate text-foreground">{name}</span>
            </div>
            {description && (
              <span className="text-[10px] text-muted leading-tight mt-0.5 line-clamp-1">{description}</span>
            )}
            {count != null && !loading && (
              <div className="mt-1.5 flex items-center gap-2">
                <div className="flex items-center gap-1">
                  <span className={`text-sm font-mono font-bold tabular-nums ${countCls}`}>{count}</span>
                  <span className="text-[10px] text-muted">只</span>
                </div>
                {hasExpired && (
                  <div className="flex items-center gap-0.5 px-1.5 py-0.5 rounded bg-red-500/8 border border-red-500/15">
                    <TrendingDown className="h-2.5 w-2.5 text-red-400" />
                    <span className="text-[10px] font-mono font-medium text-red-400">{expiredCount}</span>
                  </div>
                )}
              </div>
            )}
            {loading && <div className="mt-1 h-4 w-10 rounded bg-elevated animate-pulse" />}
          </button>
          <button onClick={(e) => { e.stopPropagation(); onSettings() }}
            className="absolute top-1.5 right-1.5 p-0.5 rounded hover:bg-elevated transition-colors cursor-pointer" title="策略设置">
            <Settings2 className="h-3 w-3 text-muted hover:text-accent transition-colors" />
          </button>
          {onToggleMonitor && (
            <button onClick={(e) => { e.stopPropagation(); onToggleMonitor() }}
              className="absolute top-1.5 right-7 p-0.5 rounded hover:bg-elevated transition-colors cursor-pointer" title={monitored ? '取消策略监控' : '开启策略监控'}>
              <RadioTower className={`relative h-3 w-3 transition-colors ${monitored ? 'text-accent' : 'text-muted hover:text-accent'}`} />
              {monitored && <span className="absolute inset-0 rounded animate-ping bg-accent/20" />}
            </button>
          )}
        </>
      ) : cardSize === 'normal' ? (
        <>
          <button onClick={onRun} disabled={disabled}
            className="flex flex-col items-start cursor-pointer disabled:opacity-50 disabled:cursor-wait min-w-0">
            <div className="flex items-center gap-1.5 min-w-0">
              <span className={`text-[9px] px-1 py-px rounded border font-medium leading-tight shrink-0 ${badgeCls}`}>{srcLabel}</span>
              <span className="text-xs font-medium truncate text-foreground">{name}</span>
              {count != null && !loading && (
                <span className={`text-xs font-mono font-bold tabular-nums shrink-0 ${countCls}`}>{count}</span>
              )}
              {loading && <span className="w-5 h-3 rounded bg-elevated animate-pulse shrink-0" />}
            </div>
            <div className="flex items-center gap-1.5 mt-0.5">
              {description && (
                <span className="text-[10px] text-muted leading-tight line-clamp-1 max-w-[120px]">{description}</span>
              )}
              {hasExpired && (
                <span className="text-[9px] font-mono text-red-400/80">{'-' + expiredCount}</span>
              )}
            </div>
          </button>
          <button onClick={(e) => { e.stopPropagation(); onSettings() }}
            className="absolute top-1.5 right-1.5 p-0.5 rounded hover:bg-elevated transition-colors cursor-pointer" title="策略设置">
            <Settings2 className="h-3 w-3 text-muted hover:text-accent transition-colors" />
          </button>
          {onToggleMonitor && (
            <button onClick={(e) => { e.stopPropagation(); onToggleMonitor() }}
              className="absolute top-1.5 right-7 p-0.5 rounded hover:bg-elevated transition-colors cursor-pointer" title={monitored ? '取消策略监控' : '开启策略监控'}>
              <RadioTower className={`relative h-3 w-3 transition-colors ${monitored ? 'text-accent' : 'text-muted hover:text-accent'}`} />
              {monitored && <span className="absolute inset-0 rounded animate-ping bg-accent/20" />}
            </button>
          )}
        </>
      ) : (
        /* mini */
        <>
          <button onClick={onRun} disabled={disabled}
            className="flex items-center gap-1 cursor-pointer disabled:opacity-50 disabled:cursor-wait">
            <span className="text-[8px] px-0.5 rounded bg-secondary/10 text-muted border border-border font-medium leading-tight">{srcLabel}</span>
            <span className="text-[10px] font-medium whitespace-nowrap text-foreground">{name}</span>
            {count != null && !loading && (
              <span className={`text-xs font-mono font-bold tabular-nums ${countCls}`}>{count}</span>
            )}
            {hasExpired && (
              <span className="text-[9px] font-mono text-red-400/70">{'-' + expiredCount}</span>
            )}
            {loading && <span className="w-4 h-2.5 rounded bg-elevated animate-pulse" />}
          </button>
          {onToggleMonitor && (
            <button onClick={(e) => { e.stopPropagation(); onToggleMonitor() }}
              className="relative p-0.5 rounded hover:bg-elevated transition-colors cursor-pointer" title={monitored ? '取消策略监控' : '开启策略监控'}>
              <RadioTower className={`h-3 w-3 transition-colors ${monitored ? 'text-accent' : 'text-muted hover:text-accent'}`} />
              {monitored && <span className="absolute inset-0 rounded animate-ping bg-accent/20" />}
            </button>
          )}
          <button onClick={(e) => { e.stopPropagation(); onSettings() }}
            className="p-0.5 rounded hover:bg-elevated transition-colors cursor-pointer" title="策略设置">
            <Settings2 className="h-3 w-3 text-muted hover:text-accent transition-colors" />
          </button>
        </>
      )}
    </motion.div>
  )
}
