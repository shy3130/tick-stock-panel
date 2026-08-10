import { useState, type FormEvent, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  CircleDashed,
  FileSearch,
  FlaskConical,
  Loader2,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  XCircle,
  type LucideIcon,
} from 'lucide-react'
import { EmptyState } from '@/components/EmptyState'
import { PageHeader } from '@/components/PageHeader'
import { toast } from '@/components/Toast'
import {
  api,
  type ResearchEvidenceKind,
  type ResearchHypothesis,
  type ResearchHypothesisStatus,
  type ResearchRunCard,
  type ResearchSchedule,
  type ResearchScheduleTemplate,
} from '@/lib/api'
import { cn } from '@/lib/cn'
import { QK } from '@/lib/queryKeys'

const INPUT = 'w-full rounded-btn border border-border bg-base px-2.5 py-1.5 text-xs text-foreground outline-none transition-colors placeholder:text-muted/60 focus:border-accent/50 disabled:cursor-not-allowed disabled:opacity-50'
const BTN_PRIMARY = 'inline-flex items-center justify-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-base transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50'
const BTN_GHOST = 'inline-flex items-center justify-center gap-1.5 rounded-btn border border-border bg-elevated px-3 py-1.5 text-xs text-secondary transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50'
const BTN_DANGER = 'inline-flex items-center justify-center gap-1.5 rounded-btn border border-danger/40 bg-danger/10 px-3 py-1.5 text-xs text-danger transition-colors hover:bg-danger/20 disabled:cursor-not-allowed disabled:opacity-50'
const CARD = 'rounded-card border border-border bg-surface/70 shadow-[0_1px_2px_hsl(var(--border)/0.35)]'

const HYPOTHESIS_STATUSES: { value: ResearchHypothesisStatus; label: string; badge: string; icon: LucideIcon }[] = [
  { value: 'exploring', label: '探索中', badge: 'bg-accent/10 text-accent', icon: CircleDashed },
  { value: 'testing', label: '检验中', badge: 'bg-warning/10 text-warning', icon: Activity },
  { value: 'validated', label: '已验证', badge: 'bg-success/10 text-success', icon: CheckCircle2 },
  { value: 'rejected', label: '已否决', badge: 'bg-danger/10 text-danger', icon: XCircle },
  { value: 'monitoring', label: '持续观察', badge: 'bg-muted/15 text-secondary', icon: FileSearch },
]

const EVIDENCE_KINDS: { value: ResearchEvidenceKind; label: string }[] = [
  { value: 'backtest', label: '回测' },
  { value: 'note', label: '笔记' },
  { value: 'observation', label: '观察' },
]

const SCHEDULE_TEMPLATES: { value: ResearchScheduleTemplate; label: string; hint: string }[] = [
  { value: 'market_recap_daily', label: '大盘日复盘', hint: '基于本地市场概览生成事实摘要' },
  { value: 'watchlist_recap_daily', label: '自选日复盘', hint: '汇总自选覆盖、行情与增强数据' },
  { value: 'strategy_pool_weekly', label: '策略池周报', hint: '统计策略池与既有 Run Card' },
]

type ResearchTab = 'hypotheses' | 'schedules'

type HypothesisDraft = {
  title: string
  thesis: string
  status: ResearchHypothesisStatus
  tags: string
}

type ScheduleDraft = {
  name: string
  template: ResearchScheduleTemplate
  cron: string
  enabled: boolean
  params: string
}

function emptyHypothesisDraft(): HypothesisDraft {
  return { title: '', thesis: '', status: 'exploring', tags: '' }
}

function emptyScheduleDraft(): ScheduleDraft {
  return {
    name: '',
    template: 'market_recap_daily',
    cron: '0 18 * * 1-5',
    enabled: true,
    params: '{}',
  }
}

function toScheduleDraft(item: ResearchSchedule): ScheduleDraft {
  return {
    name: item.name,
    template: isScheduleTemplate(item.template) ? item.template : 'market_recap_daily',
    cron: item.cron,
    enabled: item.enabled,
    params: stringifyJson(item.params),
  }
}

function isHypothesisStatus(value: string): value is ResearchHypothesisStatus {
  return HYPOTHESIS_STATUSES.some((item) => item.value === value)
}

function isScheduleTemplate(value: string): value is ResearchScheduleTemplate {
  return SCHEDULE_TEMPLATES.some((item) => item.value === value)
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function stringifyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return '{}'
  }
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : '请求未完成，请稍后重试。'
}

function fmtTime(value: string | null | undefined): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(parsed)
}

