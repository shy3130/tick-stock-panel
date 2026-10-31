import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FileInput, Pause, Play, SlidersHorizontal, X } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useBacktestTask } from '@/lib/backtestTask'
import { Modal } from '@/components/Modal'
import { executionApi, executionKey, executionListKey, sourceFromReport, type ExecutionBinding, type ExecutionPreview, type ExecutionSource } from './executionClient'

const input = 'mt-1 w-full rounded-btn border border-border bg-base px-3 py-2 text-xs outline-none focus:border-accent/50'
const button = 'rounded-btn border border-border px-3 py-1.5 text-xs hover:bg-elevated disabled:opacity-40'
const reasonLabels: Record<string, string> = { signal: '策略买卖点', stop_loss: '止损', take_profit: '止盈', trailing_stop: '移动止损', trailing_take_profit: '移动止盈', max_hold: '持仓到期' }

function SnapshotDetails({ preview }: { preview: ExecutionPreview }) {
  const s = preview.snapshot
  return <div className="space-y-3 rounded-card border border-border bg-base/40 p-3 text-xs">
    <div className="flex flex-wrap items-center gap-2"><strong>{s.name}</strong><span className="text-muted">{s.source}</span><code className="text-[10px] text-muted">快照 {preview.revision.slice(0, 10)}</code></div>
    <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
      <div><dt className="text-muted">买卖时序</dt><dd>收盘确认 → 次日开盘</dd></div>
      <div><dt className="text-muted">仓位</dt><dd>最多 {s.max_positions} 仓 · 敞口 {Math.round(s.max_exposure_pct * 100)}%</dd></div>
      <div><dt className="text-muted">初始资金</dt><dd>{s.initial_capital.toLocaleString('zh-CN')} 元</dd></div>
      <div><dt className="text-muted">股票池</dt><dd>{s.symbols ? `${s.symbols.length} 只` : '全市场 A 股'}</dd></div>
      <div className="col-span-2 break-words"><dt className="text-muted">买点 / 卖点</dt><dd>{s.overrides.entry_signals.join('、') || '策略候选入场'} / {s.overrides.exit_signals.join('、') || '无显式卖点，按风控退出'}</dd></div>
      <div className="col-span-2"><dt className="text-muted">费用</dt><dd>佣金万 {(s.fees.commission_pct * 10000).toFixed(2)} · 印花税千 {(s.fees.stamp_tax_pct * 1000).toFixed(2)} · 滑点 {s.fees.slippage_bps} bps</dd></div>
    </dl>
    <details><summary className="cursor-pointer text-accent">完整参数、过滤、评分与风控</summary><pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-all text-[10px] text-secondary">{JSON.stringify({ params: s.params, overrides: s.overrides }, null, 2)}</pre></details>
    <details><summary className="cursor-pointer text-muted">与历史回测的差异</summary><ul className="mt-2 list-disc space-y-1 pl-4 text-[11px] leading-relaxed text-muted">{preview.differences.map(d => <li key={d}>{d}</li>)}</ul></details>
  </div>
}

