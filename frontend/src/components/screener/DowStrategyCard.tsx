import { useState } from 'react'
import { Activity, ChevronDown, ChevronUp, Play, TestTube2 } from 'lucide-react'

type Fetcher = (input: string, init?: RequestInit) => Promise<{ ok: boolean; json: () => Promise<any> }>
type Stock = {
  symbol: string
  name: string
  market?: string
  strategyScore: number
  triggerTimeframes: string[]
}
type Detail = {
  timeframeStates: Record<string, { available: boolean; action?: string; phase?: string; reason?: string }>
}
type RunStatus = {
  runId: string
  status: 'queued' | 'running' | 'complete' | 'failed'
  completed?: number
  total?: number
  selected?: number
  failed?: number
  currentSymbol?: string | null
  error?: string | null
}

const marketNames: Record<string, string> = { cn: 'A股', hk: '港股', us: '美股' }
const periods = [['15m', '15分钟'], ['30m', '30分钟'], ['day', '日线']] as const

export function DowStrategyCard({ market, fetcher = fetch }: { market: string; fetcher?: Fetcher }) {
  const [open, setOpen] = useState(true)
  const [stocks, setStocks] = useState<Stock[]>([])
  const [selected, setSelected] = useState('')
  const [detail, setDetail] = useState<Detail | null>(null)
  const [metrics, setMetrics] = useState<any>(null)
  const [loading, setLoading] = useState(false)
  const [completed, setCompleted] = useState(false)
  const [progress, setProgress] = useState<RunStatus | null>(null)
  const [error, setError] = useState('')

  const run = async () => {
    setLoading(true)
    setCompleted(false)
    setProgress(null)
    setError('')
    try {
      const startResponse = await fetcher('/api/dow-strategy/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ market }),
      })
      if (!startResponse.ok) throw new Error('道氏实时扫描任务启动失败')
      let status = await startResponse.json() as RunStatus
      setProgress(status)
      while (status.status === 'queued' || status.status === 'running') {
        await new Promise(resolve => setTimeout(resolve, 1000))
        const statusResponse = await fetcher(`/api/dow-strategy/runs/${encodeURIComponent(status.runId)}`)
        if (!statusResponse.ok) throw new Error('道氏实时扫描状态读取失败')
        status = await statusResponse.json() as RunStatus
        setProgress(status)
      }
      if (status.status === 'failed') throw new Error(status.error || '道氏实时扫描失败')
      const poolResponse = await fetcher(`/api/dow-strategy/pool?market=${market}&limit=80`)
      if (!poolResponse.ok) throw new Error('道氏策略结果读取失败')
      const payload = await poolResponse.json()
      setStocks(payload.stocks ?? [])
      setCompleted(true)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '选股失败')
    } finally {
      setLoading(false)
    }
  }

  const inspect = async (symbol: string) => {
    setSelected(symbol)
    setMetrics(null)
    const response = await fetcher(`/api/dow-strategy/${encodeURIComponent(symbol)}`)
    if (response.ok) setDetail(await response.json())
  }

  const backtest = async () => {
    if (!selected) return
    setLoading(true)
    setError('')
    try {
      const end = new Date()
      const start = new Date()
      start.setDate(start.getDate() - 90)
      const selectedMarket = stocks.find(stock => stock.symbol === selected)?.market?.toLowerCase() ?? market
      const response = await fetcher('/api/dow-strategy/backtest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          market: selectedMarket === 'all' ? 'cn' : selectedMarket,
          symbols: [selected],
          start: start.toISOString(),
          end: end.toISOString(),
          initialCash: 100000,
          feeBps: 2,
          slippageBps: 3,
        }),
      })
      if (!response.ok) throw new Error('回测服务暂不可用')
      setMetrics((await response.json()).metrics)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '回测失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="rounded-card border border-cyan-400/25 bg-surface overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-3 p-4">
        <button type="button" className="flex items-center gap-2 text-left" onClick={() => setOpen(value => !value)}>
          <Activity className="h-4 w-4 text-cyan-400" />
          <span>
            <b className="block text-sm text-foreground">道氏趋势 · 多周期</b>
            <small className="text-muted">15分钟、30分钟、日线任一完成周期出现 OPEN_LONG 即入选</small>
          </span>
          {open ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
        </button>
        <button
          type="button"
          onClick={() => void run()}
          disabled={loading}
          className="inline-flex items-center gap-1.5 rounded-btn bg-cyan-500/15 border border-cyan-400/30 px-3 py-2 text-xs text-cyan-300 disabled:cursor-wait disabled:opacity-60"
        >
          <Play className="h-3.5 w-3.5" />{loading ? '执行中…' : '执行选股'}
        </button>
      </div>
      {open && (
        <div className="border-t border-border p-4 space-y-3">
          {error && <p className="text-xs text-danger">{error}</p>}
          {loading && progress && (
            <p className="text-xs text-cyan-300">
              实时扫描中：{progress.completed ?? 0}/{progress.total || '…'}
              {progress.currentSymbol ? ` · ${progress.currentSymbol}` : ''}
            </p>
          )}
          {completed && stocks.length === 0 && (
            <p className="text-xs text-cyan-300">
              {marketNames[market] ?? market}选股完成，当前暂无符合条件的股票
            </p>
          )}
          <div className="flex gap-2 overflow-x-auto pb-1">
            {stocks.map(stock => (
              <button type="button" key={stock.symbol} onClick={() => void inspect(stock.symbol)}
                className={`min-w-[150px] rounded-btn border p-3 text-left ${selected === stock.symbol ? 'border-cyan-400 bg-cyan-400/10' : 'border-border bg-base'}`}>
                <b className="block text-xs">{stock.symbol} · {stock.name}</b>
                <span className="text-[11px] text-cyan-300">
                  {stock.triggerTimeframes?.map(period => period === 'day' ? '日线' : period).join(' + ')}
                </span>
                <small className="block text-muted">评分 {stock.strategyScore?.toFixed?.(1) ?? '-'}</small>
              </button>
            ))}
          </div>
          {detail && (
            <>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                {periods.map(([key, label]) => {
                  const state = detail.timeframeStates?.[key]
                  return (
                    <div key={key} className="rounded-btn border border-border bg-base p-3">
                      <b className="text-xs">{label}</b>
                      <strong className="block mt-1 text-cyan-300 text-xs">
                        {state?.available ? state.action ?? 'WATCH' : '不可用'}
                      </strong>
                      <small className="text-muted">
                        {state?.available ? state.phase ?? '-' : state?.reason ?? '-'}
                      </small>
                    </div>
                  )
                })}
              </div>
              <button type="button" onClick={() => void backtest()}
                className="inline-flex items-center gap-1.5 rounded-btn border border-amber-400/30 bg-amber-400/10 px-3 py-2 text-xs text-amber-300">
                <TestTube2 className="h-3.5 w-3.5" />回测当前股票
              </button>
            </>
          )}
          {metrics && (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
              <span>累计收益<b className="block text-foreground">{(metrics.cumulativeReturn * 100).toFixed(2)}%</b></span>
              <span>最大回撤<b className="block text-foreground">{(metrics.maximumDrawdown * 100).toFixed(2)}%</b></span>
              <span>胜率<b className="block text-foreground">{(metrics.winRate * 100).toFixed(2)}%</b></span>
              <span>交易次数<b className="block text-foreground">{metrics.tradeCount}</b></span>
            </div>
          )}
        </div>
      )}
    </section>
  )
}
