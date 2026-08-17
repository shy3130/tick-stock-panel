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

try {
  storage.backtestActiveTab.set('grid')
  storage.parameterGridLastExperimentId.set('pg-deadbeef1234')

  assert(
    storage.backtestActiveTab.get('strategy') === 'grid',
    '回测工作台标签应跨重新读取保持',
  )
  assert(
    storage.parameterGridLastExperimentId.get(null) === 'pg-deadbeef1234',
    '参数网格实验标识应跨重新读取保持',
  )

  storage.parameterGridLastExperimentId.set(null)
  assert(
    storage.parameterGridLastExperimentId.get('pg-deadbeef1234') === null,
    '显式清空后不应恢复过期实验标识',
  )
} finally {
  if (originalLocalStorage) Object.defineProperty(globalThis, 'localStorage', originalLocalStorage)
  else Reflect.deleteProperty(globalThis, 'localStorage')
}

console.log('3/3 storage persistence tests passed')
