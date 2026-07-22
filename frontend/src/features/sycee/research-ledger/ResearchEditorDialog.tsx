import { useMemo, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2, Plus, Save, X } from 'lucide-react'

import { ConfirmDialog } from '@/components/ConfirmDialog'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import {
  RESEARCH_LEDGER_QUERY_KEY,
  researchLedgerApi,
  type ResearchEntry,
  type ResearchEntryInput,
  type ResearchStatus,
  type ResearchSubjectType,
} from './api'

interface Props {
  entry: ResearchEntry | null
  onClose: () => void
  onSaved: (entry: ResearchEntry) => void
}

interface FormState {
  title: string
  subjectType: ResearchSubjectType
  subject: string
  thesis: string
  evidence: string
  counterEvidence: string
  invalidation: string
  plan: string
  status: ResearchStatus
  tags: string
}

const SUBJECT_OPTIONS: Array<{ value: ResearchSubjectType; label: string }> = [
  { value: 'stock', label: '个股' },
  { value: 'strategy', label: '策略' },
  { value: 'sector', label: '板块' },
  { value: 'market', label: '市场' },
]

const STATUS_OPTIONS: Array<{ value: ResearchStatus; label: string }> = [
  { value: 'draft', label: '草稿' },
  { value: 'tracking', label: '跟踪中' },
  { value: 'validated', label: '已验证' },
  { value: 'invalidated', label: '已失效' },
  { value: 'archived', label: '已归档' },
]

const fieldClass = 'min-h-11 w-full rounded-input border border-border bg-base px-3 text-sm text-foreground outline-none transition-colors placeholder:text-muted/70 focus:border-accent focus:ring-2 focus:ring-accent/20 disabled:opacity-50'
const textareaClass = cn(fieldClass, 'resize-y py-2.5 leading-6')

function initialState(entry: ResearchEntry | null): FormState {
  return {
    title: entry?.title ?? '',
    subjectType: entry?.subject_type ?? 'stock',
    subject: entry?.subject ?? '',
    thesis: entry?.thesis ?? '',
    evidence: entry?.evidence.join('\n') ?? '',
    counterEvidence: entry?.counter_evidence.join('\n') ?? '',
    invalidation: entry?.invalidation ?? '',
    plan: entry?.plan ?? '',
    status: entry?.status ?? 'draft',
    tags: entry?.tags.join('，') ?? '',
  }
}

function lines(value: string): string[] {
  return value.split('\n').map(item => item.trim()).filter(Boolean)
}

function tags(value: string): string[] {
  return value.split(/[,，\n]/).map(item => item.trim()).filter(Boolean)
}