function BindingDialog({ account, binding, onClose }: { account: string; binding: ExecutionBinding | null; onClose: () => void }) {
  const qc = useQueryClient()
  const task = useBacktestTask()
  const strategies = useQuery({ queryKey: [...QK.screenerStrategies('all'), 'all'], queryFn: () => api.screenerStrategies(undefined, 'all'), staleTime: 60_000 })
  const candidates = useQuery({ queryKey: QK.backtestCandidates, queryFn: api.backtestCandidates })
  const [kind, setKind] = useState<'strategy' | 'candidate' | 'report'>('candidate')
  const [id, setId] = useState('')
  const [report, setReport] = useState<ExecutionSource | null>(null)
  const [preview, setPreview] = useState<{ source: ExecutionSource; value: ExecutionPreview } | null>(null)
  const [error, setError] = useState('')
  const [disableLegacy, setDisableLegacy] = useState(false)
  const previewM = useMutation({ mutationFn: async (source: ExecutionSource) => ({ source, value: await executionApi.preview(account, source) }), onSuccess: value => { setPreview(value); setError('') }, onError: e => setError(e.message) })
  const bindM = useMutation({ mutationFn: () => executionApi.bind(account, preview!.source, preview!.value.revision, disableLegacy), onSuccess: async result => {
    qc.setQueryData(executionKey(account), result)
    await Promise.all([qc.invalidateQueries({ queryKey: QK.paperAll }), qc.invalidateQueries({ queryKey: executionListKey })])
    onClose()
  }, onError: e => setError(e.message) })
  const busy = bindM.isPending || previewM.isPending
  const reset = () => { setPreview(null); setError('') }
  const source = kind === 'report' ? report : id ? kind === 'strategy' ? { strategy_id: id } : { candidate_id: id } : null
  return <Modal onClose={() => { if (!busy) onClose() }} closeOnBackdrop={!busy} ariaLabel="绑定策略参数快照" panelClassName="flex max-h-[88vh] w-[94vw] max-w-2xl flex-col overflow-hidden rounded-card border border-border bg-surface shadow-xl">
    <div className="min-h-0 space-y-4 overflow-y-auto p-5">
      <div className="flex items-center justify-between gap-3"><h3 className="text-sm font-semibold">{binding ? '重新绑定策略快照' : '绑定策略参数快照'}</h3><button className={button} onClick={onClose} disabled={busy} aria-label="关闭"><X className="h-4 w-4" /></button></div>
      <p className="text-[11px] leading-relaxed text-muted">使用独立、无持仓和待成交委托的账户。买卖点从策略逐股执行结果读取；参数冻结后，策略库调参不会改变本账户。</p>
      <label className="block text-xs text-muted">参数来源<select aria-label="参数来源" className={input} value={kind} disabled={busy} onChange={e => { setKind(e.target.value as typeof kind); setId(''); reset() }}>
        <option value="candidate">已保存的回测候选</option><option value="strategy">当前策略配置</option><option value="report">回测结果 / 配置 JSON</option>
      </select></label>
      {kind === 'candidate' && <label className="block text-xs text-muted">回测候选<select aria-label="回测候选" className={input} value={id} disabled={busy || candidates.isPending} onChange={e => { setId(e.target.value); reset() }}><option value="">{candidates.isPending ? '加载中…' : '请选择回测候选'}</option>{candidates.data?.items.filter(c => c.kind === 'strategy').map(c => <option value={c.id} key={c.id}>{c.name}</option>)}</select>{!candidates.isPending && !candidates.isError && !candidates.data?.items.some(c => c.kind === 'strategy') && <span className="mt-1 block">暂无候选，可在回测完成后保存候选，或选择其他来源。</span>}{candidates.isError && <span role="alert" className="text-danger">{candidates.error.message}</span>}</label>}
      {kind === 'strategy' && <label className="block text-xs text-muted">策略<select aria-label="策略" className={input} value={id} disabled={busy || strategies.isPending} onChange={e => { setId(e.target.value); reset() }}><option value="">请选择日线策略</option>{strategies.data?.presets.map(s => <option value={s.id} key={s.id}>{s.name}</option>)}</select>{strategies.isError && <span role="alert" className="text-danger">{strategies.error.message}</span>}<span className="mt-1 block">冻结已保存调参，仓位默认最多 10 仓、敞口 100%；精确复用回测请选回测候选或结果。</span></label>}
      {kind === 'report' && <div className="space-y-2 text-xs"><label className="block text-muted">读取回测 JSON<input aria-label="读取回测 JSON" type="file" accept=".json,application/json" className={input} disabled={busy} onChange={async e => {
        const file = e.target.files?.[0]
        reset(); setReport(null)
        if (!file) return
        try { if (file.size > 16 * 1024 * 1024) throw new Error('文件超过 16 MB，请只保留 config 和 strategy_info'); setReport(sourceFromReport(JSON.parse(await file.text()))) } catch (e) { setError((e as Error).message) }
      }} /></label>{task?.result && !task.result.error && <button className={button} disabled={busy} onClick={() => { reset(); setReport(sourceFromReport(task.result)) }}>使用最近完成的回测结果</button>}{report && <div className="text-accent">已读取 {String(report.config?.strategy_id)}</div>}</div>}
      <button className={`${button} flex items-center gap-1 text-accent`} disabled={!source || busy} onClick={() => { if (source) { setPreview(null); previewM.mutate(source) } }}><FileInput className="h-3.5 w-3.5" />{previewM.isPending ? '核对中…' : '预览并核对参数'}</button>
      {preview && <SnapshotDetails preview={preview.value} />}
      {preview && <><label className="flex items-start gap-2 text-[11px] leading-relaxed text-muted"><input type="checkbox" className="mt-0.5" checked={disableLegacy} disabled={busy} onChange={e => setDisableLegacy(e.target.checked)} />绑定时停用本账户的旧事件跟单规则，避免两套来源重复下单。</label><p className="text-[11px] text-muted">日线收盘确认、买卖次日开盘；保护性风控使用新鲜实时快照。仅支持当前可准确解释的配置，不支持项会在预览时说明。</p></>}
    </div>
    {(preview || error) && <div className="shrink-0 space-y-2 border-t border-border bg-surface px-5 py-3">
      {error && <div role="alert" className="rounded-btn bg-danger/10 p-2 text-xs text-danger">{error}</div>}
      {preview && <button className="w-full rounded-btn bg-accent px-3 py-2 text-xs font-medium text-white disabled:opacity-40" disabled={busy} onClick={() => bindM.mutate()}>{bindM.isPending ? '绑定中…' : '确认参数并启用跟单'}</button>}
    </div>}
  </Modal>
}

