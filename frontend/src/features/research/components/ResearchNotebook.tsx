import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
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
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import {
  addEvidence,
  createHypothesis,
  createSchedule,
  deleteSchedule,
  getHypothesis,
  getRunCard,
  listHypotheses,
  listSchedules,
  runScheduleNow,
  updateHypothesis,
  updateSchedule,
} from '../api/evidence'
import { ParameterForm } from './ParameterForm'
import { ScopeEditor } from './ScopeEditor'
import { useFactorCatalog, useFactorDetail } from '../hooks/useResearchQueries'
import {
  extractScheduledRunId,
  factorRunScheduleParams,
  isRecapScheduleTemplate,
  parseFactorRunScheduleParams,
  type AddEvidenceBody,
  type CreateHypothesisBody,
  type CreateScheduleBody,
  type RecapScheduleTemplate,
  type ResearchEvidenceKind,
  type ResearchHypothesis,
  type ResearchHypothesisStatus,
  type ResearchRunCard,
  type ResearchSchedule,
  type UpdateHypothesisBody,
  type UpdateScheduleBody,
} from '../model/notebook'
import { buildParameterForm, defaultParameters, structurallyValid } from '../model/schema'
import type { RunScope } from '../model/status'
import { scopeLabel } from '../model/status'
import { researchKeys } from '../queryKeys'

const INPUT = 'control w-full text-xs'
const BTN_PRIMARY = 'btn-primary text-xs'
const BTN_GHOST = 'btn-secondary text-xs'
const BTN_DANGER = 'btn-ghost text-xs text-danger hover:bg-danger/10 hover:text-danger border border-danger/40'
const CARD = 'panel'

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
  { value: 'factor_run', label: '因子运行' },
]

const APPENDABLE_EVIDENCE_KINDS = EVIDENCE_KINDS.filter((item) => item.value !== 'factor_run')

const RECAP_TEMPLATES: { value: RecapScheduleTemplate; label: string; hint: string }[] = [
  { value: 'market_recap_daily', label: '大盘日复盘', hint: '基于本地市场概览生成事实摘要' },
  { value: 'watchlist_recap_daily', label: '自选日复盘', hint: '汇总自选覆盖、行情与增强数据' },
  { value: 'strategy_pool_weekly', label: '策略池周报', hint: '统计策略池与既有 Run Card' },
]


type HypothesisDraft = {
  title: string
  thesis: string
  status: ResearchHypothesisStatus
  tags: string
}

type ScheduleDraft = {
  name: string
  template: RecapScheduleTemplate
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
    template: isRecapScheduleTemplate(item.template) ? item.template : 'market_recap_daily',
    cron: item.cron,
    enabled: item.enabled,
    params: stringifyJson(item.params),
  }
}

function isHypothesisStatus(value: string): value is ResearchHypothesisStatus {
  return HYPOTHESIS_STATUSES.some((item) => item.value === value)
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
  if (template === 'factor_run') return '因子运行'
  return RECAP_TEMPLATES.find((item) => item.value === template)?.label ?? template
}

