import type { FactorBacktestResult } from './api.ts'
import {
  clearFactorBacktestTask,
  getFactorBacktestTask,
  startFactorBacktest,
  stopFactorBacktest,
  tryReconnectFactorBacktest,
} from './factorBacktestTask.ts'
import { storage } from './storage.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

class MemoryStorage {
  private readonly values = new Map<string, string>()

  getItem(key: string): string | null {
    return this.values.get(key) ?? null
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value)
  }

  removeItem(key: string): void {
    this.values.delete(key)
  }
}

class FakeEventSource {
  static instances: FakeEventSource[] = []
  readonly url: string
  closed = false
  private readonly listeners = new Map<string, Array<(event: Event) => void>>()

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject | null): void {
    if (!listener) return
    const callback = typeof listener === 'function'
      ? listener
      : (event: Event) => listener.handleEvent(event)
    const list = this.listeners.get(type) ?? []
    list.push(callback)
    this.listeners.set(type, list)
  }

  close(): void {
    this.closed = true
  }

  emit(type: string, data?: string): void {
    const event = { data } as MessageEvent
    for (const listener of this.listeners.get(type) ?? []) listener(event)
  }
}

const originalLocalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
const originalEventSource = Object.getOwnPropertyDescriptor(globalThis, 'EventSource')
const originalFetch = Object.getOwnPropertyDescriptor(globalThis, 'fetch')
Object.defineProperty(globalThis, 'localStorage', {
  configurable: true,
  value: new MemoryStorage(),
})
Object.defineProperty(globalThis, 'EventSource', {
  configurable: true,
  value: FakeEventSource,
})

const payload = {
  factor_name: 'momentum_20d',
  start: '2026-01-01',
  end: '2026-03-31',
  n_groups: 5,
  rebalance: 'daily' as const,
  weight: 'equal' as const,
  fees_pct: 0.0002,
}
const result = (runId: string): FactorBacktestResult => ({
  run_id: runId,
  config: {},
  ic_mean: 0.04,
  ic_std: 0.02,
  ir: 2,
  ic_win_rate: 0.6,
  ic_series: [],
  group_stats: [],
  group_nav: [],
  long_short_stats: {},
  long_short_nav: [],
  elapsed_ms: 1,
  n_symbols: 2,
  n_dates: 3,
  error: null,
  persisted: true,
})

try {
  clearFactorBacktestTask()

  startFactorBacktest(payload)
  const firstSource = FakeEventSource.instances.at(-1)!
  const pending = getFactorBacktestTask()
  assert(pending?.isPending, '发起因子回测后应保留模块级进行中状态')
  assert(pending?.payload.factor_name === 'momentum_20d', '进行中状态应保留请求参数')
  assert(firstSource.url.includes('/api/backtest/factor/stream?'), '因子任务必须订阅 SSE 端点')
  assert(firstSource.url.includes('start=2026-01-01'), 'SSE query 必须冻结开始日期')

  firstSource.emit('progress', JSON.stringify({
    stage: 'ic', label: '计算截面 IC', completed: 65, total: 100,
  }))
  assert(getFactorBacktestTask()?.progress?.completed === 65, 'SSE progress 应更新模块级任务状态')

  firstSource.emit('done', JSON.stringify(result('factor-first')))
  const completed = getFactorBacktestTask()
  assert(!completed?.isPending, '完成后任务不应仍标记为进行中')
  assert(completed?.result?.run_id === 'factor-first', '完成结果应留在模块级状态供路由重入读取')
  assert(
    storage.factorBacktestLast.get(null)?.result?.run_id === 'factor-first',
    '完成结果应写入恢复存储',
  )

  startFactorBacktest({ ...payload, factor_name: 'momentum_5d' })
  const olderSource = FakeEventSource.instances.at(-1)!
  startFactorBacktest({ ...payload, factor_name: 'rsi_14' })
  const latestSource = FakeEventSource.instances.at(-1)!
  assert(olderSource.closed, '启动新任务必须关闭旧 SSE 订阅')
  olderSource.emit('done', JSON.stringify(result('factor-older')))
  assert(getFactorBacktestTask()?.isPending, '旧任务晚到的结果不得覆盖新任务状态')
  latestSource.emit('done', JSON.stringify(result('factor-latest')))
  const latestTask = getFactorBacktestTask()
  assert(latestTask?.result?.run_id === 'factor-latest', '最新任务结果应赢得竞争')
  assert(latestTask?.payload.factor_name === 'rsi_14', '最新任务参数应与结果保持一致')

  clearFactorBacktestTask()
  localStorage.setItem(
    'factor-backtest-reconnect',
    'factor_name=momentum_20d&start=&end=2026-03-31&n_groups=5&rebalance=daily&weight=equal&fees_pct=0.0002&slippage_bps=5&risk_free_rate=0',
  )
  assert(tryReconnectFactorBacktest(), '存在保存的 query 时应重新订阅服务端任务')
  const reconnectSource = FakeEventSource.instances.at(-1)!
  assert(getFactorBacktestTask()?.payload.start === null, '空 start 必须保持“全部历史”语义')
  reconnectSource.emit('done', JSON.stringify(result('factor-reconnected')))
  assert(getFactorBacktestTask()?.result?.run_id === 'factor-reconnected', '重连后完成结果应正常写入')

  let cancelRequest: RequestInit | undefined
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (_url: string, init?: RequestInit) => {
      cancelRequest = init
      return new Response(JSON.stringify({ ok: true }))
    },
  })
  startFactorBacktest({ ...payload, factor_name: 'alpha101_001' })
  await stopFactorBacktest()
  assert(cancelRequest?.method === 'POST', '取消必须向后端发 POST 请求')
  assert(String(cancelRequest?.body).includes('factor_name=alpha101_001'), '取消必须携带同一任务 query')
  assert(getFactorBacktestTask()?.error === '已取消', '取消后本地任务应退出进行中状态')
} finally {
  clearFactorBacktestTask()
  if (originalLocalStorage) Object.defineProperty(globalThis, 'localStorage', originalLocalStorage)
  else Reflect.deleteProperty(globalThis, 'localStorage')
  if (originalEventSource) Object.defineProperty(globalThis, 'EventSource', originalEventSource)
  else Reflect.deleteProperty(globalThis, 'EventSource')
  if (originalFetch) Object.defineProperty(globalThis, 'fetch', originalFetch)
  else Reflect.deleteProperty(globalThis, 'fetch')
}

console.log('18/18 factor backtest SSE task tests passed')
