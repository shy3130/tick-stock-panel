import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type HoldingTrade } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { StockFinancialSearch } from '@/components/financials/StockFinancialSearch'

const input = 'w-full rounded border border-border bg-base px-3 py-2 text-sm'
const button = 'rounded border border-border px-3 py-2 text-xs hover:bg-elevated disabled:opacity-40'

export function TradeForm({ initial, existing, pending, error, onSave, onCancel }: {
  initial: HoldingTrade
  existing: boolean
  pending: boolean
  error: string
  onSave: (trade: HoldingTrade) => void
  onCancel: () => void
}) {
  const [trade, setTrade] = useState(initial)
  const [name, setName] = useState('')
  const [contextVersion, setContextVersion] = useState(0)
  const [autoPrice, setAutoPrice] = useState(!existing)
  const priceEdited = useRef(false)
  const date = trade.traded_at.slice(0, 10)
  const beijingNow = new Date(Date.now() + 8 * 3600000).toISOString()
  const today = beijingNow.slice(0, 10)
  const beforeClose = date === today && beijingNow.slice(11, 16) < '15:00'
  const enabled = autoPrice && !!trade.symbol && !!date && date <= today && !beforeClose
  const close = useQuery({
    queryKey: QK.kline(trade.symbol, date, date),
    queryFn: () => api.klineDaily(trade.symbol, 120, { start: date, end: date }),
    enabled,
    staleTime: 60000,
  })
  const row = enabled ? close.data?.rows.find(r => r.date === date) : undefined
  const price = row?.raw_close
  const validPrice = price != null && Number.isFinite(price) && price > 0

  useEffect(() => {
    if (validPrice && autoPrice && !priceEdited.current) {
      setTrade(t => ({ ...t, price: String(price) }))
    }
  }, [price, validPrice, autoPrice, trade.symbol, date, contextVersion])

  function changeContext(patch: Partial<HoldingTrade>) {
    setContextVersion(v => v + 1)
    priceEdited.current = false
    setAutoPrice(true)
    setTrade(t => ({ ...t, ...patch, price: '' }))
  }

  return <form className="p-5 space-y-4 max-h-[85vh] overflow-auto" onSubmit={e => {
    e.preventDefault()
    if (!trade.symbol || !date || !trade.price || trade.qty <= 0) return
    onSave({ ...trade, traded_at: `${date}T00:00:00` })
  }}>
    <h2 className="font-medium">录入 / 修改实际成交</h2>
    {error && <p className="text-danger" role="alert">{error}</p>}
    <div className="space-y-2">
      <div className="text-xs">股票</div>
      <StockFinancialSearch assetTypes="stock" onSelect={(symbol, stockName) => {
        setName(stockName)
        changeContext({ symbol })
      }} />
      <p className="text-xs text-muted" aria-live="polite">{trade.symbol ? `已选：${name} ${trade.symbol}` : '请搜索并选择股票'}</p>
    </div>
    <label className="block text-xs">成交日期<input className={input} required type="date" max={today} value={date}
      onChange={e => changeContext({ traded_at: e.target.value })} /></label>
    <fieldset className="space-y-2"><legend className="text-xs">方向</legend>
      <div className="flex gap-6">{(['buy', 'sell'] as const).map(side => <label key={side} className="flex items-center gap-2 text-sm">
        <input type="radio" name="trade-side" value={side} checked={trade.side === side}
          onChange={() => setTrade({ ...trade, side })} />{side === 'buy' ? '买入' : '卖出'}
      </label>)}</div>
    </fieldset>
    <label className="block text-xs">数量（股）<input className={input} required type="number" min="0" step="100" value={trade.qty}
      onChange={e => setTrade({ ...trade, qty: Number(e.target.value) })} /></label>
    <label className="block text-xs">成交价格<input className={input} required type="number" min="0.000001" step="0.000001" value={trade.price}
      onChange={e => { priceEdited.current = true; setTrade({ ...trade, price: e.target.value }) }} /></label>
    {autoPrice && !!trade.symbol && !!date && <p className="text-xs text-muted" aria-live="polite">
      {beforeClose ? '当日尚未收盘，请手动填写实际成交价格。' : close.isFetching ? '正在读取当日收盘价…' : close.isError ? '收盘价读取失败，请手动填写实际成交价格。' : validPrice ? '已读取当日不复权收盘价，可手动修改为实际成交价格。' : '所选日期无可用收盘价，请手动填写（休市、停牌或本地数据缺失）。'}
    </p>}
    <label className="block text-xs">总费用（元）<input className={input} required type="number" min="0" step="0.000001" value={trade.fee}
      onChange={e => setTrade({ ...trade, fee: e.target.value })} /></label>
    <label className="block text-xs">备注<input className={input} maxLength={1000} value={trade.note}
      onChange={e => setTrade({ ...trade, note: e.target.value })} /></label>
    <div className="flex flex-wrap gap-2" aria-label="快捷备注">{['定投', '抄底', '止损', '止盈'].map(note => <button key={note} type="button" className={button}
      onClick={() => setTrade(t => ({ ...t, note: t.note.includes(note) ? t.note : [t.note.trim(), note].filter(Boolean).join('，').slice(0, 1000) }))}>{note}</button>)}</div>
    <div className="flex gap-2"><button className={button} disabled={pending || !trade.symbol || !trade.price || trade.qty <= 0}>{pending ? '保存中…' : '保存成交'}</button>
      <button type="button" className={button} disabled={pending} onClick={onCancel}>取消</button></div>
  </form>
}
