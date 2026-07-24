import { Trash2 } from 'lucide-react'
import { useState } from 'react'

import { cn } from '@/lib/cn'

import { DowMiniChart, getLatestValidDowSignalSide } from './DowMiniChart'
import { formatServerTimestamp } from './formatServerTimestamp'
import type {
  DowMonitorNotification,
  DowMonitorOverviewSymbol,
  DowMonitorTimeframeState,
  DowSignalSide,
  DowTimeframe,
} from './types'

const TIMEFRAMES: Array<{ value: DowTimeframe; label: string }> = [
  { value: '5m', label: '5分' },
  { value: '15m', label: '15分' },
  { value: '30m', label: '30分' },
  { value: '60m', label: '60分' },
  { value: 'day', label: '日K' },
]

type VisualState = 'buy' | 'sell' | 'watch' | 'none' | 'blocked'

function visualState(
  state: DowMonitorTimeframeState | undefined,
  forceBlocked: boolean,
): VisualState {
  if (forceBlocked || state?.freshness_state !== 'LIVE') {
    return state || forceBlocked ? 'blocked' : 'none'
  }
  const rawActionCode = state.snapshot?.action_code
  const actionCode = typeof rawActionCode === 'string' ? rawActionCode.toUpperCase() : null
  if (actionCode === 'OPEN_LONG' || actionCode === 'BUY') return 'buy'
  if (
    actionCode === 'OPEN_SHORT'
    || actionCode === 'CLOSE_LONG'
    || actionCode === 'CLOSE_SHORT'
    || actionCode === 'SELL'
    || actionCode === 'RISK'
    || actionCode === 'REDUCE'
  ) return 'sell'
  if (actionCode === 'WATCH') return 'watch'
  const backendSide = getLatestValidDowSignalSide(state.chart)
  if (backendSide === 'BUY') return 'buy'
  if (backendSide === 'SELL' || backendSide === 'RISK') return 'sell'
  return 'none'
}

function stateClass(state: VisualState) {
  switch (state) {
    case 'buy':
      return 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400'
    case 'sell':
      return 'border-red-500/30 bg-red-500/10 text-red-400'
    case 'watch':
      return 'border-amber-500/30 bg-amber-500/10 text-amber-400'
    case 'blocked':
      return 'border-border bg-elevated/50 text-muted opacity-60'
    default:
      return 'border-border bg-elevated/50 text-muted'
  }
}

function signalClass(side: DowSignalSide) {
  return side === 'BUY' ? 'text-emerald-400' : 'text-red-400'
}

function blockedLabel(
  item: DowMonitorOverviewSymbol,
  state: DowMonitorTimeframeState | undefined,
  forceBlocked: boolean,
  blockedReason?: string,
) {
  if (forceBlocked) return blockedReason ?? '监控状态不可用'
  if (!item.enabled) return '监控已暂停'
  if (state?.freshness_state === 'STALE_DATA') return '数据延迟'
  if (state?.freshness_state === 'ANALYSIS_PAUSED') return '分析暂停'
  return null
}