function statusMeta(status: string): { label: string; badge: string; icon: LucideIcon } {
  return HYPOTHESIS_STATUSES.find((item) => item.value === status)
    ?? { label: status || '未知状态', badge: 'bg-muted/15 text-secondary', icon: AlertCircle }
}

function scheduleTemplateLabel(template: string): string {
  return SCHEDULE_TEMPLATES.find((item) => item.value === template)?.label ?? template
}

export function Research() {
  const [tab, setTab] = useState<ResearchTab>('hypotheses')

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="研究中心" subtitle="假设、证据与定时研究均留存为可复核事实" />
      <div className="flex items-center gap-1 overflow-x-auto border-b border-border px-5 pt-3 pb-2">
        <TabButton active={tab === 'hypotheses'} icon={FlaskConical} onClick={() => setTab('hypotheses')}>
          研究假设
        </TabButton>
        <TabButton active={tab === 'schedules'} icon={Activity} onClick={() => setTab('schedules')}>
          定时研究
        </TabButton>
      </div>
      <main className="min-h-0 flex-1 overflow-auto px-5 py-4">
        <div className="mx-auto max-w-6xl">
          {tab === 'hypotheses' ? <HypothesesPanel /> : <SchedulesPanel />}
        </div>
      </main>
    </div>
  )
}

function TabButton({ active, icon: Icon, children, onClick }: {
  active: boolean
  icon: LucideIcon
  children: ReactNode
  onClick: () => void
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        'inline-flex shrink-0 items-center gap-1.5 rounded-btn px-3 py-1.5 text-xs font-medium transition-colors',
        active ? 'bg-accent/15 text-accent' : 'text-secondary hover:bg-elevated/60 hover:text-foreground',
      )}
    >
      <Icon className="h-3.5 w-3.5" />
      {children}
    </button>
  )
}

