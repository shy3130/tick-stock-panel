import { useLayoutEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  Activity,
  BellRing,
  BookOpenCheck,
  ChevronDown,
  CircleAlert,
  Download,
  Loader2,
  Moon,
  Newspaper,
  RefreshCw,
  Sun,
  WalletCards,
} from 'lucide-react'

import { PageHeader } from '@/components/PageHeader'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import { getNavIconMeta } from '@/lib/navRegistry'
import { loadDailyBriefingSources } from './api'
import {
  buildDailyBriefing,
  type BriefingMode,
  type BriefingTone,
  type DailyBriefing,
} from './briefing'
import { dailyBriefingFilename, dailyBriefingMarkdown } from './briefingMarkdown'
import type { EventDirection, EventPriorityLevel } from './eventPriority'

const moneyFormatter = new Intl.NumberFormat('zh-CN', {
  style: 'currency',
  currency: 'CNY',
  maximumFractionDigits: 0,
})

function money(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return '--'
  return moneyFormatter.format(value)
}

function signedMoney(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return '--'
  return `${value > 0 ? '+' : ''}${moneyFormatter.format(value)}`
}

function percent(value: number | null, ratio = false): string {
  if (value == null || !Number.isFinite(value)) return '--'
  const normalized = ratio ? value * 100 : value
  const display = Math.abs(normalized) < 0.005 ? 0 : normalized
  return `${display > 0 ? '+' : ''}${display.toFixed(2)}%`
}

function metricClass(value: number | null): string {
  if (value == null || value === 0) return 'text-foreground'
  return value > 0 ? 'text-bull' : 'text-bear'
}

function toneClass(tone: BriefingTone): string {
  return {
    neutral: 'border-l-accent',
    positive: 'border-l-bull',
    warning: 'border-l-warning',
    danger: 'border-l-danger',
  }[tone]
}

