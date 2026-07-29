import { Pause, Play, Trash2 } from 'lucide-react'
import type { KeyboardEvent, MouseEvent } from 'react'

import type { RealtimeSymbolState } from '@/lib/realtimeMarketData'
import { cn } from '@/lib/cn'

import { DowMonitorSparkline } from './DowMonitorSparkline'
import {
  deriveMonitorRow,
  type MonitorMomentum,
  type MonitorSignal,
} from './monitorListPresentation'
import type {
  DowMonitorNotification,
  DowMonitorOverviewSymbol,
} from './types'

function numberText(value: number | null, digits = 2): string {
  return value == null ? '--' : value.toFixed(digits)
}

function percentText(value: number | null): string {
  if (value == null) return '--'
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`
}

function momentumText(momentum: MonitorMomentum): string {
  if (momentum.valuePct == null) return '--'
  return `${momentum.valuePct > 0 ? '+' : ''}${momentum.valuePct.toFixed(2)}%`
}

function compactPercent(label: string, value: number | null): string {
  return value == null
    ? `${label} --`
    : `${label} ${value > 0 ? '+' : ''}${value.toFixed(2)}%`
}

function distancePercent(label: string, value: number | null): string {
  return value == null ? `${label} --` : `${label} ${value.toFixed(2)}%`
}

function signalTime(value: string | null): string | null {
  if (!value) return null
  const match = /T(\d{2}:\d{2})/.exec(value)
  return match?.[1] ?? value
}

function signalClass(signal: MonitorSignal | null): string {
  if (signal?.side === 'BUY') return 'border-danger/25 bg-danger/10 text-danger'
  if (signal?.side === 'SELL' || signal?.side === 'RISK') {
    return 'border-emerald-500/25 bg-emerald-500/10 text-emerald-400'
  }
  return 'border-border bg-elevated text-muted'
}

function stop(event: MouseEvent<HTMLButtonElement>) {
  event.stopPropagation()
}

export function DowMonitorList({
  items,
  notifications,
  realtimeStates,
  selectedSymbol,
  page,
  pageCount,
  total,
  nowMs,
  forceDelayed = false,
  pendingToggles = new Set(),
  pendingRemovals = new Set(),
  onPageChange,
  onSelect,
  onToggle,
  onRemove,
}: {
  items: DowMonitorOverviewSymbol[]
  notifications: DowMonitorNotification[]
  realtimeStates: ReadonlyMap<string, RealtimeSymbolState>
  selectedSymbol: string | null
  page: number
  pageCount: number
  total: number
  nowMs?: number
  forceDelayed?: boolean
  pendingToggles?: ReadonlySet<string>
  pendingRemovals?: ReadonlySet<string>
  onPageChange: (page: number) => void
  onSelect: (symbol: string) => void
  onToggle: (symbol: string, enabled: boolean) => void
  onRemove: (symbol: string) => void
}) {
  const selectFromKeyboard = (
    event: KeyboardEvent<HTMLTableRowElement>,
    symbol: string,
  ) => {
    if (event.target !== event.currentTarget) return
    if (event.key !== 'Enter' && event.key !== ' ') return
    event.preventDefault()
    onSelect(symbol)
  }

  return (
    <section aria-label="股票监控列表" className="overflow-hidden rounded-card border border-border bg-surface">
      <div data-testid="dow-monitor-table-scroll" className="max-w-full overflow-x-auto">
        <table className="w-full min-w-[1660px] border-collapse text-xs">
          <thead className="bg-elevated/70 text-[11px] text-muted">
            <tr>
              <th scope="col" className="whitespace-nowrap border-b border-border px-3 py-2 text-left font-medium">股票</th>
              <th scope="col" className="whitespace-nowrap border-b border-border px-3 py-2 text-left font-medium">价格 / 涨跌</th>
              <th scope="col" className="whitespace-nowrap border-b border-border px-3 py-2 text-left font-medium">日内走势</th>
              <th scope="col" className="whitespace-nowrap border-b border-border px-3 py-2 text-left font-medium">
                趋势 / 位置
                <span className="block text-[9px] text-muted">通道 · 控制线 · 成本位置</span>
              </th>
              <th scope="col" className="whitespace-nowrap border-b border-border px-3 py-2 text-left font-medium">
                动量 / 涨速
                <span className="block text-[9px] text-muted">1m 实时 · 5m/15m 稳</span>
              </th>
              <th scope="col" className="whitespace-nowrap border-b border-border px-3 py-2 text-left font-medium">
                量价 / 资金
                <span className="block text-[9px] text-muted">量比 · 量速 · 主买 · 五档</span>
              </th>
              <th scope="col" className="whitespace-nowrap border-b border-border px-3 py-2 text-left font-medium">
                突破 / 风险
                <span className="block text-[9px] text-muted">日高低 · ATR · 周期确认</span>
              </th>
              <th scope="col" className="whitespace-nowrap border-b border-border px-3 py-2 text-left font-medium">买卖信号</th>
              <th scope="col" className="whitespace-nowrap border-b border-border px-3 py-2 text-left font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map(item => {
              const realtime = realtimeStates.get(item.symbol.toUpperCase())
              const itemNotifications = notifications.filter(
                notification => notification.symbol === item.symbol,
              )
              const derived = deriveMonitorRow(item, itemNotifications, realtime, nowMs)
              const row = {
                ...derived,
                delayed: forceDelayed || derived.delayed,
                signal: forceDelayed && derived.signal?.level !== 'CONFIRMED'
                  ? null
                  : derived.signal,
              }
              const selected = selectedSymbol === item.symbol
              const positive = (row.changePct ?? 0) >= 0
              return (
                <tr
                  key={item.symbol}
                  aria-selected={selected}
                  tabIndex={0}
                  onClick={() => onSelect(item.symbol)}
                  onKeyDown={event => selectFromKeyboard(event, item.symbol)}
                  className={cn(
                    'cursor-pointer border-b border-border/70 outline-none transition-colors last:border-b-0 hover:bg-elevated/60 focus-visible:bg-elevated',
                    selected && 'bg-accent/8 shadow-[inset_3px_0_0_0_rgb(var(--color-accent))]',
                    !item.enabled && 'opacity-55',
                  )}
                >
                  <td className="min-w-48 px-3 py-2">
                    <div className="flex items-center gap-2">
                      <div className="min-w-0 flex-1">
                        <div className="truncate font-medium text-foreground">
                          {item.name || item.symbol}
                        </div>
                        <div className="font-mono text-[10px] text-muted">{item.symbol}</div>
                      </div>
                      <button
                        type="button"
                        aria-label={`${item.enabled ? '暂停监控' : '恢复监控'} ${item.symbol}`}
                        disabled={pendingToggles.has(item.symbol)}
                        onClick={(event) => {
                          stop(event)
                          onToggle(item.symbol, !item.enabled)
                        }}
                        className="rounded-btn p-1 text-muted hover:bg-base hover:text-secondary disabled:opacity-40"
                      >
                        {item.enabled
                          ? <Pause className="h-3.5 w-3.5" />
                          : <Play className="h-3.5 w-3.5" />}
                      </button>
                      <button
                        type="button"
                        aria-label={`移除 ${item.symbol}`}
                        disabled={pendingRemovals.has(item.symbol)}
                        onClick={(event) => {
                          stop(event)
                          onRemove(item.symbol)
                        }}
                        className="rounded-btn p-1 text-muted hover:bg-danger/10 hover:text-danger disabled:opacity-40"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </td>
                  <td className="whitespace-nowrap px-3 py-2 font-mono">
                    <div className="text-sm font-semibold text-foreground">
                      {numberText(row.price)}
                    </div>
                    <div className={positive ? 'text-danger' : 'text-emerald-400'}>
                      {percentText(row.changePct)}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <DowMonitorSparkline symbol={item.symbol} values={row.sparkline} />
                  </td>
                  <td data-testid={`trend-position-${item.symbol}`} className="whitespace-nowrap px-3 py-2">
                    <div className="flex items-center gap-2">
                      <span className="rounded border border-border px-1 text-[9px] text-muted">稳</span>
                      <strong className={cn(
                        'font-medium',
                        row.trendPosition.channel.code === 'UP' && 'text-danger',
                        row.trendPosition.channel.code === 'DOWN' && 'text-emerald-400',
                        (row.trendPosition.channel.code === 'RANGE' || row.trendPosition.channel.code === 'PENDING') && 'text-amber-400',
                        row.trendPosition.channel.code === 'UNKNOWN' && 'text-muted',
                      )}>
                        {row.trendPosition.channel.label}
                      </strong>
                    </div>
                    <div className="mt-1 flex items-center gap-2 font-mono text-[10px] text-muted">
                      <span>{compactPercent('控制', row.trendPosition.control?.distancePct ?? null)}</span>
                      <span>{compactPercent('成本', row.trendPosition.costDistancePct)}</span>
                    </div>
                  </td>
                  <td data-testid={`momentum-speed-${item.symbol}`} className="whitespace-nowrap px-3 py-2">
                    <div className="flex items-center gap-2">
                      <span className="rounded border border-cyan-400/20 px-1 text-[9px] text-cyan-300">实时</span>
                      <strong>1m {momentumText(row.momentumSpeed.momentum1m)}</strong>
                    </div>
                    <div className="mt-1 flex items-center gap-2 font-mono text-[10px] text-muted">
                      <span>5m {momentumText(row.momentumSpeed.momentum5m)}</span>
                      <span>15m {momentumText(row.momentumSpeed.momentum15m)}</span>
                    </div>
                  </td>
                  <td data-testid={`volume-funds-${item.symbol}`} className="whitespace-nowrap px-3 py-2">
                    <div className="flex items-center gap-2">
                      <span data-testid={`relative-volume-stable-badge-${item.symbol}`} className="rounded border border-border px-1 text-[9px] text-muted">稳</span>
                      <strong>
                        量比 {row.volumeFunds.relativeVolume
                          ? `${row.volumeFunds.relativeVolume.ratio.toFixed(2)}×`
                          : '--'}
                      </strong>
                      <span data-testid={`volume-speed-live-badge-${item.symbol}`} className="rounded border border-cyan-400/20 px-1 text-[9px] text-cyan-300">实时</span>
                      <span>
                        量速 {row.volumeFunds.volumeSpeed == null
                          ? '--'
                          : `${row.volumeFunds.volumeSpeed.toFixed(2)}×`}
                      </span>
                    </div>
                    <div className="mt-1 flex items-center gap-2 font-mono text-[10px] text-muted">
                      <span data-testid={`active-funds-stable-badge-${item.symbol}`} className="rounded border border-border px-1 text-[9px] text-muted">稳</span>
                      <span>
                        主买 {row.volumeFunds.activeFunds.buyRatioPct == null
                          ? '未确认'
                          : `${row.volumeFunds.activeFunds.buyRatioPct.toFixed(0)}%`}
                      </span>
                      <span data-testid={`depth-pressure-live-badge-${item.symbol}`} className="rounded border border-cyan-400/20 px-1 text-[9px] text-cyan-300">实时</span>
                      <span>{compactPercent('五档', row.volumeFunds.depthPressurePct)}</span>
                    </div>
                  </td>
                  <td data-testid={`breakout-risk-${item.symbol}`} className="whitespace-nowrap px-3 py-2">
                    <div className="flex items-center gap-2">
                      <span className="rounded border border-cyan-400/20 px-1 text-[9px] text-cyan-300">实时</span>
                      <span>{distancePercent('高', row.breakoutRisk.toDayHighPct)}</span>
                      <span className="rounded border border-cyan-400/20 px-1 text-[9px] text-cyan-300">实时</span>
                      <span>{distancePercent('低', row.breakoutRisk.fromDayLowPct)}</span>
                    </div>
                    <div className="mt-1 flex items-center gap-2 font-mono text-[10px] text-muted">
                      <span>{compactPercent('ATR14', row.breakoutRisk.atr14Pct)}</span>
                      <span>确认 {row.breakoutRisk.confirmedTimeframes}/{row.breakoutRisk.totalTimeframes}</span>
                      {row.breakoutRisk.riskTitle && <span>{row.breakoutRisk.riskTitle}</span>}
                    </div>
                  </td>
                  <td className="min-w-32 whitespace-nowrap px-3 py-2">
                    {row.delayed && (
                      <div className="mb-1 text-[10px] font-medium text-amber-400">
                        数据延迟
                      </div>
                    )}
                    {row.signal ? (
                      <div>
                        <span className={cn(
                          'inline-flex rounded border px-1.5 py-0.5 font-medium',
                          signalClass(row.signal),
                        )}>
                          {row.signal.label}
                        </span>
                        {signalTime(row.signal.occurredAt) && (
                          <div className="mt-0.5 font-mono text-[10px] text-muted">
                            {signalTime(row.signal.occurredAt)}
                          </div>
                        )}
                      </div>
                    ) : (
                      <span className="text-muted">{row.delayed ? '暂停新信号' : '观察'}</span>
                    )}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2">
                    <button
                      type="button"
                      aria-label={`查看详情 ${item.symbol}`}
                      onClick={(event) => {
                        stop(event)
                        onSelect(item.symbol)
                      }}
                      className="rounded-btn border border-accent/30 px-2.5 py-1.5 font-medium text-accent transition-colors hover:bg-accent/10"
                    >
                      查看详情
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-3 py-2 text-xs">
        <span className="text-muted">第 {page} / {pageCount} 页 · 共 {total} 只</span>
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            aria-label="上一页"
            disabled={page <= 1}
            onClick={() => onPageChange(page - 1)}
            className="rounded-btn border border-border px-2.5 py-1 text-secondary disabled:cursor-not-allowed disabled:opacity-40"
          >
            上一页
          </button>
          <button
            type="button"
            aria-label="下一页"
            disabled={page >= pageCount}
            onClick={() => onPageChange(page + 1)}
            className="rounded-btn border border-border px-2.5 py-1 text-secondary disabled:cursor-not-allowed disabled:opacity-40"
          >
            下一页
          </button>
        </div>
      </footer>
    </section>
  )
}
