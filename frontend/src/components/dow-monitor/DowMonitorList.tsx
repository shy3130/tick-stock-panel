import { Pause, Play, Trash2 } from 'lucide-react'
import type { KeyboardEvent, MouseEvent, ReactNode } from 'react'

import type { RealtimeSymbolState } from '@/lib/realtimeMarketData'
import { cn } from '@/lib/cn'

import { DowMonitorSparkline } from './DowMonitorSparkline'
import { KeyInterpretationCell } from './KeyInterpretationCell'
import { deriveInterpretationMarketContext } from './interpretationMarketContext'
import { deriveKeyInterpretation } from './keyInterpretation'
import {
  deriveMonitorRow,
  type MonitorMomentum,
  type MonitorSignal,
  type MonitorSourceFreshness,
} from './monitorListPresentation'
import type {
  DowMonitorNotification,
  DowMonitorOverviewSymbol,
} from './types'
import { formatServerTimestamp } from './formatServerTimestamp'
import {
  SUDDEN_ANOMALY_METRICS,
  suddenAnomalyKey,
  type SuddenAnomalyMetric,
  type SuddenAnomalySymbolReading,
} from './suddenAnomalyHighlights'
import { useSuddenAnomalyHighlights } from './useSuddenAnomalyHighlights'

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

function vwapText(price: number | null, distancePct: number | null): string {
  if (price == null && distancePct == null) return 'VWAP --'
  return `VWAP ${numberText(price)} / ${percentText(distancePct)}`
}

function ratioText(label: string, value: number | null): string {
  return value == null ? `${label} --` : `${label} ${value.toFixed(2)}×`
}

function freshnessText(label: string, value: MonitorSourceFreshness): string {
  return `${label}${value.ageSeconds == null ? '--' : `${value.ageSeconds}s`}`
}