export function StrategyExecutionPanel({ account }: { account: string }) {
  const qc = useQueryClient()
  const query = useQuery({ queryKey: executionKey(account), queryFn: () => executionApi.state(account), refetchInterval: 15_000, retry: false })
  const [open, setOpen] = useState(false)
  const binding = query.data?.binding
  const toggle = useMutation({ mutationFn: () => executionApi.enabled(account, !binding?.enabled), onSuccess: data => {
    qc.setQueryData(executionKey(account), data)
    void qc.invalidateQueries({ queryKey: executionListKey })
  } })
  const lastOrder = binding?.events.find(e => e.order_id)?.order_id
  const executionStamp = `${binding?.last_bar ?? ''}|${lastOrder ?? ''}`
  const observedOrder = useRef<string | undefined>()
  useEffect(() => {
    if (executionStamp !== '|' && executionStamp !== observedOrder.current) {
      observedOrder.current = executionStamp
      void qc.invalidateQueries({ queryKey: QK.paperOrders(account) })
      void qc.invalidateQueries({ queryKey: QK.paperOverview(account) })
      void qc.invalidateQueries({ queryKey: QK.paperCompare })
    }
  }, [executionStamp, account, qc])
  return <section className="space-y-3 rounded-card border border-accent/25 bg-surface p-4" aria-label="策略买卖点跟单">
    <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="flex items-center gap-1.5 text-sm font-medium"><SlidersHorizontal className="h-4 w-4 text-accent" />策略买卖点跟单</h3><button className={button} disabled={query.isPending || query.isError} onClick={() => setOpen(true)}>{binding ? '重新绑定' : '绑定参数快照'}</button></div>
    {query.isPending && <p className="text-xs text-muted">读取跟单状态…</p>}
    {query.isError && <div role="alert" className="text-xs text-danger">{query.error.message}<button className={`${button} ml-2`} onClick={() => query.refetch()}>重试</button></div>}
    {!query.isPending && !query.isError && !binding && <p className="text-[11px] leading-relaxed text-muted">绑定回测配置，按策略买点开仓、卖点平仓。直接读取逐股信号，支持多标的同时触发，运行不依赖页面保持打开。</p>}
    {binding && <>
      <div className="flex items-start justify-between gap-2"><div className="min-w-0"><div className="truncate text-xs font-medium">{binding.snapshot.name} · {binding.enabled ? '已启用' : binding.status === 'blocked' ? '已阻止执行' : '已暂停'}</div><p className="mt-1 text-[11px] leading-relaxed text-muted">{binding.message}</p>{binding.risk_message && <p className="mt-1 text-[11px] text-muted">{binding.risk_message}</p>}</div><button className={`${button} flex shrink-0 items-center gap-1`} disabled={toggle.isPending} onClick={() => toggle.mutate()}>{binding.enabled ? <Pause className="h-3 w-3" /> : <Play className="h-3 w-3" />}{binding.enabled ? '暂停' : '恢复'}</button></div>
      <div className="text-[10px] text-muted">最近处理信号日：{binding.last_bar || '尚未处理'} · 快照 {binding.revision.slice(0, 10)}</div>
      <details><summary className="cursor-pointer text-xs text-accent">参数与差异</summary><div className="mt-2"><SnapshotDetails preview={binding} /></div></details>
      <p className="text-[10px] leading-relaxed text-muted">请勿在本账户手动下单或启用旧规则；检测到其他来源委托会暂停跟单。暂停不撤销已有委托，请在订单列表处理。</p>
      <details><summary className="cursor-pointer text-xs text-muted">最近执行记录 ({binding.events.length})</summary><div className="mt-2 max-h-44 space-y-2 overflow-y-auto text-[10px] text-muted">{!binding.events.length && <div>暂无委托决策</div>}{binding.events.map((e, i) => <div key={`${e.at}-${i}`} className="break-words">{e.at.slice(5, 19).replace('T', ' ')} · {e.symbol} · {e.side === 'buy' ? '买入' : '卖出'} · {reasonLabels[e.reason] || e.reason} · {e.message}</div>)}</div></details>
    </>}
    {toggle.isError && <p role="alert" className="text-xs text-danger">{toggle.error.message}</p>}
    {open && <BindingDialog key={account} account={account} binding={binding ?? null} onClose={() => setOpen(false)} />}
  </section>
}
