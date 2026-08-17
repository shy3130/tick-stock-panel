import { useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ArrowUpRight, CalendarClock, Pencil, Plus, Search, Trash2 } from 'lucide-react'
import { api, type Lot } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtPct, priceColorClass } from '@/lib/format'
import { PageHeader } from '@/components/PageHeader'
import { Modal } from '@/components/Modal'
import { DatePicker } from '@/components/DatePicker'
import { DateShortcuts } from '@/components/DateShortcuts'
import { StockPreviewDialog, toNavItems } from '@/components/StockPreviewDialog'
import { boardTag } from '@/components/stock-table/primitives'

const emptyDraft = (): Lot => ({
  id: '',
  symbol: '',
  qty: 0,
  cost_price: 0,
  buy_date: '',
  target_pct: 0,
  stop_pct: 0,
  remind_date: '',
  lead_days: 1,
})

/** 剩余天数单元格: 到期日 − 今天, 可为负 = 已超期; 无到期日 → — */
function RemainingDays({ remind }: { remind?: string | null }) {
  if (!remind) return <span className="text-muted/60">—</span>
  const remindMs = new Date(`${remind}T00:00:00`).getTime()
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const n = Math.floor((remindMs - today.getTime()) / 86400000)
  return <span className={cn('font-mono text-secondary', n < 0 && 'text-warning')}>{n}天</span>
}

/** 成本 vs 现价的盈亏% (纯价格比例, 无数量参与) */
function CostPnL({ close, cost }: { close?: number; cost: number }) {
  if (close == null || !(cost > 0)) return <span className="text-muted/60">—</span>
  const pnl = (close - cost) / cost
  return <span className={cn('font-mono', priceColorClass(pnl))}>{fmtPct(pnl)}</span>
}

