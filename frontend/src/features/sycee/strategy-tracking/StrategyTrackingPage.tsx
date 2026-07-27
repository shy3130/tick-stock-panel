import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  ChevronDown,
  ChevronUp,
  CircleAlert,
  Loader2,
  NotebookPen,
  Play,
  Plus,
  RefreshCw,
  Square,
  Trash2,
  X,
} from 'lucide-react'

import { ConfirmDialog } from '@/components/ConfirmDialog'
import { Modal } from '@/components/Modal'
import { PageHeader } from '@/components/PageHeader'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import { getNavIconMeta } from '@/lib/navRegistry'
import { strategyCompareApi } from '../strategy-compare/api'
import {
  ComparisonCancelledError,
  startStrategyBacktest,
  type StrategyRunHandle,
} from '../strategy-compare/comparisonStream'
import {
  STRATEGY_TRACKS_QUERY_KEY,
  strategyTrackingApi,
  type StrategyTrack,
  type StrategyTrackStatus,
} from './api'
import { latestObservation, observationFromResult, trackingSummary } from './tracking'
import { TrackEditorDialog } from './TrackEditorDialog'

type StatusFilter = 'all' | StrategyTrackStatus

const STATUS_META: Record<StrategyTrackStatus, { label: string; className: string }> = {
  tracking: { label: '跟踪中', className: 'text-bear bg-bear/10 border-bear/20' },
  paused: { label: '已暂停', className: 'text-warn bg-warn/10 border-warn/20' },
  closed: { label: '已结束', className: 'text-muted bg-elevated border-border' },
}

function localDate(): string {
  return new Intl.DateTimeFormat('en-CA', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date())
}

