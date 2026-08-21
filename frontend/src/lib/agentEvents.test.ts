import { applyAgentEvent, type ChatMsg } from './agentEvents.ts'
import type { AgentEvent } from './api.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

function assistant(partial?: Partial<ChatMsg>): ChatMsg {
  return { role: 'assistant', content: '', ...partial }
}

const ref = { current: null as string | null }

{
  const prev: ChatMsg[] = [{ role: 'user', content: 'hi' }]
  const next = applyAgentEvent(prev, { type: 'delta', content: 'x' } as AgentEvent, ref)
  assert(next === prev, '最后一条非 assistant 时 reducer 必须原样返回')
}

{
  ref.current = null
  const prev = [assistant()]
  applyAgentEvent(prev, { type: 'attempt_start', attempt_id: 'att_1' } as AgentEvent, ref)
  assert(ref.current === 'att_1', 'attempt_start 应写入 attemptIdRef')
}

{
  const prev = [assistant({ content: 'a' })]
  const next = applyAgentEvent(prev, { type: 'delta', content: 'b' } as AgentEvent, ref)
  assert(next[0].content === 'ab', 'delta 应追加到 assistant 内容')
  assert(next !== prev, 'reducer 必须返回新数组')
}

{
  const prev = [assistant()]
  const afterCall = applyAgentEvent(prev, { type: 'tool_call', name: 'screen_stock_pool', args: { limit: 10 } } as AgentEvent, ref)
  const afterResult = applyAgentEvent(afterCall, {
    type: 'tool_result',
    name: 'screen_stock_pool',
    result: { pool_id: 'abc' },
    elapsed_ms: 12,
  } as AgentEvent, ref)
  const poolResult = afterResult[0].tools?.[0].result as { pool_id?: string } | undefined
  assert(afterResult[0].tools?.length === 1, 'tool_result 应回填同名未完成调用')
  assert(poolResult?.pool_id === 'abc', 'tool_result 应写入 result')
  assert(afterResult[0].tools?.[0].elapsed_ms === 12, 'tool_result 应写入耗时')
}

{
  const prev = [assistant({ content: 'ok' })]
  const next = applyAgentEvent(prev, { type: 'error', message: 'boom', elapsed_ms: 3 } as AgentEvent, ref)
  assert(next[0].content.includes('[错误] boom'), 'error 应追加中文错误标记')
}

{
  const prev = [assistant({ content: 'partial' })]
  const next = applyAgentEvent(prev, { type: 'cancelled' } as AgentEvent, ref)
  assert(next[0].content.endsWith('[已停止]'), 'cancelled 应追加已停止标记')
}

console.log('agentEvents.test.ts ok')
