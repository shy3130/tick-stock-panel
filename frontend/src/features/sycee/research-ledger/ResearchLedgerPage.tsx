import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Archive,
  BarChart3,
  BookOpen,
  CheckCircle2,
  CircleDashed,
  Edit3,
  FileQuestion,
  Gauge,
  Layers3,
  Loader2,
  Plus,
  RadioTower,
  Search,
  Target,
  Trash2,
  XCircle,
} from 'lucide-react'
import { useSearchParams } from 'react-router-dom'

import { ConfirmDialog } from '@/components/ConfirmDialog'
import { PageHeader } from '@/components/PageHeader'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import { getNavIconMeta } from '@/lib/navRegistry'
import {
  RESEARCH_LEDGER_QUERY_KEY,
  researchLedgerApi,
  type ResearchEntry,
  type ResearchCapture,
  type ResearchStatus,
  type ResearchSubjectType,
} from './api'
import { ResearchEditorDialog } from './ResearchEditorDialog'

type StatusFilter = 'all' | 'open' | 'concluded' | 'archived'

const STATUS_META: Record<ResearchStatus, { label: string; className: string; icon: typeof CircleDashed }> = {
  draft: { label: '草稿', className: 'border-border bg-elevated text-secondary', icon: CircleDashed },
  tracking: { label: '跟踪中', className: 'border-accent/30 bg-accent/10 text-accent', icon: Target },
  validated: { label: '已验证', className: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-500', icon: CheckCircle2 },
  invalidated: { label: '已失效', className: 'border-danger/30 bg-danger/10 text-danger', icon: XCircle },
  archived: { label: '已归档', className: 'border-border bg-base text-muted', icon: Archive },
}

const SUBJECT_META: Record<ResearchSubjectType, { label: string; icon: typeof BarChart3 }> = {
  stock: { label: '个股', icon: BarChart3 },
  strategy: { label: '策略', icon: Gauge },
  sector: { label: '板块', icon: Layers3 },
  market: { label: '市场', icon: BookOpen },
}

const STATUS_OPTIONS = Object.entries(STATUS_META) as Array<[ResearchStatus, (typeof STATUS_META)[ResearchStatus]]>
const FILTERS: Array<{ value: StatusFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'open', label: '进行中' },
  { value: 'concluded', label: '已结论' },
  { value: 'archived', label: '归档' },
]

function formatTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

function StatusBadge({ status }: { status: ResearchStatus }) {
  const meta = STATUS_META[status]
  const Icon = meta.icon
  return (
    <span className={cn('inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium', meta.className)}>
      <Icon className="h-3 w-3" />
      {meta.label}
    </span>
  )
}

function EvidenceList({ items, empty }: { items: string[]; empty: string }) {
  if (items.length === 0) return <p className="text-sm leading-6 text-muted">{empty}</p>
  return (
    <ul className="space-y-2">
      {items.map((item, index) => (
        <li key={`${index}-${item}`} className="flex gap-2 text-sm leading-6 text-secondary">
          <span className="mt-[9px] h-1.5 w-1.5 shrink-0 rounded-full bg-current opacity-60" aria-hidden="true" />
          <span>{item}</span>
        </li>
      ))}
    </ul>
  )
}

function captureSnapshotItems(capture: ResearchCapture): string[] {
  const items: string[] = []
  const price = capture.snapshot.price
  if (typeof price === 'number') items.push(`价格 ${price.toFixed(2)}`)
  const changePct = capture.snapshot.change_pct
  if (typeof changePct === 'number') {
    items.push(`涨跌幅 ${changePct >= 0 ? '+' : ''}${(changePct * 100).toFixed(2)}%`)
  }
  const signals = capture.snapshot.signals
  if (typeof signals === 'string' && signals) items.push(`信号 ${signals}`)
  const triggeredAt = capture.snapshot.triggered_at
  if (typeof triggeredAt === 'string' && triggeredAt) items.push(`触发 ${formatTime(triggeredAt)}`)
  return items
}

