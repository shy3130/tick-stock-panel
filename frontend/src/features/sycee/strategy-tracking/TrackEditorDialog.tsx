import { useMemo, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, Loader2, X } from 'lucide-react'

import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import type { ComparableStrategy } from '../strategy-compare/api'
import { buildStrategyDefaults } from '../strategy-compare/comparison'
import {
  STRATEGY_TRACKS_QUERY_KEY,
  strategyTrackingApi,
  type StrategyTrackInput,
} from './api'
import { normalizeSymbolDraft } from './tracking'

interface Props {
  strategies: ComparableStrategy[]
  watchlistSymbols: string[]
  onClose: () => void
  onCreated: () => void
}

interface FormState {
  strategyId: string
  symbols: string
  startDate: string
  initialCapital: string
  maxPositions: string
  commission: string
  stampTax: string
  slippage: string
  note: string
}

const fieldClass = 'min-h-11 w-full rounded-input border border-border bg-base px-3 text-sm text-foreground outline-none transition-colors placeholder:text-muted/70 focus:border-accent focus:ring-2 focus:ring-accent/20 disabled:opacity-50'

function localDate(date: Date): string {
  return new Intl.DateTimeFormat('en-CA', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(date)
}

function defaultStartDate(): string {
  const start = new Date()
  start.setMonth(start.getMonth() - 6)
  return localDate(start)
}

export function TrackEditorDialog({
  strategies,
  watchlistSymbols,
  onClose,
  onCreated,
}: Props) {
  const queryClient = useQueryClient()
  const strategyRef = useRef<HTMLSelectElement>(null)
  const [error, setError] = useState('')
  const [form, setForm] = useState<FormState>(() => ({
    strategyId: strategies[0]?.id ?? '',
    symbols: watchlistSymbols.join(', '),
    startDate: defaultStartDate(),
    initialCapital: '1000000',
    maxPositions: '10',
    commission: '2',
    stampTax: '1',
    slippage: '5',
    note: '',
  }))
  const symbols = useMemo(() => normalizeSymbolDraft(form.symbols), [form.symbols])

  const create = useMutation({
    mutationFn: (input: StrategyTrackInput) => strategyTrackingApi.create(input),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: STRATEGY_TRACKS_QUERY_KEY })
      toast('策略跟踪计划已建立', 'success')
      onCreated()
    },
    onError: cause => setError(cause instanceof Error ? cause.message : '策略跟踪计划保存失败'),
  })

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm(current => ({ ...current, [key]: value }))
    setError('')
  }

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    const strategy = strategies.find(item => item.id === form.strategyId)
    const initialCapital = Number(form.initialCapital)
    const maxPositions = Number(form.maxPositions)
    const commission = Number(form.commission)
    const stampTax = Number(form.stampTax)
    const slippage = Number(form.slippage)
    if (!strategy) return setError('请选择有效策略')
    if (symbols.length === 0 || symbols.some(symbol => !/^[0-9A-Z._-]{2,32}$/.test(symbol))) {
      return setError('请输入有效股票代码')
    }
    if (symbols.length > 50) return setError('股票池最多允许 50 个标的')
    if (!form.startDate) return setError('请选择跟踪开始日期')
    if (!Number.isFinite(initialCapital) || initialCapital <= 0) return setError('初始资金必须大于 0')
    if (!Number.isInteger(maxPositions) || maxPositions <= 0 || maxPositions > 100) {
      return setError('最大持仓数必须是 1 到 100 的整数')
    }
    if (![commission, stampTax, slippage].every(value => Number.isFinite(value) && value >= 0)) {
      return setError('交易成本不能小于 0')
    }
    const defaults = buildStrategyDefaults(strategy)
    create.mutate({
      strategy_id: strategy.id,
      strategy_name: strategy.name,
      symbols,
      start_date: form.startDate,
      initial_capital: initialCapital,
      max_positions: maxPositions,
      commission_pct: commission / 10_000,
      stamp_tax_pct: stampTax / 1_000,
      slippage_bps: slippage,
      params: defaults.params,
      overrides: defaults.overrides,
      note: form.note.trim(),
    })
  }

  return (
    <Modal
      onClose={() => { if (!create.isPending) onClose() }}
      labelledBy="track-editor-title"
      initialFocusRef={strategyRef}
      panelClassName="flex max-h-[92vh] w-[94vw] max-w-2xl flex-col overflow-hidden rounded-card border border-border bg-surface shadow-2xl"
    >
      <div className="flex min-h-14 items-center justify-between border-b border-border px-4 sm:px-5">
        <div>
          <h2 id="track-editor-title" className="text-sm font-semibold text-foreground">新建策略跟踪</h2>
          <div className="mt-0.5 font-mono text-[10px] text-muted">STRATEGY TRACKING LEDGER</div>
        </div>
        <button type="button" title="关闭" aria-label="关闭" disabled={create.isPending} onClick={onClose} className="flex h-9 w-9 items-center justify-center rounded-btn text-muted hover:bg-elevated hover:text-foreground disabled:opacity-50">
          <X className="h-4 w-4" />
        </button>
      </div>

      <form onSubmit={submit} className="min-h-0 overflow-y-auto">
        <div className="grid gap-4 p-4 sm:grid-cols-2 sm:p-5">
          <label className="sm:col-span-2">
            <span className="mb-1.5 block text-xs font-medium text-secondary">策略</span>
            <select ref={strategyRef} value={form.strategyId} onChange={event => set('strategyId', event.target.value)} className={fieldClass}>
              {strategies.map(strategy => <option key={strategy.id} value={strategy.id}>{strategy.name}</option>)}
            </select>
          </label>

          <label className="sm:col-span-2">
            <span className="mb-1.5 flex items-center justify-between gap-3 text-xs font-medium text-secondary">
              <span>股票池</span>
              <span className="font-mono text-[10px] text-muted">{symbols.length} / 50</span>
            </span>
            <textarea value={form.symbols} onChange={event => set('symbols', event.target.value)} rows={3} placeholder="600519.SH, 000001.SZ" className={`${fieldClass} resize-none py-2.5 font-mono`} />
            {watchlistSymbols.length > 0 && (
              <button type="button" onClick={() => set('symbols', watchlistSymbols.join(', '))} className="mt-2 inline-flex min-h-8 items-center gap-1.5 text-xs text-accent hover:text-accent/80">
                <Check className="h-3.5 w-3.5" />使用全部自选股
              </button>
            )}
          </label>

          <label>
            <span className="mb-1.5 block text-xs font-medium text-secondary">起始日</span>
            <input type="date" max={localDate(new Date())} value={form.startDate} onChange={event => set('startDate', event.target.value)} className={`${fieldClass} font-mono`} />
          </label>
          <label>
            <span className="mb-1.5 block text-xs font-medium text-secondary">初始资金</span>
            <input type="number" min="1" step="10000" value={form.initialCapital} onChange={event => set('initialCapital', event.target.value)} className={`${fieldClass} font-mono`} />
          </label>
          <label>
            <span className="mb-1.5 block text-xs font-medium text-secondary">最大持仓</span>
            <input type="number" min="1" max="100" step="1" value={form.maxPositions} onChange={event => set('maxPositions', event.target.value)} className={`${fieldClass} font-mono`} />
          </label>
          <label>
            <span className="mb-1.5 block text-xs font-medium text-secondary">滑点 bps</span>
            <input type="number" min="0" step="1" value={form.slippage} onChange={event => set('slippage', event.target.value)} className={`${fieldClass} font-mono`} />
          </label>
          <label>
            <span className="mb-1.5 block text-xs font-medium text-secondary">佣金 万分之</span>
            <input type="number" min="0" step="0.1" value={form.commission} onChange={event => set('commission', event.target.value)} className={`${fieldClass} font-mono`} />
          </label>
          <label>
            <span className="mb-1.5 block text-xs font-medium text-secondary">印花税 千分之</span>
            <input type="number" min="0" step="0.1" value={form.stampTax} onChange={event => set('stampTax', event.target.value)} className={`${fieldClass} font-mono`} />
          </label>
          <label className="sm:col-span-2">
            <span className="mb-1.5 block text-xs font-medium text-secondary">跟踪备注</span>
            <textarea value={form.note} onChange={event => set('note', event.target.value)} rows={3} maxLength={3000} className={`${fieldClass} resize-none py-2.5`} />
          </label>
          {error && <div role="alert" className="sm:col-span-2 text-xs text-danger">{error}</div>}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-border bg-base/60 px-4 py-3 sm:px-5">
          <button type="button" onClick={onClose} disabled={create.isPending} className="min-h-10 rounded-btn border border-border px-4 text-sm text-secondary hover:bg-elevated disabled:opacity-50">取消</button>
          <button type="submit" disabled={create.isPending || strategies.length === 0} className="inline-flex min-h-10 items-center gap-2 rounded-btn bg-accent px-4 text-sm font-medium text-white hover:bg-accent/90 disabled:opacity-50">
            {create.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            建立跟踪
          </button>
        </div>
      </form>
    </Modal>
  )
}