function percent(value: number | null | undefined, signed = true): string {
  if (value == null || !Number.isFinite(value)) return '--'
  return `${signed && value > 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
}

function metricColor(value: number | null | undefined): string {
  if (value == null || value === 0) return 'text-foreground'
  return value > 0 ? 'text-bear' : 'text-danger'
}

function money(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '--'
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(value)
}

function NoteDialog({
  track,
  pending,
  onClose,
  onSave,
}: {
  track: StrategyTrack
  pending: boolean
  onClose: () => void
  onSave: (note: string) => void
}) {
  const [note, setNote] = useState(track.note)
  const noteRef = useRef<HTMLTextAreaElement>(null)
  return (
    <Modal onClose={() => { if (!pending) onClose() }} labelledBy="track-note-title" initialFocusRef={noteRef} panelClassName="w-[92vw] max-w-lg overflow-hidden rounded-card border border-border bg-surface shadow-2xl">
      <div className="flex min-h-14 items-center justify-between border-b border-border px-4">
        <h2 id="track-note-title" className="text-sm font-semibold text-foreground">编辑跟踪备注</h2>
        <button type="button" title="关闭" aria-label="关闭" disabled={pending} onClick={onClose} className="flex h-9 w-9 items-center justify-center rounded-btn text-muted hover:bg-elevated hover:text-foreground disabled:opacity-50"><X className="h-4 w-4" /></button>
      </div>
      <div className="p-4">
        <div className="mb-2 text-xs text-secondary">{track.strategy_name}</div>
        <textarea ref={noteRef} value={note} onChange={event => setNote(event.target.value)} maxLength={3000} rows={7} className="w-full resize-none rounded-input border border-border bg-base px-3 py-2.5 text-sm leading-6 text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/20" />
      </div>
      <div className="flex justify-end gap-2 border-t border-border bg-base/60 px-4 py-3">
        <button type="button" disabled={pending} onClick={onClose} className="min-h-10 rounded-btn border border-border px-4 text-sm text-secondary hover:bg-elevated disabled:opacity-50">取消</button>
        <button type="button" disabled={pending} onClick={() => onSave(note.trim())} className="inline-flex min-h-10 items-center gap-2 rounded-btn bg-accent px-4 text-sm font-medium text-white hover:bg-accent/90 disabled:opacity-50">
          {pending && <Loader2 className="h-4 w-4 animate-spin" />}保存
        </button>
      </div>
    </Modal>
  )
}

function ObservationHistory({ track }: { track: StrategyTrack }) {
  if (track.observations.length === 0) {
    return <div className="border-t border-border bg-base/40 px-4 py-5 text-center text-xs text-muted">暂无跟踪快照</div>
  }
  return (
    <div className="border-t border-border bg-base/40 px-3 py-3 sm:px-4">
      <div className="hidden grid-cols-[110px_repeat(6,minmax(72px,1fr))] gap-3 border-b border-border px-2 pb-2 text-[10px] font-medium text-muted lg:grid">
        <span>截止日期</span><span className="text-right">累计收益</span><span className="text-right">年化</span><span className="text-right">夏普</span><span className="text-right">最大回撤</span><span className="text-right">胜率</span><span className="text-right">交易数</span>
      </div>
      <div className="divide-y divide-border/70">
        {track.observations.map(item => (
          <div key={item.id} className="grid grid-cols-2 gap-x-4 gap-y-2 px-2 py-3 text-xs lg:grid-cols-[110px_repeat(6,minmax(72px,1fr))] lg:items-center lg:gap-3">
            <div className="font-mono text-foreground">{item.end_date}</div>
            <div className={cn('text-right font-mono font-semibold', metricColor(item.total_return))}>{percent(item.total_return)}</div>
            <div className="lg:text-right"><span className="mr-2 text-[10px] text-muted lg:hidden">年化</span><span className="font-mono text-secondary">{percent(item.annual_return)}</span></div>
            <div className="text-right lg:text-right"><span className="mr-2 text-[10px] text-muted lg:hidden">夏普</span><span className="font-mono text-secondary">{item.sharpe?.toFixed(2) ?? '--'}</span></div>
            <div><span className="mr-2 text-[10px] text-muted lg:hidden">回撤</span><span className="font-mono text-danger">{percent(item.max_drawdown, false)}</span></div>
            <div className="text-right"><span className="mr-2 text-[10px] text-muted lg:hidden">胜率</span><span className="font-mono text-secondary">{percent(item.win_rate, false)}</span></div>
            <div><span className="mr-2 text-[10px] text-muted lg:hidden">交易</span><span className="font-mono text-secondary">{item.trade_count ?? '--'}</span></div>
          </div>
        ))}
      </div>
    </div>
  )
}

export function StrategyTrackingPage() {
  const navMeta = getNavIconMeta('/strategy-tracking')
  const queryClient = useQueryClient()
  const [filter, setFilter] = useState<StatusFilter>('all')
  const [createOpen, setCreateOpen] = useState(false)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [noteTrack, setNoteTrack] = useState<StrategyTrack | null>(null)
  const [deleteTrack, setDeleteTrack] = useState<StrategyTrack | null>(null)
  const [activeRun, setActiveRun] = useState<{ trackId: string; day: number; total: number } | null>(null)
  const activeHandle = useRef<StrategyRunHandle | null>(null)
  const runToken = useRef(0)

  const tracksQuery = useQuery({
    queryKey: STRATEGY_TRACKS_QUERY_KEY,
    queryFn: strategyTrackingApi.list,
  })
  const strategiesQuery = useQuery({
    queryKey: ['sycee', 'strategy-tracking', 'strategies'],
    queryFn: strategyCompareApi.strategies,
    staleTime: 60_000,
  })
  const watchlistQuery = useQuery({
    queryKey: ['sycee', 'strategy-tracking', 'watchlist'],
    queryFn: strategyCompareApi.watchlist,
    staleTime: 30_000,
  })

  useEffect(() => () => {
    runToken.current += 1
    activeHandle.current?.cancel()
  }, [])

  const tracks = useMemo(() => tracksQuery.data?.tracks ?? [], [tracksQuery.data?.tracks])
  const summary = useMemo(() => trackingSummary(tracks), [tracks])
  const filteredTracks = useMemo(
    () => filter === 'all' ? tracks : tracks.filter(track => track.status === filter),
    [filter, tracks],
  )

  const update = useMutation({
    mutationFn: ({ trackId, changes }: { trackId: string; changes: { status?: StrategyTrackStatus; note?: string } }) => strategyTrackingApi.update(trackId, changes),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: STRATEGY_TRACKS_QUERY_KEY })
      setNoteTrack(null)
    },
    onError: cause => toast(cause instanceof Error ? cause.message : '策略跟踪计划更新失败', 'error'),
  })
  const remove = useMutation({
    mutationFn: (trackId: string) => strategyTrackingApi.delete(trackId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: STRATEGY_TRACKS_QUERY_KEY })
      setDeleteTrack(null)
      toast('策略跟踪计划已删除', 'success')
    },
    onError: cause => toast(cause instanceof Error ? cause.message : '策略跟踪计划删除失败', 'error'),
  })

  const cancelRun = () => activeHandle.current?.cancel()

  const runTrack = async (track: StrategyTrack) => {
    const strategy = strategiesQuery.data?.strategies.find(item => item.id === track.strategy_id)
    if (!strategy) {
      toast('原策略已不存在，无法更新快照', 'error')
      return
    }
    const endDate = localDate()
    const token = ++runToken.current
    setActiveRun({ trackId: track.id, day: 0, total: 0 })
    const handle = startStrategyBacktest(
      {
        strategyId: track.strategy_id,
        symbols: track.symbols,
        start: track.start_date,
        end: endDate,
        initialCapital: track.initial_capital,
        maxPositions: track.max_positions,
        commissionPct: track.commission_pct,
        stampTaxPct: track.stamp_tax_pct,
        slippageBps: track.slippage_bps,
        params: track.params,
        overrides: track.overrides,
      },
      progress => {
        if (runToken.current === token) {
          setActiveRun({ trackId: track.id, day: progress.day, total: progress.total })
        }
      },
    )
    activeHandle.current = handle
    try {
      const result = await handle.promise
      if (runToken.current !== token) return
      const saved = await strategyTrackingApi.saveObservation(
        track.id,
        observationFromResult(result, endDate),
      )
      await queryClient.invalidateQueries({ queryKey: STRATEGY_TRACKS_QUERY_KEY })
      toast(saved.action === 'replaced' ? '今日跟踪快照已更新' : '跟踪快照已保存', 'success')
      setExpandedId(track.id)
    } catch (cause) {
      if (!(cause instanceof ComparisonCancelledError)) {
        toast(cause instanceof Error ? cause.message : '策略跟踪回测失败', 'error')
      }
    } finally {
      if (runToken.current === token) {
        activeHandle.current = null
        setActiveRun(null)
      }
    }
  }

  return (
    <>
      <PageHeader
        title="策略跟踪"
        icon={navMeta?.icon}
        group={navMeta?.group}
        right={(
          <button type="button" onClick={() => setCreateOpen(true)} disabled={!strategiesQuery.data?.strategies.length} className="inline-flex min-h-11 items-center gap-2 rounded-btn bg-accent px-4 text-sm font-medium text-white hover:bg-accent/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50 lg:min-h-9">
            <Plus className="h-4 w-4" />新建跟踪
          </button>
        )}
      />

      <div className="space-y-4 p-3 lg:p-5">
        <section aria-label="策略跟踪概览" className="grid grid-cols-2 overflow-hidden rounded-card border border-border bg-surface sm:grid-cols-4">
          {[
            ['跟踪中', summary.tracking, 'text-bear'],
            ['已暂停', summary.paused, 'text-warn'],
            ['已结束', summary.closed, 'text-muted'],
            ['累计快照', summary.observations, 'text-accent'],
          ].map(([label, value, color], index) => (
            <div key={String(label)} className={cn('px-4 py-4', index % 2 === 0 ? 'border-r border-border' : '', index < 2 ? 'border-b border-border sm:border-b-0' : '', index > 0 && 'sm:border-l sm:border-border', index === 2 && 'border-r border-border sm:border-r-0')}>
              <div className="text-[11px] text-muted">{label}</div>
              <div className={cn('mt-1 font-mono text-xl font-semibold', color)}>{value}</div>
            </div>
          ))}
        </section>

        <section aria-labelledby="tracking-list-title" className="overflow-hidden rounded-card border border-border bg-surface">
          <div className="flex min-h-14 flex-wrap items-center justify-between gap-3 border-b border-border px-3 py-2 sm:px-4">
            <div className="flex items-center gap-2">
              <Activity className="h-4 w-4 text-accent" />
              <h2 id="tracking-list-title" className="text-sm font-semibold text-foreground">跟踪计划</h2>
            </div>
            <div className="grid grid-cols-4 rounded-btn border border-border bg-base p-0.5" aria-label="状态筛选">
              {([
                ['all', '全部'],
                ['tracking', '跟踪中'],
                ['paused', '暂停'],
                ['closed', '结束'],
              ] as const).map(([value, label]) => (
                <button key={value} type="button" aria-pressed={filter === value} onClick={() => setFilter(value)} className={cn('min-h-8 px-2 text-[11px] text-muted sm:px-3', filter === value && 'rounded-[4px] bg-elevated font-medium text-foreground shadow-sm')}>{label}</button>
              ))}
            </div>
          </div>

          {tracksQuery.isLoading ? (
            <div className="flex min-h-48 items-center justify-center"><Loader2 className="h-5 w-5 animate-spin text-accent" /></div>
          ) : tracksQuery.isError ? (
            <div className="flex min-h-48 flex-col items-center justify-center gap-3 px-5 text-center">
              <CircleAlert className="h-6 w-6 text-danger" />
              <div className="text-sm text-danger">策略跟踪数据读取失败</div>
              <button type="button" onClick={() => tracksQuery.refetch()} className="inline-flex min-h-9 items-center gap-2 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated"><RefreshCw className="h-3.5 w-3.5" />重新读取</button>
            </div>
          ) : filteredTracks.length === 0 ? (
            <div className="flex min-h-48 flex-col items-center justify-center gap-3 px-5 text-center">
              <Activity className="h-7 w-7 text-muted/60" />
              <div className="text-sm text-muted">当前状态下没有跟踪计划</div>
            </div>
          ) : (
            <div className="divide-y divide-border">
              {filteredTracks.map(track => {
                const latest = latestObservation(track)
                const running = activeRun?.trackId === track.id
                const expanded = expandedId === track.id
                const changing = update.isPending && update.variables?.trackId === track.id
                return (
                  <article key={track.id}>
                    <div className="grid gap-4 px-3 py-4 sm:px-4 xl:grid-cols-[minmax(220px,1.4fr)_minmax(310px,1fr)_auto] xl:items-center">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <h3 className="truncate text-sm font-semibold text-foreground">{track.strategy_name}</h3>
                          <span className={cn('rounded-[4px] border px-1.5 py-0.5 text-[10px] font-medium', STATUS_META[track.status].className)}>{STATUS_META[track.status].label}</span>
                        </div>
                        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[10px] text-muted">
                          <span>{track.start_date} → {latest?.end_date ?? '尚未运行'}</span>
                          <span>{track.symbols.length} 标的</span>
                          <span>{money(track.initial_capital)}</span>
                        </div>
                        {track.note && <div className="mt-2 line-clamp-2 text-xs leading-5 text-secondary">{track.note}</div>}
                      </div>

                      <div className="grid grid-cols-3 divide-x divide-border rounded-input border border-border bg-base/50">
                        <div className="px-3 py-2.5">
                          <div className="text-[10px] text-muted">累计收益</div>
                          <div className={cn('mt-1 font-mono text-sm font-semibold', metricColor(latest?.total_return))}>{percent(latest?.total_return)}</div>
                        </div>
                        <div className="px-3 py-2.5">
                          <div className="text-[10px] text-muted">最大回撤</div>
                          <div className="mt-1 font-mono text-sm font-semibold text-danger">{percent(latest?.max_drawdown, false)}</div>
                        </div>
                        <div className="px-3 py-2.5">
                          <div className="text-[10px] text-muted">夏普</div>
                          <div className="mt-1 font-mono text-sm font-semibold text-foreground">{latest?.sharpe?.toFixed(2) ?? '--'}</div>
                        </div>
                      </div>

                      <div className="flex flex-wrap items-center justify-end gap-1.5">
                        <select aria-label={`${track.strategy_name} 状态`} value={track.status} disabled={changing || running} onChange={event => update.mutate({ trackId: track.id, changes: { status: event.target.value as StrategyTrackStatus } })} className="min-h-9 rounded-btn border border-border bg-base px-2 text-xs text-secondary outline-none focus:border-accent disabled:opacity-50">
                          <option value="tracking">跟踪中</option><option value="paused">已暂停</option><option value="closed">已结束</option>
                        </select>
                        <button type="button" title="编辑备注" aria-label="编辑备注" onClick={() => setNoteTrack(track)} className="flex h-9 w-9 items-center justify-center rounded-btn border border-border text-muted hover:bg-elevated hover:text-foreground"><NotebookPen className="h-4 w-4" /></button>
                        {running ? (
                          <button type="button" title="取消运行" aria-label="取消运行" onClick={cancelRun} className="flex h-9 w-9 items-center justify-center rounded-btn border border-danger/30 bg-danger/5 text-danger hover:bg-danger/10"><Square className="h-3.5 w-3.5" /></button>
                        ) : (
                          <button type="button" title="更新快照" aria-label="更新快照" disabled={track.status !== 'tracking' || activeRun !== null || strategiesQuery.isLoading} onClick={() => { void runTrack(track) }} className="flex h-9 w-9 items-center justify-center rounded-btn border border-accent/30 bg-accent/5 text-accent hover:bg-accent/10 disabled:opacity-40"><Play className="h-4 w-4" /></button>
                        )}
                        <button type="button" title="删除计划" aria-label="删除计划" disabled={running} onClick={() => setDeleteTrack(track)} className="flex h-9 w-9 items-center justify-center rounded-btn border border-border text-muted hover:border-danger/30 hover:bg-danger/5 hover:text-danger disabled:opacity-40"><Trash2 className="h-4 w-4" /></button>
                        <button type="button" title={expanded ? '收起快照' : '展开快照'} aria-label={expanded ? '收起快照' : '展开快照'} aria-expanded={expanded} onClick={() => setExpandedId(expanded ? null : track.id)} className="flex h-9 w-9 items-center justify-center rounded-btn text-muted hover:bg-elevated hover:text-foreground">{expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}</button>
                      </div>
                    </div>
                    {running && (
                      <div className="border-t border-accent/15 bg-accent/5 px-4 py-2">
                        <div className="flex items-center justify-between gap-3 text-[10px] text-accent">
                          <span className="inline-flex items-center gap-1.5"><Loader2 className="h-3 w-3 animate-spin" />回测运行中</span>
                          <span className="font-mono">{activeRun.total > 0 ? `${activeRun.day} / ${activeRun.total}` : '准备数据'}</span>
                        </div>
                        <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-accent/10"><div className="h-full bg-accent transition-[width]" style={{ width: activeRun.total > 0 ? `${Math.min(100, (activeRun.day / activeRun.total) * 100)}%` : '4%' }} /></div>
                      </div>
                    )}
                    {expanded && <ObservationHistory track={track} />}
                  </article>
                )
              })}
            </div>
          )}
        </section>
      </div>

      {createOpen && (
        <TrackEditorDialog
          strategies={strategiesQuery.data?.strategies ?? []}
          watchlistSymbols={(watchlistQuery.data?.symbols ?? []).map(item => item.symbol)}
          onClose={() => setCreateOpen(false)}
          onCreated={() => setCreateOpen(false)}
        />
      )}
      {noteTrack && (
        <NoteDialog key={noteTrack.id} track={noteTrack} pending={update.isPending} onClose={() => setNoteTrack(null)} onSave={note => update.mutate({ trackId: noteTrack.id, changes: { note } })} />
      )}
      <ConfirmDialog
        open={deleteTrack !== null}
        title="删除这项策略跟踪？"
        message="计划及全部历史快照将被删除。"
        confirmText="删除计划"
        danger
        pending={remove.isPending}
        onCancel={() => setDeleteTrack(null)}
        onConfirm={() => { if (deleteTrack) remove.mutate(deleteTrack.id) }}
      />
    </>
  )
}
