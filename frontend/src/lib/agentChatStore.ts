import type { AgentMsg } from '@/lib/api'

const KEY = 'agent_chat_history'

export function loadAgentChat(): AgentMsg[] {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return []
    const arr = JSON.parse(raw)
    return Array.isArray(arr) ? arr : []
  } catch {
    return []
  }
}

export function saveAgentChat(msgs: AgentMsg[]): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(msgs.slice(-50)))
  } catch {
    // ignore storage failures
  }
}

export function clearAgentChat(): void {
  try {
    localStorage.removeItem(KEY)
  } catch {
    // ignore storage failures
  }
}
