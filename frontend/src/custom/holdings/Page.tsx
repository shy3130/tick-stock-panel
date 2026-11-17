import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type HoldingTrade, type HoldingPosition } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { Modal } from '@/components/Modal'
import { PageHeader } from '@/components/PageHeader'
import { TradeForm } from './TradeForm'
import { openStockPreview } from '@/extensions/stockPreview'

const money = (v: number | null | undefined) => v == null ? '—' : v.toLocaleString('zh-CN', {maximumFractionDigits: 2, minimumFractionDigits: 2})
const button = 'rounded border border-border px-3 py-2 text-xs hover:bg-elevated disabled:opacity-40'
const nowCN = () => new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10)
const draft = (p?: HoldingPosition): HoldingTrade => ({id: crypto.randomUUID(), symbol: p?.symbol ?? '', side: p ? 'sell' : 'buy', qty: p?.qty ?? 100, price: '', fee: '5', note: '', order: 0, traded_at: nowCN(), voided: false})

function Chips({symbol}: {symbol: string}) {
  const q = useQuery({queryKey: QK.holdingChips(symbol), queryFn: () => api.holdingChips(symbol), staleTime: 60000})
  const bins = q.data?.bins ?? []
  const max = Math.max(...bins.map(b => b.weight), 0.001)
  return <section className="space-y-2"><h3 className="font-medium">市场筹码分布</h3>
    <p className="text-xs text-muted">{q.isLoading ? '计算中…' : q.isError ? '筹码数据加载失败' : q.data?.message}{q.data?.as_of && ` · 截至 ${q.data.as_of}`}</p>
    {q.isError && <button className={button} onClick={() => q.refetch()}>重试</button>}
    {!!bins.length && <div className="max-h-80 overflow-auto space-y-1" aria-label="市场筹码分布图">{[...bins].reverse().map((b, i) => <div key={i} className="flex items-center gap-2 text-xs font-mono"><span className="w-16 text-right">{b.price.toFixed(2)}</span><div className="flex-1"><div className="h-2 rounded bg-accent/70" style={{width: `${b.weight / max * 100}%`}} /></div><span className="w-14 text-right">{(b.weight * 100).toFixed(1)}%</span></div>)}</div>}
    <p className="text-xs text-muted">分布为模型估算，不是实际股东持仓；前复权价位不可直接与个人成交成本比较。</p>
  </section>
}

