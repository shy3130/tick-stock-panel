import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FileUp, Loader2, NotebookPen, Trash2 } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { api, type JournalLedger, type JournalPreview } from '@/lib/api'

const FIELD_LABELS: Record<string, string> = {
  date: '日期',
  time: '时间',
  code: '代码',
  name: '名称',
  category: '交易类别',
  qty: '数量',
  price: '价格',
  amount: '发生金额',
  fee: '费用',
}

const FIELDS = Object.keys(FIELD_LABELS)

export function TradeJournal() {
  const qc = useQueryClient()
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<JournalPreview | null>(null)
  const [mapping, setMapping] = useState<Record<string, string>>({})
  const [sheet, setSheet] = useState('')
  const [benchmark, setBenchmark] = useState('000300.SH')
  const [accountId, setAccountId] = useState('default')
  const [appendMode, setAppendMode] = useState(false)
  const [narrative, setNarrative] = useState(false)

  const presets = useQuery({ queryKey: ['journal-presets'], queryFn: api.journalPresets })
  const ledger = useQuery<JournalLedger>({
    queryKey: ['journal-ledger'],
    queryFn: api.journalLedger,
    retry: false,
  })
  const previewUpload = useMutation({
    mutationFn: (f: File) => api.journalUpload(f, false),
    onSuccess: (data) => {
      const p = data as JournalPreview
      setPreview(p)
      setMapping(p.guessed_mapping)
      setSheet(p.sheets.includes('交易记录') ? '交易记录' : (p.sheets[0] ?? ''))
    },
  })
  const commitUpload = useMutation({
    mutationFn: () => api.journalUpload(file!, true, mapping, sheet, benchmark, accountId, appendMode, narrative),
    onSuccess: () => {
      setPreview(null)
      setFile(null)
      qc.invalidateQueries({ queryKey: ['journal-ledger'] })
    },
  })
  const deleteLedger = useMutation({
    mutationFn: api.journalDelete,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['journal-ledger'] }),
  })
  const feedback = useMutation({ mutationFn: api.journalFeedback })

  useEffect(() => {
    const first = presets.data?.benchmarks?.[0]?.symbol
    if (first) setBenchmark(first)
  }, [presets.data])

  const current = ledger.data
  const previewColumns = useMemo(() => preview?.columns.slice(0, 8) ?? [], [preview])

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="交易复盘" subtitle="券商成交流水上传 · FIFO 台账 · 行为诊断" />
      <div className="flex-1 overflow-auto px-5 py-6">
        <div className="mx-auto flex max-w-6xl flex-col gap-5">
          <section className="rounded-card border border-border bg-surface p-5">
            <div className="flex flex-wrap items-center gap-3">
              <label className="inline-flex cursor-pointer items-center gap-2 rounded-card border border-border px-3 py-2 text-sm text-foreground hover:bg-surface-hover">
                <FileUp className="h-4 w-4" />
                <span>{file ? file.name : '选择 xlsx / csv'}</span>
                <input
                  type="file"
                  accept=".xlsx,.xls,.csv"
                  className="hidden"
                  onChange={(e) => {
                    const f = e.target.files?.[0] ?? null
                    setFile(f)
                    if (f) previewUpload.mutate(f)
                  }}
                />
              </label>
              {previewUpload.isPending && <Loader2 className="h-4 w-4 animate-spin text-muted" />}
              {preview && (
                <>
                  <select className="rounded-card border border-border bg-base px-3 py-2 text-sm" value={sheet} onChange={(e) => setSheet(e.target.value)}>
                    {preview.sheets.map((s) => <option key={s} value={s}>{s}</option>)}
                  </select>
                  <select className="rounded-card border border-border bg-base px-3 py-2 text-sm" value={benchmark} onChange={(e) => setBenchmark(e.target.value)}>
                    {presets.data?.benchmarks.map((b) => <option key={b.symbol} value={b.symbol}>{b.name}</option>)}
                  </select>
                  <input
                    className="w-32 rounded-card border border-border bg-base px-3 py-2 text-sm"
                    value={accountId}
                    onChange={(e) => setAccountId(e.target.value)}
                    placeholder="账户"
                  />
                  <label className="inline-flex items-center gap-1.5 text-xs text-secondary">
                    <input type="checkbox" checked={appendMode} onChange={(e) => setAppendMode(e.target.checked)} />
                    追加去重
                  </label>
                  <label className="inline-flex items-center gap-1.5 text-xs text-secondary">
                    <input type="checkbox" checked={narrative} onChange={(e) => setNarrative(e.target.checked)} />
                    聚合摘要
                  </label>
                  <button
                    className="rounded-card bg-accent px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
                    disabled={!file || commitUpload.isPending}
                    onClick={() => commitUpload.mutate()}
                  >
                    {commitUpload.isPending ? '导入中…' : '确认导入'}
                  </button>
                </>
              )}
              {current && (
                <button className="ml-auto inline-flex items-center gap-2 rounded-card border border-border px-3 py-2 text-sm text-danger" onClick={() => deleteLedger.mutate()}>
                  <Trash2 className="h-4 w-4" />删除台账
                </button>
              )}
            </div>
          </section>

          {preview && (
            <section className="rounded-card border border-border bg-surface p-5">
              <h3 className="text-sm font-semibold text-foreground">列映射预览</h3>
              <div className="mt-3 grid gap-3 md:grid-cols-3">
                {preview.columns.map((col) => (
                  <label key={col} className="flex items-center gap-2 text-sm">
                    <span className="w-28 truncate text-secondary" title={col}>{col}</span>
                    <select
                      className="min-w-0 flex-1 rounded-card border border-border bg-base px-2 py-1.5"
                      value={mapping[col] ?? ''}
                      onChange={(e) => setMapping({ ...mapping, [col]: e.target.value })}
                    >
                      <option value="">忽略</option>
                      {FIELDS.map((f) => <option key={f} value={f}>{FIELD_LABELS[f]}</option>)}
                    </select>
                  </label>
                ))}
              </div>
              <div className="mt-4 overflow-auto">
                <table className="min-w-full text-left text-xs">
                  <thead className="text-muted"><tr>{previewColumns.map((c) => <th key={c} className="px-2 py-1">{c}</th>)}</tr></thead>
                  <tbody>
                    {preview.preview_rows.slice(0, 8).map((row, i) => (
                      <tr key={i} className="border-t border-border">
                        {previewColumns.map((c) => <td key={c} className="px-2 py-1 text-secondary">{String(row[c] ?? '')}</td>)}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {current && <Report ledger={current} onFeedback={(rating) => feedback.mutate(rating)} feedbackPending={feedback.isPending} />}
        </div>
      </div>
    </div>
  )
}

function Report({
  ledger,
  onFeedback,
  feedbackPending,
}: {
  ledger: JournalLedger
  onFeedback: (rating: 'helpful' | 'not_helpful') => void
  feedbackPending: boolean
}) {
  const s = ledger.summary
  const d = ledger.diagnosis
  return (
    <>
      <section className="grid gap-3 md:grid-cols-4">
        <Metric label="完成回合" value={s.total_trips} />
        <Metric label="总盈亏" value={money(s.total_pnl)} tone={s.total_pnl >= 0 ? 'bull' : 'bear'} />
        <Metric label="胜率" value={pct(s.win_rate)} />
        <Metric label={`超额 vs ${ledger.benchmark.name}`} value={ledger.benchmark.account.excess == null ? '—' : pct(ledger.benchmark.account.excess)} tone={(ledger.benchmark.account.excess ?? 0) >= 0 ? 'bull' : 'bear'} />
      </section>
      {(ledger.accounts?.length || ledger.import || ledger.narrative) && (
        <section className="rounded-card border border-border bg-surface p-4 text-sm text-secondary">
          {ledger.accounts?.length ? (
            <div>账户：{ledger.accounts.map(a => `${a.id}(${a.fills})`).join('、')}</div>
          ) : null}
          {ledger.import ? (
            <div className="mt-1">最近导入：{ledger.import.mode === 'append' ? '追加' : '替换'} · {ledger.import.account_id} · 新成交 {ledger.import.new_fills} · 去重 {ledger.import.deduped_fills}</div>
          ) : null}
          {ledger.narrative ? <div className="mt-2 text-foreground">{ledger.narrative}</div> : null}
        </section>
      )}
      <section className="rounded-card border border-border bg-surface p-4">
        <div className="flex flex-wrap items-center gap-2 text-sm text-secondary">
          <span>这份诊断有帮助吗</span>
          <button disabled={feedbackPending} onClick={() => onFeedback('helpful')} className="rounded border border-border px-2 py-1 text-xs hover:bg-elevated disabled:opacity-50">有帮助</button>
          <button disabled={feedbackPending} onClick={() => onFeedback('not_helpful')} className="rounded border border-border px-2 py-1 text-xs hover:bg-elevated disabled:opacity-50">没帮助</button>
        </div>
      </section>
      <section className="rounded-card border border-border bg-surface p-5">
        <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-foreground">
          <NotebookPen className="h-4 w-4" />行为诊断
        </div>
        <div className="grid gap-3 md:grid-cols-4">
          <Diagnosis title="处置效应" flag={d.disposition?.flag} value={`${num(d.disposition?.loss_to_win_holding_ratio)}x`} />
          <Diagnosis title="过度交易" flag={d.overtrading?.flag} value={`${num(d.overtrading?.monthly_roundtrips)} / 月`} />
          <Diagnosis title="追涨买入" flag={d.chasing?.flag} value={pct(d.chasing?.ratio)} />
          <Diagnosis title="浮亏加仓" flag={d.anchoring?.flag} value={pct(d.anchoring?.ratio)} />
        </div>
      </section>
      <section className="rounded-card border border-border bg-surface p-5">
        <h3 className="text-sm font-semibold text-foreground">Roundtrip 台账</h3>
        <p className="mt-1 text-xs text-muted">{ledger.benchmark.noise_note}</p>
        <div className="mt-3 overflow-auto">
          <table className="min-w-full text-left text-xs">
            <thead className="text-muted">
              <tr><th className="px-2 py-1">账户</th><th>代码</th><th>建仓</th><th>清仓</th><th>数量</th><th>盈亏</th><th>收益率</th><th>基准</th><th>超额</th></tr>
            </thead>
            <tbody>
              {ledger.trips.slice(0, 200).map((t, i) => {
                const b = ledger.benchmark.per_trip.find((r) => (r.account_id ?? 'default') === (t.account_id ?? 'default') && r.symbol === t.symbol && r.open_date === t.open_date && r.close_date === t.close_date)
                return (
                <tr key={`${t.symbol}-${t.open_date}-${i}`} className="border-t border-border">
                  <td className="px-2 py-1 text-muted">{t.account_id ?? 'default'}</td>
                  <td className="text-foreground">{t.symbol}</td>
                  <td>{t.open_date}</td>
                  <td>{t.close_date}</td>
                  <td>{num(t.qty)}</td>
                  <td className={Number(t.total_pnl) >= 0 ? 'text-bull' : 'text-bear'}>{money(t.total_pnl)}</td>
                  <td>{pct(t.pnl_pct)}</td>
                  <td>{b?.benchmark_pct == null ? '—' : pct(b.benchmark_pct)}</td>
                  <td>{b?.excess == null ? '—' : pct(b.excess)}</td>
                </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </section>
    </>
  )
}

function Metric({ label, value, tone }: { label: string; value: string | number; tone?: 'bull' | 'bear' }) {
  return (
    <div className="rounded-card border border-border bg-surface p-4">
      <div className="text-xs text-muted">{label}</div>
      <div className={`mt-1 text-xl font-semibold ${tone === 'bull' ? 'text-bull' : tone === 'bear' ? 'text-bear' : 'text-foreground'}`}>{value}</div>
    </div>
  )
}

function Diagnosis({ title, flag, value }: { title: string; flag?: boolean; value: string }) {
  return (
    <div className="rounded-card border border-border bg-base p-3">
      <div className="text-xs text-muted">{title}</div>
      <div className={flag ? 'mt-1 text-sm font-semibold text-bear' : 'mt-1 text-sm font-semibold text-foreground'}>{value}</div>
    </div>
  )
}

function money(v: any) { return Number(v ?? 0).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) }
function pct(v: any) { return `${(Number(v ?? 0) * 100).toFixed(1)}%` }
function num(v: any) { return Number(v ?? 0).toFixed(2) }