export function DowMonitorCard({
  item,
  notifications,
  onOpen,
  onToggle,
  onRemove,
  onRead,
  forceBlocked = false,
  blockedReason,
  quoteReady = true,
  notificationLoading = false,
  notificationError = false,
  togglePending = false,
  removePending = false,
  readPendingIds,
}: {
  item: DowMonitorOverviewSymbol
  notifications: DowMonitorNotification[]
  onOpen: (symbol: string, timeframe: DowTimeframe) => void
  onToggle: (symbol: string, enabled: boolean) => void
  onRemove: (symbol: string) => void
  onRead?: (notificationId: string) => void
  forceBlocked?: boolean
  blockedReason?: string
  quoteReady?: boolean
  notificationLoading?: boolean
  notificationError?: boolean
  togglePending?: boolean
  removePending?: boolean
  readPendingIds?: ReadonlySet<string>
}) {
  const [timeframe, setTimeframe] = useState<DowTimeframe>('5m')
  const selectedState = item.states[timeframe]
  const blocked = blockedLabel(item, selectedState, forceBlocked, blockedReason)
  const price = quoteReady
    && typeof item.last_price === 'number'
    && Number.isFinite(item.last_price)
    ? item.last_price
    : null
  const change = quoteReady
    && typeof item.change_pct === 'number'
    && Number.isFinite(item.change_pct)
    ? item.change_pct * 100
    : null
  const priceDirectionClass = change == null
    ? 'text-foreground'
    : change > 0
      ? 'text-bull'
      : change < 0
        ? 'text-bear'
        : 'text-muted'
  const name = typeof item.name === 'string' && item.name.trim() && item.name.trim() !== item.symbol
    ? item.name.trim()
    : null
  const quoteTime = quoteReady ? formatServerTimestamp(item.quote_timestamp) : null
  const successTime = quoteReady ? formatServerTimestamp(item.last_success_at) : null
  return (
    <article
      data-testid={`card-${item.symbol}`}
      data-tradable={blocked ? 'false' : 'true'}
      className={cn(
        'group relative min-w-0 overflow-hidden rounded-card border bg-surface transition-colors hover:border-accent/40',
        blocked ? 'border-border/70 opacity-75' : 'border-border',
      )}
    >
      <div
        data-testid={`card-summary-${item.symbol}`}
        data-layout="compact-two-row"
        className="grid grid-cols-[minmax(0,1fr)_auto_auto] grid-rows-2 items-center gap-x-2 px-2.5 py-1.5"
      >
        <div className="flex min-w-0 items-center gap-2">
          <span className="shrink-0 font-mono text-sm font-semibold tracking-wide">
            {item.symbol}
          </span>
          {name && <span className="truncate text-xs text-secondary">{name}</span>}
        </div>

        <button
          type="button"
          role="switch"
          aria-label={`${item.symbol} 监控开关`}
          aria-checked={item.enabled}
          disabled={togglePending}
          onClick={() => onToggle(item.symbol, !item.enabled)}
          className={cn(
            'relative col-start-2 row-start-1 h-[18px] w-8 shrink-0 rounded-full transition-colors disabled:cursor-wait disabled:opacity-50',
            item.enabled ? 'bg-accent/70' : 'bg-border',
          )}
        >
          <span
            className={cn(
              'absolute top-0.5 h-3.5 w-3.5 rounded-full bg-white transition-transform',
              item.enabled ? 'translate-x-0' : '-translate-x-4',
            )}
            style={{ right: 2 }}
          />
        </button>

        <button
          type="button"
          aria-label={`移除 ${item.symbol}`}
          disabled={removePending}
          onClick={() => onRemove(item.symbol)}
          className="col-start-3 row-start-1 rounded p-0.5 text-muted transition-colors hover:bg-danger/10 hover:text-danger disabled:cursor-wait disabled:opacity-50"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>

        <div className="col-span-3 row-start-2 mt-0.5 flex min-w-0 items-baseline gap-2 overflow-hidden">
          <span className={cn(
            'shrink-0 font-mono text-[16px] tabular-nums',
            priceDirectionClass,
          )}>
            {price == null ? '—' : price.toFixed(2)}
          </span>
          {change != null && (
            <span className={cn(
              'shrink-0 font-mono text-[10px] tabular-nums',
              priceDirectionClass,
            )}>
              {change > 0 ? '+' : ''}{change.toFixed(2)}%
            </span>
          )}
          {(quoteTime || successTime) && (
            <span className="ml-auto flex min-w-0 gap-2 overflow-hidden font-mono text-[9px] text-muted">
              {quoteTime && <span className="whitespace-nowrap">行情 {quoteTime}</span>}
              {successTime && <span className="whitespace-nowrap">成功 {successTime}</span>}
            </span>
          )}
        </div>
      </div>

      <div className="grid grid-cols-5 gap-1 px-2.5 pb-1">
        {TIMEFRAMES.map(option => {
          const state = item.states[option.value]
          const currentVisualState = visualState(state, forceBlocked || !item.enabled)
          return (
            <button
              key={option.value}
              type="button"
              aria-label={option.label}
              aria-pressed={timeframe === option.value}
              data-tradable={currentVisualState === 'blocked' ? 'false' : 'true'}
              onClick={() => setTimeframe(option.value)}
              className={cn(
                'h-5 rounded border text-[9px] font-medium transition-colors',
                stateClass(currentVisualState),
                timeframe === option.value && 'ring-1 ring-accent/70',
              )}
            >
              {option.label}
            </button>
          )
        })}
      </div>

      <button
        type="button"
        aria-label={`打开 ${item.symbol} 完整K线`}
        onClick={() => onOpen(item.symbol, timeframe)}
        className="block w-full border-y border-border/50 px-1 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
      >
        <DowMiniChart
          chart={selectedState?.chart ?? {}}
          testId={`mini-chart-${item.symbol}-${timeframe}`}
          height={180}
        />
      </button>

      <section
        role="log"
        aria-label={`${item.symbol} 消息通知`}
        className="h-32 overflow-y-auto border-t border-border/60 bg-base/20 px-2.5 py-1.5"
      >
        {blocked && (
          <div className="mb-1 text-[10px] font-medium text-muted">{blocked}</div>
        )}
        {notificationLoading && (
          <div className="mb-1 text-center text-[10px] text-muted">正在加载通知</div>
        )}
        {notificationError && (
          <div className="mb-1 text-center text-[10px] text-danger">通知加载失败</div>
        )}
        {notifications.length === 0 && !notificationLoading && !notificationError ? (
          <div className="flex h-full items-center justify-center text-[10px] text-muted">
            暂无消息通知
          </div>
        ) : notifications.length > 0 ? (
          <div>
            {notifications.map((notification, index) => {
              const isLatest = index === 0
              const triggerTimeframe = TIMEFRAMES.find(
                option => option.value === notification.timeframe,
              )?.label ?? notification.timeframe
              const triggerTime = formatServerTimestamp(notification.triggered_at)
              const triggerPrice = Number.isFinite(notification.trigger_price)
                ? notification.trigger_price.toFixed(2)
                : null
              return (
                <div
                  key={notification.notification_id}
                  data-testid={`card-message-${notification.notification_id}`}
                  className={cn(
                    'border-b border-border/50 py-1.5 last:border-b-0',
                    isLatest && 'border-l-2 border-l-accent pl-2',
                  )}
                >
                  <div className="flex min-w-0 items-center gap-1.5 text-[10px]">
                    {isLatest && (
                      <span className="shrink-0 text-[9px] font-medium text-accent">最新</span>
                    )}
                    <span className={cn('shrink-0 font-medium', signalClass(notification.side))}>
                      {notification.action_name}
                    </span>
                    <span className="shrink-0 font-mono text-[9px] text-secondary">
                      周期 {triggerTimeframe}
                    </span>
                    <span className="truncate text-secondary">{notification.shape_name}</span>
                    {notification.read_at == null && onRead && (
                      <button
                        type="button"
                        aria-label={`标记 ${notification.symbol} 已读`}
                        disabled={readPendingIds?.has(notification.notification_id)}
                        onClick={() => onRead(notification.notification_id)}
                        className="ml-auto shrink-0 text-[9px] text-muted underline-offset-2 hover:text-foreground hover:underline disabled:cursor-wait disabled:opacity-50"
                      >
                        已读
                      </button>
                    )}
                  </div>
                  <div className="mt-0.5 flex min-w-0 items-center gap-1.5 font-mono text-[9px] text-muted">
                    <span className="shrink-0">触发 {triggerTime ?? '—'}</span>
                    <span className="shrink-0 text-foreground">@{triggerPrice ?? '—'}</span>
                  </div>
                </div>
              )
            })}
          </div>
        ) : null}
      </section>
    </article>
  )
}
