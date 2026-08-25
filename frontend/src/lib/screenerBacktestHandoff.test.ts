import {
  clearScreenerBacktestHandoff,
  filterActiveScreenerRows,
  peekScreenerBacktestHandoff,
  stageScreenerBacktestHandoff,
} from './screenerBacktestHandoff.ts'

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

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
const sessionStorage = new MemoryStorage()
Object.defineProperty(globalThis, 'window', {
  configurable: true,
  value: { sessionStorage },
})

try {
  // symbols 规范化: 大写/去重/剔除非法值 + 500 只上限
  const many = Array.from({ length: 600 }, (_, i) => `sh${String(60000 + i)}`)
  assert(
    stageScreenerBacktestHandoff({ target: 'strategy', symbols: many, asOf: '2026-08-19' }) === 500,
    '超过 500 只时应截断为 500',
  )
  let handoff = peekScreenerBacktestHandoff()
  assert(handoff?.symbols.length === 500, 'peek 应返回截断后的 500 只标的')
  assert(handoff?.symbols[0] === 'SH60000', '标的应规范化为大写')
  clearScreenerBacktestHandoff()

  // strategyId 往返: 非空才持久化
  const staged = stageScreenerBacktestHandoff({
    target: 'strategy',
    symbols: [' sh600000 ', 'SH600000', 42, ''],
    asOf: '2026-08-19',
    strategyId: ' broken_board_recovery ',
  })
  assert(staged === 1, '重复/非法标的应去重后仅保留 1 只')
  handoff = peekScreenerBacktestHandoff()
  assert(handoff?.target === 'strategy' && handoff.asOf === '2026-08-19', 'target/asOf 应往返保留')
  assert(handoff?.strategyId === 'broken_board_recovery', '非空 strategyId 应规范化后往返保留')

  // 空 strategyId 不持久化, 交接本身仍可用
  stageScreenerBacktestHandoff({ target: 'factor', symbols: ['SZ000001'], asOf: null, strategyId: '' })
  handoff = peekScreenerBacktestHandoff()
  assert(handoff?.target === 'factor' && handoff.symbols[0] === 'SZ000001', '无有效 strategyId 的交接仍应正常写入/读取')
  assert(handoff?.strategyId == null, '空 strategyId 不应持久化')

  // 纯策略交接: 空 symbols + 任意非空 strategyId 仍持久化 (策略在回测区间内逐日选股, 无当日池)
  clearScreenerBacktestHandoff()
  const screenCount = stageScreenerBacktestHandoff({
    target: 'strategy',
    symbols: [],
    asOf: null,
    strategyId: 'screen:abc123',
  })
  assert(screenCount === 0, '空 symbols 应返回 0 只')
  handoff = peekScreenerBacktestHandoff()
  assert(handoff?.target === 'strategy' && handoff?.strategyId === 'screen:abc123', '带 strategyId 的空池交接应可 peek')
  assert(Array.isArray(handoff?.symbols) && handoff.symbols.length === 0, '纯策略交接 symbols 应为空数组')
  // F12: AI 生成策略 id (无 screen: 前缀) 同样可纯策略交接, 前缀限制已放宽
  const aiCount = stageScreenerBacktestHandoff({ target: 'strategy', symbols: [], asOf: null, strategyId: 'ai_l8zz123' })
  assert(aiCount === 0, '空 symbols 的纯策略交接返回 0 只 (0 是标的数, 不代表失败)')
  handoff = peekScreenerBacktestHandoff()
  assert(handoff?.strategyId === 'ai_l8zz123', '任意非空 strategyId (无前缀) 的空池交接应可 peek')
  clearScreenerBacktestHandoff()
  // 空 symbols 且无 strategyId: 不持久化 (旧行为不变)
  assert(
    stageScreenerBacktestHandoff({ target: 'strategy', symbols: [], asOf: '2026-08-19' }) === 0
      && peekScreenerBacktestHandoff() == null,
    '空 symbols 且无 strategyId 不应持久化',
  )

  // 旧格式 payload (无 strategyId 字段) 仍可 peek
  sessionStorage.setItem('condition-screener-backtest-handoff', JSON.stringify({
    target: 'strategy',
    symbols: ['SH600519'],
    asOf: '2026-08-18',
  }))
  handoff = peekScreenerBacktestHandoff()
  assert(handoff?.symbols[0] === 'SH600519', '旧格式 payload (无 strategyId) 应仍可 peek')
  assert(handoff?.strategyId == null, '旧格式 payload 读出的 strategyId 应为 null')

  // 失效行过滤: _expired 行不进自选/回测标的
  const active = filterActiveScreenerRows([
    { symbol: 'SH600000' },
    { symbol: 'SH600001', _expired: true },
    { symbol: 'SZ000001', _expired: false },
  ])
  assert(
    active.length === 2 && active[0].symbol === 'SH600000' && active[1].symbol === 'SZ000001',
    '带 _expired 标记的行不应进入自选/回测标的',
  )
} finally {
  if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow)
  else Reflect.deleteProperty(globalThis, 'window')
}

console.log('17/17 screener backtest handoff tests passed')
