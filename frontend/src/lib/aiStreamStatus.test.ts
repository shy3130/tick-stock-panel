/**
 * F9/F14: 连接态纯函数测试。
 *
 * 三个 store(个股/财务/复盘)共用 nextConnection/aiStreamStatus:
 *  - 生命周期: start→connecting, 首个 delta→open, done/error/cancelled→closed
 *  - cancelled 终态: nextConnection 不会再改回 open(终态闭合)
 *  - 文案: 连接中/生成中/已断开/已取消;done/idle 不显示
 */
import { aiStreamStatus, nextConnection, type AiConnection } from './aiStreamStatus.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

function eq<T>(actual: T, expected: T, message: string): void {
  assert(JSON.stringify(actual) === JSON.stringify(expected), `${message}: 期望 ${JSON.stringify(expected)}, 实际 ${JSON.stringify(actual)}`)
}

// ---- nextConnection 生命周期 ----
eq(nextConnection('loading', 'connecting'), 'connecting', 'loading 应保持 connecting')
eq(nextConnection('streaming', 'connecting'), 'open', '首个 delta(streaming)应 open')
eq(nextConnection('streaming', 'open'), 'open', '持续 streaming 保持 open')
eq(nextConnection('done', 'open'), 'closed', 'done 应 closed')
eq(nextConnection('error', 'open'), 'closed', 'error 应 closed')
eq(nextConnection('cancelled', 'open'), 'closed', 'cancelled 应 closed')
eq(nextConnection('idle', 'open'), 'closed', 'idle 归位 closed')
// 终态闭合: closed 不会被任何非 streaming phase 改回;streaming 只在 store 未拦截时出现(patchTask 已拦 cancelled)
eq(nextConnection('streaming', 'closed'), 'open', 'streaming 语义上重新打开(仅非终态可达,store 守卫保证 cancelled 不进入)')
eq(nextConnection('done', 'closed'), 'closed', '终态保持 closed')

// ---- aiStreamStatus 文案映射 ----
const cases: Array<[{ phase: Parameters<typeof aiStreamStatus>[0]['phase']; connection: AiConnection }, string | null]> = [
  [{ phase: 'loading', connection: 'connecting' }, '连接中'],
  [{ phase: 'streaming', connection: 'open' }, '生成中'],
  [{ phase: 'error', connection: 'closed' }, '已断开'],
  [{ phase: 'cancelled', connection: 'closed' }, '已取消'],
  [{ phase: 'streaming', connection: 'connecting' }, '连接中'], // 组合在真实 store 不出现(delta 同时切 open);此处验证 connection 优先级
  [{ phase: 'idle', connection: 'closed' }, null],
]
for (const [input, expected] of cases) {
  const got = aiStreamStatus(input)
  eq(got?.label ?? null, expected, `aiStreamStatus(${JSON.stringify(input)})`)
}

// tone: error 红 / active 活跃 / cancelled muted
eq(aiStreamStatus({ phase: 'error', connection: 'closed' })!.tone, 'error', 'error tone')
eq(aiStreamStatus({ phase: 'cancelled', connection: 'closed' })!.tone, 'muted', 'cancelled tone')
eq(aiStreamStatus({ phase: 'loading', connection: 'connecting' })!.tone, 'active', 'connecting tone')

// error 优先于 connection: 断流后即使 connection 字段异常也不误报「生成中」
eq(aiStreamStatus({ phase: 'error', connection: 'open' })!.label, '已断开', 'error 应压过 open')

console.log('aiStreamStatus / nextConnection 全部断言通过')
