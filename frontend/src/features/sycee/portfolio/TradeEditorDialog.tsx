import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowDownLeft,
  ArrowUpRight,
  Check,
  Loader2,
  Search,
  X,
} from 'lucide-react'

import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import {
  PORTFOLIO_QUERY_KEY,
  portfolioApi,
  type InstrumentSearchResult,
  type Portfolio,
  type PortfolioPosition,
  type PortfolioTrade,
  type PortfolioTradeInput,
  type PortfolioTradeSide,
} from './api'

interface InitialTrade {
  symbol?: string
  name?: string
  side?: PortfolioTradeSide
}

interface Props {
  trade: PortfolioTrade | null
  initial?: InitialTrade
  positions: PortfolioPosition[]
  onClose: () => void
  onSaved: (portfolio: Portfolio) => void
}

interface FormState {
  symbol: string
  name: string
  side: PortfolioTradeSide
  quantity: string
  price: string
  fees: string
  tradeDate: string
  note: string
}

interface FormErrors {
  symbol?: string
  quantity?: string
  price?: string
  tradeDate?: string
}

const fieldClass = 'min-h-11 w-full rounded-input border border-border bg-base px-3 text-sm text-foreground outline-none transition-colors placeholder:text-muted/70 focus:border-accent focus:ring-2 focus:ring-accent/20 disabled:opacity-50'

function localDate(): string {
  return new Intl.DateTimeFormat('en-CA', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date())
}

function initialState(trade: PortfolioTrade | null, initial?: InitialTrade): FormState {
  return {
    symbol: trade?.symbol ?? initial?.symbol ?? '',
    name: trade?.name ?? initial?.name ?? '',
    side: trade?.side ?? initial?.side ?? 'buy',
    quantity: trade ? String(trade.quantity) : '',
    price: trade ? String(trade.price) : '',
    fees: trade ? String(trade.fees) : '0',
    tradeDate: trade?.trade_date ?? localDate(),
    note: trade?.note ?? '',
  }
}