export function HypothesesPanel() {
  const qc = useQueryClient()
  const [status, setStatus] = useState<string>('')
  const [search, setSearch] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [draft, setDraft] = useState<HypothesisDraft>(emptyHypothesisDraft)

  const hypothesesQuery = useQuery({
    queryKey: researchKeys.hypotheses(status || undefined, search || undefined),
    queryFn: () => listHypotheses({ status: status || undefined, query: search.trim() || undefined }),
  })

  const invalidateHypotheses = (id?: string) => {
    void qc.invalidateQueries({ queryKey: researchKeys.hypothesesRoot })
    if (id) void qc.invalidateQueries({ queryKey: researchKeys.hypothesis(id) })
  }

  const createMutation = useMutation({
    mutationFn: (body: CreateHypothesisBody) => createHypothesis(body),
    onSuccess: () => {
      invalidateHypotheses()
      setCreateOpen(false)
      setDraft(emptyHypothesisDraft())
      toast('研究假设已建立', 'success')
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: UpdateHypothesisBody }) =>
      updateHypothesis(id, body),
    onSuccess: (_, variables) => {
      invalidateHypotheses(variables.id)
      toast('假设状态已更新', 'success')
    },
  })

  const evidenceMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: AddEvidenceBody }) =>
      addEvidence(id, body),
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
      <section className={cn(CARD)} aria-label="研究假设筛选">
        <div className="panel-body flex flex-col gap-3 md:flex-row md:items-center">
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
        <section className={cn(CARD)} aria-labelledby="new-hypothesis-title">
          <div className="panel-body">
          <h2 id="new-hypothesis-title" className="section-title">建立研究假设</h2>
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
          </div>
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
                  {evidence.ref && evidence.kind === 'factor_run' ? (
                    <Link
                      to={`/research/runs/${encodeURIComponent(evidence.ref)}`}
                      className="break-all text-[10px] text-secondary underline decoration-border underline-offset-2 hover:text-accent"
                    >
                      查看因子运行 · {evidence.ref}
                    </Link>
                  ) : evidence.ref ? (
                    <button type="button" onClick={() => setRunCardId(evidence.ref)} className="break-all text-[10px] text-secondary underline decoration-border underline-offset-2 hover:text-accent">
                      查看 Run Card · {evidence.ref}
                    </button>
                  ) : null}
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
                {APPENDABLE_EVIDENCE_KINDS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
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
    queryKey: researchKeys.hypothesis(id),
    queryFn: () => getHypothesis(id),
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
    queryKey: researchKeys.runCard(runId),
    queryFn: () => getRunCard(runId),
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

export function SchedulesPanel({ kind }: { kind: 'recap' | 'factor_run' }) {
  const qc = useQueryClient()
  const [createOpen, setCreateOpen] = useState(false)
  const [draft, setDraft] = useState<ScheduleDraft>(emptyScheduleDraft)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [deleteCandidate, setDeleteCandidate] = useState<ResearchSchedule | null>(null)
  const factorRun = kind === 'factor_run'

  const schedulesQuery = useQuery({ queryKey: researchKeys.schedules, queryFn: listSchedules })
  const invalidateSchedules = () => void qc.invalidateQueries({ queryKey: researchKeys.schedules })
  const items = (schedulesQuery.data?.items ?? []).filter((item) =>
    factorRun ? item.template === 'factor_run' : item.template !== 'factor_run',
  )

  const createMutation = useMutation({
    mutationFn: (body: CreateScheduleBody) => createSchedule(body),
    onSuccess: () => {
      invalidateSchedules()
      setCreateOpen(false)
      setDraft(emptyScheduleDraft())
      toast(factorRun ? '因子运行任务已创建' : '定时复盘已创建', 'success')
    },
  })
  const patchMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: UpdateScheduleBody }) => updateSchedule(id, body),
    onSuccess: () => { invalidateSchedules(); setEditingId(null); toast('定时研究已更新', 'success') },
  })
  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteSchedule(id),
    onSuccess: () => { invalidateSchedules(); setDeleteCandidate(null); toast('定时研究已删除', 'success') },
  })
  const runMutation = useMutation({
    mutationFn: (id: string) => runScheduleNow(id),
    onSuccess: (response) => {
      invalidateSchedules()
      const runId = extractScheduledRunId(response.result)
      toast(runId ? `已创建运行 ${runId}` : '研究任务已运行，状态已刷新', 'success')
    },
  })

  return (
    <div className="min-w-0 space-y-4">
      <section className={cn(CARD)}>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-foreground">{factorRun ? '因子运行' : '三类复盘'}</h2>
            <p className="mt-1 text-xs leading-relaxed text-muted">
              {factorRun
                ? '冻结 factor_id、scope 与完整 parameters；按 Cron 创建 Durable Run，不写入旧 Run Card。'
                : '大盘日复盘、自选日复盘、策略池周报继续生成并关联既有 Run Card。'}
            </p>
          </div>
          <button type="button" onClick={() => setCreateOpen((open) => !open)} className={cn(BTN_PRIMARY, 'min-h-11 shrink-0')} aria-expanded={createOpen}>
            <Plus className="h-3.5 w-3.5" />{factorRun ? '新建因子运行' : '新建复盘'}
          </button>
        </div>
        {createMutation.isError && <InlineError message={messageOf(createMutation.error)} />}
      </section>

      {createOpen && (factorRun ? (
        <FactorRunScheduleForm
          title="新建因子运行"
          pending={createMutation.isPending}
          submitLabel="创建任务"
          onCancel={() => setCreateOpen(false)}
          onSubmit={(body) => createMutation.mutate(body)}
        />
      ) : (
        <ScheduleForm
          title="新建定时复盘"
          draft={draft}
          pending={createMutation.isPending}
          submitLabel="创建任务"
          onChange={setDraft}
          onCancel={() => { setCreateOpen(false); setDraft(emptyScheduleDraft()) }}
          onSubmit={(body) => createMutation.mutate(body)}
        />
      ))}

      {schedulesQuery.isPending ? <LoadingState label="正在读取定时研究" /> : null}
      {schedulesQuery.isError ? <QueryError onRetry={() => void schedulesQuery.refetch()} message={messageOf(schedulesQuery.error)} /> : null}
      {schedulesQuery.data && items.length === 0 ? (
        <EmptyState
          icon={Activity}
          title={factorRun ? '尚未设置因子运行' : '尚未设置定时复盘'}
          hint={factorRun ? '选择因子并冻结范围与参数后，可按 Cron 创建 Durable Run。' : '创建任务后，可按 Cron 生成大盘、自选或策略池的本地事实摘要。'}
        />
      ) : null}
      <div className="grid min-w-0 gap-3">
        {items.map((item) => (
          editingId === item.id ? (
            factorRun ? (
              <FactorRunScheduleForm
                key={item.id}
                title={`编辑：${item.name}`}
                initial={item}
                pending={patchMutation.isPending && patchMutation.variables?.id === item.id}
                submitLabel="保存修改"
                error={patchMutation.isError && patchMutation.variables?.id === item.id ? messageOf(patchMutation.error) : undefined}
                onCancel={() => setEditingId(null)}
                onSubmit={(body) => patchMutation.mutate({ id: item.id, body })}
              />
            ) : (
              <ScheduleForm
                key={item.id}
                title={`编辑：${item.name}`}
                draft={toScheduleDraft(item)}
                pending={patchMutation.isPending && patchMutation.variables?.id === item.id}
                submitLabel="保存修改"
                error={patchMutation.isError && patchMutation.variables?.id === item.id ? messageOf(patchMutation.error) : undefined}
                onCancel={() => setEditingId(null)}
                onSubmit={(body) => patchMutation.mutate({ id: item.id, body })}
              />
            )
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
      {deleteCandidate ? (
        <section className="rounded-card border border-danger/40 bg-danger/5 p-4" aria-label="确认删除定时研究">
          <p className="text-sm font-medium text-foreground">删除「{deleteCandidate.name}」？</p>
          <p className="mt-1 text-xs text-secondary">
            {factorRun ? '将移除该调度；已生成的因子 Run 不会被删除。' : '将移除该任务及其调度配置，既有 Run Card 不会被删除。'}
          </p>
          {deleteMutation.isError && <InlineError message={messageOf(deleteMutation.error)} />}
          <div className="mt-3 flex justify-end gap-2">
            <button type="button" onClick={() => setDeleteCandidate(null)} className={BTN_GHOST}>取消</button>
            <button type="button" onClick={() => deleteMutation.mutate(deleteCandidate.id)} disabled={deleteMutation.isPending} className={BTN_DANGER}>
              {deleteMutation.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}确认删除
            </button>
          </div>
        </section>
      ) : null}
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
  const factorParams = item.template === 'factor_run' ? parseFactorRunScheduleParams(item.params) : null

  return (
    <article className={cn(CARD, 'min-w-0 overflow-hidden')}>
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
          <button type="button" onClick={onEdit} className="inline-flex h-11 w-11 items-center justify-center rounded-btn text-muted hover:bg-elevated hover:text-foreground" aria-label={`编辑 ${item.name}`}><Pencil className="h-3.5 w-3.5" /></button>
          <button type="button" onClick={onDelete} className="inline-flex h-11 w-11 items-center justify-center rounded-btn text-muted hover:bg-danger/10 hover:text-danger" aria-label={`删除 ${item.name}`}><Trash2 className="h-3.5 w-3.5" /></button>
        </div>
      </div>
      <div className="mt-4 grid grid-cols-1 gap-x-3 gap-y-2 border-y border-border/60 py-3 text-xs sm:grid-cols-2">
        <MetaRow label="Cron" value={item.cron} mono />
        <MetaRow label="最近运行" value={fmtTime(item.last_run_at)} />
        <MetaRow label="状态" value={lastState.label} />
        <MetaRow label="更新于" value={fmtTime(item.updated_at)} />
        {factorParams ? (
          <>
            <MetaRow label="因子" value={factorParams.factor_id} mono />
            <MetaRow label="范围" value={scopeLabel(factorParams.scope)} />
          </>
        ) : null}
      </div>
      {item.template === 'factor_run' && factorParams ? (
        <Link
          to={`/research/factors/${encodeURIComponent(factorParams.factor_id)}`}
          className="mt-3 inline-block break-all text-[11px] text-accent hover:underline"
        >
          打开因子工作台
        </Link>
      ) : null}
      {item.last_error && <p className="mt-3 break-words rounded-btn bg-danger/10 px-2.5 py-2 text-xs leading-relaxed text-danger">最近错误：{item.last_error}</p>}
      {runError && <InlineError message={runError} />}
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
        <button type="button" role="switch" aria-checked={item.enabled} disabled={togglePending} onClick={() => onToggle(!item.enabled)} className={cn(BTN_GHOST, 'min-h-11 px-2 py-1', item.enabled && 'border-success/30 text-success')}>
          {togglePending ? <Loader2 className="h-3 w-3 animate-spin" /> : <Activity className="h-3 w-3" />}{item.enabled ? '停用任务' : '启用任务'}
        </button>
        <button type="button" onClick={onRun} disabled={runPending} className={cn(BTN_PRIMARY, 'min-h-11 px-2 py-1')}>
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
  onSubmit: (body: CreateScheduleBody) => void
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
    <section className={cn(CARD, 'min-w-0')}>
      <h2 className="text-sm font-semibold text-foreground">{title}</h2>
      <form onSubmit={submit} className="mt-4 grid gap-3 md:grid-cols-2">
        <label className="grid gap-1.5 text-xs text-secondary">
          名称
          <input value={draft.name} onChange={(event) => update({ ...draft, name: event.target.value })} className={INPUT} required maxLength={120} placeholder="例如：每周策略池事实摘要" />
        </label>
        <label className="grid gap-1.5 text-xs text-secondary">
          研究模板
          <select value={draft.template} onChange={(event) => update({ ...draft, template: event.target.value as RecapScheduleTemplate })} className={INPUT}>
            {RECAP_TEMPLATES.map((template) => <option key={template.value} value={template.value}>{template.label}</option>)}
          </select>
          <span className="text-[10px] text-muted">{RECAP_TEMPLATES.find((item) => item.value === draft.template)?.hint}</span>
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

function FactorRunScheduleForm({
  title,
  initial,
  pending,
  submitLabel,
  error,
  onCancel,
  onSubmit,
}: {
  title: string
  initial?: ResearchSchedule
  pending: boolean
  submitLabel: string
  error?: string
  onCancel: () => void
  onSubmit: (body: CreateScheduleBody) => void
}) {
  const parsed = initial ? parseFactorRunScheduleParams(initial.params) : null
  const [name, setName] = useState(initial?.name ?? '')
  const [cron, setCron] = useState(initial?.cron ?? '0 19 * * 1-5')
  const [enabled, setEnabled] = useState(initial?.enabled ?? true)
  const [factorId, setFactorId] = useState(parsed?.factor_id ?? '')
  const [scope, setScope] = useState<RunScope>(parsed?.scope ?? { type: 'symbols', symbols: [] })
  const [parameters, setParameters] = useState<Record<string, unknown>>(parsed?.parameters ?? {})
  const [formError, setFormError] = useState<string | null>(null)
  const [hydratedFor, setHydratedFor] = useState<string | null>(parsed?.factor_id ?? null)

  const catalog = useFactorCatalog({})
  const detailQuery = useFactorDetail(factorId || undefined)
  const detail = detailQuery.data
  const form = useMemo(
    () => buildParameterForm(detail?.parameter_schema ?? null, detail?.ui_groups),
    [detail],
  )

  useEffect(() => {
    if (!detail || hydratedFor === detail.id) return
    if (parsed?.factor_id === detail.id) {
      setHydratedFor(detail.id)
      return
    }
    setScope(detail.supported_scopes.includes('symbols') ? { type: 'symbols', symbols: [] } : { type: 'full_market' })
    setParameters(defaultParameters(form))
    setHydratedFor(detail.id)
  }, [detail, form, hydratedFor, parsed?.factor_id])

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!factorId) {
      setFormError('请选择因子。')
      return
    }
    const structureError = structurallyValid(form, parameters, scope)
    if (structureError) {
      setFormError(structureError)
      return
    }
    setFormError(null)
    onSubmit({
      name: name.trim(),
      template: 'factor_run',
      cron: cron.trim(),
      enabled,
      params: factorRunScheduleParams({ factor_id: factorId, scope, parameters }),
    })
  }

  return (
    <section className={cn(CARD, 'min-w-0')}>
      <h2 className="text-sm font-semibold text-foreground">{title}</h2>
      <form onSubmit={submit} className="mt-4 grid min-w-0 gap-3">
        <label className="grid gap-1.5 text-xs text-secondary">
          名称
          <input value={name} onChange={(event) => setName(event.target.value)} className={INPUT} required maxLength={120} placeholder="例如：MACD 全市场周跑" />
        </label>
        <label className="grid gap-1.5 text-xs text-secondary">
          因子
          <select
            value={factorId}
            onChange={(event) => {
              const next = event.target.value
              setFactorId(next)
              if (parsed && parsed.factor_id === next) {
                setScope(parsed.scope)
                setParameters(parsed.parameters)
                setHydratedFor(next)
                return
              }
              setHydratedFor(null)
            }}
            className={INPUT}
            required
          >
            <option value="">{catalog.isPending ? '读取因子目录…' : '选择因子'}</option>
            {(catalog.data?.items ?? []).map((item) => (
              <option key={item.id} value={item.id}>{item.title} · {item.id}</option>
            ))}
          </select>
        </label>
        {catalog.data && catalog.data.items.length === 0 ? (
          <p className="text-xs text-muted">因子目录为空，无法创建 factor_run。</p>
        ) : null}
        {catalog.isError ? <InlineError message={messageOf(catalog.error)} /> : null}
        {detailQuery.isError ? <InlineError message={messageOf(detailQuery.error)} /> : null}
        {detail ? <ScopeEditor detail={detail} scope={scope} onChange={setScope} /> : null}
        {detail ? (
          <ParameterForm
            form={form}
            values={parameters}
            onChange={(field, value) => setParameters((current) => ({ ...current, [field]: value }))}
          />
        ) : null}
        <label className="grid gap-1.5 text-xs text-secondary">
          Cron 表达式
          <input value={cron} onChange={(event) => setCron(event.target.value)} className={cn(INPUT, 'font-mono')} required placeholder="0 19 * * 1-5" />
          <span className="text-[10px] text-muted">分 时 日 月 周，共五段；服务端会校验段数。</span>
        </label>
        <label className="flex items-center gap-2 text-xs text-secondary">
          <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} className="h-3.5 w-3.5 accent-accent" />
          创建后立即启用
        </label>
        {formError ? <InlineError message={formError} /> : null}
        {error ? <InlineError message={error} /> : null}
        <div className="flex justify-end gap-2">
          <button type="button" onClick={onCancel} className={BTN_GHOST}>取消</button>
          <button type="submit" disabled={pending || !name.trim() || !cron.trim() || !factorId} className={BTN_PRIMARY}>
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