export function Lots() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [editing, setEditing] = useState<Lot | null>(null) // null=关闭
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const lotsQuery = useQuery({ queryKey: QK.lots, queryFn: api.lotsList })
  const lots = lotsQuery.data?.lots ?? []

  const allSymbols = useMemo(() => Array.from(new Set(lots.map(l => l.symbol))), [lots])
  const namesQuery = useQuery({
    queryKey: ['instrument-names', allSymbols.join(',')],
    queryFn: () => api.instrumentNames(allSymbols),
    enabled: allSymbols.length > 0,
    staleTime: 300000,
  })
  const symbolNames = namesQuery.data?.names ?? {}

  // 9999 哨兵: 未记买入日期的排最后
  const sortedLots = useMemo(() => {
    return [...lots].sort((a, b) => (a.buy_date ?? '9999-12-31').localeCompare(b.buy_date ?? '9999-12-31'))
  }, [lots])
  const lotsNavItems = useMemo(
    () => toNavItems(sortedLots.map(l => ({ symbol: l.symbol, name: symbolNames[l.symbol] }))),
    [sortedLots, symbolNames],
  )

  const dailyQuery = useQuery({
    queryKey: QK.lotsKline(allSymbols.join(',')),
    queryFn: () => api.klineDailyBatch(allSymbols, 5),
    enabled: allSymbols.length > 0,
    staleTime: 60000,
  })
  const lastPrices = useMemo(() => {
    const m: Record<string, number> = {}
    for (const [sym, rows] of Object.entries(dailyQuery.data?.data ?? {})) {
      const last = rows[rows.length - 1]
      if (last?.close != null) m[sym] = Number(last.close)
    }
    return m
  }, [dailyQuery.data])

  const del = useMutation({
    mutationFn: api.lotDelete,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.lots })
      qc.invalidateQueries({ queryKey: QK.monitorRules })
      setConfirmId(null)
    },
  })

  // 删除: 第一次进确认态, 第二次真删, 3 秒后自动复位 (与监控中心一致)
  const handleClickDelete = (id: string) => {
    if (confirmId === id) {
      if (resetTimer.current) clearTimeout(resetTimer.current)
      setConfirmId(null)
      del.mutate(id)
    } else {
      setConfirmId(id)
      if (resetTimer.current) clearTimeout(resetTimer.current)
      resetTimer.current = setTimeout(() => setConfirmId(null), 3000)
    }
  }

  return (
    <div className="flex flex-col h-full">
      <PageHeader title="持仓" subtitle="记录买入批次, 自动生成止盈止损 / 到期监控规则" />
      <div className="flex-1 min-h-0 px-5 py-4">
        <div className="mx-auto max-w-5xl space-y-4">
          <div className="flex items-center justify-between">
            <div className="text-xs text-secondary">{lots.length} 个批次</div>
            <button
              onClick={() => setEditing(emptyDraft())}
              className="inline-flex h-9 items-center gap-1.5 rounded-btn border border-accent/30 bg-accent/10 px-3 text-xs font-medium text-accent transition-colors hover:bg-accent/15 cursor-pointer"
            >
              <Plus className="h-3.5 w-3.5" />新增批次
            </button>
          </div>

          {lots.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border px-6 py-12 text-center">
              <div className="text-sm text-muted">还没有批次</div>
              <div className="mt-1 text-[11px] text-muted/70">记录买入批次后, 系统自动按「成本价 ± %」生成止盈止损监控, 到期日自动生成日期提醒</div>
            </div>
          ) : (
            <div className="overflow-hidden rounded-xl border border-border bg-surface/40 shadow-sm">
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-border/60 bg-surface/60 text-[10px] uppercase tracking-wide text-muted">
                      <th className="px-4 py-2 font-medium">标的</th>
                      <th className="px-2 py-2 font-medium text-right">数量</th>
                      <th className="px-2 py-2 font-medium text-right">成本</th>
                      <th className="px-2 py-2 font-medium text-right">盈亏%</th>
                      <th className="px-2 py-2 font-medium text-right">止盈%</th>
                      <th className="px-2 py-2 font-medium text-right">止损%</th>
                      <th className="px-2 py-2 font-medium">买入日期</th>
                      <th className="px-2 py-2 font-medium text-right">剩余天数</th>
                      <th className="px-2 py-2 font-medium">到期提醒</th>
                      <th className="px-3 py-2" />
                    </tr>
                  </thead>
                  <tbody>
                    {sortedLots.map(lot => (
                      <tr key={lot.id} className="border-b border-border/40 last:border-0 hover:bg-elevated/40">
                        <td className="px-4 py-2.5">
                          <button
                            onClick={() => setPreviewSymbol(lot.symbol)}
                            title={`查看 ${lot.symbol} 日K`}
                            className="inline-flex items-center gap-1.5 min-w-0 hover:bg-elevated/50 rounded px-0.5 py-0.5 transition-colors cursor-pointer"
                          >
                            <span className="font-mono font-medium text-foreground">{lot.symbol}</span>
                            {(() => { const b = boardTag(lot.symbol); return b && <span className={`inline-flex items-center justify-center rounded px-1 text-[9px] font-bold leading-tight border ${b.color}`}>{b.label}</span> })()}
                            {symbolNames[lot.symbol] && <span className="text-secondary truncate max-w-28">{symbolNames[lot.symbol]}</span>}
                          </button>
                        </td>
                        <td className="px-2 py-2.5 text-right font-mono text-secondary">{lot.qty}</td>
                        <td className="px-2 py-2.5 text-right font-mono text-foreground">{lot.cost_price}</td>
                        <td className="px-2 py-2.5 text-right"><CostPnL close={lastPrices[lot.symbol]} cost={lot.cost_price} /></td>
                        <td className="px-2 py-2.5 text-right font-mono text-emerald-500">{lot.target_pct > 0 ? lot.target_pct : '—'}</td>
                        <td className="px-2 py-2.5 text-right font-mono text-danger">{lot.stop_pct > 0 ? lot.stop_pct : '—'}</td>
                        <td className="px-2 py-2.5 text-muted">{lot.buy_date || '—'}</td>
                        <td className="px-2 py-2.5 text-right"><RemainingDays remind={lot.remind_date} /></td>
                        <td className="px-2 py-2.5">
                          {lot.remind_date ? (
                            <span className="inline-flex items-center gap-1 text-rose-400">
                              <CalendarClock className="h-3 w-3" />
                              {lot.remind_date}
                              {lot.lead_days > 0 && <span className="text-muted">· 提前{lot.lead_days}天</span>}
                            </span>
                          ) : <span className="text-muted/60">—</span>}
                        </td>
                        <td className="px-3 py-2.5">
                          <div className="flex items-center justify-end gap-0.5">
                            <button
                              onClick={() => setEditing(lot)}
                              title="编辑"
                              className="p-1.5 rounded-md text-secondary transition-all hover:bg-accent/10 hover:text-accent cursor-pointer"
                            >
                              <Pencil className="h-3.5 w-3.5" />
                            </button>
                            {confirmId === lot.id ? (
                              <button
                                onClick={() => handleClickDelete(lot.id)}
                                title="再次点击确认删除"
                                className="inline-flex items-center gap-1 rounded-md bg-danger/15 px-1.5 py-0.5 text-[9px] font-medium text-danger border border-danger/30 animate-pulse cursor-pointer"
                              >
                                <Trash2 className="h-2.5 w-2.5" />确认
                              </button>
                            ) : (
                              <button
                                onClick={() => handleClickDelete(lot.id)}
                                title="删除 (同步删除生成的监控规则)"
                                className="p-1.5 rounded-md text-secondary transition-all hover:bg-danger/10 hover:text-danger cursor-pointer"
                              >
                                <Trash2 className="h-3.5 w-3.5" />
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <div className="flex items-center justify-center gap-1 text-[11px] text-muted">
            自动生成止盈止损 / 到期监控规则
            <button onClick={() => navigate('/monitor')} className="inline-flex items-center gap-0.5 text-accent hover:text-accent/80 cursor-pointer">
              前往监控中心 <ArrowUpRight className="h-3 w-3" />
            </button>
          </div>
        </div>
      </div>

      {editing && <LotDialog lot={editing} onClose={() => setEditing(null)} />}

      <StockPreviewDialog
        symbol={previewSymbol}
        name={previewSymbol ? symbolNames[previewSymbol] : undefined}
        navList={lotsNavItems}
        onNavigate={(sym) => setPreviewSymbol(sym)}
        onClose={() => setPreviewSymbol(null)}
      />
    </div>
  )
}

function LotDialog({ lot, onClose }: { lot: Lot; onClose: () => void }) {
  const qc = useQueryClient()
  const [draft, setDraft] = useState<Lot>(() => ({ ...lot }))
  const [symbolQuery, setSymbolQuery] = useState('')
  const [error, setError] = useState('')

  const symbolSearch = useQuery({
    queryKey: QK.instrumentSearch(symbolQuery, 'stock'),
    queryFn: () => api.instrumentSearch(symbolQuery, 20, 'stock'),
    enabled: symbolQuery.trim().length > 0,
  })

  const save = useMutation({
    mutationFn: () => api.lotSave({ ...draft, symbol: draft.symbol.trim() }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.lots })
      qc.invalidateQueries({ queryKey: QK.monitorRules })
      onClose()
    },
    onError: err => setError(String((err as any)?.message ?? err)),
  })

  const submit = () => {
    setError('')
    if (!draft.symbol.trim()) return setError('请选择标的')
    if (!(draft.cost_price > 0)) return setError('成本价必须为正数')
    if (draft.qty < 0 || draft.target_pct < 0 || draft.stop_pct < 0 || draft.lead_days < 0) return setError('数量 / 百分比 / 提前天数不能为负数')
    if (!(draft.target_pct > 0 || draft.stop_pct > 0 || draft.remind_date)) return setError('止盈% / 止损% / 到期日 至少设置一项')
    save.mutate()
  }

  const num = (set: (v: number) => void) => (e: React.ChangeEvent<HTMLInputElement>) => set(parseFloat(e.target.value) || 0)

  return (
    <Modal onClose={onClose} ariaLabel={lot.id ? '编辑批次' : '新增批次'} panelClassName="w-[92vw] max-w-md bg-surface border border-border rounded-card shadow-xl">
      <div className="flex items-center justify-between border-b border-border/60 px-4 py-3">
        <span className="text-sm font-medium text-foreground">{lot.id ? '编辑批次' : '新增批次'}</span>
        <span className="text-[10px] text-muted">保存后自动同步监控规则</span>
      </div>
      <div className="space-y-3 px-4 py-4">
        {/* 标的 */}
        <div className="space-y-1.5">
          <span className="text-[11px] text-muted">标的</span>
          {draft.symbol ? (
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center gap-1 rounded bg-elevated px-2 py-1 font-mono text-[11px] text-secondary">
                {draft.symbol}
                <button onClick={() => setDraft(d => ({ ...d, symbol: '' }))} className="text-muted hover:text-danger cursor-pointer"><span className="text-[10px]">✕</span></button>
              </span>
              <span className="text-[10px] text-muted">点击 ✕ 更换</span>
            </div>
          ) : (
            <div className="relative">
              <input
                value={symbolQuery}
                onChange={e => setSymbolQuery(e.target.value)}
                placeholder="搜索代码或名称..."
                autoFocus
                className="h-9 w-full rounded-btn border border-border bg-base pl-8 pr-3 text-xs text-foreground focus:outline-none focus:border-accent/50"
              />
              <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted" />
              {symbolSearch.data && symbolSearch.data.results.length > 0 && (
                <div className="absolute z-10 mt-1 max-h-48 w-full overflow-auto rounded border border-border bg-surface shadow-lg">
                  {symbolSearch.data.results.map(r => (
                    <button
                      key={r.symbol}
                      onClick={() => { setDraft(d => ({ ...d, symbol: r.symbol })); setSymbolQuery('') }}
                      className="block w-full px-2.5 py-1.5 text-left text-[11px] hover:bg-elevated cursor-pointer"
                    >
                      <span className="font-mono text-foreground/80">{r.symbol}</span>
                      <span className="ml-1.5 text-muted">{r.name}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        <div className="grid grid-cols-2 gap-3">
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">数量 (参考)</span>
            <input type="number" min={0} value={draft.qty} onChange={num(v => setDraft(d => ({ ...d, qty: v })))} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
          </label>
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">成本价</span>
            <input type="number" min={0} step="any" value={draft.cost_price} onChange={num(v => setDraft(d => ({ ...d, cost_price: v })))} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
          </label>
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">止盈 %</span>
            <input type="number" min={0} step="any" value={draft.target_pct} onChange={num(v => setDraft(d => ({ ...d, target_pct: v })))} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
          </label>
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">止损 %</span>
            <input type="number" min={0} step="any" value={draft.stop_pct} onChange={num(v => setDraft(d => ({ ...d, stop_pct: v })))} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
          </label>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <span className="text-[11px] text-muted">买入日期 (可选)</span>
            <DateShortcuts value={draft.buy_date ?? ''} onChange={v => setDraft(d => ({ ...d, buy_date: v || null }))} options={[{ label: '今天', days: 0 }]} />
            <DatePicker value={draft.buy_date ?? ''} onChange={v => setDraft(d => ({ ...d, buy_date: v || null }))} placeholder="不记录" />
          </div>
          <div className="space-y-1.5">
            <span className="text-[11px] text-muted">到期日 (可选)</span>
            <DateShortcuts value={draft.remind_date ?? ''} onChange={v => setDraft(d => ({ ...d, remind_date: v || null }))} options={[{ label: '5天', days: 5 }, { label: '10天', days: 10 }, { label: '15天', days: 15 }]} base={draft.buy_date || undefined} />
            <DatePicker value={draft.remind_date ?? ''} onChange={v => setDraft(d => ({ ...d, remind_date: v || null }))} placeholder="不提醒" />
          </div>
        </div>

        {draft.remind_date && (
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">提前提醒天数</span>
            <input type="number" min={0} value={draft.lead_days} onChange={num(v => setDraft(d => ({ ...d, lead_days: Math.floor(v) })))} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
            <span className="block text-[10px] text-muted">提醒仅在交易时段评估; 到期日落在周末/节假日建议提前 ≥ 2 天</span>
          </label>
        )}

        {error && <div className="rounded border border-danger/30 bg-danger/10 px-3 py-2 text-[11px] text-danger">{error}</div>}
      </div>
      <div className="flex items-center justify-end gap-2 border-t border-border/60 px-4 py-3">
        <button onClick={onClose} className="h-9 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated cursor-pointer">取消</button>
        <button
          onClick={submit}
          disabled={save.isPending}
          className={cn('h-9 rounded-btn px-4 text-xs font-medium bg-accent/90 text-white hover:bg-accent cursor-pointer disabled:opacity-50')}
        >
          {save.isPending ? '保存中...' : '保存'}
        </button>
      </div>
    </Modal>
  )
}
