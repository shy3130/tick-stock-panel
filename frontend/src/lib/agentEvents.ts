import type { AgentEvent, AgentMsg, AgentToolTrace } from '@/lib/api'

export type ToolTrace = AgentToolTrace

export interface ChatMsg extends AgentMsg {
  tools?: ToolTrace[]
  elapsed_ms?: number
}

/**
 * Agent SSE 事件 reducer：把一条事件应用到当前消息列表，返回新列表（纯函数）。
 * 仅当最后一条是 assistant 气泡时生效；attempt_start 只写入 attemptIdRef。
 */
export function applyAgentEvent(prev: ChatMsg[], evt: AgentEvent, attemptIdRef: { current: string | null }): ChatMsg[] {
  const lastIdx = prev.length - 1
  const last = prev[lastIdx]
  if (last?.role !== 'assistant') return prev

  const nextLast: ChatMsg = { ...last, tools: last.tools ? [...last.tools] : [] }
  if (evt.type === 'attempt_start') {
    attemptIdRef.current = evt.attempt_id
  } else if (evt.type === 'delta') {
    nextLast.content += evt.content
  } else if (evt.type === 'tool_call') {
    nextLast.tools = [...(nextLast.tools ?? []), { name: evt.name, args: evt.args }]
  } else if (evt.type === 'tool_result') {
    const tools = [...(nextLast.tools ?? [])]
    let idx = -1
    for (let k = tools.length - 1; k >= 0; k--) {
      if (tools[k].name === evt.name && tools[k].result === undefined) { idx = k; break }
    }
    if (idx >= 0) {
      tools[idx] = { ...tools[idx], result: evt.result, elapsed_ms: evt.elapsed_ms }
    } else {
      tools.push({ name: evt.name, result: evt.result, elapsed_ms: evt.elapsed_ms })
    }
    nextLast.tools = tools
  } else if (evt.type === 'done') {
    nextLast.elapsed_ms = evt.elapsed_ms
  } else if (evt.type === 'error') {
    nextLast.content += `\n[错误] ${evt.message}`
    nextLast.elapsed_ms = evt.elapsed_ms
  } else if (evt.type === 'cancelled') {
    nextLast.content += nextLast.content ? '\n[已停止]' : '[已停止]'
  }
  const next = [...prev]
  next[lastIdx] = nextLast
  return next
}
