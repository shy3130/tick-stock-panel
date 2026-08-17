import { api, type FactorBacktestResult } from './api.ts'
import {
  clearFactorBacktestTask,
  getFactorBacktestTask,
  startFactorBacktest,
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
}

const originalLocalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
Object.defineProperty(globalThis, 'localStorage', {
  configurable: true,
  value: new MemoryStorage(),
})

const originalFactorRun = api.factorRun
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
})

try {
  clearFactorBacktestTask()

  let resolveFirst: ((value: FactorBacktestResult) => void) | undefined
  api.factorRun = () => new Promise<FactorBacktestResult>(resolve => {
    resolveFirst = resolve
  })
  const first = startFactorBacktest(payload)
  const pending = getFactorBacktestTask()
  assert(pending?.isPending, '发起因子回测后应保留模块级进行中状态')
  assert(pending?.payload.factor_name === 'momentum_20d', '进行中状态应保留请求参数')
  assert(
    storage.factorBacktestLast.get(null) === null,
    '进行中的请求不得被伪装为可在完整刷新后恢复的任务',
  )

  resolveFirst?.(result('factor-first'))
  await first
  const completed = getFactorBacktestTask()
  assert(!completed?.isPending, '完成后任务不应仍标记为进行中')
  assert(completed?.result?.run_id === 'factor-first', '完成结果应留在模块级状态供路由重入读取')
  assert(
    storage.factorBacktestLast.get(null)?.result?.run_id === 'factor-first',
    '完成结果应写入恢复存储',
  )


  let resolveOlder: ((value: FactorBacktestResult) => void) | undefined
  let resolveLatest: ((value: FactorBacktestResult) => void) | undefined
  let callCount = 0
  api.factorRun = () => new Promise<FactorBacktestResult>(resolve => {
    if (callCount++ === 0) resolveOlder = resolve
    else resolveLatest = resolve
  })
  const older = startFactorBacktest({ ...payload, factor_name: 'momentum_5d' })
  const latest = startFactorBacktest({ ...payload, factor_name: 'rsi_14' })
  const storedWhilePending = storage.factorBacktestLast.get(null)
  assert(
    storedWhilePending?.payload.factor_name === 'momentum_20d',
    '新请求进行中不得覆盖上一份已完成结果的恢复参数',
  )
  assert(
    storedWhilePending?.result?.run_id === 'factor-first',
    '新请求进行中不得覆盖上一份已完成结果的恢复结果',
  )
  resolveOlder?.(result('factor-older'))
  await older
  assert(getFactorBacktestTask()?.isPending, '旧请求返回不得覆盖更新任务的进行中状态')

  resolveLatest?.(result('factor-latest'))
  await latest
  const latestTask = getFactorBacktestTask()
  assert(latestTask?.result?.run_id === 'factor-latest', '最新任务结果应赢得竞争')
  assert(latestTask?.payload.factor_name === 'rsi_14', '最新任务参数应与结果保持一致')
} finally {
  api.factorRun = originalFactorRun
  clearFactorBacktestTask()
  if (originalLocalStorage) Object.defineProperty(globalThis, 'localStorage', originalLocalStorage)
  else Reflect.deleteProperty(globalThis, 'localStorage')
}

console.log('11/11 factor backtest task persistence tests passed')
