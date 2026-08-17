import {
  api,
  type ParameterGridLaunchResponse,
  type ParameterGridRequest,
} from './api.ts'
import {
  clearParameterGridExperiment,
  clearParameterGridExperimentIfCurrent,
  getParameterGridTask,
  startParameterGridExperiment,
} from './parameterGridTask.ts'
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

const originalLaunch = api.parameterGridLaunch
const payload: ParameterGridRequest = {
  strategy_id: 'trend_breakout',
  grid: { volume_ratio: [1.5, 2] },
  objective: 'risk_adjusted',
}

function launch(experimentId: string): ParameterGridLaunchResponse {
  return {
    experiment_id: experimentId,
    config_hash: `hash-${experimentId}`,
    scenario_count: 2,
    truncated: false,
    status: 'started',
  }
}

try {
  clearParameterGridExperiment()

  let resolveFirst: ((value: ParameterGridLaunchResponse) => void) | undefined
  api.parameterGridLaunch = () => new Promise<ParameterGridLaunchResponse>(resolve => {
    resolveFirst = resolve
  })
  const first = startParameterGridExperiment(payload)
  assert(getParameterGridTask().isLaunching, '创建中状态应跨路由保留')

  resolveFirst?.(launch('pg-first'))
  const firstOutcome = await first
  const firstTask = getParameterGridTask()
  assert(firstOutcome.adopted, '首个完成的创建请求应成为当前实验')
  assert(firstTask.experimentId === 'pg-first', '完成后应保存服务端实验标识')
  assert(storage.parameterGridLastExperimentId.get(null) === 'pg-first', '完成后应持久化实验标识')

  let resolveOlder: ((value: ParameterGridLaunchResponse) => void) | undefined
  let resolveLatest: ((value: ParameterGridLaunchResponse) => void) | undefined
  let callCount = 0
  api.parameterGridLaunch = () => new Promise<ParameterGridLaunchResponse>(resolve => {
    if (callCount++ === 0) resolveOlder = resolve
    else resolveLatest = resolve
  })
  const staleRevision = firstTask.revision
  const older = startParameterGridExperiment({ ...payload, objective: 'sharpe' })
  const latest = startParameterGridExperiment({ ...payload, objective: 'calmar' })
  assert(
    getParameterGridTask().experimentId === null && getParameterGridTask().isLaunching,
    '新创建请求进行中不得继续声明旧实验为当前',
  )
  assert(
    storage.parameterGridLastExperimentId.get(null) === 'pg-first',
    '新创建请求进行中应保留上一份完成实验供完整刷新恢复',
  )
  assert(
    !clearParameterGridExperimentIfCurrent('pg-first', staleRevision),
    '旧轮询的失效响应不得清除更新创建中的实验状态',
  )
  resolveOlder?.(launch('pg-older'))
  const olderOutcome = await older
  assert(!olderOutcome.adopted, '旧创建响应不得覆盖更新请求')
  assert(
    getParameterGridTask().experimentId === null && getParameterGridTask().isLaunching,
    '旧创建响应返回后仍应等待最新请求',
  )

  resolveLatest?.(launch('pg-latest'))
  const latestOutcome = await latest
  const latestTask = getParameterGridTask()
  assert(latestOutcome.adopted, '最新创建响应应成为当前实验')
  assert(latestTask.experimentId === 'pg-latest', '最新实验标识应赢得竞争')
  assert(storage.parameterGridLastExperimentId.get(null) === 'pg-latest', '最新实验应写入恢复存储')
  assert(
    clearParameterGridExperimentIfCurrent('pg-latest', latestTask.revision),
    '当前失效实验应被清除，避免无限轮询',
  )
  assert(getParameterGridTask().experimentId === null, '清除失效实验后不应保留实验标识')
  assert(storage.parameterGridLastExperimentId.get(null) === null, '清除失效实验后不应保留恢复标识')

  let resolveDiscarded: ((value: ParameterGridLaunchResponse) => void) | undefined
  api.parameterGridLaunch = () => new Promise<ParameterGridLaunchResponse>(resolve => {
    resolveDiscarded = resolve
  })
  const discarded = startParameterGridExperiment(payload)
  clearParameterGridExperiment()
  resolveDiscarded?.(launch('pg-discarded'))
  const discardedOutcome = await discarded
  assert(!discardedOutcome.adopted, '清除恢复记录后旧创建响应不得复活实验')
  assert(getParameterGridTask().experimentId === null, '清除后不应保留实验标识')
  assert(storage.parameterGridLastExperimentId.get(null) === null, '清除后不应保留持久化实验标识')
} finally {
  api.parameterGridLaunch = originalLaunch
  clearParameterGridExperiment()
  if (originalLocalStorage) Object.defineProperty(globalThis, 'localStorage', originalLocalStorage)
  else Reflect.deleteProperty(globalThis, 'localStorage')
}

console.log('18/18 parameter grid task persistence tests passed')
