import { useState } from 'react'
import { ChevronDown, ChevronUp, Play, TrendingUp } from 'lucide-react'

type Fetcher = (input: string, init?: RequestInit) => Promise<{ ok: boolean; json: () => Promise<any> }>

type NextDaySignal = {
  side?: string
  probability?: number
  bullishProbability?: number
  bearishProbability?: number
  expectedReturn?: number
  support?: number
  resistance?: number
  capitalLabel?: string
}

type Stock = {
  symbol: string
  name: string
  market?: string
  strategyScore?: number
  lastDone?: number
  modelReason?: string
  nextDayDirection?: NextDaySignal | null
}

const marketNames: Record<string, string> = { cn: 'CN', hk: 'HK', us: 'US' }

function pct(value?: number | null) {
  if (value == null || Number.isNaN(value)) return '-'
  return `${(value * 100).toFixed(1)}%`
}

function price(value?: number | null) {
  if (value == null || Number.isNaN(value)) return '-'
  return value.toFixed(3).replace(/\.?0+$/, '')
}

export function NextDayDirectionCard({ market, fetcher = fetch }: { market: string; fetcher?: Fetcher }) {
  const [open, setOpen] = useState(true)
  const [stocks, setStocks] = useState<Stock[]>([])
  const [selected, setSelected] = useState<Stock | null>(null)
  const [detail, setDetail] = useState<Stock | null>(null)
  const [loading, setLoading] = useState(false)
  const [completed, setCompleted] = useState(false)
  const [error, setError] = useState('')

  const run = async () => {
    setLoading(true)
    setCompleted(false)
    setError('')
    setDetail(null)
    try {
      const response = await fetcher(`/api/dow-strategy/next-day-direction/pool?market=${market}&limit=80`)
      if (!response.ok) throw new Error('Next-day direction strategy unavailable')
      const payload = await response.json()
      const rows = (payload.stocks ?? []) as Stock[]
      setStocks(rows)
      setSelected(rows[0] ?? null)
      setCompleted(true)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Run failed')
    } finally {
      setLoading(false)
    }
  }

  const inspect = async (stock: Stock) => {
    setSelected(stock)
    setDetail(null)
    const response = await fetcher(`/api/dow-strategy/next-day-direction/${encodeURIComponent(stock.symbol)}`)
    if (response.ok) setDetail(await response.json())
  }

  const active = detail ?? selected
  const signal = active?.nextDayDirection ?? null
  const sideClass = signal?.side === 'bearish' ? 'text-danger' : 'text-emerald-300'

  return (
    <section className="rounded-card border border-emerald-400/25 bg-surface overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-3 p-4">
        <button type="button" className="flex items-center gap-2 text-left" onClick={() => setOpen(value => !value)}>
          <TrendingUp className="h-4 w-4 text-emerald-400" />
          <span>
            <b className="block text-sm text-foreground">Next Day Direction</b>
            <small className="text-muted">Historical similarity probability with support, resistance and capital context</small>
          </span>
          {open ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
        </button>
        <button
          type="button"
          onClick={() => void run()}
          disabled={loading}
          className="inline-flex items-center gap-1.5 rounded-btn bg-emerald-500/15 border border-emerald-400/30 px-3 py-2 text-xs text-emerald-300 disabled:cursor-wait disabled:opacity-60"
        >
          <Play className="h-3.5 w-3.5" />{loading ? 'Running...' : 'Run'}
        </button>
      </div>
      {open && (
        <div className="border-t border-border p-4 space-y-3">
          {error && <p className="text-xs text-danger">{error}</p>}
          {loading && <p className="text-xs text-emerald-300">Scanning {marketNames[market] ?? market} symbols...</p>}
          {completed && stocks.length === 0 && (
            <p className="text-xs text-emerald-300">No candidate reached the configured probability threshold.</p>
          )}
          <div className="flex gap-2 overflow-x-auto pb-1">
            {stocks.map(stock => {
              const itemSignal = stock.nextDayDirection ?? null
              return (
                <button type="button" key={stock.symbol} onClick={() => void inspect(stock)}
                  className={`min-w-[170px] rounded-btn border p-3 text-left ${selected?.symbol === stock.symbol ? 'border-emerald-400 bg-emerald-400/10' : 'border-border bg-base'}`}>
                  <b className="block text-xs">{stock.symbol} · {stock.name}</b>
                  <span className={`text-[11px] ${itemSignal?.side === 'bearish' ? 'text-danger' : 'text-emerald-300'}`}>
                    {itemSignal?.side ?? 'watch'} · {pct(itemSignal?.probability)}
                  </span>
                  <small className="block text-muted">score {stock.strategyScore?.toFixed?.(1) ?? '-'}</small>
                </button>
              )
            })}
          </div>
          {active && (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
              <span>Direction<b className={`block ${sideClass}`}>{signal?.side ?? '-'}</b></span>
              <span>Probability<b className="block text-foreground">{pct(signal?.probability)}</b></span>
              <span>Expected<b className="block text-foreground">{pct(signal?.expectedReturn)}</b></span>
              <span>Capital<b className="block text-foreground">{signal?.capitalLabel ?? '-'}</b></span>
              <span>Support<b className="block text-foreground">{price(signal?.support)}</b></span>
              <span>Resistance<b className="block text-foreground">{price(signal?.resistance)}</b></span>
              <span>Last<b className="block text-foreground">{price(active.lastDone)}</b></span>
              <span>Model<b className="block text-foreground">{active.modelReason ? 'ready' : '-'}</b></span>
            </div>
          )}
        </div>
      )}
    </section>
  )
}
