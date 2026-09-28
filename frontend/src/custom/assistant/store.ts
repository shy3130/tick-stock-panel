/**
 * AI 助手前端状态 — 会话/消息/发送编排, useSyncExternalStore 单例 store。
 *
 * 后端无状态, 会话历史仅存 localStorage(最近 20 个会话); 每轮请求把可见的
 * user/assistant 消息回传, 由后端负责截断。工具往返(footprint)只用于本地
 * 展示, 不进入请求历史。
 */
import { useSyncExternalStore } from 'react'
import {
  assistantChatStream,
  decideAssistantAction,
  type AssistantChart,
  type AssistantContext,
  type AssistantEvent,
  type ChatHistoryMessage,
} from './client'

/**
 * running=执行中; awaiting=动作工具等待用户确认; approved/denied=用户已点
 * 确认/拒绝(短暂中间态, 后端 tool_result 随后到达并覆盖为 ok/error)。
 */
export type ToolCallStatus = 'running' | 'awaiting' | 'approved' | 'denied' | 'ok' | 'error'

export interface ToolCallConfirm {
  label: string
  risk: string
}

export interface ToolCallRecord {
  callId: string
  name: string
  args: Record<string, unknown>
  status: ToolCallStatus
  summary?: string
  elapsedMs?: number
  charts?: AssistantChart[]
  confirm?: ToolCallConfirm
}

export type ChatMessage =
  | { id: string; role: 'user'; content: string; ts: number }
  | { id: string; role: 'assistant'; content: string; ts: number; streaming: boolean }
  | { id: string; role: 'footprint'; calls: ToolCallRecord[]; ts: number }
  | { id: string; role: 'error'; kind: string; message: string; hint?: string; ts: number }
  | { id: string; role: 'notice'; content: string; ts: number }

export interface AssistantSession {
  id: string
  title: string
  createdAt: number
  messages: ChatMessage[]
}

interface AssistantState {
  sessions: AssistantSession[]
  activeId: string
  open: boolean
  sending: boolean
}

const STORAGE_KEY = 'assistant.sessions.v1'
const MAX_SESSIONS = 20
const MAX_SESSION_MESSAGES = 200
const MAX_INPUT_CHARS = 4000

let state: AssistantState = {
  sessions: [],
  activeId: '',
  open: false,
  sending: false,
}

const listeners = new Set<() => void>()
let abortController: AbortController | null = null
let pageContext: AssistantContext = {}

function emit() {
  for (const listener of listeners) listener()
}

function setState(partial: Partial<AssistantState>, persist = true) {
  state = { ...state, ...partial }
  if (persist) persistSessions()
  emit()
}

function persistSessions() {
  try {
    const trimmed = state.sessions
      .slice(0, MAX_SESSIONS)
      .map(session => ({ ...session, messages: session.messages.slice(-MAX_SESSION_MESSAGES) }))
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ sessions: trimmed, activeId: state.activeId }))
  } catch { /* 存储满/隐私模式时静默降级为内存态 */ }
}

function loadSessions() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return
    const parsed = JSON.parse(raw) as { sessions?: AssistantSession[]; activeId?: string }
    if (!Array.isArray(parsed.sessions)) return
    // 旧数据兼容: 只校验结构, 不校验具体消息形状(未知消息渲染为空)。
    const sessions = parsed.sessions.filter(s => s && typeof s.id === 'string' && Array.isArray(s.messages))
    const activeId = parsed.activeId && sessions.some(s => s.id === parsed.activeId)
      ? parsed.activeId
      : sessions[0]?.id ?? ''
    state = { ...state, sessions, activeId }
  } catch { /* 损坏数据视为无历史 */ }
}

function uid(): string {
  return Math.random().toString(36).slice(2, 10) + Date.now().toString(36)
}

export function activeSession(): AssistantSession | null {
  return state.sessions.find(s => s.id === state.activeId) ?? null
}

function ensureSession(): AssistantSession {
  let session = activeSession()
  if (!session) {
    session = { id: uid(), title: '新对话', createdAt: Date.now(), messages: [] }
    setState({ sessions: [session, ...state.sessions], activeId: session.id })
  }
  return session
}

function patchActiveSession(
  patch: (session: AssistantSession) => AssistantSession,
  { persist = true }: { persist?: boolean } = {},
) {
  setState({
    sessions: state.sessions.map(s => (s.id === state.activeId ? patch(s) : s)),
  }, persist)
}