export default function Holdings() {
  const qc = useQueryClient()
  const q = useQuery({queryKey: QK.holdings, queryFn: api.holdings, refetchInterval: 60000})
  const [restoreTrades, setRestoreTrades] = useState<HoldingTrade[] | null>(null)
  const [restoreRevision, setRestoreRevision] = useState('')
  const restore = useMutation({mutationFn: () => api.holdingsRestore(restoreRevision, restoreTrades!), onSuccess: async () => {setRestoreTrades(null); setSelected(null); setError(''); await qc.invalidateQueries({queryKey: QK.holdings})}, onError: (e: Error) => setError(e.message)})
  const [closed, setClosed] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [editing, setEditing] = useState<HoldingTrade | null>(null)
  const [editRevision, setEditRevision] = useState('')
  const [error, setError] = useState('')
  const [voidId, setVoidId] = useState<string | null>(null)
  const data = q.data
  const positions = data?.positions ?? []
  const position = positions.find(p => p.id === selected)
  const rows = data?.transactions.filter(t => t.cycle_id === selected) ?? []
  const mutation = useMutation({mutationFn: ({trade, revision}: {trade: HoldingTrade; revision: string}) => api.holdingTrade(revision, trade),
    onSuccess: async () => {setEditing(null); setVoidId(null); setError(''); await qc.invalidateQueries({queryKey: QK.holdings})},
    onError: (e: Error) => setError(e.message)})
  const edit = (t: HoldingTrade) => {if (!data?.trades.some(x => x.id === t.id)) t = {...t, order: Math.max(0, ...(data?.trades.map(x => x.order) ?? [])) + 1}; setError(''); setEditRevision(data?.revision ?? ''); setEditing({...((data?.trades.find(x => x.id === t.id)) ?? t), traded_at: t.traded_at.slice(0, 10)})}
  const shown = positions.filter(p => closed ? p.qty === 0 : p.qty > 0)
  return <div className="h-full overflow-auto"><PageHeader title="持仓管理" subtitle="实际成交登记 · 移动加权平均成本（含买入费用）" />
    <div className="p-5 space-y-5 max-w-7xl mx-auto">
      <div className="flex flex-wrap gap-2"><button className={button} aria-pressed={!closed} onClick={() => setClosed(false)}>当前持仓（{positions.filter(p => p.qty > 0).length}）</button><button className={button} aria-pressed={closed} onClick={() => setClosed(true)}>已清仓（{positions.filter(p => !p.qty).length}）</button><button className={button} disabled={!data || q.isError} onClick={() => edit(draft())}>录入交易</button><button className={button} onClick={() => q.refetch()}>刷新</button>
      <button className={button} disabled={!data} onClick={() => {const blob = new Blob([JSON.stringify({version: 1, trades: data?.trades}, null, 2)], {type: 'application/json'}); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = 'holdings-ledger.json'; a.click(); URL.revokeObjectURL(url)}}>导出账本</button><label className={button}>恢复账本<input className="hidden" type="file" accept=".json" disabled={!data || mutation.isPending || restore.isPending} onChange={async e => {const file = e.target.files?.[0]; e.target.value = ''; if (!file) return; try {if (file.size > 10 * 1024 * 1024) throw new Error('文件不能超过10MB'); const backup = JSON.parse(await file.text()); if (backup.version !== 1 || !Array.isArray(backup.trades)) throw new Error('账本格式不支持'); setRestoreTrades(backup.trades); setRestoreRevision(data!.revision); setError('')} catch (err) {setError((err as Error).message)}}} /></label></div>
      {q.isLoading && <p>正在加载持仓…</p>}{q.isError && <p className="text-danger">{q.error.message}</p>}{error && <p role="alert" className="text-danger">{error}</p>}
      {data?.market_error && <p className="text-warning">{data.market_error}</p>}
      {!q.isLoading && !q.isError && !shown.length && <p className="text-muted">{closed ? '暂无清仓记录' : '暂无持仓，录入买入交易后自动登记。'}</p>}
      {!!shown.length && <div className="overflow-x-auto rounded border border-border"><table className="w-full text-xs whitespace-nowrap"><thead><tr>{['股票', '数量', '平均成本', '现价 / 日期', '市值', '浮动盈亏', '已实现盈亏', '建仓 / 清仓日', '持有天数', '操作'].map(t => <th key={t} className="p-3 text-left">{t}</th>)}</tr></thead><tbody>{shown.map(p => <tr key={p.id} className="border-t border-border"><td className="p-3"><button className="text-accent" onClick={() => setSelected(p.id)}>{p.name || p.symbol}<br />{p.symbol}</button></td><td>{p.qty}</td><td>{money(p.avg_cost)}</td><td>{money(p.last_price)}<br />{p.quote_date ?? '无行情'}</td><td>{money(p.market_value)}</td><td>{money(p.unrealized)}{p.unrealized != null && p.cost > 0 && <div>{(p.unrealized / p.cost * 100).toFixed(2)}%</div>}</td><td>{money(p.realized)}</td><td>{p.opened}<br />{p.closed}</td><td>{p.holding_days}天</td><td><button className={button} onClick={() => setSelected(p.id)}>详情</button>{p.qty > 0 && <button className={button} onClick={() => edit(draft(p))}>卖出 / 清仓</button>}</td></tr>)}</tbody></table></div>}
      {position && <section className="rounded border border-border p-4 space-y-5">
        <div className="flex flex-wrap items-center gap-3"><h2 className="font-medium">{position.name} {position.symbol} · {position.opened} 起</h2><button className={button} onClick={() => openStockPreview({symbol: position.symbol})}>查看行情图</button><button className={button} onClick={() => edit({...draft(), symbol: position.symbol})}>录入交易</button><button className={button} onClick={() => setSelected(null)}>收起</button></div>
        <div className="flex gap-5 flex-wrap text-sm"><span>平均成本 {money(position.avg_cost)}</span><span>持有 {position.holding_days} 天（自然日差）</span><span>本轮总盈亏 {money(position.qty ? position.unrealized == null ? null : position.realized + position.unrealized : position.realized)}</span><span>累计买入 {money(position.bought)}</span><span>卖出净额 {money(position.sold)}</span></div>
        <div className="flex gap-2 flex-wrap">{positions.filter(p => p.symbol === position.symbol).map(p => <button className={button} key={p.id} onClick={() => setSelected(p.id)}>{p.opened} {p.closed ? '已清仓' : '持仓中'}</button>)}</div>
        <h3>交易与核算明细</h3><div className="overflow-x-auto"><table className="w-full text-xs whitespace-nowrap"><thead><tr>{['成交日期', '方向', '价格', '股数', '费用', '交易后数量', '交易后成本', '本笔收益', '备注', '操作'].map(t => <th key={t} className="p-2 text-left">{t}</th>)}</tr></thead><tbody>{rows.map(t => <tr key={t.id} className="border-t border-border">{[t.traded_at.slice(0, 10), t.side === 'buy' ? '买入' : '卖出', t.price, t.qty, t.fee, t.balance, money(t.avg_cost), money(t.realized), t.note].map((v, i) => <td key={i} className="p-2 max-w-48 truncate" title={String(v)}>{v}</td>)}<td><button className={button} disabled={mutation.isPending} onClick={() => edit(t)}>修改</button><button className={button} disabled={mutation.isPending} onClick={() => {if (voidId !== t.id) {setVoidId(t.id); return} const original = data!.trades.find(x => x.id === t.id)!; mutation.mutate({revision: data!.revision, trade: {...original, voided: true}})}}>{voidId === t.id ? '确认作废' : '作废'}</button></td></tr>)}</tbody></table></div>
        <Chips symbol={position.symbol} />
      </section>}
      <p className="text-xs text-muted">现价为本地最新不复权行情，请核对日期。送转、分红等公司行动尚未自动入账；发生此类事件时需核对账本。已作废记录保留在账本导出中。</p>
    </div>
    {restoreTrades && <Modal ariaLabel="确认恢复账本" onClose={() => {if (!restore.isPending) setRestoreTrades(null)}}><div className="p-5 space-y-4"><h2>恢复账本</h2><p>将用文件中的 {restoreTrades.length} 笔记录替换当前账本。请先导出当前账本备份。保存前会校验所有交易。</p>{error && <p role="alert" className="text-danger">{error}</p>}<button className={button} disabled={restore.isPending} onClick={() => restore.mutate()}>确认替换</button><button className={button} disabled={restore.isPending} onClick={() => setRestoreTrades(null)}>取消</button></div></Modal>}
    {editing && <Modal ariaLabel="录入成交" onClose={() => {if (!mutation.isPending) setEditing(null)}}>
      <TradeForm key={editing.id} initial={editing} existing={!!data?.trades.some(t => t.id === editing.id)} pending={mutation.isPending} error={error}
        onSave={trade => mutation.mutate({revision: editRevision, trade})} onCancel={() => setEditing(null)} />
    </Modal>}
  </div>
}