export function ResearchLedgerPage() {
  const queryClient = useQueryClient()
  const [searchParams] = useSearchParams()
  const detailRef = useRef<HTMLElement>(null)
  const [selectedId, setSelectedId] = useState<string | null>(() => searchParams.get('entry'))
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [subjectFilter, setSubjectFilter] = useState<ResearchSubjectType | 'all'>('all')
  const [search, setSearch] = useState('')
  const [editorEntry, setEditorEntry] = useState<ResearchEntry | null | undefined>(undefined)
  const [deleteTarget, setDeleteTarget] = useState<ResearchEntry | null>(null)

  useLayoutEffect(() => {
    document.querySelector('main')?.scrollTo({ top: 0 })
  }, [])

  const navMeta = getNavIconMeta('/research-ledger')
  const list = useQuery({
    queryKey: RESEARCH_LEDGER_QUERY_KEY,
    queryFn: researchLedgerApi.list,
  })
  const entries = useMemo(() => list.data?.entries ?? [], [list.data?.entries])

  const counts = useMemo(() => ({
    draft: entries.filter(entry => entry.status === 'draft').length,
    tracking: entries.filter(entry => entry.status === 'tracking').length,
    concluded: entries.filter(entry => entry.status === 'validated' || entry.status === 'invalidated').length,
    archived: entries.filter(entry => entry.status === 'archived').length,
  }), [entries])

  const filtered = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase()
    return entries.filter(entry => {
      const statusMatches = statusFilter === 'all'
        || (statusFilter === 'open' && (entry.status === 'draft' || entry.status === 'tracking'))
        || (statusFilter === 'concluded' && (entry.status === 'validated' || entry.status === 'invalidated'))
        || (statusFilter === 'archived' && entry.status === 'archived')
      const subjectMatches = subjectFilter === 'all' || entry.subject_type === subjectFilter
      const textMatches = !needle || [entry.title, entry.subject, entry.thesis, ...entry.tags]
        .join(' ')
        .toLocaleLowerCase()
        .includes(needle)
      return statusMatches && subjectMatches && textMatches
    })
  }, [entries, search, statusFilter, subjectFilter])

  const selected = filtered.find(entry => entry.id === selectedId)
    ?? filtered[0]
    ?? null

  const patchStatus = useMutation({
    mutationFn: ({ id, status }: { id: string; status: ResearchStatus }) => researchLedgerApi.update(id, { status }),
    onSuccess: async ({ entry }) => {
      setSelectedId(entry.id)
      await queryClient.invalidateQueries({ queryKey: RESEARCH_LEDGER_QUERY_KEY })
      toast(`状态已更新为“${STATUS_META[entry.status].label}”`, 'success')
    },
  })

  const remove = useMutation({
    mutationFn: researchLedgerApi.delete,
    onSuccess: async () => {
      setSelectedId(null)
      setDeleteTarget(null)
      await queryClient.invalidateQueries({ queryKey: RESEARCH_LEDGER_QUERY_KEY })
      toast('研究记录已删除', 'success')
    },
  })

  const selectEntry = (entry: ResearchEntry) => {
    setSelectedId(entry.id)
    if (window.matchMedia('(max-width: 1023px)').matches) {
      requestAnimationFrame(() => detailRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
    }
  }

  return (
    <>
      <PageHeader
        title="研究账本"
        subtitle="记录假设、证据与失效条件，让每次判断都可回看。"
        icon={navMeta?.icon}
        group={navMeta?.group}
        right={
          <button
            type="button"
            onClick={() => setEditorEntry(null)}
            className="inline-flex min-h-11 items-center justify-center gap-2 rounded-btn bg-accent px-4 text-sm font-medium text-white transition-colors hover:bg-accent/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-base lg:min-h-9"
          >
            <Plus className="h-4 w-4" />
            新建研究
          </button>
        }
      />

      <section aria-label="研究账本摘要" className="grid grid-cols-2 divide-x divide-y divide-border border-b border-border bg-surface/40 sm:grid-cols-4 sm:divide-y-0">
        {[
          { label: '待整理', value: counts.draft, hint: '草稿' },
          { label: '跟踪中', value: counts.tracking, hint: '持续验证' },
          { label: '已形成结论', value: counts.concluded, hint: '验证或失效' },
          { label: '已归档', value: counts.archived, hint: '历史记录' },
        ].map(item => (
          <div key={item.label} className="flex min-h-16 items-center gap-3 px-4 py-2.5 lg:px-5">
            <span className="min-w-8 font-mono text-xl font-semibold tabular-nums text-foreground">{item.value}</span>
            <span>
              <span className="block text-xs font-medium text-secondary">{item.label}</span>
              <span className="mt-0.5 block text-[11px] text-muted">{item.hint}</span>
            </span>
          </div>
        ))}
      </section>

      <div className="p-3 lg:p-5">
        <div className="mb-3 flex flex-col gap-2 xl:flex-row xl:items-center xl:justify-between">
          <div className="flex min-w-0 overflow-x-auto rounded-btn border border-border bg-surface p-1" role="group" aria-label="按研究状态筛选">
            {FILTERS.map(filter => (
              <button
                key={filter.value}
                type="button"
                aria-pressed={statusFilter === filter.value}
                onClick={() => setStatusFilter(filter.value)}
                className={cn(
                  'min-h-9 shrink-0 rounded px-3 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                  statusFilter === filter.value ? 'bg-elevated-2 text-foreground' : 'text-muted hover:bg-elevated hover:text-secondary',
                )}
              >
                {filter.label}
              </button>
            ))}
          </div>
          <div className="flex min-w-0 flex-col gap-2 sm:flex-row">
            <label className="sr-only" htmlFor="research-subject-filter">研究对象类型</label>
            <select
              id="research-subject-filter"
              value={subjectFilter}
              onChange={event => setSubjectFilter(event.target.value as ResearchSubjectType | 'all')}
              className="min-h-11 rounded-input border border-border bg-surface px-3 text-sm text-secondary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20 sm:min-h-9"
            >
              <option value="all">全部对象</option>
              <option value="stock">个股</option>
              <option value="strategy">策略</option>
              <option value="sector">板块</option>
              <option value="market">市场</option>
            </select>
            <label className="relative min-w-0 sm:w-72">
              <span className="sr-only">搜索研究记录</span>
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
              <input
                value={search}
                onChange={event => setSearch(event.target.value)}
                className="min-h-11 w-full rounded-input border border-border bg-surface pl-9 pr-3 text-sm text-foreground outline-none placeholder:text-muted focus:border-accent focus:ring-2 focus:ring-accent/20 sm:min-h-9"
                placeholder="搜索标题、对象或标签"
              />
            </label>
          </div>
        </div>

        {list.isError && (
          <div role="alert" className="mb-3 flex flex-col gap-3 rounded-card border border-danger/30 bg-danger/10 p-4 text-sm text-danger sm:flex-row sm:items-center sm:justify-between">
            <span>研究账本加载失败，请检查服务后重试。</span>
            <button type="button" onClick={() => list.refetch()} className="min-h-11 rounded-btn border border-danger/30 px-3 font-medium transition-colors hover:bg-danger/10 sm:min-h-9">重新加载</button>
          </div>
        )}

        <div className="grid items-start gap-3 lg:grid-cols-[minmax(300px,0.72fr)_minmax(0,1.28fr)]">
          <section aria-label="研究记录列表" className="overflow-hidden rounded-card border border-border bg-surface">
            <div className="flex items-center justify-between border-b border-border px-3 py-2.5">
              <span className="text-xs font-semibold text-secondary">研究记录</span>
              <span className="font-mono text-[11px] text-muted">{filtered.length} / {entries.length}</span>
            </div>

            {list.isLoading && (
              <div className="space-y-1 p-2" aria-label="正在加载研究记录">
                {[0, 1, 2].map(item => (
                  <div key={item} className="animate-pulse rounded-btn border border-border p-3">
                    <div className="h-3 w-20 rounded bg-elevated-2" />
                    <div className="mt-3 h-4 w-3/4 rounded bg-elevated-2" />
                    <div className="mt-3 h-3 w-full rounded bg-elevated" />
                  </div>
                ))}
              </div>
            )}

            {!list.isLoading && filtered.length === 0 && (
              <div className="px-5 py-12 text-center">
                <FileQuestion className="mx-auto h-7 w-7 text-muted" />
                <p className="mt-3 text-sm font-medium text-secondary">没有符合条件的记录</p>
                <p className="mt-1 text-xs leading-5 text-muted">调整筛选条件，或建立第一条研究记录。</p>
              </div>
            )}

            {filtered.length > 0 && (
              <div className="space-y-1 p-2 lg:max-h-[calc(100dvh-18rem)] lg:overflow-y-auto">
                {filtered.map(entry => {
                  const subject = SUBJECT_META[entry.subject_type]
                  const SubjectIcon = subject.icon
                  const active = selected?.id === entry.id
                  return (
                    <button
                      key={entry.id}
                      type="button"
                      onClick={() => selectEntry(entry)}
                      aria-pressed={active}
                      className={cn(
                        'w-full rounded-btn border p-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                        active ? 'border-g-research/50 bg-g-research/10' : 'border-transparent hover:border-border hover:bg-elevated/60',
                      )}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <StatusBadge status={entry.status} />
                        <span className="shrink-0 font-mono text-[10px] text-muted">{formatTime(entry.updated_at)}</span>
                      </div>
                      <h2 className="mt-2 line-clamp-2 text-sm font-semibold leading-5 text-foreground">{entry.title}</h2>
                      <div className="mt-2 flex min-w-0 items-center gap-1.5 text-[11px] text-muted">
                        <SubjectIcon className="h-3.5 w-3.5 shrink-0" />
                        <span className="shrink-0">{subject.label}</span>
                        {entry.subject && <span className="truncate font-mono text-secondary">{entry.subject}</span>}
                      </div>
                      {entry.thesis && <p className="mt-2 line-clamp-2 text-xs leading-5 text-secondary">{entry.thesis}</p>}
                    </button>
                  )
                })}
              </div>
            )}
          </section>

          <section ref={detailRef} aria-label="研究记录详情" className="scroll-mt-3 overflow-hidden rounded-card border border-border bg-surface">
            {!selected ? (
              <div className="flex min-h-80 flex-col items-center justify-center px-6 py-12 text-center">
                <BookOpen className="h-8 w-8 text-muted" />
                <h2 className="mt-4 text-base font-semibold text-foreground">建立你的第一条研究记录</h2>
                <p className="mt-2 max-w-md text-sm leading-6 text-muted">从一个明确假设开始，同时写下支持证据、反方证据和判断失效的条件。</p>
                <button type="button" onClick={() => setEditorEntry(null)} className="mt-5 inline-flex min-h-11 items-center gap-2 rounded-btn bg-accent px-4 text-sm font-medium text-white transition-colors hover:bg-accent/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
                  <Plus className="h-4 w-4" />新建研究
                </button>
              </div>
            ) : (
              <article>
                <div className="border-b border-border px-4 py-4 sm:px-5">
                  <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <StatusBadge status={selected.status} />
                        <span className="text-xs text-muted">更新于 {formatTime(selected.updated_at)}</span>
                      </div>
                      <h2 className="mt-3 text-xl font-semibold leading-7 text-foreground">{selected.title}</h2>
                      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-secondary">
                        <span>{SUBJECT_META[selected.subject_type].label}</span>
                        {selected.subject && <span className="rounded border border-border bg-base px-2 py-0.5 font-mono">{selected.subject}</span>}
                        {selected.tags.map(tag => <span key={tag} className="rounded border border-g-research/20 bg-g-research/10 px-2 py-0.5 text-g-research">{tag}</span>)}
                      </div>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <label className="sr-only" htmlFor="research-status-select">更新研究状态</label>
                      <select
                        id="research-status-select"
                        value={selected.status}
                        disabled={patchStatus.isPending}
                        onChange={event => patchStatus.mutate({ id: selected.id, status: event.target.value as ResearchStatus })}
                        className="min-h-11 rounded-btn border border-border bg-base px-3 text-sm text-secondary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20 disabled:opacity-50 xl:min-h-9"
                      >
                        {STATUS_OPTIONS.map(([value, meta]) => <option key={value} value={value}>{meta.label}</option>)}
                      </select>
                      <button type="button" onClick={() => setEditorEntry(selected)} className="inline-flex min-h-11 items-center gap-2 rounded-btn border border-border px-3 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent xl:min-h-9">
                        <Edit3 className="h-4 w-4" />编辑
                      </button>
                      <button type="button" onClick={() => setDeleteTarget(selected)} className="inline-flex min-h-11 items-center gap-2 rounded-btn border border-danger/25 px-3 text-sm text-danger transition-colors hover:bg-danger/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger xl:min-h-9">
                        <Trash2 className="h-4 w-4" />删除
                      </button>
                    </div>
                  </div>
                </div>

                <div className="space-y-0 px-4 sm:px-5">
                  {selected.captures.length > 0 && (
                    <section className="border-b border-border py-5">
                      <div className="flex items-center justify-between gap-3">
                        <h3 className="flex items-center gap-2 text-xs font-semibold text-g-research">
                          <RadioTower className="h-4 w-4" />系统捕获记录
                        </h3>
                        <span className="font-mono text-[11px] text-muted">{selected.captures.length} 条</span>
                      </div>
                      <ol className="relative mt-4 space-y-4 border-l border-g-research/25 pl-4">
                        {selected.captures.map(capture => {
                          const snapshotItems = captureSnapshotItems(capture)
                          return (
                            <li key={capture.id} className="relative">
                              <span className="absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full border-2 border-surface bg-g-research" aria-hidden="true" />
                              <div className="flex flex-wrap items-center gap-2">
                                <span className="rounded border border-g-research/25 bg-g-research/10 px-2 py-0.5 text-[11px] font-medium text-g-research">{capture.source_label}</span>
                                <time className="font-mono text-[10px] text-muted" dateTime={capture.captured_at}>{formatTime(capture.captured_at)}</time>
                              </div>
                              <p className="mt-1.5 text-sm leading-6 text-secondary">{capture.summary}</p>
                              {snapshotItems.length > 0 && (
                                <div className="mt-2 flex flex-wrap gap-1.5">
                                  {snapshotItems.map(item => (
                                    <span key={item} className="rounded bg-elevated px-2 py-1 font-mono text-[10px] text-muted">{item}</span>
                                  ))}
                                </div>
                              )}
                            </li>
                          )
                        })}
                      </ol>
                    </section>
                  )}

                  <section className="border-b border-border py-5">
                    <h3 className="text-xs font-semibold uppercase text-muted">核心判断</h3>
                    <p className={cn('mt-3 whitespace-pre-wrap text-sm leading-7', selected.thesis ? 'text-foreground' : 'text-muted')}>
                      {selected.thesis || '尚未记录核心判断。'}
                    </p>
                  </section>

                  <div className="grid md:grid-cols-2">
                    <section className="border-b border-border py-5 md:border-r md:pr-5">
                      <h3 className="flex items-center gap-2 text-xs font-semibold text-secondary"><CheckCircle2 className="h-4 w-4 text-emerald-500" />支持证据</h3>
                      <div className="mt-3"><EvidenceList items={selected.evidence} empty="尚未添加支持证据。" /></div>
                    </section>
                    <section className="border-b border-border py-5 md:pl-5">
                      <h3 className="flex items-center gap-2 text-xs font-semibold text-secondary"><XCircle className="h-4 w-4 text-danger" />反方证据</h3>
                      <div className="mt-3"><EvidenceList items={selected.counter_evidence} empty="尚未主动记录反方证据。" /></div>
                    </section>
                  </div>

                  <div className="grid md:grid-cols-2">
                    <section className="border-b border-border py-5 md:border-r md:pr-5">
                      <h3 className="text-xs font-semibold text-warning">失效条件</h3>
                      <p className={cn('mt-3 whitespace-pre-wrap text-sm leading-7', selected.invalidation ? 'text-secondary' : 'text-muted')}>
                        {selected.invalidation || '尚未定义判断失效的条件。'}
                      </p>
                    </section>
                    <section className="border-b border-border py-5 md:pl-5">
                      <h3 className="text-xs font-semibold text-accent">下一步</h3>
                      <p className={cn('mt-3 whitespace-pre-wrap text-sm leading-7', selected.plan ? 'text-secondary' : 'text-muted')}>
                        {selected.plan || '尚未安排下一项验证或监控动作。'}
                      </p>
                    </section>
                  </div>

                  <footer className="flex flex-col gap-2 py-4 text-[11px] text-muted sm:flex-row sm:items-center sm:justify-between">
                    <span>创建于 {formatTime(selected.created_at)}</span>
                    <span className="font-mono">{selected.id}</span>
                  </footer>
                </div>
              </article>
            )}
          </section>
        </div>
      </div>

      {editorEntry !== undefined && (
        <ResearchEditorDialog
          key={editorEntry?.id ?? 'new'}
          entry={editorEntry}
          onClose={() => setEditorEntry(undefined)}
          onSaved={entry => {
            setSelectedId(entry.id)
            setEditorEntry(undefined)
          }}
        />
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        title="删除研究记录？"
        message={deleteTarget ? `“${deleteTarget.title}”将被永久删除，且无法恢复。` : ''}
        confirmText="删除记录"
        danger
        pending={remove.isPending}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={() => { if (deleteTarget) remove.mutate(deleteTarget.id) }}
      />

      {patchStatus.isPending && (
        <div role="status" aria-live="polite" className="fixed bottom-4 left-1/2 z-40 flex -translate-x-1/2 items-center gap-2 rounded-full border border-border bg-surface px-4 py-2 text-xs text-secondary shadow-lg">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />正在更新状态
        </div>
      )}
    </>
  )
}