function appendMessage(message: ChatMessage) {
  patchActiveSession(session => ({ ...session, messages: [...session.messages, message] }))
}

function activeMessages(): ChatMessage[] {
  return activeSession()?.messages ?? []
}

/** 发给后端的历史: 仅 user/assistant 且有内容(足迹/错误/提示不入上下文)。 */
function isTextMessage(m: ChatMessage): m is Extract<ChatMessage, { role: 'user' | 'assistant' }> {
  return (m.role === 'user' || m.role === 'assistant') && m.content.trim().length > 0
}

function buildHistory(messages: ChatMessage[]): ChatHistoryMessage[] {
  return messages
    .filter(isTextMessage)
    .map(m => ({ role: m.role, content: m.content }))
}

/** 一轮生成的完整编排: 建占位 → 流事件落库 → 终态收敛。 */
async function runGeneration() {
  const history = buildHistory(activeMessages())
  const assistantId = uid()
  const footprintId = uid()
  appendMessage({ id: footprintId, role: 'footprint', calls: [], ts: Date.now() })
  appendMessage({ id: assistantId, role: 'assistant', content: '', ts: Date.now(), streaming: true })
  setState({ sending: true })

  abortController = new AbortController()
  try {
    for await (const event of assistantChatStream(
      { messages: history, context: pageContext },
      abortController.signal,
    )) {
      applyEvent(event, footprintId, assistantId)
    }
  } catch (error) {
    const aborted = abortController.signal.aborted
    const message: ChatMessage = aborted
      ? { id: uid(), role: 'notice', content: '已中断, 可点击重试继续。', ts: Date.now() }
      : { id: uid(), role: 'error', kind: 'network', message: error instanceof Error ? error.message : String(error), ts: Date.now() }
    appendMessage(message)
  } finally {
    abortController = null
    patchActiveSession(session => ({
      ...session,
      messages: session.messages.map(m => (m.id === assistantId ? { ...m, streaming: false } : m)),
    }))
    setState({ sending: false })
  }
}

function applyEvent(event: AssistantEvent, footprintId: string, assistantId: string) {
  switch (event.type) {
    case 'tool_call':
      patchActiveSession(session => ({
        ...session,
        messages: session.messages.map(m => m.id === footprintId && m.role === 'footprint'
          ? {
              ...m,
              calls: [...m.calls, {
                callId: event.call_id,
                name: event.name,
                args: event.args ?? {},
                status: 'running' as ToolCallStatus,
              }],
            }
          : m),
      }))
      break
    case 'tool_result':
      patchActiveSession(session => ({
        ...session,
        messages: session.messages.map(m => m.id === footprintId && m.role === 'footprint'
          ? {
              ...m,
              calls: m.calls.map(c => c.callId === event.call_id
                ? {
                    ...c,
                    status: event.ok ? 'ok' as ToolCallStatus : 'error' as ToolCallStatus,
                    summary: event.summary,
                    elapsedMs: event.elapsed_ms,
                    charts: event.charts,
                  }
                : c),
            }
          : m),
      }))
      break
    case 'action_confirm':
      patchActiveSession(session => ({
        ...session,
        messages: session.messages.map(m => m.id === footprintId && m.role === 'footprint'
          ? {
              ...m,
              calls: m.calls.map(c => c.callId === event.call_id
                ? {
                    ...c,
                    status: 'awaiting' as ToolCallStatus,
                    confirm: { label: event.label, risk: event.risk },
                  }
                : c),
            }
          : m),
      }))
      break
    case 'rounds_confirm':
      // 轮次检查点: 以合成足迹记录承载「继续/停止」卡, 决策走同一端点。
      patchActiveSession(session => ({
        ...session,
        messages: session.messages.map(m => m.id === footprintId && m.role === 'footprint'
          ? {
              ...m,
              calls: [...m.calls, {
                callId: event.call_id,
                name: '__rounds',
                args: { reached: event.reached },
                status: 'awaiting' as ToolCallStatus,
                confirm: {
                  label: `已连续 ${event.reached} 轮工具调用仍未完成`,
                  risk: '继续将再运行一个周期(至多再 100 轮), 消耗更多时间与 token; 停止则保留已生成内容。',
                },
              }],
            }
          : m),
      }))
      break
    case 'delta':
      // 逐字流式高频到达: 只更新内存态, 由生成结束时的 setState({sending:false}) 统一落盘。
      patchActiveSession(session => ({
        ...session,
        messages: session.messages.map(m => (m.id === assistantId && m.role === 'assistant'
          ? { ...m, content: m.content + event.content }
          : m)),
      }), { persist: false })
      break
    case 'error':
      appendMessage({ id: uid(), role: 'error', kind: event.kind, message: event.message, hint: event.hint, ts: Date.now() })
      break
    case 'notice':
      appendMessage({ id: uid(), role: 'notice', content: event.message, ts: Date.now() })
      break
    case 'done':
      // 流结束: 仍处于 awaiting 的确认(后端超时未执行)收敛为已取消, 卡片不再悬挂。
      patchActiveSession(session => ({
        ...session,
        messages: session.messages.map(m => m.role === 'footprint'
          ? { ...m, calls: m.calls.map(c => (c.status === 'awaiting' ? { ...c, status: 'denied' as ToolCallStatus } : c)) }
          : m),
      }))
      break
    default:
      break
  }
}