function formatAlertTime(timestamp: number): string {
  return new Date(timestamp).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function formatGeneratedAt(value: string): string {
  return new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

function priorityClass(level: EventPriorityLevel): string {
  return {
    high: 'border-danger/30 bg-danger/10 text-danger',
    medium: 'border-warning/30 bg-warning/10 text-warning',
    normal: 'border-border bg-elevated text-secondary',
  }[level]
}

function directionMeta(direction: EventDirection): { label: string; className: string } {
  return {
    risk: { label: '风险', className: 'bg-danger/10 text-danger' },
    opportunity: { label: '机会', className: 'bg-bull/10 text-bull' },
    observe: { label: '观察', className: 'bg-accent/10 text-accent' },
  }[direction]
}

function sourceLabel(source: string): string {
  return {
    strategy: '策略',
    signal: '信号',
    price: '价格',
    market: '市场',
    ladder: '封单',
  }[source] ?? source
}

function conditionText(condition: { field: string; op: string; value?: number | null }): string {
  return condition.op === 'truth'
    ? condition.field
    : `${condition.field} ${condition.op} ${condition.value ?? '--'}`
}

function downloadBriefing(briefing: DailyBriefing) {
  const blob = new Blob([dailyBriefingMarkdown(briefing)], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = dailyBriefingFilename(briefing)
  anchor.click()
  URL.revokeObjectURL(url)
}

function SummaryCell({ label, value, hint, valueClassName }: {
  label: string
  value: string
  hint: string
  valueClassName?: string
}) {
  return (
    <div className="min-w-0 px-4 py-3 lg:px-5 lg:py-4">
      <div className="text-[11px] font-medium text-muted">{label}</div>
      <div className={cn('mt-1 truncate font-mono text-lg font-semibold tabular-nums text-foreground', valueClassName)} title={value}>{value}</div>
      <div className="mt-1 truncate text-[10px] text-muted" title={hint}>{hint}</div>
    </div>
  )
}

function SectionHeader({ id, icon: Icon, title, aside }: {
  id: string
  icon: typeof Newspaper
  title: string
  aside?: string
}) {
  return (
    <div className="flex min-h-12 items-center justify-between gap-3 border-b border-border px-4 py-2.5">
      <div className="flex min-w-0 items-center gap-2.5">
        <Icon className="h-4 w-4 shrink-0 text-accent" />
        <h2 id={id} className="truncate text-sm font-semibold text-foreground">{title}</h2>
      </div>
      {aside && <span className="shrink-0 font-mono text-[10px] text-muted">{aside}</span>}
    </div>
  )
}

export function DailyBriefingPage() {
  const navMeta = getNavIconMeta('/daily-briefing')
  const [mode, setMode] = useState<BriefingMode>(() => new Date().getHours() < 12 ? 'morning' : 'evening')
  const sources = useQuery({
    queryKey: ['sycee', 'daily-briefing'],
    queryFn: loadDailyBriefingSources,
    staleTime: 30_000,
  })

  useLayoutEffect(() => {
    document.querySelector('main')?.scrollTo({ top: 0 })
  }, [])

  const briefing = useMemo(
    () => sources.data ? buildDailyBriefing(sources.data, mode) : null,
    [mode, sources.data],
  )

  const refresh = async () => {
    const result = await sources.refetch()
    if (result.data) toast('日报数据已刷新', 'success')
  }

  return (
    <>
      <PageHeader
        title="每日简报"
        subtitle={briefing ? `${briefing.asOf} · ${formatGeneratedAt(briefing.generatedAt)} 生成` : '正在聚合个人数据'}
        icon={navMeta?.icon}
        group={navMeta?.group}
        rightClassName="overflow-visible"
        right={(
          <div className="flex min-w-max items-center justify-end gap-2">
            <div className="grid grid-cols-2 rounded-btn border border-border bg-base p-0.5" aria-label="简报时段">
              {([
                ['morning', '晨报', Sun],
                ['evening', '晚报', Moon],
              ] as const).map(([value, label, Icon]) => (
                <button
                  key={value}
                  type="button"
                  aria-pressed={mode === value}
                  onClick={() => setMode(value)}
                  className={cn(
                    'inline-flex min-h-10 items-center justify-center gap-1.5 rounded-input px-3 text-xs transition-colors lg:min-h-8',
                    mode === value ? 'bg-elevated text-foreground shadow-sm' : 'text-muted hover:text-foreground',
                  )}
                >
                  <Icon className="h-3.5 w-3.5" />{label}
                </button>
              ))}
            </div>
            <button
              type="button"
              title="刷新日报"
              aria-label="刷新日报"
              disabled={sources.isFetching}
              onClick={() => { void refresh() }}
              className="flex h-10 w-10 items-center justify-center rounded-btn border border-border text-secondary hover:bg-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50 lg:h-9 lg:w-9"
            >
              <RefreshCw className={cn('h-4 w-4', sources.isFetching && 'animate-spin')} />
            </button>
            <button
              type="button"
              onClick={() => { if (briefing) downloadBriefing(briefing) }}
              disabled={!briefing}
              className="inline-flex min-h-10 items-center gap-2 rounded-btn bg-accent px-3 text-xs font-medium text-white hover:bg-accent/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50 lg:min-h-9"
            >
              <Download className="h-4 w-4" />导出
            </button>
          </div>
        )}
      />

      {sources.isLoading ? (
        <div className="flex min-h-[50vh] items-center justify-center gap-2 text-sm text-muted">
          <Loader2 className="h-4 w-4 animate-spin" />生成个人简报
        </div>
      ) : sources.isError || !briefing ? (
        <div className="flex min-h-[50vh] flex-col items-center justify-center gap-3 px-6 text-center">
          <CircleAlert className="h-8 w-8 text-danger" />
          <p className="text-sm font-medium text-foreground">日报生成失败</p>
          <button type="button" onClick={() => { void sources.refetch() }} className="inline-flex min-h-10 items-center gap-2 rounded-btn border border-border px-4 text-sm text-secondary hover:bg-elevated">
            <RefreshCw className="h-4 w-4" />重新读取
          </button>
        </div>
      ) : (
        <div className="space-y-4 p-3 lg:p-5">
          {briefing.unavailable.length > 0 && (
            <div className="flex items-start gap-2.5 rounded-card border border-warning/30 bg-warning/10 px-3 py-2.5 text-xs leading-5 text-warning" role="status">
              <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" />
              <span>本次未能读取：{briefing.unavailable.join('、')}。相关段落已按缺失状态展示。</span>
            </div>
          )}

          <section aria-label="日报摘要" className="grid grid-cols-2 overflow-hidden rounded-card border border-border bg-surface lg:grid-cols-4">
            <SummaryCell
              label="市场情绪"
              value={briefing.market.label}
              hint={briefing.market.score == null ? '暂无情绪分' : `情绪分 ${briefing.market.score.toFixed(0)} · 上涨率 ${percent(briefing.market.upPct)}`}
            />
            <div className="border-l border-border">
              <SummaryCell
                label="持仓浮盈"
                value={signedMoney(briefing.portfolio.unrealizedPnl)}
                hint={`${briefing.portfolio.positions.length} 只持仓 · ${percent(briefing.portfolio.floatingReturn, true)}`}
                valueClassName={metricClass(briefing.portfolio.unrealizedPnl)}
              />
            </div>
            <div className="border-t border-border lg:border-l lg:border-t-0">
              <SummaryCell
                label="重点事件"
                value={String(briefing.eventGroups.length)}
                hint={`原始 ${briefing.alerts.length} · 持仓 ${briefing.alertCounts.holding} · 自选 ${briefing.alertCounts.watchlist}`}
                valueClassName={briefing.alertCounts.holding > 0 ? 'text-warning' : undefined}
              />
            </div>
            <div className="border-l border-t border-border lg:border-t-0">
              <SummaryCell
                label="待跟进"
                value={String(briefing.staleTrackCount + briefing.openResearchCount)}
                hint={`策略 ${briefing.staleTrackCount} · 研究 ${briefing.openResearchCount}`}
              />
            </div>
          </section>

          <div className="grid gap-4 xl:grid-cols-[minmax(0,1.45fr)_minmax(320px,0.75fr)]">
            <section aria-labelledby="briefing-focus-title" className="overflow-hidden rounded-card border border-border bg-surface">
              <SectionHeader id="briefing-focus-title" icon={Newspaper} title={mode === 'morning' ? '盘前关注' : '收盘检查'} aside={`${briefing.focus.length} 项`} />
              <div className="divide-y divide-border/70">
                {briefing.focus.map(item => {
                  const content = (
                    <div className={cn('border-l-2 px-4 py-3', toneClass(item.tone))}>
                      <div className="text-sm font-medium text-foreground">{item.title}</div>
                      <div className="mt-1 text-xs leading-5 text-muted">{item.detail}</div>
                    </div>
                  )
                  return item.href ? (
                    <Link key={item.id} to={item.href} className="block transition-colors hover:bg-elevated/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent">{content}</Link>
                  ) : <div key={item.id}>{content}</div>
                })}
              </div>
            </section>

            <section aria-labelledby="briefing-market-title" className="overflow-hidden rounded-card border border-border bg-surface">
              <SectionHeader id="briefing-market-title" icon={BookOpenCheck} title="市场状态" aside={briefing.asOf} />
              <div className="grid grid-cols-3 divide-x divide-border border-b border-border">
                <div className="px-3 py-3 text-center"><div className="text-[10px] text-muted">上涨</div><div className="mt-1 font-mono text-base font-semibold text-bull">{briefing.market.up ?? '--'}</div></div>
                <div className="px-3 py-3 text-center"><div className="text-[10px] text-muted">平盘</div><div className="mt-1 font-mono text-base font-semibold text-muted">{briefing.market.flat ?? '--'}</div></div>
                <div className="px-3 py-3 text-center"><div className="text-[10px] text-muted">下跌</div><div className="mt-1 font-mono text-base font-semibold text-bear">{briefing.market.down ?? '--'}</div></div>
              </div>
              <div className="px-4 py-3">
                <div className="flex items-center justify-between gap-3 text-xs"><span className="text-muted">涨停 / 炸板 / 跌停</span><span className="font-mono text-secondary">{briefing.market.limitUp ?? '--'} / {briefing.market.broken ?? '--'} / {briefing.market.limitDown ?? '--'}</span></div>
                {briefing.market.leaders.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {briefing.market.leaders.map(item => (
                      <span key={`${item.kind}-${item.name}`} className="inline-flex items-center gap-1 rounded-input border border-border bg-base px-2 py-1 text-[10px] text-secondary">
                        <span className="text-muted">{item.kind}</span>{item.name}<span className={metricClass(item.avgPct)}>{percent(item.avgPct)}</span>
                      </span>
                    ))}
                  </div>
                )}
                {briefing.market.recapSummary && <p className="mt-3 border-t border-border pt-3 text-xs leading-5 text-secondary">{briefing.market.recapSummary}</p>}
              </div>
            </section>
          </div>

          <section aria-labelledby="briefing-portfolio-title" className="overflow-hidden rounded-card border border-border bg-surface">
            <SectionHeader id="briefing-portfolio-title" icon={WalletCards} title="当前持仓" aside={`${briefing.portfolio.positions.length} 只 · 市值 ${money(briefing.portfolio.marketValue)}`} />
            {briefing.portfolio.positions.length === 0 ? (
              <div className="px-4 py-8 text-center text-xs text-muted">暂无持仓</div>
            ) : (
              <>
                <div className="hidden grid-cols-[minmax(180px,1.4fr)_100px_110px_110px_120px_100px] gap-3 border-b border-border bg-base/40 px-4 py-2 text-[10px] font-medium text-muted md:grid">
                  <span>标的</span><span className="text-right">数量</span><span className="text-right">现价</span><span className="text-right">当日</span><span className="text-right">浮动盈亏</span><span className="text-right">持仓收益</span>
                </div>
                <div className="divide-y divide-border/70">
                  {briefing.portfolio.positions.map(position => (
                    <div key={position.symbol} className="px-4 py-3 md:grid md:grid-cols-[minmax(180px,1.4fr)_100px_110px_110px_120px_100px] md:items-center md:gap-3">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium text-foreground">{position.name}</div>
                        <div className="mt-0.5 flex items-center gap-2 font-mono text-[10px] text-muted"><span>{position.symbol}</span><span>{position.isLive ? '实盘' : position.quoteDate || '待补行情'}</span></div>
                      </div>
                      <div className="mt-3 grid grid-cols-3 gap-3 md:contents">
                        <div className="md:text-right"><div className="text-[10px] text-muted md:hidden">数量</div><div className="mt-0.5 font-mono text-xs text-secondary md:mt-0">{position.quantity.toLocaleString('zh-CN')}</div></div>
                        <div className="text-right"><div className="text-[10px] text-muted md:hidden">现价</div><div className="mt-0.5 font-mono text-xs text-secondary md:mt-0">{position.currentPrice?.toFixed(2) ?? '--'}</div></div>
                        <div className="text-right"><div className="text-[10px] text-muted md:hidden">当日</div><div className={cn('mt-0.5 font-mono text-xs md:mt-0', metricClass(position.dailyChangePct))}>{percent(position.dailyChangePct)}</div></div>
                        <div className="md:text-right"><div className="text-[10px] text-muted md:hidden">浮动盈亏</div><div className={cn('mt-0.5 font-mono text-xs font-semibold md:mt-0', metricClass(position.unrealizedPnl))}>{signedMoney(position.unrealizedPnl)}</div></div>
                        <div className="text-right"><div className="text-[10px] text-muted md:hidden">持仓收益</div><div className={cn('mt-0.5 font-mono text-xs md:mt-0', metricClass(position.returnPct))}>{percent(position.returnPct, true)}</div></div>
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}
          </section>

          <section aria-labelledby="briefing-alerts-title" className="overflow-hidden rounded-card border border-border bg-surface">
            <SectionHeader id="briefing-alerts-title" icon={BellRing} title="重点事件与证据" aside={`${mode === 'morning' ? '近 24 小时' : '今日'} · 按优先级`} />
            {briefing.eventGroups.length === 0 ? (
              <div className="px-4 py-8 text-center text-xs text-muted">报告窗口内暂无相关提醒</div>
            ) : (
              <div className="divide-y divide-border/70">
                {briefing.eventGroups.map(group => {
                  const direction = directionMeta(group.direction)
                  return (
                    <details key={group.id} className="group">
                      <summary className="cursor-pointer list-none px-4 py-3 transition-colors hover:bg-elevated/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent [&::-webkit-details-marker]:hidden">
                        <div className="flex items-start gap-3">
                          <span className={cn('flex h-10 w-10 shrink-0 flex-col items-center justify-center rounded-btn border font-mono', priorityClass(group.level))}>
                            <span className="text-sm font-semibold leading-none">{group.score}</span>
                            <span className="mt-0.5 text-[8px] leading-none">优先级</span>
                          </span>
                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-center gap-1.5">
                              <span className="text-sm font-medium text-foreground">{group.name}</span>
                              <span className="font-mono text-[10px] text-muted">{group.symbol}</span>
                              <span className={cn('rounded-input px-1.5 py-0.5 text-[9px]', group.scope === 'holding' ? 'bg-warning/10 text-warning' : 'bg-accent/10 text-accent')}>{group.scope === 'holding' ? '持仓' : '自选'}</span>
                              <span className={cn('rounded-input px-1.5 py-0.5 text-[9px]', direction.className)}>{direction.label}</span>
                            </div>
                            <div className="mt-1 line-clamp-2 text-xs leading-5 text-secondary">{group.primary.message}</div>
                            <div className="mt-2 flex flex-wrap gap-1">
                              {group.reasons.map(reason => (
                                <span key={reason.key} className="rounded-input border border-border bg-base px-1.5 py-0.5 text-[9px] text-muted">{reason.label} <span className="font-mono text-secondary">+{reason.points}</span></span>
                              ))}
                            </div>
                          </div>
                          <div className="flex shrink-0 items-center gap-2">
                            <span className="font-mono text-[10px] text-muted">{group.evidence.length} 条</span>
                            <ChevronDown className="h-4 w-4 text-muted transition-transform group-open:rotate-180" />
                          </div>
                        </div>
                      </summary>
                      <div className="border-t border-border bg-base/40 px-4 py-3 sm:pl-[4.25rem]">
                        <div className="space-y-3 border-l border-border pl-3">
                          {group.evidence.map((alert, index) => (
                            <div key={`${alert.ts}-${alert.rule_id ?? alert.type}-${index}`} className="relative">
                              <span className="absolute -left-[0.95rem] top-1.5 h-1.5 w-1.5 rounded-full bg-accent" aria-hidden="true" />
                              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                                <span className="font-mono text-[10px] text-muted">{formatAlertTime(alert.ts)}</span>
                                <span className="rounded-input bg-elevated px-1.5 py-0.5 text-[9px] text-secondary">{alert.rule_name || sourceLabel(alert.source)}</span>
                                {alert.severity && <span className="text-[9px] text-muted">{alert.severity}</span>}
                              </div>
                              <div className="mt-1 text-xs leading-5 text-secondary">{alert.message}</div>
                              {(alert.conditions?.length || alert.signals?.length) ? (
                                <div className="mt-1 flex flex-wrap gap-1">
                                  {alert.conditions?.map((condition, conditionIndex) => (
                                    <span key={`condition-${conditionIndex}`} className="rounded-input bg-accent/8 px-1.5 py-0.5 font-mono text-[9px] text-accent">{conditionText(condition)}</span>
                                  ))}
                                  {alert.signals?.map(signal => (
                                    <span key={signal} className="rounded-input bg-elevated px-1.5 py-0.5 font-mono text-[9px] text-muted">{signal}</span>
                                  ))}
                                </div>
                              ) : null}
                            </div>
                          ))}
                        </div>
                      </div>
                    </details>
                  )
                })}
              </div>
            )}
          </section>

          <div className="grid gap-4 lg:grid-cols-2">
            <section aria-labelledby="briefing-tracks-title" className="overflow-hidden rounded-card border border-border bg-surface">
              <SectionHeader id="briefing-tracks-title" icon={Activity} title="策略跟踪" aside={`${briefing.staleTrackCount} 项待更新`} />
              {briefing.tracks.length === 0 ? (
                <div className="px-4 py-8 text-center text-xs text-muted">暂无策略跟踪计划</div>
              ) : (
                <div className="divide-y divide-border/70">
                  {briefing.tracks.slice(0, 6).map(track => (
                    <Link key={track.id} to="/strategy-tracking" className="flex min-h-14 items-center justify-between gap-3 px-4 py-2.5 hover:bg-elevated/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent">
                      <div className="min-w-0"><div className="truncate text-xs font-medium text-foreground">{track.name}</div><div className="mt-1 font-mono text-[10px] text-muted">{track.latest ? `${track.latest.end_date} · ${percent(track.latest.total_return, true)}` : '暂无快照'}</div></div>
                      <span className={cn('shrink-0 rounded-input px-2 py-1 text-[10px]', track.pending ? 'bg-warning/10 text-warning' : track.status === 'tracking' ? 'bg-bull/10 text-bull' : 'bg-elevated text-muted')}>{track.pending ? '待更新' : track.status === 'tracking' ? '已对齐' : track.status === 'paused' ? '暂停' : '结束'}</span>
                    </Link>
                  ))}
                </div>
              )}
            </section>

            <section aria-labelledby="briefing-research-title" className="overflow-hidden rounded-card border border-border bg-surface">
              <SectionHeader id="briefing-research-title" icon={BookOpenCheck} title="研究动作" aside={`${briefing.openResearchCount} 项待跟进`} />
              {briefing.research.length === 0 ? (
                <div className="px-4 py-8 text-center text-xs text-muted">暂无待整理或跟踪中的研究</div>
              ) : (
                <div className="divide-y divide-border/70">
                  {briefing.research.map(entry => (
                    <Link key={entry.id} to="/research-ledger" className="block px-4 py-3 hover:bg-elevated/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent">
                      <div className="flex items-center justify-between gap-3"><div className="truncate text-xs font-medium text-foreground">{entry.title}</div><span className="shrink-0 text-[10px] text-muted">{entry.status === 'draft' ? '待整理' : '跟踪中'}</span></div>
                      <div className="mt-1 truncate text-[10px] text-secondary">{entry.plan || '尚未填写下一步'}</div>
                    </Link>
                  ))}
                </div>
              )}
            </section>
          </div>
        </div>
      )}
    </>
  )
}