function signalTime(value: string | null): string | null {
  const formatted = formatServerTimestamp(value)
  if (!formatted) return null
  const match = /(\d{2}:\d{2})$/.exec(formatted)
  return match?.[1] ?? formatted
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

function AnomalyMetric({
  active,
  metric,
  symbol,
  label,
  className,
  children,
}: {
  active: boolean
  metric: SuddenAnomalyMetric
  symbol: string
  label: string
  className?: string
  children: ReactNode
}) {
  return (
    <span
      data-testid={`anomaly-${metric}-${symbol}`}
      aria-label={active ? `${label}，突发异动` : label}
      className={cn(
        'inline-flex items-center gap-1',
        className,
        active && 'rounded border border-danger bg-danger/10 px-1 py-0.5 text-danger',
      )}
    >
      {children}
      {active && <span className="text-[9px] font-semibold">异动</span>}
    </span>
  )
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

  const presentedItems = items.map(item => {
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
    return { item, row, realtime }
  })
  const anomalyReadings: SuddenAnomalySymbolReading[] = presentedItems.map(
    ({ item, row }) => ({
      symbol: item.symbol,
      metrics: {
        changePct: {
          value: row.changePct,
          delayed: forceDelayed || row.freshness.quote.delayed,
        },
        momentum1m: {
          value: row.momentumSpeed.momentum1m.valuePct,
          delayed: forceDelayed || row.freshness.candlestick.delayed,
        },
        volumeSpeed: {
          value: row.volumeFunds.volumeSpeed,
          delayed: forceDelayed || row.freshness.candlestick.delayed,
        },
        depthPressurePct: {
          value: row.volumeFunds.depthPressurePct,
          delayed: forceDelayed || row.freshness.depth.delayed,
        },
        toDayHighPct: {
          value: row.breakoutRisk.toDayHighPct,
          delayed: forceDelayed || row.freshness.quote.delayed,
        },
        fromDayLowPct: {
          value: row.breakoutRisk.fromDayLowPct,
          delayed: forceDelayed || row.freshness.quote.delayed,
        },
      },
    }),
  )
  const anomalyHighlights = useSuddenAnomalyHighlights(anomalyReadings)
  const isAnomaly = (symbol: string, metric: SuddenAnomalyMetric) =>
    anomalyHighlights.has(suddenAnomalyKey(symbol, metric))
  const interpretedItems = presentedItems.map(({ item, row, realtime }) => ({
    item,
    row,
    interpretation: deriveKeyInterpretation({
      context: deriveInterpretationMarketContext({ item, row, realtime }),
      anomalies: new Set(
        SUDDEN_ANOMALY_METRICS.filter(metric => isAnomaly(item.symbol, metric)),
      ),
    }),
  }))

  return (
    <section aria-label="股票监控列表" className="overflow-hidden rounded-card border border-border bg-surface">
      <div data-testid="dow-monitor-table-scroll" className="max-w-full overflow-x-auto">
        <table className="w-full min-w-[1980px] border-collapse text-xs">
          <thead className="bg-elevated/70 text-[11px] text-muted">
            <tr>
              <th scope="col" className="whitespace-nowrap border-b border-border px-3 py-2 text-left font-medium">股票</th>
              <th scope="col" className="whitespace-nowrap border-b border-border px-3 py-2 text-left font-medium">价格 / 涨跌</th>
              <th scope="col" className="whitespace-nowrap border-b border-border px-3 py-2 text-left font-medium">日内走势</th>
              <th scope="col" className="min-w-[320px] whitespace-nowrap border-b border-border px-3 py-2 text-left font-medium">
                重点解读
                <span className="block text-[9px] text-muted">结论 · 市场行为 · 关键价</span>
              </th>
              <th scope="col" className="whitespace-nowrap border-b border-border px-3 py-2 text-left font-medium">
                趋势 / 位置
                <span className="block text-[9px] text-muted">通道 · 控制线 · VWAP</span>
              </th>
              <th scope="col" className="whitespace-nowrap border-b border-border px-3 py-2 text-left font-medium">
                动量 / 涨速
                <span className="block text-[9px] text-muted">1m 实时 · 5m/15m 稳</span>
              </th>
              <th scope="col" className="whitespace-nowrap border-b border-border px-3 py-2 text-left font-medium">
                量价 / 资金
                <span className="block text-[9px] text-muted">量比 · 量速 · 资金流入 · 五档</span>
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
            {interpretedItems.map(({ item, row, interpretation }) => {
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
                      <AnomalyMetric
                        active={isAnomaly(item.symbol, 'changePct')}
                        metric="changePct"
                        symbol={item.symbol}
                        label={`涨跌幅 ${percentText(row.changePct)}`}
                      >
                        {percentText(row.changePct)}
                      </AnomalyMetric>
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <DowMonitorSparkline symbol={item.symbol} values={row.sparkline} />
                  </td>
                  <td className="min-w-[320px] px-3 py-2">
                    <KeyInterpretationCell interpretation={interpretation} />
                  </td>
                  <td data-testid={`trend-position-${item.symbol}`} className="whitespace-nowrap px-3 py-2">
                    <div data-testid={`trend-position-primary-row-${item.symbol}`} className="flex items-center gap-2">
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
                    <div data-testid={`trend-position-secondary-row-${item.symbol}`} className="mt-1 flex items-center gap-2 font-mono text-[10px] text-muted">
                      <span>{compactPercent('控制', row.trendPosition.control?.distancePct ?? null)}</span>
                      <span>{vwapText(
                        row.trendPosition.vwap.price,
                        row.trendPosition.vwap.distancePct,
                      )}</span>
                    </div>
                  </td>
                  <td data-testid={`momentum-speed-${item.symbol}`} className="whitespace-nowrap px-3 py-2">
                    <div data-testid={`momentum-speed-primary-row-${item.symbol}`} className="flex items-center gap-2">
                      <span className="rounded border border-cyan-400/20 px-1 text-[9px] text-cyan-300">实时</span>
                      <AnomalyMetric
                        active={isAnomaly(item.symbol, 'momentum1m')}
                        metric="momentum1m"
                        symbol={item.symbol}
                        label={`1m 涨速 ${momentumText(row.momentumSpeed.momentum1m)}`}
                        className="font-semibold"
                      >
                        1m {momentumText(row.momentumSpeed.momentum1m)}
                      </AnomalyMetric>
                    </div>
                    <div data-testid={`momentum-speed-secondary-row-${item.symbol}`} className="mt-1 flex items-center gap-2 font-mono text-[10px] text-muted">
                      <span>5m {momentumText(row.momentumSpeed.momentum5m)}</span>
                      <span>15m {momentumText(row.momentumSpeed.momentum15m)}</span>
                    </div>
                  </td>
                  <td data-testid={`volume-funds-${item.symbol}`} className="whitespace-nowrap px-3 py-2">
                    <div data-testid={`volume-funds-primary-row-${item.symbol}`} className="flex items-center gap-2">
                      <span data-testid={`relative-volume-stable-badge-${item.symbol}`} className="rounded border border-border px-1 text-[9px] text-muted">稳</span>
                      <strong>
                        量比 {row.volumeFunds.relativeVolume
                          ? `${row.volumeFunds.relativeVolume.ratio.toFixed(2)}×`
                          : '--'}
                      </strong>
                      <span data-testid={`volume-speed-live-badge-${item.symbol}`} className="rounded border border-cyan-400/20 px-1 text-[9px] text-cyan-300">实时</span>
                      <AnomalyMetric
                        active={isAnomaly(item.symbol, 'volumeSpeed')}
                        metric="volumeSpeed"
                        symbol={item.symbol}
                        label={`1m 量速 ${ratioText('', row.volumeFunds.volumeSpeed).trim()}`}
                      >
                        量速 {row.volumeFunds.volumeSpeed == null
                          ? '--'
                          : `${row.volumeFunds.volumeSpeed.toFixed(2)}×`}
                      </AnomalyMetric>
                    </div>
                    <div data-testid={`volume-funds-secondary-row-${item.symbol}`} className="mt-1 flex items-center gap-2 font-mono text-[10px] text-muted">
                      <span data-testid={`capital-inflow-stable-badge-${item.symbol}`} className="rounded border border-border px-1 text-[9px] text-muted">稳</span>
                      <span>
                        资金流入 {row.volumeFunds.capitalInflow.inflowRatioPct == null
                          ? '未确认'
                          : `${row.volumeFunds.capitalInflow.inflowRatioPct.toFixed(0)}%`}
                      </span>
                      <span data-testid={`depth-pressure-live-badge-${item.symbol}`} className="rounded border border-cyan-400/20 px-1 text-[9px] text-cyan-300">实时</span>
                      <AnomalyMetric
                        active={isAnomaly(item.symbol, 'depthPressurePct')}
                        metric="depthPressurePct"
                        symbol={item.symbol}
                        label={`五档盘口 ${percentText(row.volumeFunds.depthPressurePct)}`}
                      >
                        {compactPercent('五档', row.volumeFunds.depthPressurePct)}
                      </AnomalyMetric>
                    </div>
                  </td>
                  <td data-testid={`breakout-risk-${item.symbol}`} className="whitespace-nowrap px-3 py-2">
                    <div data-testid={`breakout-risk-primary-row-${item.symbol}`} className="flex items-center gap-2">
                      <span className="rounded border border-cyan-400/20 px-1 text-[9px] text-cyan-300">实时</span>
                      <AnomalyMetric
                        active={isAnomaly(item.symbol, 'toDayHighPct')}
                        metric="toDayHighPct"
                        symbol={item.symbol}
                        label={`距日高 ${numberText(row.breakoutRisk.toDayHighPct)}%`}
                      >
                        {distancePercent('高', row.breakoutRisk.toDayHighPct)}
                      </AnomalyMetric>
                      <span className="rounded border border-cyan-400/20 px-1 text-[9px] text-cyan-300">实时</span>
                      <AnomalyMetric
                        active={isAnomaly(item.symbol, 'fromDayLowPct')}
                        metric="fromDayLowPct"
                        symbol={item.symbol}
                        label={`距日低 ${numberText(row.breakoutRisk.fromDayLowPct)}%`}
                      >
                        {distancePercent('低', row.breakoutRisk.fromDayLowPct)}
                      </AnomalyMetric>
                      <span>位置 {row.trendPosition.intradayPositionPct == null
                        ? '--'
                        : row.trendPosition.intradayPositionPct.toFixed(0)}</span>
                    </div>
                    <div data-testid={`breakout-risk-secondary-row-${item.symbol}`} className="mt-1 flex items-center gap-2 font-mono text-[10px] text-muted">
                      <span>{compactPercent('ATR14', row.breakoutRisk.atr14Pct)}</span>
                      <span>{ratioText('振幅/ATR', row.breakoutRisk.dayRangeAtrRatio)}</span>
                      <span>周期 {row.breakoutRisk.confirmedTimeframes}/{row.breakoutRisk.totalTimeframes}</span>
                      {row.breakoutRisk.confirmationTimeframes.map(confirmation => (
                        <span
                          key={confirmation.timeframe}
                          className={confirmation.confirmed ? 'text-foreground' : 'text-muted'}
                        >
                          {confirmation.timeframe}{confirmation.confirmed ? '✓' : '○'}
                        </span>
                      ))}
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
                            北京时间 {signalTime(row.signal.occurredAt)}
                          </div>
                        )}
                      </div>
                    ) : (
                      <span className="text-muted">{row.delayed ? '暂停新信号' : '观察'}</span>
                    )}
                    <div
                      data-testid={`freshness-${item.symbol}`}
                      aria-label={[
                        '数据时效',
                        freshnessText('行情', row.freshness.quote),
                        freshnessText('盘口', row.freshness.depth),
                        freshnessText('1m K线', row.freshness.candlestick),
                        freshnessText('分析', row.freshness.analysis),
                      ].join('，')}
                      className="mt-1 flex items-center gap-1 font-mono text-[9px] text-muted"
                      title="行情、盘口、1m K线、后端分析的数据年龄"
                    >
                      <span>时效</span>
                      {([
                        ['行', row.freshness.quote],
                        ['盘', row.freshness.depth],
                        ['K', row.freshness.candlestick],
                        ['析', row.freshness.analysis],
                      ] as const).map(([label, value]) => (
                        <span
                          key={label}
                          className={value.delayed ? 'text-amber-400' : undefined}
                        >
                          {freshnessText(label, value)}
                        </span>
                      ))}
                    </div>
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