// ===== 对外动作 =====

export function sendMessage(textRaw: string) {
  const text = textRaw.trim().slice(0, MAX_INPUT_CHARS)
  if (!text || state.sending) return
  const session = ensureSession()
  if (!session.messages.length) {
    patchActiveSession(s => ({ ...s, title: text.slice(0, 16) }))
  }
  appendMessage({ id: uid(), role: 'user', content: text, ts: Date.now() })
  void runGeneration()
}

/** 重试: 丢弃最后一个 user 消息之后的失败产物, 用同一上下文重新生成。 */
export function retryLast() {
  if (state.sending) return
  const messages = activeMessages()
  const lastUserIdx = findLastIndex(messages, m => m.role === 'user')
  if (lastUserIdx < 0) return
  patchActiveSession(session => ({ ...session, messages: session.messages.slice(0, lastUserIdx + 1) }))
  void runGeneration()
}

export function stopSending() {
  abortController?.abort()
}

export function openAssistant() {
  setState({ open: true }, false)
  ensureSession()
}

export function closeAssistant() {
  setState({ open: false }, false)
}

export function toggleAssistant() {
  if (state.open) closeAssistant()
  else openAssistant()
}

export function newSession() {
  if (state.sending) stopSending()
  const session: AssistantSession = { id: uid(), title: '新对话', createdAt: Date.now(), messages: [] }
  setState({ sessions: [session, ...state.sessions].slice(0, MAX_SESSIONS), activeId: session.id })
}

export function selectSession(id: string) {
  if (id === state.activeId) return
  if (state.sending) stopSending()
  setState({ activeId: id }, false)
}

export function deleteSession(id: string) {
  if (state.sending && id === state.activeId) stopSending()
  const sessions = state.sessions.filter(s => s.id !== id)
  const activeId = id === state.activeId ? sessions[0]?.id ?? '' : state.activeId
  setState({ sessions, activeId })
}

/** 确认卡按钮: POST 决策并立刻反馈; 后端随后以 tool_result 终态覆盖该记录。 */
export async function decideAction(callId: string, approve: boolean) {
  patchConfirmStatus(callId, approve ? 'approved' : 'denied')
  try {
    await decideAssistantAction(callId, approve)
  } catch {
    // 会话已断/确认过期: 本地收敛为拒绝态, 由 tool_result(或界面)呈现结果
    patchConfirmStatus(callId, 'denied')
  }
}

function patchConfirmStatus(callId: string, status: ToolCallStatus) {
  patchActiveSession(session => ({
    ...session,
    messages: session.messages.map(m => m.role === 'footprint'
      ? { ...m, calls: m.calls.map(c => (c.callId === callId ? { ...c, status } : c)) }
      : m),
  }))
}

/** 页面上下文由插槽组件上报(仅影响后续轮次), 用户可见于足迹中由后端回显。 */
export function setPageContext(context: AssistantContext) {
  pageContext = context
}

function findLastIndex<T>(items: T[], predicate: (item: T) => boolean): number {
  for (let i = items.length - 1; i >= 0; i -= 1) {
    if (predicate(items[i])) return i
  }
  return -1
}

// ===== React 绑定 =====

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function useAssistantStore(): AssistantState {
  return useSyncExternalStore(subscribe, () => state, () => state)
}

loadSessions()
