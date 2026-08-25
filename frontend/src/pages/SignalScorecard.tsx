import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle,
  BarChart3,
  ChevronDown,
  ChevronUp,
  Database,
  FlaskConical,
  Loader2,
  Play,
  RefreshCw,
  Save,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Target,
} from 'lucide-react'
import { EmptyState } from '@/components/EmptyState'
import { PageHeader } from '@/components/PageHeader'
import { toast } from '@/components/Toast'
import {
  api,
  type SignalScorecardEvent,
  type SignalScorecardOutcome,
  type SignalScorecardStat,
  type SignalScorecardTrackedItem,
} from '@/lib/api'
import { cn } from '@/lib/cn'
import { fmtPrice } from '@/lib/format'
import { BUILTIN_SIGNAL_DEFINITIONS } from '@/lib/signals'

const INPUT = 'control w-full text-xs'
const BTN_PRIMARY = 'btn-primary text-xs'
const BTN_GHOST = 'btn-secondary text-xs'
const CARD = 'panel'

const SCORECARD_ROOT = ['signal-scorecard'] as const
const HORIZON_LABEL: Record<number, string> = { 1: 'T+1', 3: 'T+3', 5: 'T+5', 10: 'T+10' }

type EventStatus = '' | 'pending' | 'mature'
type Direction = SignalScorecardTrackedItem['direction']

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : '请求未完成，请稍后重试。'
}

function signalName(key: string, configured: SignalScorecardTrackedItem[]): string {
  return configured.find((item) => item.signal_key === key)?.signal_name
    ?? BUILTIN_SIGNAL_DEFINITIONS.find((item) => item.id === key)?.name
    ?? key
}

function defaultDirection(signalKey: string): Direction {
  const kind = BUILTIN_SIGNAL_DEFINITIONS.find((item) => item.id === signalKey)?.kind
  return kind === 'exit' ? 'not_up' : 'up'
}

function formatPct(value: number | null): string {
  return value == null || Number.isNaN(value) ? '—' : `${value.toFixed(2)}%`
}

function isBackfillRangeValid(dateFrom: string, dateTo: string): boolean {
  if (!dateFrom || !dateTo || dateFrom > dateTo) return false
  const days = (new Date(`${dateTo}T00:00:00`).getTime() - new Date(`${dateFrom}T00:00:00`).getTime()) / 86_400_000
  return days <= 366
}