function HypothesesPanel() {
  const qc = useQueryClient()
  const [status, setStatus] = useState<string>('')
  const [search, setSearch] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [draft, setDraft] = useState<HypothesisDraft>(emptyHypothesisDraft)

  const hypothesesQuery = useQuery({
    queryKey: QK.researchHypotheses(status || undefined, search || undefined),
    queryFn: () => api.researchListHypotheses({ status: status || undefined, query: search.trim() || undefined }),
  })

  const invalidateHypotheses = (id?: string) => {
    void qc.invalidateQueries({ queryKey: QK.researchHypothesesRoot })
    if (id) void qc.invalidateQueries({ queryKey: QK.researchHypothesis(id) })
  }

  const createMutation = useMutation({
    mutationFn: (body: Parameters<typeof api.researchCreateHypothesis>[0]) => api.researchCreateHypothesis(body),
    onSuccess: () => {
      invalidateHypotheses()
      setCreateOpen(false)
      setDraft(emptyHypothesisDraft())
      toast('研究假设已建立', 'success')
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Parameters<typeof api.researchUpdateHypothesis>[1] }) =>
      api.researchUpdateHypothesis(id, body),
    onSuccess: (_, variables) => {
      invalidateHypotheses(variables.id)
      toast('假设状态已更新', 'success')
    },
  })

  const evidenceMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Parameters<typeof api.researchAddEvidence>[1] }) =>
      api.researchAddEvidence(id, body),
    onSuccess: (_, variables) => {
      invalidateHypotheses(variables.id)
      toast('证据已追加', 'success')
    },
  })

  const submitCreate = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!draft.title.trim() || !draft.thesis.trim()) return
    createMutation.mutate({
      title: draft.title.trim(),
      thesis: draft.thesis.trim(),
      status: draft.status,
      tags: draft.tags.split(/[，,]/).map((tag) => tag.trim()).filter(Boolean),
    })
  }

  return (
    <div className="space-y-4">
      <section className={cn(CARD, 'p-3')} aria-label="研究假设筛选">
        <div className="flex flex-col gap-3 md:flex-row md:items-center">
          <label className="relative flex-1">
            <span className="sr-only">搜索研究假设</span>
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              className={cn(INPUT, 'pl-8')}
              placeholder="搜索标题或研究命题"
            />
          </label>
          <label className="flex items-center gap-2 text-xs text-secondary">
            <span className="shrink-0">状态</span>
            <select value={status} onChange={(event) => setStatus(event.target.value)} className={INPUT}>
              <option value="">全部</option>
              {HYPOTHESIS_STATUSES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
            </select>
          </label>
          <button
            type="button"
            onClick={() => setCreateOpen((open) => !open)}
            className={BTN_PRIMARY}
            aria-expanded={createOpen}
          >
            <Plus className="h-3.5 w-3.5" />新建假设
          </button>
        </div>
        {createMutation.isError && <InlineError message={messageOf(createMutation.error)} />}
      </section>

      {createOpen && (
        <section className={cn(CARD, 'p-4')} aria-labelledby="new-hypothesis-title">
          <h2 id="new-hypothesis-title" className="text-sm font-semibold text-foreground">建立研究假设</h2>
          <p className="mt-1 text-xs leading-relaxed text-muted">写下可被证据支持或否决的命题；不会生成任何交易建议。</p>
          <form onSubmit={submitCreate} className="mt-4 grid gap-3 md:grid-cols-2">
            <label className="grid gap-1.5 text-xs text-secondary">
              标题
              <input value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} className={INPUT} required maxLength={120} placeholder="例如：低波动阶段动量延续性" />
            </label>
            <label className="grid gap-1.5 text-xs text-secondary">
              初始状态
              <select value={draft.status} onChange={(event) => setDraft({ ...draft, status: event.target.value as ResearchHypothesisStatus })} className={INPUT}>
                {HYPOTHESIS_STATUSES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
              </select>
            </label>
            <label className="grid gap-1.5 text-xs text-secondary md:col-span-2">
              研究命题
              <textarea value={draft.thesis} onChange={(event) => setDraft({ ...draft, thesis: event.target.value })} className={cn(INPUT, 'min-h-24 resize-y')} required placeholder="明确观察对象、预期关系与可能被否决的条件。" />
            </label>
            <label className="grid gap-1.5 text-xs text-secondary md:col-span-2">
              标签 <span className="text-muted">（以逗号分隔，可选）</span>
              <input value={draft.tags} onChange={(event) => setDraft({ ...draft, tags: event.target.value })} className={INPUT} placeholder="市场环境，动量，横截面" />
            </label>
            <div className="flex items-center justify-end gap-2 md:col-span-2">
              <button type="button" onClick={() => { setCreateOpen(false); setDraft(emptyHypothesisDraft()) }} className={BTN_GHOST}>取消</button>
              <button type="submit" disabled={createMutation.isPending || !draft.title.trim() || !draft.thesis.trim()} className={BTN_PRIMARY}>
                {createMutation.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
                建立假设
              </button>
            </div>
          </form>
        </section>
      )}

      {hypothesesQuery.isPending ? <LoadingState label="正在读取研究假设" /> : null}
      {hypothesesQuery.isError ? <QueryError onRetry={() => void hypothesesQuery.refetch()} message={messageOf(hypothesesQuery.error)} /> : null}
      {hypothesesQuery.data && hypothesesQuery.data.items.length === 0 ? (
        <EmptyState
          icon={FlaskConical}
          title="尚无匹配的研究假设"
          hint={status || search ? '尝试放宽筛选条件，或建立一个待验证的研究命题。' : '从一个可观察、可否决的命题开始，逐步追加回测、笔记或观察证据。'}
        />
      ) : null}
      {hypothesesQuery.data?.items.map((item) => (
        <HypothesisCard
          key={item.id}
          item={item}
          updatePending={updateMutation.isPending && updateMutation.variables?.id === item.id}
          evidencePending={evidenceMutation.isPending && evidenceMutation.variables?.id === item.id}
          onStatusChange={(nextStatus) => updateMutation.mutate({ id: item.id, body: { status: nextStatus } })}
          onAddEvidence={(body) => evidenceMutation.mutate({ id: item.id, body })}
        />
      ))}
    </div>
  )
}

function HypothesisCard({ item, updatePending, evidencePending, onStatusChange, onAddEvidence }: {
  item: ResearchHypothesis
  updatePending: boolean
  evidencePending: boolean
  onStatusChange: (status: ResearchHypothesisStatus) => void
  onAddEvidence: (body: { kind: ResearchEvidenceKind; ref?: string; summary: string }) => void
}) {
  const [evidenceOpen, setEvidenceOpen] = useState(false)
  const [kind, setKind] = useState<ResearchEvidenceKind>('observation')
  const [ref, setRef] = useState('')
  const [summary, setSummary] = useState('')
  const [runCardId, setRunCardId] = useState<string | null>(null)
  const [detailOpen, setDetailOpen] = useState(false)

  const meta = statusMeta(item.status)
  const StatusIcon = meta.icon

  const submitEvidence = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!summary.trim()) return
    onAddEvidence({ kind, ref: ref.trim() || undefined, summary: summary.trim() })
    setSummary('')
    setRef('')
    setEvidenceOpen(false)
  }

  return (
    <article className={cn(CARD, 'overflow-hidden')}>
      <div className="border-b border-border/70 px-4 py-3">
        <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-sm font-semibold text-foreground">{item.title}</h2>
              <span className={cn('inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-medium', meta.badge)}>
                <StatusIcon className="h-3 w-3" />{meta.label}
              </span>
            </div>
            <p className="mt-2 whitespace-pre-wrap text-xs leading-relaxed text-secondary">{item.thesis}</p>
            {item.tags.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {item.tags.map((tag) => <span key={tag} className="rounded-md bg-elevated px-1.5 py-0.5 text-[10px] text-muted">#{tag}</span>)}
              </div>
            )}
          </div>
          <label className="flex shrink-0 items-center gap-1.5 text-[10px] text-muted">
            <span className="sr-only">更新假设状态</span>
            <select
              value={isHypothesisStatus(item.status) ? item.status : ''}
              disabled={updatePending || !isHypothesisStatus(item.status)}
              onChange={(event) => onStatusChange(event.target.value as ResearchHypothesisStatus)}
              className={cn(INPUT, 'min-w-28 py-1')}
            >
              {HYPOTHESIS_STATUSES.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
            {updatePending && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" />}
          </label>
        </div>
      </div>
      <div className="px-4 py-3">
        <div className="flex items-center gap-2">
          <h3 className="text-xs font-medium text-secondary">证据链</h3>
          <span className="rounded-md bg-elevated px-1.5 py-0.5 text-[10px] text-muted">{item.evidence.length}</span>
          <button type="button" onClick={() => setDetailOpen((open) => !open)} className={cn(BTN_GHOST, 'ml-auto px-2 py-1')} aria-expanded={detailOpen}>
            <FileSearch className="h-3 w-3" />刷新详情
          </button>
          <button type="button" onClick={() => setEvidenceOpen((open) => !open)} className={cn(BTN_GHOST, 'px-2 py-1')} aria-expanded={evidenceOpen}>
            <Plus className="h-3 w-3" />追加证据
          </button>
        </div>
        {item.evidence.length === 0 ? (
          <p className="mt-3 text-xs text-muted">尚无证据。追加回测、研究笔记或持续观察，形成可复核的证据链。</p>
        ) : (
          <ul className="mt-3 space-y-2" aria-label="已记录证据">
            {item.evidence.map((evidence, index) => (
              <li key={`${evidence.ts}-${index}`} className="border-l-2 border-border pl-3">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="text-[10px] font-medium text-accent">{EVIDENCE_KINDS.find((option) => option.value === evidence.kind)?.label ?? evidence.kind}</span>
                  <span className="font-mono text-[10px] text-muted">{fmtTime(evidence.ts)}</span>
                  {evidence.ref && (
                    <button type="button" onClick={() => setRunCardId(evidence.ref)} className="text-[10px] text-secondary underline decoration-border underline-offset-2 hover:text-accent">
                      查看 Run Card · {evidence.ref}
                    </button>
                  )}
                </div>
                <p className="mt-1 whitespace-pre-wrap text-xs leading-relaxed text-secondary">{evidence.summary}</p>
              </li>
            ))}
          </ul>
        )}
        {evidenceOpen && (
          <form onSubmit={submitEvidence} className="mt-3 grid gap-2 rounded-btn border border-border/70 bg-base/60 p-3 md:grid-cols-2">
            <label className="grid gap-1 text-[10px] text-muted">
              证据类别
              <select value={kind} onChange={(event) => setKind(event.target.value as ResearchEvidenceKind)} className={INPUT}>
                {EVIDENCE_KINDS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
            </label>
            <label className="grid gap-1 text-[10px] text-muted">
              Run Card ID 或引用 <span className="text-muted/70">（可选）</span>
              <input value={ref} onChange={(event) => setRef(event.target.value)} className={INPUT} placeholder="例如：strategy-run-20260810" />
            </label>
            <label className="grid gap-1 text-[10px] text-muted md:col-span-2">
              <textarea value={summary} onChange={(event) => setSummary(event.target.value)} className={cn(INPUT, 'min-h-20 resize-y')} required placeholder="记录数据范围、结论与限制条件。" />
            </label>
            <div className="flex justify-end gap-2 md:col-span-2">
              <button type="button" onClick={() => setEvidenceOpen(false)} className={cn(BTN_GHOST, 'px-2 py-1')}>取消</button>
              <button type="submit" disabled={evidencePending || !summary.trim()} className={cn(BTN_PRIMARY, 'px-2 py-1')}>
                {evidencePending && <Loader2 className="h-3 w-3 animate-spin" />}追加
              </button>
            </div>
          </form>
        )}
      </div>
      {detailOpen && <HypothesisDetailInline id={item.id} onClose={() => setDetailOpen(false)} />}

      {runCardId && <RunCardInline runId={runCardId} onClose={() => setRunCardId(null)} />}
    </article>
  )
}

function HypothesisDetailInline({ id, onClose }: { id: string; onClose: () => void }) {
  const detailQuery = useQuery({
    queryKey: QK.researchHypothesis(id),
    queryFn: () => api.researchGetHypothesis(id),
  })

  return (
    <section className="border-t border-border/70 bg-base/35 px-4 py-3" aria-label={`研究假设详情 ${id}`}>
      <div className="flex items-center gap-2">
        <FileSearch className="h-3.5 w-3.5 text-accent" />
        <h3 className="text-xs font-semibold text-foreground">服务端详情</h3>
        <span className="font-mono text-[10px] text-muted">{id}</span>
        <button type="button" onClick={onClose} className="ml-auto text-[10px] text-muted hover:text-foreground">收起</button>
      </div>
      {detailQuery.isPending && <LoadingState label="正在读取假设详情" compact />}
      {detailQuery.isError && <InlineError message={`假设详情读取失败：${messageOf(detailQuery.error)}`} />}
      {detailQuery.data && (
        <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 rounded-btn border border-border/60 bg-surface/60 p-3 text-xs">
          <MetaRow label="建立时间" value={fmtTime(detailQuery.data.created_at)} />
          <MetaRow label="最后更新" value={fmtTime(detailQuery.data.updated_at)} />
          <MetaRow label="状态" value={statusMeta(detailQuery.data.status).label} />
          <MetaRow label="证据数量" value={String(detailQuery.data.evidence.length)} />
        </dl>
      )}
    </section>
  )
}

function RunCardInline({ runId, onClose }: { runId: string; onClose: () => void }) {
  const runCardQuery = useQuery({
    queryKey: QK.researchRunCard(runId),
    queryFn: () => api.researchGetRunCard(runId),
  })

  return (
    <section className="border-t border-border/70 bg-base/35 px-4 py-3" aria-label={`Run Card ${runId}`}>
      <div className="flex items-center gap-2">
        <FileSearch className="h-3.5 w-3.5 text-accent" />
        <h3 className="text-xs font-semibold text-foreground">Run Card</h3>
        <span className="font-mono text-[10px] text-muted">{runId}</span>
        <button type="button" onClick={onClose} className="ml-auto text-[10px] text-muted hover:text-foreground">收起</button>
      </div>
      {runCardQuery.isPending && <LoadingState label="正在读取 Run Card" compact />}
      {runCardQuery.isError && <InlineError message={`Run Card 读取失败：${messageOf(runCardQuery.error)}`} />}
      {runCardQuery.data === null && (
        <p className="mt-3 text-xs text-muted">此引用暂未对应已保存的 Run Card；它可能来自外部笔记或尚未持久化的运行记录。</p>
      )}
      {runCardQuery.data && <RunCardContent card={runCardQuery.data} />}
    </section>
  )
}

function RunCardContent({ card }: { card: ResearchRunCard }) {
  return (
    <div className="mt-3 grid gap-3 text-xs md:grid-cols-2">
      <div className="space-y-1 rounded-btn border border-border/60 bg-surface/60 p-3">
        <MetaRow label="类型" value={card.kind} />
        <MetaRow label="创建时间" value={fmtTime(card.created_at)} />
        <MetaRow label="配置哈希" value={card.config_hash || '—'} mono />
        <MetaRow label="策略哈希" value={card.strategy_hash || '—'} mono />
      </div>
      <JsonDetails title="统计结果" value={card.stats} />
      <div className="md:col-span-2"><JsonDetails title="运行配置" value={card.config} /></div>
    </div>
  )
}

function MetaRow({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return <div className="flex items-center justify-between gap-3"><span className="text-muted">{label}</span><span className={cn('truncate text-secondary', mono && 'font-mono')}>{value}</span></div>
}

function JsonDetails({ title, value }: { title: string; value: Record<string, unknown> }) {
  return (
    <details className="rounded-btn border border-border/60 bg-surface/60 p-3">
      <summary className="flex cursor-pointer list-none items-center gap-1 text-xs font-medium text-secondary"><ChevronDown className="h-3.5 w-3.5" />{title}</summary>
      <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-[10px] leading-relaxed text-muted">{stringifyJson(value)}</pre>
    </details>
  )
}

function SchedulesPanel() {
  const qc = useQueryClient()
  const [createOpen, setCreateOpen] = useState(false)
  const [draft, setDraft] = useState<ScheduleDraft>(emptyScheduleDraft)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [deleteCandidate, setDeleteCandidate] = useState<ResearchSchedule | null>(null)

  const schedulesQuery = useQuery({ queryKey: QK.researchSchedules, queryFn: api.researchListSchedules })
  const invalidateSchedules = () => void qc.invalidateQueries({ queryKey: QK.researchSchedules })

  const createMutation = useMutation({
    mutationFn: (body: Parameters<typeof api.researchCreateSchedule>[0]) => api.researchCreateSchedule(body),
    onSuccess: () => {
      invalidateSchedules()
      setCreateOpen(false)
      setDraft(emptyScheduleDraft())
      toast('定时研究已创建', 'success')
    },
  })
  const patchMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Parameters<typeof api.researchUpdateSchedule>[1] }) => api.researchUpdateSchedule(id, body),
    onSuccess: () => { invalidateSchedules(); setEditingId(null); toast('定时研究已更新', 'success') },
  })
  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.researchDeleteSchedule(id),
    onSuccess: () => { invalidateSchedules(); setDeleteCandidate(null); toast('定时研究已删除', 'success') },
  })
  const runMutation = useMutation({
    mutationFn: (id: string) => api.researchRunScheduleNow(id),
    onSuccess: () => { invalidateSchedules(); toast('研究任务已运行，状态已刷新', 'success') },
  })

  const submitCreate = (payload: Parameters<typeof api.researchCreateSchedule>[0]) => createMutation.mutate(payload)
  const submitEdit = (id: string, payload: Parameters<typeof api.researchUpdateSchedule>[1]) => patchMutation.mutate({ id, body: payload })

  return (
    <div className="space-y-4">
      <section className={cn(CARD, 'p-3')}>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-sm font-semibold text-foreground">定时研究</h2>
            <p className="mt-1 text-xs text-muted">仅运行本地研究模板；运行结果会留下最近状态与可追溯 Run Card。</p>
          </div>
          <button type="button" onClick={() => setCreateOpen((open) => !open)} className={BTN_PRIMARY} aria-expanded={createOpen}>
            <Plus className="h-3.5 w-3.5" />新建定时研究
          </button>
        </div>
        {createMutation.isError && <InlineError message={messageOf(createMutation.error)} />}
      </section>

      {createOpen && (
        <ScheduleForm
          title="新建定时研究"
          draft={draft}
          pending={createMutation.isPending}
          submitLabel="创建任务"
          onChange={setDraft}
          onCancel={() => { setCreateOpen(false); setDraft(emptyScheduleDraft()) }}
          onSubmit={submitCreate}
        />
      )}

      {schedulesQuery.isPending && <LoadingState label="正在读取定时研究" />}
      {schedulesQuery.isError && <QueryError onRetry={() => void schedulesQuery.refetch()} message={messageOf(schedulesQuery.error)} />}
      {schedulesQuery.data?.items.length === 0 && (
        <EmptyState icon={Activity} title="尚未设置定时研究" hint="创建任务后，可按 Cron 周期生成大盘、自选或策略池的本地事实摘要。" />
      )}
      <div className="grid gap-3 lg:grid-cols-2">
        {schedulesQuery.data?.items.map((item) => (
          editingId === item.id ? (
            <ScheduleForm
              key={item.id}
              title={`编辑：${item.name}`}
              draft={toScheduleDraft(item)}
              pending={patchMutation.isPending && patchMutation.variables?.id === item.id}
              submitLabel="保存修改"
              error={patchMutation.isError && patchMutation.variables?.id === item.id ? messageOf(patchMutation.error) : undefined}
              onCancel={() => setEditingId(null)}
              onSubmit={(body) => submitEdit(item.id, body)}
            />
          ) : (
            <ScheduleCard
              key={item.id}
              item={item}
              togglePending={patchMutation.isPending && patchMutation.variables?.id === item.id}
              runPending={runMutation.isPending && runMutation.variables === item.id}
              runError={runMutation.isError && runMutation.variables === item.id ? messageOf(runMutation.error) : undefined}
              onToggle={(enabled) => patchMutation.mutate({ id: item.id, body: { enabled } })}
              onEdit={() => setEditingId(item.id)}
              onDelete={() => setDeleteCandidate(item)}
              onRun={() => runMutation.mutate(item.id)}
            />
          )
        ))}
      </div>
      {deleteCandidate && (
        <section className="rounded-card border border-danger/40 bg-danger/5 p-4" aria-label="确认删除定时研究">
          <p className="text-sm font-medium text-foreground">删除「{deleteCandidate.name}」？</p>
          <p className="mt-1 text-xs text-secondary">将移除该任务及其调度配置，既有 Run Card 不会被删除。</p>
          {deleteMutation.isError && <InlineError message={messageOf(deleteMutation.error)} />}
          <div className="mt-3 flex justify-end gap-2">
            <button type="button" onClick={() => setDeleteCandidate(null)} className={BTN_GHOST}>取消</button>
            <button type="button" onClick={() => deleteMutation.mutate(deleteCandidate.id)} disabled={deleteMutation.isPending} className={BTN_DANGER}>
              {deleteMutation.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}确认删除
            </button>
          </div>
        </section>
      )}
    </div>
  )
}

