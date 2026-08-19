import { api, type OptimizerLaunchResponse, type OptimizerRequest } from './api.ts'
import {
  clearOptimizerExperiment,
  clearOptimizerExperimentIfCurrent,
  getOptimizerTask,
  startOptimizerExperiment,
} from './optimizerTask.ts'
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

const originalLaunch = api.optimizerLaunch
const payload: OptimizerRequest = {
  strategy_ids: ['trend_breakout'],
  include_all_a: true,
  holding_days: [5, 10],
  objective: 'risk_adjusted',
}

function launch(experimentId: string): OptimizerLaunchResponse {
  return {
    experiment_id: experimentId,
    config_hash: `hash-${experimentId}`,
    scenario_count: 8,
    requested_count: 8,
    truncated: false,
    objective: 'risk_adjusted',
    start: '2018-01-01',
    end: '2026-08-14',
    train_end: '2024-12-31',
    holdout_start: '2025-01-01',
    status: 'started',
  }
}

try {
  clearOptimizerExperiment()

  let resolveFirst: ((value: OptimizerLaunchResponse) => void) | undefined
  api.optimizerLaunch = () => new Promise<OptimizerLaunchResponse>(resolve => {
    resolveFirst = resolve
  })
  const first = startOptimizerExperiment(payload)
  assert(getOptimizerTask().isLaunching, '创建中状态应跨路由保留')

  resolveFirst?.(launch('opt-first'))
  await first
  const firstTask = getOptimizerTask()
  assert(firstTask.experimentId === 'opt-first', '完成后应保存服务端实验标识')
  assert(storage.optimizerLastExperimentId.get(null) === 'opt-first', '完成后应持久化实验标识')

  let resolveOlder: ((value: OptimizerLaunchResponse) => void) | undefined
  let resolveLatest: ((value: OptimizerLaunchResponse) => void) | undefined
  let callCount = 0
  api.optimizerLaunch = () => new Promise<OptimizerLaunchResponse>(resolve => {
    if (callCount++ === 0) resolveOlder = resolve
    else resolveLatest = resolve
  })
  const staleRevision = firstTask.revision
  const older = startOptimizerExperiment({ ...payload, objective: 'sharpe' })
  const latest = startOptimizerExperiment({ ...payload, objective: 'calmar' })
  assert(
    getOptimizerTask().experimentId === null && getOptimizerTask().isLaunching,
    '新创建请求进行中不得继续声明旧实验为当前',
  )
  assert(
    !clearOptimizerExperimentIfCurrent('opt-first', staleRevision),
    '旧轮询的失效响应不得清除更新创建中的实验状态',
  )
  resolveOlder?.(launch('opt-older'))
  await older
  assert(
    getOptimizerTask().experimentId === null && getOptimizerTask().isLaunching,
    '旧创建响应返回后仍应等待最新请求',
  )

  resolveLatest?.(launch('opt-latest'))
  await latest
  const latestTask = getOptimizerTask()
  assert(latestTask.experimentId === 'opt-latest', '最新实验标识应赢得竞争')
  assert(storage.optimizerLastExperimentId.get(null) === 'opt-latest', '最新实验应写入恢复存储')
  assert(
    clearOptimizerExperimentIfCurrent('opt-latest', latestTask.revision),
    '当前失效实验应被清除，避免无限轮询',
  )
  assert(getOptimizerTask().experimentId === null, '清除失效实验后不应保留实验标识')

  let resolveDiscarded: ((value: OptimizerLaunchResponse) => void) | undefined
  api.optimizerLaunch = () => new Promise<OptimizerLaunchResponse>(resolve => {
    resolveDiscarded = resolve
  })
  const discarded = startOptimizerExperiment(payload)
  clearOptimizerExperiment()
  resolveDiscarded?.(launch('opt-discarded'))
  await discarded
  assert(getOptimizerTask().experimentId === null, '清除后不应复活实验')
  assert(storage.optimizerLastExperimentId.get(null) === null, '清除后不应保留持久化实验标识')

  console.log('optimizer task persistence tests passed')
} finally {
  api.optimizerLaunch = originalLaunch
  clearOptimizerExperiment()
  if (originalLocalStorage) Object.defineProperty(globalThis, 'localStorage', originalLocalStorage)
}
