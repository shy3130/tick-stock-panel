import type { BacktestProgress, StrategyBacktestResult } from './api'

export interface StrategyRunInput {
  strategyId: string
  symbols: string[]
  start: string
  end: string
  initialCapital: number
  maxPositions: number
  commissionPct: number
  stampTaxPct: number
  slippageBps: number
  params: Record<string, unknown>
  overrides: Record<string, unknown>
}

export interface StrategyRunHandle {
  promise: Promise<StrategyBacktestResult>
  cancel: () => void
}

export class ComparisonCancelledError extends Error {
  constructor() {
    super('对比任务已取消')
    this.name = 'ComparisonCancelledError'
  }
}

export function buildStrategyBacktestQuery(input: StrategyRunInput): string {
  const query = new URLSearchParams({
    strategy_id: input.strategyId,
    symbols: input.symbols.join(','),
    start: input.start,
    end: input.end,
    matching: 'open_t+1',
    entry_fill: 'open_t+1',
    exit_fill: 'open_t+1',
    commission_pct: String(input.commissionPct),
    stamp_tax_pct: String(input.stampTaxPct),
    slippage_bps: String(input.slippageBps),
    max_positions: String(input.maxPositions),
    max_exposure_pct: '1',
    initial_capital: String(input.initialCapital),
    position_sizing: 'equal',
    params: JSON.stringify(input.params),
    overrides: JSON.stringify(input.overrides),
    mode: 'position',
    holding_days: '5',
    asset_type: 'stock',
    minute_fill: 'false',
  })
  return query.toString()
}

export function startStrategyBacktest(
  input: StrategyRunInput,
  onProgress: (progress: BacktestProgress) => void,
): StrategyRunHandle {
  const query = buildStrategyBacktestQuery(input)
  const eventSource = new EventSource(`/api/backtest/strategy/stream?${query}`)
  let settled = false
  let rejectPromise: (reason: unknown) => void = () => undefined
  let connectionErrors = 0

  const promise = new Promise<StrategyBacktestResult>((resolve, reject) => {
    rejectPromise = reject
    const fail = (error: Error) => {
      if (settled) return
      settled = true
      eventSource.close()
      reject(error)
    }

    eventSource.onopen = () => {
      connectionErrors = 0
    }
    eventSource.addEventListener('progress', event => {
      try {
        onProgress(JSON.parse((event as MessageEvent).data) as BacktestProgress)
      } catch {
        // A malformed progress event does not invalidate the eventual result.
      }
    })
    eventSource.addEventListener('done', event => {
      if (settled) return
      try {
        const result = JSON.parse((event as MessageEvent).data) as StrategyBacktestResult
        if (result.error) {
          fail(new Error(result.error))
          return
        }
        settled = true
        eventSource.close()
        resolve(result)
      } catch {
        fail(new Error('回测结果解析失败'))
      }
    })
    eventSource.addEventListener('error', event => {
      const messageEvent = event as MessageEvent
      if (messageEvent.data) {
        try {
          const payload = JSON.parse(messageEvent.data) as { message?: string }
          fail(new Error(payload.message || '回测失败'))
        } catch {
          fail(new Error('回测失败'))
        }
        return
      }
      connectionErrors += 1
      if (connectionErrors > 5) fail(new Error('回测连接中断，请重试'))
    })
  })

  return {
    promise,
    cancel: () => {
      if (settled) return
      settled = true
      eventSource.close()
      void fetch('/api/backtest/strategy/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ qs: query }),
      }).catch(() => undefined)
      rejectPromise(new ComparisonCancelledError())
    },
  }
}