function ScheduleCard({ item, togglePending, runPending, runError, onToggle, onEdit, onDelete, onRun }: {
  item: ResearchSchedule
  togglePending: boolean
  runPending: boolean
  runError?: string
  onToggle: (enabled: boolean) => void
  onEdit: () => void
  onDelete: () => void
  onRun: () => void
}) {
  const lastState = item.last_status === 'success'
    ? { label: '最近成功', cls: 'text-success' }
    : item.last_status === 'failed'
      ? { label: '最近失败', cls: 'text-danger' }
      : { label: '尚未运行', cls: 'text-muted' }

  return (
    <article className={cn(CARD, 'p-4')}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="truncate text-sm font-semibold text-foreground">{item.name}</h2>
            <span className={cn('rounded-md px-1.5 py-0.5 text-[10px] font-medium', item.enabled ? 'bg-success/10 text-success' : 'bg-muted/15 text-muted')}>
              {item.enabled ? '已启用' : '已停用'}
            </span>
          </div>
          <p className="mt-1 text-xs text-secondary">{scheduleTemplateLabel(item.template)}</p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <button type="button" onClick={onEdit} className="inline-flex h-7 w-7 items-center justify-center rounded-btn text-muted hover:bg-elevated hover:text-foreground" aria-label={`编辑 ${item.name}`}><Pencil className="h-3.5 w-3.5" /></button>
          <button type="button" onClick={onDelete} className="inline-flex h-7 w-7 items-center justify-center rounded-btn text-muted hover:bg-danger/10 hover:text-danger" aria-label={`删除 ${item.name}`}><Trash2 className="h-3.5 w-3.5" /></button>
        </div>
      </div>
      <div className="mt-4 grid grid-cols-2 gap-x-3 gap-y-2 border-y border-border/60 py-3 text-xs">
        <MetaRow label="Cron" value={item.cron} mono />
        <MetaRow label="最近运行" value={fmtTime(item.last_run_at)} />
        <MetaRow label="状态" value={lastState.label} />
        <MetaRow label="更新于" value={fmtTime(item.updated_at)} />
      </div>
      {item.last_error && <p className="mt-3 rounded-btn bg-danger/10 px-2.5 py-2 text-xs leading-relaxed text-danger">最近错误：{item.last_error}</p>}
      {runError && <InlineError message={runError} />}
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
        <button type="button" role="switch" aria-checked={item.enabled} disabled={togglePending} onClick={() => onToggle(!item.enabled)} className={cn(BTN_GHOST, 'px-2 py-1', item.enabled && 'border-success/30 text-success')}>
          {togglePending ? <Loader2 className="h-3 w-3 animate-spin" /> : <Activity className="h-3 w-3" />}{item.enabled ? '停用任务' : '启用任务'}
        </button>
        <button type="button" onClick={onRun} disabled={runPending} className={cn(BTN_PRIMARY, 'px-2 py-1')}>
          {runPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <Play className="h-3 w-3" />}立即运行
        </button>
      </div>
    </article>
  )
}

