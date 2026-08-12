import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FileUp, Loader2, NotebookPen, Trash2 } from 'lucide-react'
import { EmptyState } from '@/components/EmptyState'
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
  const [benchmark, setBenchmark] = useState('000300.INDEX')
  const [accountId, setAccountId] = useState('default')
  const [appendMode, setAppendMode] = useState(false)
  const [narrative, setNarrative] = useState(false)

  const presets = useQuery({ queryKey: ['journal-presets'], queryFn: api.journalPresets })
  const ledger = useQuery<JournalLedger | null>({
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
    <div className="workspace-page">
      <PageHeader title="交易复盘" subtitle="券商成交流水上传 · FIFO 台账 · 行为诊断" />
      <div className="workspace-content overflow-auto">
        <div className="mx-auto flex w-full max-w-6xl min-w-0 flex-col gap-3">
          <section className="panel">
            <div className="panel-header">
              <div>
                <div className="section-kicker">Import</div>
                <h2 className="section-title">流水导入</h2>
              </div>
            </div>
            <div className="panel-body">
              <div className="workspace-toolbar flex-wrap">
                <label className="btn-secondary cursor-pointer !h-8">
                  <FileUp className="h-4 w-4" />
                  <span className="max-w-[14rem] truncate">{file ? file.name : '选择 xlsx / csv'}</span>
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
                    <select className="control w-auto" value={sheet} onChange={(e) => setSheet(e.target.value)}>
                      {preview.sheets.map((s) => <option key={s} value={s}>{s}</option>)}
                    </select>
                    <select className="control w-auto" value={benchmark} onChange={(e) => setBenchmark(e.target.value)}>
                      {presets.data?.benchmarks.map((b) => <option key={b.symbol} value={b.symbol}>{b.name}</option>)}
                    </select>
                    <input
                      className="control w-32"
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
                      className="btn-primary"
                      disabled={!file || commitUpload.isPending}
                      onClick={() => commitUpload.mutate()}
                    >
                      {commitUpload.isPending ? '导入中…' : '确认导入'}
                    </button>
                  </>
                )}
                {current && (
                  <button className="btn-ghost ml-auto text-danger hover:text-danger" onClick={() => deleteLedger.mutate()}>
                    <Trash2 className="h-4 w-4" />删除台账
                  </button>
                )}
              </div>
            </div>
          </section>

          {preview && (
            <section className="panel">
              <div className="panel-header">
                <div>
                  <div className="section-kicker">Mapping</div>
                  <h2 className="section-title">列映射预览</h2>
                </div>
              </div>
              <div className="panel-body space-y-3">
                <div className="grid gap-3 md:grid-cols-3">
                  {preview.columns.map((col) => (
                    <label key={col} className="flex min-w-0 items-center gap-2 text-sm">
                      <span className="w-28 shrink-0 truncate text-secondary" title={col}>{col}</span>
                      <select
                        className="control min-w-0 flex-1"
                        value={mapping[col] ?? ''}
                        onChange={(e) => setMapping({ ...mapping, [col]: e.target.value })}
                      >
                        <option value="">忽略</option>
                        {FIELDS.map((f) => <option key={f} value={f}>{FIELD_LABELS[f]}</option>)}
                      </select>
                    </label>
                  ))}
                </div>
                <div className="data-table-scroll">
                  <table className="data-table min-w-full text-xs">
                    <thead><tr>{previewColumns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
                    <tbody>
                      {preview.preview_rows.slice(0, 8).map((row, i) => (
                        <tr key={i}>
                          {previewColumns.map((c) => <td key={c} className="text-secondary">{String(row[c] ?? '')}</td>)}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </section>
          )}

          {!ledger.isLoading && !ledger.isError && !current && (
            <section className="panel">
              <EmptyState
                icon={NotebookPen}
                title="尚未导入交易复盘台账"
                hint="选择券商导出的 xlsx 或 csv，预览列映射并确认导入后，这里会显示 FIFO 台账与行为诊断。"
              />
            </section>
          )}

          {ledger.isError && (
            <section className="panel px-6 py-8 text-center text-sm text-danger">
              台账读取失败，请稍后重试。
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
        <section className="panel panel-body text-sm text-secondary">
          {ledger.accounts?.length ? (
            <div>账户：{ledger.accounts.map(a => `${a.id}(${a.fills})`).join('、')}</div>
          ) : null}
          {ledger.import ? (
            <div className="mt-1">最近导入：{ledger.import.mode === 'append' ? '追加' : '替换'} · {ledger.import.account_id} · 新成交 {ledger.import.new_fills} · 去重 {ledger.import.deduped_fills}</div>
          ) : null}
          {ledger.narrative ? <div className="mt-2 text-foreground">{ledger.narrative}</div> : null}
        </section>
      )}
      <section className="panel">
        <div className="panel-body">
          <div className="workspace-toolbar text-sm text-secondary">
            <span>这份诊断有帮助吗</span>
            <button disabled={feedbackPending} onClick={() => onFeedback('helpful')} className="btn-secondary !h-7 text-xs">有帮助</button>
            <button disabled={feedbackPending} onClick={() => onFeedback('not_helpful')} className="btn-secondary !h-7 text-xs">没帮助</button>
          </div>
        </div>
      </section>
      <section className="panel">
        <div className="panel-header">
          <div className="flex items-center gap-2">
            <NotebookPen className="h-4 w-4" />
            <h3 className="section-title">行为诊断</h3>
          </div>
        </div>
        <div className="panel-body">
          <div className="grid gap-3 md:grid-cols-4">
            <Diagnosis title="处置效应" flag={d.disposition?.flag} value={`${num(d.disposition?.loss_to_win_holding_ratio)}x`} />
            <Diagnosis title="过度交易" flag={d.overtrading?.flag} value={`${num(d.overtrading?.monthly_roundtrips)} / 月`} />
            <Diagnosis title="追涨买入" flag={d.chasing?.flag} value={pct(d.chasing?.ratio)} />
            <Diagnosis title="浮亏加仓" flag={d.anchoring?.flag} value={pct(d.anchoring?.ratio)} />
          </div>
        </div>
      </section>
      <section className="panel">
        <div className="panel-header">
          <div>
            <div className="section-kicker">Ledger</div>
            <h3 className="section-title">Roundtrip 台账</h3>
          </div>
        </div>
        <div className="panel-body space-y-2">
          <p className="text-xs text-muted">{ledger.benchmark.noise_note}</p>
          <div className="data-table-scroll">
            <table className="data-table min-w-full text-xs">
              <thead>
                <tr><th>账户</th><th>代码</th><th>建仓</th><th>清仓</th><th>数量</th><th>盈亏</th><th>收益率</th><th>基准</th><th>超额</th></tr>
              </thead>
              <tbody>
                {ledger.trips.slice(0, 200).map((t, i) => {
                  const b = ledger.benchmark.per_trip.find((r) => (r.account_id ?? 'default') === (t.account_id ?? 'default') && r.symbol === t.symbol && r.open_date === t.open_date && r.close_date === t.close_date)
                  return (
                  <tr key={`${t.symbol}-${t.open_date}-${i}`}>
                    <td className="text-muted">{t.account_id ?? 'default'}</td>
                    <td className="text-foreground">{t.symbol}</td>
                    <td className="num">{t.open_date}</td>
                    <td className="num">{t.close_date}</td>
                    <td className="num">{num(t.qty)}</td>
                    <td className={Number(t.total_pnl) >= 0 ? 'text-bull num' : 'text-bear num'}>{money(t.total_pnl)}</td>
                    <td className="num">{pct(t.pnl_pct)}</td>
                    <td className="num">{b?.benchmark_pct == null ? '—' : pct(b.benchmark_pct)}</td>
                    <td className="num">{b?.excess == null ? '—' : pct(b.excess)}</td>
                  </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </>
  )
}

function Metric({ label, value, tone }: { label: string; value: string | number; tone?: 'bull' | 'bear' }) {
  return (
    <div className="panel p-3">
      <div className="section-kicker">{label}</div>
      <div className={`metric-value mt-1 text-base ${tone === 'bull' ? 'text-bull' : tone === 'bear' ? 'text-bear' : ''}`}>{value}</div>
    </div>
  )
}

function Diagnosis({ title, flag, value }: { title: string; flag?: boolean; value: string }) {
  return (
    <div className="rounded-input border border-border bg-elevated/40 p-3">
      <div className="text-xs text-muted">{title}</div>
      <div className={flag ? 'metric-value mt-1 !text-sm text-bear' : 'metric-value mt-1 !text-sm'}>{value}</div>
    </div>
  )
}

function money(v: any) { return Number(v ?? 0).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) }
function pct(v: any) { return `${(Number(v ?? 0) * 100).toFixed(1)}%` }
function num(v: any) { return Number(v ?? 0).toFixed(2) }