export function ResearchEditorDialog({ entry, onClose, onSaved }: Props) {
  const queryClient = useQueryClient()
  const titleInputRef = useRef<HTMLInputElement>(null)
  const original = useMemo(() => initialState(entry), [entry])
  const [form, setForm] = useState<FormState>(original)
  const [titleTouched, setTitleTouched] = useState(false)
  const [confirmDiscard, setConfirmDiscard] = useState(false)

  const dirty = JSON.stringify(form) !== JSON.stringify(original)
  const titleInvalid = titleTouched && !form.title.trim()

  const save = useMutation({
    mutationFn: async (payload: ResearchEntryInput) => (
      entry
        ? researchLedgerApi.update(entry.id, payload)
        : researchLedgerApi.create(payload)
    ),
    onSuccess: async ({ entry: saved }) => {
      await queryClient.invalidateQueries({ queryKey: RESEARCH_LEDGER_QUERY_KEY })
      toast(entry ? '研究记录已更新' : '研究记录已创建', 'success')
      onSaved(saved)
    },
  })

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm(current => ({ ...current, [key]: value }))
  }

  const requestClose = () => {
    if (save.isPending) return
    if (dirty) {
      setConfirmDiscard(true)
      return
    }
    onClose()
  }

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    setTitleTouched(true)
    if (!form.title.trim()) return
    save.mutate({
      title: form.title.trim(),
      subject_type: form.subjectType,
      subject: form.subject.trim(),
      thesis: form.thesis.trim(),
      evidence: lines(form.evidence),
      counter_evidence: lines(form.counterEvidence),
      invalidation: form.invalidation.trim(),
      plan: form.plan.trim(),
      status: form.status,
      tags: tags(form.tags),
    })
  }

  return (
    <>
      <Modal
        onClose={requestClose}
        labelledBy="research-editor-title"
        initialFocusRef={titleInputRef}
        closeOnBackdrop={false}
        overlayClassName="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-3 backdrop-blur-sm sm:p-6"
        panelClassName="flex max-h-[calc(100dvh-1.5rem)] w-full max-w-4xl flex-col overflow-hidden rounded-dialog border border-border bg-surface shadow-2xl sm:max-h-[calc(100dvh-3rem)]"
      >
        <div className="flex shrink-0 items-start justify-between gap-4 border-b border-border px-4 py-3.5 sm:px-6">
          <div>
            <div className="flex items-center gap-2 text-xs font-medium text-g-research">
              {entry ? <Save className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
              SYCEE RESEARCH LEDGER
            </div>
            <h2 id="research-editor-title" className="mt-1 text-lg font-semibold text-foreground">
              {entry ? '编辑研究记录' : '建立研究记录'}
            </h2>
          </div>
          <button
            type="button"
            onClick={requestClose}
            disabled={save.isPending}
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-btn text-muted transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50 sm:h-9 sm:w-9"
            aria-label="关闭编辑器"
            title="关闭"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <form onSubmit={submit} className="flex min-h-0 flex-1 flex-col">
          <div className="min-h-0 flex-1 space-y-6 overflow-y-auto px-4 py-5 sm:px-6">
            <fieldset className="space-y-4">
              <legend className="text-xs font-semibold text-secondary">研究对象</legend>
              <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_160px]">
                <label className="block">
                  <span className="mb-1.5 block text-xs font-medium text-secondary">标题 <span className="text-danger">*</span></span>
                  <input
                    ref={titleInputRef}
                    value={form.title}
                    onChange={event => set('title', event.target.value)}
                    onBlur={() => setTitleTouched(true)}
                    maxLength={120}
                    aria-invalid={titleInvalid}
                    aria-describedby={titleInvalid ? 'research-title-error' : undefined}
                    className={cn(fieldClass, titleInvalid && 'border-danger focus:border-danger focus:ring-danger/20')}
                    placeholder="例如：贵州茅台渠道库存改善验证"
                  />
                  {titleInvalid && <span id="research-title-error" role="alert" className="mt-1 block text-xs text-danger">请输入研究标题</span>}
                </label>
                <label className="block">
                  <span className="mb-1.5 block text-xs font-medium text-secondary">状态</span>
                  <select value={form.status} onChange={event => set('status', event.target.value as ResearchStatus)} className={fieldClass}>
                    {STATUS_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
                  </select>
                </label>
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <label className="block">
                  <span className="mb-1.5 block text-xs font-medium text-secondary">对象类型</span>
                  <select value={form.subjectType} onChange={event => set('subjectType', event.target.value as ResearchSubjectType)} className={fieldClass}>
                    {SUBJECT_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
                  </select>
                </label>
                <label className="block">
                  <span className="mb-1.5 block text-xs font-medium text-secondary">对象</span>
                  <input value={form.subject} onChange={event => set('subject', event.target.value)} maxLength={100} className={fieldClass} placeholder="股票代码、策略名或板块名" />
                </label>
              </div>
              <label className="block">
                <span className="mb-1.5 block text-xs font-medium text-secondary">标签</span>
                <input value={form.tags} onChange={event => set('tags', event.target.value)} className={fieldClass} placeholder="用逗号分隔，最多 8 个" />
              </label>
            </fieldset>

            <fieldset className="space-y-4 border-t border-border pt-5">
              <legend className="px-2 text-xs font-semibold text-secondary">研究假设</legend>
              <label className="block">
                <span className="mb-1.5 block text-xs font-medium text-secondary">核心判断</span>
                <textarea value={form.thesis} onChange={event => set('thesis', event.target.value)} maxLength={5000} rows={4} className={textareaClass} placeholder="写下你认为会发生什么，以及判断成立的原因。" />
              </label>
              <div className="grid gap-4 md:grid-cols-2">
                <label className="block">
                  <span className="mb-1.5 block text-xs font-medium text-secondary">支持证据</span>
                  <textarea value={form.evidence} onChange={event => set('evidence', event.target.value)} rows={5} className={textareaClass} placeholder={'每行一项\n例如：批价连续三周企稳'} />
                </label>
                <label className="block">
                  <span className="mb-1.5 block text-xs font-medium text-secondary">反方证据</span>
                  <textarea value={form.counterEvidence} onChange={event => set('counterEvidence', event.target.value)} rows={5} className={textareaClass} placeholder={'每行一项\n主动记录可能推翻判断的信息'} />
                </label>
              </div>
            </fieldset>

            <fieldset className="space-y-4 border-t border-border pt-5">
              <legend className="px-2 text-xs font-semibold text-secondary">决策边界</legend>
              <div className="grid gap-4 md:grid-cols-2">
                <label className="block">
                  <span className="mb-1.5 block text-xs font-medium text-secondary">失效条件</span>
                  <textarea value={form.invalidation} onChange={event => set('invalidation', event.target.value)} maxLength={3000} rows={4} className={textareaClass} placeholder="出现什么情况时，应承认原判断不再成立？" />
                </label>
                <label className="block">
                  <span className="mb-1.5 block text-xs font-medium text-secondary">下一步</span>
                  <textarea value={form.plan} onChange={event => set('plan', event.target.value)} maxLength={3000} rows={4} className={textareaClass} placeholder="下一项验证、监控或复核动作。" />
                </label>
              </div>
            </fieldset>
          </div>

          <div className="flex shrink-0 flex-col-reverse gap-2 border-t border-border bg-base/70 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-6">
            <span className="text-xs text-muted">支持证据、反方证据均按每行一项保存。</span>
            <div className="flex gap-2">
              <button type="button" onClick={requestClose} disabled={save.isPending} className="min-h-11 rounded-btn border border-border px-4 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50">
                取消
              </button>
              <button type="submit" disabled={save.isPending} className="inline-flex min-h-11 items-center justify-center gap-2 rounded-btn bg-accent px-5 text-sm font-medium text-white transition-colors hover:bg-accent/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-not-allowed disabled:opacity-50">
                {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                {save.isPending ? '保存中' : '保存记录'}
              </button>
            </div>
          </div>
        </form>
      </Modal>

      <ConfirmDialog
        open={confirmDiscard}
        title="放弃未保存的修改？"
        message="当前编辑内容尚未保存，关闭后将无法恢复。"
        confirmText="放弃修改"
        danger
        onCancel={() => setConfirmDiscard(false)}
        onConfirm={onClose}
      />
    </>
  )
}