export function SignalScorecard() {
  const queryClient = useQueryClient()
  const [selectedSignal, setSelectedSignal] = useState('')
  const [eventStatus, setEventStatus] = useState<EventStatus>('')
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null)
  const [trackedDraft, setTrackedDraft] = useState<SignalScorecardTrackedItem[]>([])
  const [configDirty, setConfigDirty] = useState(false)
  const [builtinToAdd, setBuiltinToAdd] = useState('')
  const [backfillKeys, setBackfillKeys] = useState<string[]>([])
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [backfillAcknowledged, setBackfillAcknowledged] = useState(false)

  const trackedQuery = useQuery({
    queryKey: [...SCORECARD_ROOT, 'tracked'],
    queryFn: api.signalScorecardTracked,
  })

  useEffect(() => {
    if (!configDirty && trackedQuery.data) setTrackedDraft(trackedQuery.data.items)
  }, [configDirty, trackedQuery.data])

  const configured = trackedQuery.data?.items ?? []
  const enabledTracked = useMemo(() => configured.filter((item) => item.enabled), [configured])
  const hasEnabledTracking = enabledTracked.length > 0

  const statsQuery = useQuery({
    queryKey: [...SCORECARD_ROOT, 'stats', selectedSignal],
    queryFn: () => api.signalScorecardStats(selectedSignal || undefined),
    enabled: hasEnabledTracking,
  })
  const eventsQuery = useQuery({
    queryKey: [...SCORECARD_ROOT, 'events', selectedSignal, eventStatus],
    queryFn: () => api.signalScorecardEvents({
      signal_key: selectedSignal || undefined,
      status: eventStatus || undefined,
      limit: 200,
    }),
    enabled: hasEnabledTracking,
  })
  const detailQuery = useQuery({
    queryKey: [...SCORECARD_ROOT, 'event-detail', selectedEventId],
    queryFn: () => api.signalScorecardEventDetail(selectedEventId ?? ''),
    enabled: selectedEventId !== null,
  })

  const invalidateScorecard = () => {
    void queryClient.invalidateQueries({ queryKey: SCORECARD_ROOT })
  }

  const saveTrackedMutation = useMutation({
    mutationFn: (items: SignalScorecardTrackedItem[]) => api.signalScorecardUpdateTracked(items),
    onSuccess: ({ items }) => {
      queryClient.setQueryData([...SCORECARD_ROOT, 'tracked'], { items })
      setTrackedDraft(items)
      setConfigDirty(false)
      setBackfillKeys((current) => current.filter((key) => items.some((item) => item.signal_key === key && item.enabled)))
      toast('跟踪配置已保存；后续仅记录已启用信号。', 'success')
      invalidateScorecard()
    },
    onError: (error) => toast(`保存跟踪配置失败：${messageOf(error)}`),
  })

  const evaluateMutation = useMutation({
    mutationFn: api.signalScorecardEvaluate,
    onSuccess: () => {
      toast('已提交本地事件的成熟度评估。', 'success')
      invalidateScorecard()
    },
    onError: (error) => toast(`立即评估失败：${messageOf(error)}`),
  })

  const backfillMutation = useMutation({
    mutationFn: ({ keys, from, to }: { keys: string[]; from: string; to: string }) => api.signalScorecardBackfill(keys, from, to),
    onSuccess: () => {
      toast('历史事件回填已提交；结果仅用于回顾性研究。', 'success')
      invalidateScorecard()
    },
    onError: (error) => toast(`历史回填失败：${messageOf(error)}`),
  })

  const availableBuiltins = useMemo(
    () => BUILTIN_SIGNAL_DEFINITIONS.filter((definition) => !trackedDraft.some((item) => item.signal_key === definition.id)),
    [trackedDraft],
  )
  const displayStats = statsQuery.data?.stats ?? []
  const displayEvents = eventsQuery.data?.events ?? []
  const backfillRangeValid = isBackfillRangeValid(dateFrom, dateTo)

  const addBuiltin = () => {
    if (!builtinToAdd) return
    const definition = BUILTIN_SIGNAL_DEFINITIONS.find((item) => item.id === builtinToAdd)
    if (!definition) return
    setTrackedDraft((items) => [...items, {
      signal_key: definition.id,
      signal_name: definition.name,
      signal_kind: definition.kind,
      direction: defaultDirection(definition.id),
      enabled: true,
    }])
    setBuiltinToAdd('')
    setConfigDirty(true)
  }

  const updateDraft = (signalKey: string, patch: Partial<SignalScorecardTrackedItem>) => {
    setTrackedDraft((items) => items.map((item) => item.signal_key === signalKey ? { ...item, ...patch } : item))
    setConfigDirty(true)
  }

  const removeDraft = (signalKey: string) => {
    setTrackedDraft((items) => items.filter((item) => item.signal_key !== signalKey))
    setBackfillKeys((items) => items.filter((key) => key !== signalKey))
    setConfigDirty(true)
  }

  return (
    <div className="workspace-page h-full min-h-0">
      <PageHeader
        title="信号记分卡"
        subtitle="本地事件事实流 · append-only · 可追溯"
        right={
          <button
            type="button"
            className={BTN_PRIMARY}
            onClick={() => evaluateMutation.mutate()}
            disabled={!hasEnabledTracking || evaluateMutation.isPending}
            title={hasEnabledTracking ? '评估已有事件的已成熟结果' : '请先启用至少一个要跟踪的信号'}
          >
            {evaluateMutation.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
            立即评估
          </button>
        }
      />

      <main className="workspace-content min-h-0 flex-1 overflow-auto">
        <div className="mx-auto max-w-6xl space-y-3">
          <section className="panel border-warning/35 bg-warning/5" aria-label="研究边界">
            <div className="panel-body flex items-start gap-2.5 !py-3">
              <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
              <div>
                <h2 className="text-sm font-semibold text-foreground">仅回顾性研究</h2>
                <p className="mt-1 text-xs leading-relaxed text-secondary">
                  此页只统计本地数据中已记录信号的历史后续表现，不荐股，不进入选股、回测、监控或交易，也不产生下单动作。事件与结果按 append-only 方式保留来源和时间，待成熟样本不会被解释为预测。
                </p>
              </div>
            </div>
          </section>

          <TrackingConfiguration
            items={trackedDraft}
            availableBuiltins={availableBuiltins}
            builtinToAdd={builtinToAdd}
            dirty={configDirty}
            loading={trackedQuery.isPending}
            saving={saveTrackedMutation.isPending}
            error={trackedQuery.isError ? messageOf(trackedQuery.error) : saveTrackedMutation.isError ? messageOf(saveTrackedMutation.error) : null}
            onBuiltinChange={setBuiltinToAdd}
            onAdd={addBuiltin}
            onDirectionChange={(signalKey, direction) => updateDraft(signalKey, { direction })}
            onEnabledChange={(signalKey, enabled) => updateDraft(signalKey, { enabled })}
            onRemove={removeDraft}
            onSave={() => saveTrackedMutation.mutate(trackedDraft)}
            onRetry={() => void trackedQuery.refetch()}
          />

          {!hasEnabledTracking && !trackedQuery.isPending ? (
            <EmptyState
              icon={Target}
              title="默认不采集任何信号"
              hint="请从上方内置信号中主动加入并保存配置。只有已启用的信号会在本地数据链路中记录，避免未授权的全量采集。"
            />
          ) : (
            <>
              <section className={cn(CARD)} aria-label="记分卡筛选">
                <div className="panel-body flex flex-col gap-3 sm:flex-row sm:items-end !py-3">
                  <label className="grid min-w-0 flex-1 gap-1.5 text-xs text-secondary">
                    查看信号
                    <select value={selectedSignal} onChange={(event) => { setSelectedSignal(event.target.value); setSelectedEventId(null) }} className={INPUT}>
                      <option value="">全部已启用信号</option>
                      {enabledTracked.map((item) => <option key={item.signal_key} value={item.signal_key}>{item.signal_name}</option>)}
                    </select>
                  </label>
                  <label className="grid min-w-0 flex-1 gap-1.5 text-xs text-secondary">
                    事件状态
                    <select value={eventStatus} onChange={(event) => { setEventStatus(event.target.value as EventStatus); setSelectedEventId(null) }} className={INPUT}>
                      <option value="">全部状态</option>
                      <option value="pending">待成熟</option>
                      <option value="mature">已成熟</option>
                    </select>
                  </label>
                  <button type="button" className={BTN_GHOST} onClick={() => { void statsQuery.refetch(); void eventsQuery.refetch() }} disabled={statsQuery.isFetching || eventsQuery.isFetching}>
                    {statsQuery.isFetching || eventsQuery.isFetching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                    刷新
                  </button>
                </div>
              </section>

              <ScorecardStats
                stats={displayStats}
                signalNameFor={(key) => signalName(key, configured)}
                loading={statsQuery.isPending}
                error={statsQuery.isError ? messageOf(statsQuery.error) : null}
                onRetry={() => void statsQuery.refetch()}
              />

              <EventList
                events={displayEvents}
                total={eventsQuery.data?.total ?? 0}
                selectedEventId={selectedEventId}
                signalNameFor={(key) => signalName(key, configured)}
                loading={eventsQuery.isPending}
                error={eventsQuery.isError ? messageOf(eventsQuery.error) : null}
                onRetry={() => void eventsQuery.refetch()}
                onSelect={setSelectedEventId}
              />

              {selectedEventId && (
                <EventDetail
                  event={detailQuery.data?.event ?? null}
                  outcomes={detailQuery.data?.outcomes ?? []}
                  status={detailQuery.data?.status ?? null}
                  loading={detailQuery.isPending}
                  error={detailQuery.isError ? messageOf(detailQuery.error) : null}
                  onClose={() => setSelectedEventId(null)}
                  onRetry={() => void detailQuery.refetch()}
                />
              )}

              <BackfillForm
                enabledItems={enabledTracked}
                selectedKeys={backfillKeys}
                dateFrom={dateFrom}
                dateTo={dateTo}
                acknowledged={backfillAcknowledged}
                pending={backfillMutation.isPending}
                error={backfillMutation.isError ? messageOf(backfillMutation.error) : null}
                onKeysChange={setBackfillKeys}
                onDateFromChange={setDateFrom}
                onDateToChange={setDateTo}
                onAcknowledgedChange={setBackfillAcknowledged}
                onSubmit={() => backfillMutation.mutate({ keys: backfillKeys, from: dateFrom, to: dateTo })}
                rangeValid={backfillRangeValid}
              />
            </>
          )}
        </div>
      </main>
    </div>
  )
}

function TrackingConfiguration({
  items,
  availableBuiltins,
  builtinToAdd,
  dirty,
  loading,
  saving,
  error,
  onBuiltinChange,
  onAdd,
  onDirectionChange,
  onEnabledChange,
  onRemove,
  onSave,
  onRetry,
}: {
  items: SignalScorecardTrackedItem[]
  availableBuiltins: typeof BUILTIN_SIGNAL_DEFINITIONS
  builtinToAdd: string
  dirty: boolean
  loading: boolean
  saving: boolean
  error: string | null
  onBuiltinChange: (value: string) => void
  onAdd: () => void
  onDirectionChange: (signalKey: string, direction: Direction) => void
  onEnabledChange: (signalKey: string, enabled: boolean) => void
  onRemove: (signalKey: string) => void
  onSave: () => void
  onRetry: () => void
}) {
  return (
    <section className={cn(CARD)} aria-labelledby="tracked-signals-title">
      <div className="flex flex-col gap-3 border-b border-border pb-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex gap-2.5">
          <SlidersHorizontal className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
          <div>
            <h2 id="tracked-signals-title" className="text-sm font-semibold text-foreground">主动跟踪配置</h2>
            <p className="mt-1 text-xs leading-relaxed text-muted">默认关闭。只有保存后启用的内置信号，才可在本地主链路中被记录和回顾；方向仅定义命中判定，不构成交易意见。</p>
          </div>
        </div>
        <button type="button" onClick={onSave} disabled={!dirty || saving || loading} className={BTN_PRIMARY}>
          {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
          保存配置
        </button>
      </div>

      {loading ? <LoadingState label="正在读取跟踪配置" compact /> : null}
      {error ? <QueryError title="跟踪配置读取失败" message={error} onRetry={onRetry} compact /> : null}
      {!loading && !error ? (
        <>
          <div className="mt-3 flex flex-col gap-2 sm:flex-row">
            <label className="sr-only" htmlFor="scorecard-builtin">选择内置信号</label>
            <select id="scorecard-builtin" value={builtinToAdd} onChange={(event) => onBuiltinChange(event.target.value)} className={INPUT}>
              <option value="">选择一个内置信号加入配置</option>
              {availableBuiltins.map((signal) => <option key={signal.id} value={signal.id}>{signal.category} · {signal.name}</option>)}
            </select>
            <button type="button" onClick={onAdd} disabled={!builtinToAdd} className={BTN_GHOST}>加入草稿</button>
          </div>

          {items.length === 0 ? (
            <p className="mt-3 rounded-btn border border-dashed border-border px-3 py-3 text-xs leading-relaxed text-muted">尚未选择跟踪信号。配置保持为空即不采集任何信号。</p>
          ) : (
            <ul className="mt-3 divide-y divide-border rounded-btn border border-border">
              {items.map((item) => (
                <li key={item.signal_key} className="flex flex-col gap-2 p-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0">
                    <p className="truncate text-xs font-medium text-foreground">{item.signal_name}</p>
                    <p className="mt-0.5 truncate font-mono text-[11px] text-muted">{item.signal_key} · {item.signal_kind}</p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <label className="flex items-center gap-1.5 text-xs text-secondary">
                      <span>方向</span>
                      <select value={item.direction} onChange={(event) => onDirectionChange(item.signal_key, event.target.value as Direction)} className="rounded-btn border border-border bg-base px-2 py-1 text-xs text-foreground outline-none focus:border-accent/50">
                        <option value="up">向上</option>
                        <option value="not_up">非向上</option>
                      </select>
                    </label>
                    <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-secondary">
                      <input type="checkbox" checked={item.enabled} onChange={(event) => onEnabledChange(item.signal_key, event.target.checked)} className="h-3.5 w-3.5 rounded border-border accent-accent" />
                      启用
                    </label>
                    <button type="button" onClick={() => onRemove(item.signal_key)} className="text-xs text-muted underline-offset-2 hover:text-danger hover:underline">移除</button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </>
      ) : null}
    </section>
  )
}

function ScorecardStats({ stats, signalNameFor, loading, error, onRetry }: {
  stats: SignalScorecardStat[]
  signalNameFor: (signalKey: string) => string
  loading: boolean
  error: string | null
  onRetry: () => void
}) {
  const totalCompleted = stats.reduce((sum, stat) => sum + stat.completed, 0)
  const totalPending = stats.reduce((sum, stat) => sum + stat.pending, 0)
  const matureStats = stats.filter((stat) => stat.completed > 0)
  const weightedHitRate = totalCompleted > 0
    ? matureStats.reduce((sum, stat) => sum + (stat.hit_rate_pct ?? 0) * stat.completed, 0) / totalCompleted
    : null

  return (
    <section className="space-y-3" aria-labelledby="scorecard-stats-title">
      <div className="flex items-center gap-2">
        <BarChart3 className="h-4 w-4 text-accent" />
        <h2 id="scorecard-stats-title" className="text-sm font-semibold text-foreground">历史结果</h2>
        <span className="text-xs text-muted">仅已完成样本进入命中率</span>
      </div>
      {loading ? <LoadingState label="正在读取历史统计" /> : null}
      {error ? <QueryError title="历史统计读取失败" message={error} onRetry={onRetry} /> : null}
      {!loading && !error && stats.length === 0 ? (
        <EmptyState icon={BarChart3} title="尚无可汇总的历史结果" hint="启用信号后，等待本地数据链路产生事件；也可在下方受限日期范围内主动回填历史事件。" />
      ) : null}
      {!loading && !error && stats.length > 0 ? (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <StatCard label="已完成观测" value={String(totalCompleted)} hint="按 horizon 统计" />
            <StatCard label="待成熟观测" value={String(totalPending)} hint="不计入命中率" />
            <StatCard label="加权命中率" value={formatPct(weightedHitRate)} hint="仅历史完成样本" accent />
          </div>
          <div className={cn(CARD, 'overflow-hidden')}>
            <div className="border-b border-border px-3 py-2">
              <h3 className="text-xs font-semibold text-foreground">按 horizon 汇总</h3>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[620px] text-left text-xs">
                <thead className="bg-elevated/50 text-muted">
                  <tr>
                    <th className="px-3 py-2 font-medium">信号</th>
                    <th className="px-3 py-2 font-medium">周期</th>
                    <th className="px-3 py-2 text-right font-medium">完成/待成熟</th>
                    <th className="px-3 py-2 text-right font-medium">命中率</th>
                    <th className="px-3 py-2 text-right font-medium">命中/未中/中性</th>
                    <th className="px-3 py-2 text-right font-medium">平均涨跌幅</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {stats.map((stat) => (
                    <tr key={`${stat.signal_key}-${stat.horizon}`} className="text-secondary">
                      <td className="max-w-36 truncate px-3 py-2 text-foreground" title={signalNameFor(stat.signal_key)}>{signalNameFor(stat.signal_key)}</td>
                      <td className="px-3 py-2 font-medium text-foreground">{HORIZON_LABEL[stat.horizon] ?? `T+${stat.horizon}`}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{stat.completed} / {stat.pending}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{formatPct(stat.hit_rate_pct)}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{stat.hit_count} / {stat.miss_count} / {stat.neutral_count}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{formatPct(stat.avg_return_pct)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      ) : null}
    </section>
  )
}

function EventList({ events, total, selectedEventId, signalNameFor, loading, error, onRetry, onSelect }: {
  events: SignalScorecardEvent[]
  total: number
  selectedEventId: string | null
  signalNameFor: (signalKey: string) => string
  loading: boolean
  error: string | null
  onRetry: () => void
  onSelect: (id: string | null) => void
}) {
  return (
    <section className={cn(CARD, 'overflow-hidden')} aria-labelledby="scorecard-events-title">
      <div className="flex items-center justify-between gap-3 border-b border-border px-3 py-3">
        <div>
          <h2 id="scorecard-events-title" className="text-sm font-semibold text-foreground">信号事件</h2>
          <p className="mt-0.5 text-xs text-muted">显示最近 {events.length} / {total} 条；点选后查看各周期结果与来源。</p>
        </div>
        <Database className="h-4 w-4 shrink-0 text-muted" />
      </div>
      {loading ? <LoadingState label="正在读取本地信号事件" /> : null}
      {error ? <div className="p-3"><QueryError title="信号事件读取失败" message={error} onRetry={onRetry} compact /></div> : null}
      {!loading && !error && events.length === 0 ? (
        <EmptyState icon={Target} title="暂无匹配的信号事件" hint="已启用信号尚未触发，或当前筛选条件没有匹配记录；这不代表任何未来表现。" />
      ) : null}
      {!loading && !error && events.length > 0 ? (
        <ul className="divide-y divide-border">
          {events.map((event) => {
            const selected = event.id === selectedEventId
            return (
              <li key={event.id}>
                <button
                  type="button"
                  onClick={() => onSelect(selected ? null : event.id)}
                  aria-expanded={selected}
                  className={cn('flex w-full items-center gap-3 px-3 py-3 text-left transition-colors hover:bg-elevated/45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/60', selected && 'bg-elevated/60')}
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                      <span className="font-mono text-xs text-muted">{event.date}</span>
                      <span className="text-xs font-semibold text-foreground">{event.symbol}</span>
                      {event.name ? <span className="max-w-32 truncate text-xs text-secondary">{event.name}</span> : null}
                      <DirectionBadge direction={event.direction_expected} />
                    </div>
                    <p className="mt-1 truncate text-xs text-secondary">{signalNameFor(event.signal_key)} <span className="text-muted">· {event.source} · 锚定价 {fmtPrice(event.anchor_price)}</span></p>
                  </div>
                  {selected ? <ChevronUp className="h-4 w-4 shrink-0 text-muted" /> : <ChevronDown className="h-4 w-4 shrink-0 text-muted" />}
                </button>
              </li>
            )
          })}
        </ul>
      ) : null}
    </section>
  )
}

function EventDetail({ event, outcomes, status, loading, error, onClose, onRetry }: {
  event: SignalScorecardEvent | null
  outcomes: SignalScorecardOutcome[]
  status: 'pending' | 'mature' | null
  loading: boolean
  error: string | null
  onClose: () => void
  onRetry: () => void
}) {
  return (
    <section className={cn(CARD)} aria-labelledby="event-detail-title">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 id="event-detail-title" className="text-sm font-semibold text-foreground">单事件结果</h2>
          <p className="mt-1 text-xs text-muted">每个周期独立记录；不可用或待成熟均不被当作预测。</p>
        </div>
        <button type="button" onClick={onClose} className={BTN_GHOST}>收起</button>
      </div>
      {loading ? <LoadingState label="正在读取单事件结果" compact /> : null}
      {error ? <QueryError title="单事件结果读取失败" message={error} onRetry={onRetry} compact /> : null}
      {!loading && !error && event ? (
        <>
          <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 rounded-btn bg-elevated/35 p-3 text-xs sm:grid-cols-4">
            <Meta label="标的" value={event.name ? `${event.symbol} · ${event.name}` : event.symbol} />
            <Meta label="锚定日" value={event.date} />
            <Meta label="锚定价" value={fmtPrice(event.anchor_price)} />
            <Meta label="状态" value={status === 'mature' ? '已成熟' : '待成熟'} />
          </dl>
          <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            {outcomes.map((outcome) => <OutcomeCard key={outcome.horizon} outcome={outcome} />)}
          </div>
          {outcomes.length === 0 ? <p className="mt-3 text-xs text-muted">该事件尚未生成可展示的周期结果。</p> : null}
        </>
      ) : null}
    </section>
  )
}

function BackfillForm({
  enabledItems,
  selectedKeys,
  dateFrom,
  dateTo,
  acknowledged,
  pending,
  error,
  rangeValid,
  onKeysChange,
  onDateFromChange,
  onDateToChange,
  onAcknowledgedChange,
  onSubmit,
}: {
  enabledItems: SignalScorecardTrackedItem[]
  selectedKeys: string[]
  dateFrom: string
  dateTo: string
  acknowledged: boolean
  pending: boolean
  error: string | null
  rangeValid: boolean
  onKeysChange: (keys: string[]) => void
  onDateFromChange: (value: string) => void
  onDateToChange: (value: string) => void
  onAcknowledgedChange: (value: boolean) => void
  onSubmit: () => void
}) {
  const canSubmit = selectedKeys.length > 0 && rangeValid && acknowledged && !pending
  const today = new Date().toISOString().slice(0, 10)

  return (
    <section className={cn(CARD)} aria-labelledby="scorecard-backfill-title">
      <div className="flex items-start gap-2.5">
        <FlaskConical className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
        <div>
          <h2 id="scorecard-backfill-title" className="text-sm font-semibold text-foreground">受限历史回填</h2>
          <p className="mt-1 text-xs leading-relaxed text-muted">仅对已启用信号和明确选择的日期范围扫描本地数据；最多 366 天，不访问外部数据，不生成推荐或交易动作。</p>
        </div>
      </div>
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        <fieldset className="rounded-btn border border-border p-3">
          <legend className="px-1 text-xs text-secondary">已启用信号</legend>
          <div className="grid gap-2">
            {enabledItems.map((item) => {
              const checked = selectedKeys.includes(item.signal_key)
              return (
                <label key={item.signal_key} className="flex cursor-pointer items-center gap-2 text-xs text-secondary">
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={(event) => onKeysChange(event.target.checked ? [...selectedKeys, item.signal_key] : selectedKeys.filter((key) => key !== item.signal_key))}
                    className="h-3.5 w-3.5 rounded border-border accent-accent"
                  />
                  <span className="truncate">{item.signal_name}</span>
                </label>
              )
            })}
          </div>
        </fieldset>
        <div className="grid content-start gap-3">
          <label className="grid gap-1.5 text-xs text-secondary">
            起始日期
            <input type="date" value={dateFrom} max={dateTo || today} onChange={(event) => onDateFromChange(event.target.value)} className={INPUT} />
          </label>
          <label className="grid gap-1.5 text-xs text-secondary">
            结束日期
            <input type="date" value={dateTo} min={dateFrom || undefined} max={today} onChange={(event) => onDateToChange(event.target.value)} className={INPUT} />
          </label>
          {dateFrom && dateTo && !rangeValid ? <p className="text-xs text-danger" role="alert">请选择不超过 366 天、且起始日不晚于结束日的范围。</p> : null}
        </div>
      </div>
      <label className="mt-3 flex cursor-pointer items-start gap-2 text-xs leading-relaxed text-secondary">
        <input type="checkbox" checked={acknowledged} onChange={(event) => onAcknowledgedChange(event.target.checked)} className="mt-0.5 h-3.5 w-3.5 rounded border-border accent-accent" />
        <span>我确认仅为回顾性研究回填这些本地历史事件，不将结果用于荐股、选股、监控或交易。</span>
      </label>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted">{selectedKeys.length ? `已选择 ${selectedKeys.length} 个信号` : '尚未选择信号'}</p>
        <button type="button" onClick={onSubmit} disabled={!canSubmit} className={BTN_PRIMARY}>
          {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
          回填本地历史事件
        </button>
      </div>
      {error ? <QueryError title="历史回填未完成" message={error} onRetry={onSubmit} compact /> : null}
    </section>
  )
}

function StatCard({ label, value, hint, accent = false }: { label: string; value: string; hint: string; accent?: boolean }) {
  return (
    <div className={cn(CARD)}>
      <div className="panel-body !py-2.5">
        <p className="section-kicker normal-case tracking-normal">{label}</p>
        <p className={cn('metric-value mt-1 text-xl', accent && 'text-accent')}>{value}</p>
        <p className="mt-1 text-[11px] text-muted">{hint}</p>
      </div>
    </div>
  )
}

function DirectionBadge({ direction }: { direction: Direction }) {
  return <span className="rounded-full bg-muted/15 px-1.5 py-0.5 text-[10px] text-secondary">{direction === 'up' ? '向上' : '非向上'}</span>
}

function OutcomeCard({ outcome }: { outcome: SignalScorecardOutcome }) {
  const meta = outcome.eval_status === 'pending'
    ? { label: '待成熟', className: 'bg-muted/15 text-secondary' }
    : outcome.eval_status === 'unable'
      ? { label: '不可用', className: 'bg-warning/10 text-warning' }
      : outcome.outcome === 'hit'
        ? { label: '命中', className: 'bg-success/10 text-success' }
        : outcome.outcome === 'miss'
          ? { label: '未中', className: 'bg-danger/10 text-danger' }
          : { label: '中性', className: 'bg-muted/15 text-secondary' }
  return (
    <article className="rounded-btn border border-border bg-base/50 p-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-semibold text-foreground">{HORIZON_LABEL[outcome.horizon] ?? `T+${outcome.horizon}`}</h3>
        <span className={cn('rounded-full px-1.5 py-0.5 text-[10px] font-medium', meta.className)}>{meta.label}</span>
      </div>
      <p className="mt-2 text-sm font-medium tabular-nums text-foreground">{formatPct(outcome.stock_return_pct)}</p>
      <p className="mt-1 text-[11px] text-muted">收盘 {fmtPrice(outcome.end_close)}{outcome.unable_reason ? ` · ${outcome.unable_reason}` : ''}</p>
    </article>
  )
}

function Meta({ label, value }: { label: string; value: string }) {
  return <div><dt className="text-[11px] text-muted">{label}</dt><dd className="mt-0.5 truncate text-xs text-foreground" title={value}>{value}</dd></div>
}

function LoadingState({ label, compact = false }: { label: string; compact?: boolean }) {
  return <div className={cn('flex items-center justify-center gap-2 text-xs text-muted', compact ? 'py-4' : 'min-h-40')} role="status"><Loader2 className="h-4 w-4 animate-spin" />{label}</div>
}

function QueryError({ title, message, onRetry, compact = false }: { title: string; message: string; onRetry: () => void; compact?: boolean }) {
  return (
    <section className={cn('panel border-danger/40 bg-danger/5 p-4 text-center', compact && 'mt-3 p-3')} role="alert">
      <AlertCircle className="mx-auto h-5 w-5 text-danger" />
      <p className="mt-2 text-sm font-medium text-foreground">{title}</p>
      <p className="mt-1 break-words text-xs text-danger">{message}</p>
      <button type="button" onClick={onRetry} className={cn(BTN_GHOST, 'mt-3')}><RefreshCw className="h-3.5 w-3.5" />重试</button>
    </section>
  )
}
