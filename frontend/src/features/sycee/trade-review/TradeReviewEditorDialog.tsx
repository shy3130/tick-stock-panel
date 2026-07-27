import { useMemo, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { BookOpenCheck, Loader2, Save, Trash2, X } from 'lucide-react'

import { ConfirmDialog } from '@/components/ConfirmDialog'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import {
  TRADE_REVIEWS_QUERY_KEY,
  tradeReviewApi,
  type MistakeTag,
  type ReviewStrategy,
  type TradeReviewInput,
  type TradeReviewItem,
} from './api'
import { MISTAKE_TAG_OPTIONS } from './reviewView'

interface Props {
  item: TradeReviewItem & { trade: NonNullable<TradeReviewItem['trade']> }
  strategies: ReviewStrategy[]
  onClose: () => void
  onSaved: () => void
}

interface FormState {
  strategyId: string
  entryReason: string
  expectation: string
  invalidation: string
  exitReason: string
  conclusion: string
  mistakeTags: MistakeTag[]
}

const fieldClass = 'min-h-11 w-full rounded-input border border-border bg-base px-3 text-sm text-foreground outline-none transition-colors placeholder:text-muted/70 focus:border-accent focus:ring-2 focus:ring-accent/20 disabled:opacity-50'
const textareaClass = cn(fieldClass, 'resize-y py-2.5 leading-6')

function initialState(item: TradeReviewItem): FormState {
  return {
    strategyId: item.review?.strategy_id ?? '',
    entryReason: item.review?.entry_reason ?? '',
    expectation: item.review?.expectation ?? '',
    invalidation: item.review?.invalidation ?? '',
    exitReason: item.review?.exit_reason ?? '',
    conclusion: item.review?.conclusion ?? '',
    mistakeTags: item.review?.mistake_tags ?? [],
  }
}

function signedMoney(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return '--'
  const prefix = value > 0 ? '+¥' : value < 0 ? '-¥' : '¥'
  return `${prefix}${Math.abs(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export function TradeReviewEditorDialog({ item, strategies, onClose, onSaved }: Props) {
  const queryClient = useQueryClient()
  const firstFieldRef = useRef<HTMLTextAreaElement>(null)
  const original = useMemo(() => initialState(item), [item])
  const [form, setForm] = useState<FormState>(original)
  const [formError, setFormError] = useState('')
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const trade = item.trade
  const dirty = JSON.stringify(form) !== JSON.stringify(original)

  const save = useMutation({
    mutationFn: (payload: TradeReviewInput) => tradeReviewApi.save(trade.id, payload),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: TRADE_REVIEWS_QUERY_KEY })
      toast(item.review ? '交易复盘已更新' : '交易复盘已保存', 'success')
      onSaved()
    },
  })
  const remove = useMutation({
    mutationFn: () => tradeReviewApi.delete(trade.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: TRADE_REVIEWS_QUERY_KEY })
      toast('交易复盘已删除', 'success')
      onSaved()
    },
    onError: error => {
      toast(error instanceof Error ? error.message : '交易复盘删除失败', 'error')
    },
  })

  const pending = save.isPending || remove.isPending
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm(current => ({ ...current, [key]: value }))
    setFormError('')
  }
  const toggleTag = (tag: MistakeTag) => {
    set('mistakeTags', form.mistakeTags.includes(tag)
      ? form.mistakeTags.filter(item => item !== tag)
      : [...form.mistakeTags, tag])
  }
  const requestClose = () => {
    if (pending) return
    if (dirty) {
      setConfirmDiscard(true)
      return
    }
    onClose()
  }
  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    const payload: TradeReviewInput = {
      strategy_id: form.strategyId,
      entry_reason: form.entryReason.trim(),
      expectation: form.expectation.trim(),
      invalidation: form.invalidation.trim(),
      exit_reason: form.exitReason.trim(),
      conclusion: form.conclusion.trim(),
      mistake_tags: form.mistakeTags,
    }
    if (!payload.strategy_id && !payload.entry_reason && !payload.expectation
      && !payload.invalidation && !payload.exit_reason && !payload.conclusion
      && payload.mistake_tags.length === 0) {
      setFormError('至少填写一项复盘内容')
      return
    }
    save.mutate(payload)
  }

  const unavailableStrategy = form.strategyId
    && !strategies.some(strategy => strategy.id === form.strategyId)
  const pnl = item.attribution?.realized_pnl ?? null

  return (
    <>
      <Modal
        onClose={requestClose}
        labelledBy="trade-review-editor-title"
        initialFocusRef={firstFieldRef}
        closeOnBackdrop={false}
        overlayClassName="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-3 backdrop-blur-sm sm:p-6"
        panelClassName="flex max-h-[calc(100dvh-1.5rem)] w-full max-w-4xl flex-col overflow-hidden rounded-dialog border border-border bg-surface shadow-2xl sm:max-h-[calc(100dvh-3rem)]"
      >
        <div className="flex shrink-0 items-start justify-between gap-4 border-b border-border px-4 py-3.5 sm:px-6">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-xs font-medium text-g-research">
              <BookOpenCheck className="h-3.5 w-3.5" />SYCEE TRADE REVIEW
            </div>
            <h2 id="trade-review-editor-title" className="mt-1 truncate text-lg font-semibold text-foreground">
              {trade.name || trade.symbol} · {trade.side === 'buy' ? '交易计划' : '卖出复盘'}
            </h2>
          </div>
          <button
            type="button"
            onClick={requestClose}
            disabled={pending}
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-btn text-muted hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50 sm:h-9 sm:w-9"
            aria-label="关闭交易复盘"
            title="关闭"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <form onSubmit={submit} className="flex min-h-0 flex-1 flex-col">
          <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-4 py-5 sm:px-6">
            <div className="grid grid-cols-2 gap-px overflow-hidden rounded-input border border-border bg-border sm:grid-cols-4">
              <div className="bg-base px-3 py-2.5"><div className="text-[10px] text-muted">交易</div><div className={cn('mt-1 text-xs font-medium', trade.side === 'buy' ? 'text-bull' : 'text-bear')}>{trade.trade_date} · {trade.side === 'buy' ? '买入' : '卖出'}</div></div>
              <div className="bg-base px-3 py-2.5"><div className="text-[10px] text-muted">成交</div><div className="mt-1 font-mono text-xs text-secondary">{trade.quantity.toLocaleString('zh-CN')} × {trade.price.toLocaleString('zh-CN')}</div></div>
              <div className="bg-base px-3 py-2.5"><div className="text-[10px] text-muted">实际盈亏</div><div className={cn('mt-1 font-mono text-xs font-semibold', pnl == null || pnl === 0 ? 'text-muted' : pnl > 0 ? 'text-bull' : 'text-bear')}>{signedMoney(pnl)}</div></div>
              <div className="bg-base px-3 py-2.5"><div className="text-[10px] text-muted">持有周期</div><div className="mt-1 font-mono text-xs text-secondary">{item.attribution?.holding_days == null ? '--' : `${item.attribution.holding_days} 天`}</div></div>
            </div>

            <label className="block">
              <span className="mb-1.5 block text-xs font-medium text-secondary">关联策略</span>
              <select value={form.strategyId} onChange={event => set('strategyId', event.target.value)} className={fieldClass}>
                <option value="">未关联策略</option>
                {unavailableStrategy && <option value={form.strategyId}>{form.strategyId}（当前不可用）</option>}
                {strategies.map(strategy => <option key={strategy.id} value={strategy.id}>{strategy.name}</option>)}
              </select>
            </label>

            <div className="grid gap-4 md:grid-cols-2">
              <label className="block">
                <span className="mb-1.5 block text-xs font-medium text-secondary">入场逻辑</span>
                <textarea ref={firstFieldRef} value={form.entryReason} onChange={event => set('entryReason', event.target.value)} maxLength={3000} rows={4} className={textareaClass} placeholder="当时为什么建立或继续这笔仓位？" />
              </label>
              <label className="block">
                <span className="mb-1.5 block text-xs font-medium text-secondary">预期路径</span>
                <textarea value={form.expectation} onChange={event => set('expectation', event.target.value)} maxLength={3000} rows={4} className={textareaClass} placeholder="预期价格、时间或事件如何发展？" />
              </label>
              <label className="block">
                <span className="mb-1.5 block text-xs font-medium text-secondary">失效条件</span>
                <textarea value={form.invalidation} onChange={event => set('invalidation', event.target.value)} maxLength={3000} rows={4} className={textareaClass} placeholder="什么情况出现后，原判断不再成立？" />
              </label>
              <label className="block">
                <span className="mb-1.5 block text-xs font-medium text-secondary">退出依据</span>
                <textarea value={form.exitReason} onChange={event => set('exitReason', event.target.value)} maxLength={3000} rows={4} className={textareaClass} placeholder="实际退出由信号、止损、止盈还是判断变化触发？" />
              </label>
            </div>

            <label className="block">
              <span className="mb-1.5 block text-xs font-medium text-secondary">复盘结论</span>
              <textarea value={form.conclusion} onChange={event => set('conclusion', event.target.value)} maxLength={5000} rows={4} className={textareaClass} placeholder="结果来自判断、纪律、仓位还是执行？下一次保留或改变什么？" />
            </label>

            <fieldset>
              <legend className="mb-2 text-xs font-medium text-secondary">错误归因</legend>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                {MISTAKE_TAG_OPTIONS.map(option => (
                  <label key={option.value} className={cn(
                    'flex min-h-10 cursor-pointer items-center gap-2 rounded-input border px-3 text-xs transition-colors',
                    form.mistakeTags.includes(option.value)
                      ? 'border-danger/35 bg-danger/10 text-danger'
                      : 'border-border bg-base text-secondary hover:bg-elevated',
                  )}>
                    <input
                      type="checkbox"
                      checked={form.mistakeTags.includes(option.value)}
                      onChange={() => toggleTag(option.value)}
                      className="h-4 w-4 rounded border-border accent-[var(--danger)]"
                    />
                    {option.label}
                  </label>
                ))}
              </div>
            </fieldset>
          </div>

          <div className="flex shrink-0 flex-col gap-3 border-t border-border bg-base/70 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-6">
            <div>
              {item.review && (
                <button type="button" onClick={() => setConfirmDelete(true)} disabled={pending} className="inline-flex min-h-10 items-center gap-2 rounded-btn px-3 text-xs text-danger hover:bg-danger/10 disabled:opacity-50">
                  <Trash2 className="h-3.5 w-3.5" />删除复盘
                </button>
              )}
              {(formError || save.isError) && <span role="alert" className="block text-xs text-danger">{formError || (save.error instanceof Error ? save.error.message : '保存失败')}</span>}
            </div>
            <div className="flex justify-end gap-2">
              <button type="button" onClick={requestClose} disabled={pending} className="min-h-11 rounded-btn border border-border px-4 text-sm text-secondary hover:bg-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50">取消</button>
              <button type="submit" disabled={pending} className="inline-flex min-h-11 items-center gap-2 rounded-btn bg-accent px-5 text-sm font-medium text-white hover:bg-accent/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50">
                {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                {save.isPending ? '保存中' : '保存复盘'}
              </button>
            </div>
          </div>
        </form>
      </Modal>

      <ConfirmDialog
        open={confirmDiscard}
        title="放弃未保存的修改？"
        message="当前交易复盘尚未保存，关闭后将无法恢复。"
        confirmText="放弃修改"
        danger
        onCancel={() => setConfirmDiscard(false)}
        onConfirm={onClose}
      />
      <ConfirmDialog
        open={confirmDelete}
        title="删除这条交易复盘？"
        message="只删除复盘文字和标签，不会影响交易流水、持仓成本或盈亏。"
        confirmText="删除复盘"
        danger
        pending={remove.isPending}
        onCancel={() => setConfirmDelete(false)}
        onConfirm={() => remove.mutate()}
      />
    </>
  )
}