function ScheduleForm({ title, draft: initialDraft, pending, submitLabel, error, onChange, onCancel, onSubmit }: {
  title: string
  draft: ScheduleDraft
  pending: boolean
  submitLabel: string
  error?: string
  onChange?: (draft: ScheduleDraft) => void
  onCancel: () => void
  onSubmit: (body: Parameters<typeof api.researchCreateSchedule>[0]) => void
}) {
  const [draft, setDraft] = useState(initialDraft)
  const [paramsError, setParamsError] = useState<string | null>(null)
  const update = (next: ScheduleDraft) => { setDraft(next); onChange?.(next) }

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    let params: Record<string, unknown> = {}
    try {
      const parsed: unknown = JSON.parse(draft.params)
      const record = asRecord(parsed)
      if (!record) {
        setParamsError('参数必须是 JSON 对象，例如 {"hypothesis_id":"hyp-..."}。')
        return
      }
      params = record
    } catch {
      setParamsError('参数不是有效的 JSON。')
      return
    }
    setParamsError(null)
    onSubmit({ name: draft.name.trim(), template: draft.template, cron: draft.cron.trim(), enabled: draft.enabled, params })
  }

  return (
    <section className={cn(CARD, 'p-4')}>
      <h2 className="text-sm font-semibold text-foreground">{title}</h2>
      <form onSubmit={submit} className="mt-4 grid gap-3 md:grid-cols-2">
        <label className="grid gap-1.5 text-xs text-secondary">
          名称
          <input value={draft.name} onChange={(event) => update({ ...draft, name: event.target.value })} className={INPUT} required maxLength={120} placeholder="例如：每周策略池事实摘要" />
        </label>
        <label className="grid gap-1.5 text-xs text-secondary">
          研究模板
          <select value={draft.template} onChange={(event) => update({ ...draft, template: event.target.value as ResearchScheduleTemplate })} className={INPUT}>
            {SCHEDULE_TEMPLATES.map((template) => <option key={template.value} value={template.value}>{template.label}</option>)}
          </select>
          <span className="text-[10px] text-muted">{SCHEDULE_TEMPLATES.find((item) => item.value === draft.template)?.hint}</span>
        </label>
        <label className="grid gap-1.5 text-xs text-secondary">
          Cron 表达式
          <input value={draft.cron} onChange={(event) => update({ ...draft, cron: event.target.value })} className={cn(INPUT, 'font-mono')} required placeholder="0 18 * * 1-5" />
          <span className="text-[10px] text-muted">分 时 日 月 周，共五段；服务端会校验段数。</span>
        </label>
        <label className="flex items-center gap-2 self-start pt-5 text-xs text-secondary">
          <input type="checkbox" checked={draft.enabled} onChange={(event) => update({ ...draft, enabled: event.target.checked })} className="h-3.5 w-3.5 accent-accent" />
          创建后立即启用
        </label>
        <label className="grid gap-1.5 text-xs text-secondary md:col-span-2">
          参数 JSON <span className="text-muted">（可选；可传 hypothesis_id 以自动追加观察证据）</span>
          <textarea value={draft.params} onChange={(event) => update({ ...draft, params: event.target.value })} className={cn(INPUT, 'min-h-24 resize-y font-mono')} spellCheck={false} />
        </label>
        {paramsError && <InlineError message={paramsError} />}
        {error && <InlineError message={error} />}
        <div className="flex justify-end gap-2 md:col-span-2">
          <button type="button" onClick={onCancel} className={BTN_GHOST}>取消</button>
          <button type="submit" disabled={pending || !draft.name.trim() || !draft.cron.trim()} className={BTN_PRIMARY}>
            {pending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}{submitLabel}
          </button>
        </div>
      </form>
    </section>
  )
}

function LoadingState({ label, compact = false }: { label: string; compact?: boolean }) {
  return (
    <div className={cn('flex items-center justify-center gap-2 text-xs text-muted', compact ? 'py-5' : 'min-h-48')} role="status">
      <Loader2 className="h-4 w-4 animate-spin" />{label}
    </div>
  )
}

function QueryError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <section className="rounded-card border border-danger/40 bg-danger/5 p-5 text-center" role="alert">
      <AlertCircle className="mx-auto h-5 w-5 text-danger" />
      <p className="mt-2 text-sm font-medium text-foreground">研究数据读取失败</p>
      <p className="mt-1 break-words text-xs text-danger">{message}</p>
      <button type="button" onClick={onRetry} className={cn(BTN_GHOST, 'mt-3')}><RefreshCw className="h-3.5 w-3.5" />重试</button>
    </section>
  )
}

function InlineError({ message }: { message: string }) {
  return <p className="mt-3 flex items-start gap-1.5 rounded-btn bg-danger/10 px-2.5 py-2 text-xs leading-relaxed text-danger" role="alert"><AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />{message}</p>
}