export function TradeEditorDialog({ trade, initial, positions, onClose, onSaved }: Props) {
  const queryClient = useQueryClient()
  const symbolInputRef = useRef<HTMLInputElement>(null)
  const quantityInputRef = useRef<HTMLInputElement>(null)
  const searchContainerRef = useRef<HTMLDivElement>(null)
  const original = useMemo(() => initialState(trade, initial), [initial, trade])
  const [form, setForm] = useState<FormState>(original)
  const [errors, setErrors] = useState<FormErrors>({})
  const [searchOpen, setSearchOpen] = useState(false)
  const [activeResult, setActiveResult] = useState(-1)

  const search = useQuery({
    queryKey: ['sycee', 'portfolio-instrument-search', form.symbol.trim()],
    queryFn: () => portfolioApi.searchInstruments(form.symbol.trim()),
    enabled: searchOpen && form.symbol.trim().length > 0,
    staleTime: 30_000,
  })
  const results = search.data?.results ?? []
  const held = positions.find(position => position.symbol === form.symbol.trim().toUpperCase())

  const save = useMutation({
    mutationFn: (payload: PortfolioTradeInput) => (
      trade
        ? portfolioApi.updateTrade(trade.id, payload)
        : portfolioApi.createTrade(payload)
    ),
    onSuccess: async ({ portfolio }) => {
      queryClient.setQueryData(PORTFOLIO_QUERY_KEY, portfolio)
      await queryClient.invalidateQueries({ queryKey: ['sycee', 'portfolio-quotes'] })
      toast(trade ? '交易记录已更新' : '交易记录已保存', 'success')
      onSaved(portfolio)
    },
  })

  useEffect(() => {
    const closeSearch = (event: MouseEvent) => {
      if (!searchContainerRef.current?.contains(event.target as Node)) setSearchOpen(false)
    }
    document.addEventListener('mousedown', closeSearch)
    return () => document.removeEventListener('mousedown', closeSearch)
  }, [])

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm(current => ({ ...current, [key]: value }))
    if (key === 'symbol' || key === 'quantity' || key === 'price' || key === 'tradeDate') {
      setErrors(current => ({ ...current, [key]: undefined }))
    }
  }

  const selectInstrument = (result: InstrumentSearchResult) => {
    setForm(current => ({ ...current, symbol: result.symbol, name: result.name }))
    setSearchOpen(false)
    setActiveResult(-1)
  }

  const handleSearchKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Escape') {
      setSearchOpen(false)
      return
    }
    if (!searchOpen || results.length === 0) return
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActiveResult(current => Math.min(current + 1, results.length - 1))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActiveResult(current => Math.max(current - 1, 0))
    } else if (event.key === 'Enter' && activeResult >= 0) {
      event.preventDefault()
      selectInstrument(results[activeResult])
    }
  }

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    const symbol = form.symbol.trim().toUpperCase()
    const quantity = Number(form.quantity)
    const price = Number(form.price)
    const fees = Number(form.fees || 0)
    const nextErrors: FormErrors = {}
    if (!/^[0-9A-Z._-]{2,32}$/.test(symbol)) nextErrors.symbol = '请选择或输入有效标的代码'
    if (!Number.isFinite(quantity) || quantity <= 0) nextErrors.quantity = '数量必须大于 0'
    if (!Number.isFinite(price) || price <= 0) nextErrors.price = '成交价必须大于 0'
    if (!form.tradeDate) nextErrors.tradeDate = '请选择交易日期'
    if (Object.keys(nextErrors).length > 0) {
      setErrors(nextErrors)
      return
    }
    save.mutate({
      symbol,
      name: form.name.trim(),
      side: form.side,
      quantity,
      price,
      fees: Number.isFinite(fees) && fees >= 0 ? fees : 0,
      trade_date: form.tradeDate,
      note: form.note.trim(),
    })
  }

  const requestClose = () => {
    if (!save.isPending) onClose()
  }

  return (
    <Modal
      onClose={requestClose}
      labelledBy="portfolio-trade-editor-title"
      initialFocusRef={original.symbol ? quantityInputRef : symbolInputRef}
      closeOnBackdrop={false}
      overlayClassName="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-3 backdrop-blur-sm sm:p-6"
      panelClassName="flex max-h-[calc(100dvh-1.5rem)] w-full max-w-2xl flex-col overflow-hidden rounded-dialog border border-border bg-surface shadow-2xl sm:max-h-[calc(100dvh-3rem)]"
    >
      <div className="flex shrink-0 items-start justify-between gap-4 border-b border-border px-4 py-3.5 sm:px-6">
        <div>
          <div className="flex items-center gap-2 text-xs font-medium text-g-core">
            {form.side === 'buy' ? <ArrowDownLeft className="h-3.5 w-3.5" /> : <ArrowUpRight className="h-3.5 w-3.5" />}
            SYCEE POSITION BOOK
          </div>
          <h2 id="portfolio-trade-editor-title" className="mt-1 text-lg font-semibold text-foreground">
            {trade ? '编辑交易记录' : '记录一笔交易'}
          </h2>
        </div>
        <button
          type="button"
          onClick={requestClose}
          disabled={save.isPending}
          className="flex h-11 w-11 shrink-0 items-center justify-center rounded-btn text-muted transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50 sm:h-9 sm:w-9"
          aria-label="关闭交易编辑器"
          title="关闭"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <form onSubmit={submit} className="flex min-h-0 flex-1 flex-col">
        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-4 py-5 sm:px-6">
          <fieldset>
            <legend className="sr-only">交易方向</legend>
            <div className="grid grid-cols-2 rounded-btn border border-border bg-base p-1" role="group" aria-label="交易方向">
              {([
                { value: 'buy' as const, label: '买入', icon: ArrowDownLeft },
                { value: 'sell' as const, label: '卖出', icon: ArrowUpRight },
              ]).map(option => {
                const Icon = option.icon
                const active = form.side === option.value
                return (
                  <button
                    key={option.value}
                    type="button"
                    aria-pressed={active}
                    onClick={() => set('side', option.value)}
                    className={cn(
                      'flex min-h-10 items-center justify-center gap-2 rounded text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                      active
                        ? option.value === 'buy' ? 'bg-bull/10 text-bull' : 'bg-bear/10 text-bear'
                        : 'text-muted hover:bg-elevated hover:text-secondary',
                    )}
                  >
                    <Icon className="h-4 w-4" />
                    {option.label}
                  </button>
                )
              })}
            </div>
          </fieldset>

          <fieldset className="space-y-4">
            <legend className="text-xs font-semibold text-secondary">标的信息</legend>
            <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
              <div ref={searchContainerRef} className="relative">
                <label htmlFor="portfolio-trade-symbol" className="mb-1.5 block text-xs font-medium text-secondary">
                  股票代码 <span className="text-danger">*</span>
                </label>
                <div className="relative">
                  <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
                  <input
                    ref={symbolInputRef}
                    id="portfolio-trade-symbol"
                    value={form.symbol}
                    onFocus={() => setSearchOpen(true)}
                    onChange={event => {
                      set('symbol', event.target.value)
                      setSearchOpen(true)
                      setActiveResult(-1)
                    }}
                    onKeyDown={handleSearchKeyDown}
                    maxLength={32}
                    autoComplete="off"
                    aria-invalid={!!errors.symbol}
                    aria-expanded={searchOpen}
                    aria-controls="portfolio-instrument-results"
                    className={cn(fieldClass, 'pl-9 font-mono', errors.symbol && 'border-danger')}
                    placeholder="代码或名称"
                  />
                </div>
                {errors.symbol && <span className="mt-1 block text-xs text-danger">{errors.symbol}</span>}
                {searchOpen && form.symbol.trim() && (
                  <div id="portfolio-instrument-results" role="listbox" className="absolute z-20 mt-1 max-h-56 w-full overflow-y-auto rounded-btn border border-border bg-surface p-1 shadow-xl">
                    {search.isLoading ? (
                      <div className="flex items-center gap-2 px-3 py-3 text-xs text-muted"><Loader2 className="h-3.5 w-3.5 animate-spin" />搜索中</div>
                    ) : results.length === 0 ? (
                      <div className="px-3 py-3 text-xs text-muted">未找到匹配标的，可直接输入完整代码。</div>
                    ) : results.map((result, index) => (
                      <button
                        key={result.symbol}
                        type="button"
                        role="option"
                        aria-selected={index === activeResult}
                        onMouseDown={event => event.preventDefault()}
                        onClick={() => selectInstrument(result)}
                        className={cn(
                          'flex min-h-10 w-full items-center justify-between gap-3 rounded px-3 text-left text-sm transition-colors',
                          index === activeResult ? 'bg-accent/10 text-foreground' : 'text-secondary hover:bg-elevated',
                        )}
                      >
                        <span className="truncate">{result.name}</span>
                        <span className="shrink-0 font-mono text-xs text-muted">{result.symbol}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <label className="block">
                <span className="mb-1.5 block text-xs font-medium text-secondary">股票名称</span>
                <input value={form.name} onChange={event => set('name', event.target.value)} maxLength={80} className={fieldClass} placeholder="选择标的后自动填写" />
              </label>
            </div>
            {form.side === 'sell' && held && (
              <p className="text-xs text-muted">
                当前持仓 <span className="font-mono text-secondary">{held.quantity.toLocaleString('zh-CN')}</span> 股，移动均价 <span className="font-mono text-secondary">{held.average_cost.toFixed(3)}</span>
              </p>
            )}
          </fieldset>

          <fieldset className="border-t border-border pt-5">
            <legend className="px-2 text-xs font-semibold text-secondary">成交信息</legend>
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <label className="block">
                <span className="mb-1.5 block text-xs font-medium text-secondary">数量（股）<span className="text-danger"> *</span></span>
                <input ref={quantityInputRef} type="number" min="0" step="any" inputMode="decimal" value={form.quantity} onChange={event => set('quantity', event.target.value)} className={cn(fieldClass, 'font-mono', errors.quantity && 'border-danger')} placeholder="100" />
                {errors.quantity && <span className="mt-1 block text-xs text-danger">{errors.quantity}</span>}
              </label>
              <label className="block">
                <span className="mb-1.5 block text-xs font-medium text-secondary">成交价 <span className="text-danger">*</span></span>
                <input type="number" min="0" step="any" inputMode="decimal" value={form.price} onChange={event => set('price', event.target.value)} className={cn(fieldClass, 'font-mono', errors.price && 'border-danger')} placeholder="0.00" />
                {errors.price && <span className="mt-1 block text-xs text-danger">{errors.price}</span>}
              </label>
              <label className="block">
                <span className="mb-1.5 block text-xs font-medium text-secondary">费用</span>
                <input type="number" min="0" step="any" inputMode="decimal" value={form.fees} onChange={event => set('fees', event.target.value)} className={cn(fieldClass, 'font-mono')} placeholder="0.00" />
              </label>
              <label className="block">
                <span className="mb-1.5 block text-xs font-medium text-secondary">交易日期 <span className="text-danger">*</span></span>
                <input type="date" value={form.tradeDate} onChange={event => set('tradeDate', event.target.value)} className={cn(fieldClass, 'font-mono', errors.tradeDate && 'border-danger')} />
                {errors.tradeDate && <span className="mt-1 block text-xs text-danger">{errors.tradeDate}</span>}
              </label>
            </div>
          </fieldset>

          <label className="block border-t border-border pt-5">
            <span className="mb-1.5 block text-xs font-medium text-secondary">备注</span>
            <textarea value={form.note} onChange={event => set('note', event.target.value)} maxLength={500} rows={3} className={cn(fieldClass, 'resize-y py-2.5 leading-6')} placeholder="记录交易原因、计划或执行偏差。" />
          </label>

          {save.isError && (
            <div role="alert" className="rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-xs leading-5 text-danger">
              {save.error instanceof Error ? save.error.message : '交易记录保存失败'}
            </div>
          )}
        </div>

        <div className="flex shrink-0 items-center justify-end gap-2 border-t border-border bg-base/70 px-4 py-3 sm:px-6">
          <button type="button" onClick={requestClose} disabled={save.isPending} className="min-h-11 rounded-btn border border-border px-4 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50">
            取消
          </button>
          <button type="submit" disabled={save.isPending} className="inline-flex min-h-11 items-center justify-center gap-2 rounded-btn bg-accent px-5 text-sm font-medium text-white transition-colors hover:bg-accent/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-not-allowed disabled:opacity-50">
            {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
            {save.isPending ? '保存中' : '保存交易'}
          </button>
        </div>
      </form>
    </Modal>
  )
}
