import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Bot, Download, Loader2, Paperclip, Pencil, Plus, RotateCcw, Send, Square, Trash2, X, Wrench } from 'lucide-react'
import { AiProviderSelector } from '@/components/AiProviderSelector'
import { PageHeader } from '@/components/PageHeader'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { api, type AgentEvent, type AgentMsg, type AgentSession, type DocumentEnvelope } from '@/lib/api'
import { clearAgentChat, loadAgentChat, saveAgentChat } from '@/lib/agentChatStore'

interface ToolTrace {
  name: string
  args?: unknown
  result?: unknown
}

interface ChatMsg extends AgentMsg {
  tools?: ToolTrace[]
}

interface ExampleItem {
  title: string
  prompt: string
}

interface ExampleCategory {
  label: string
  items: ExampleItem[]
}

const CAPABILITY_CHIPS = ['策略筛选', '因子分析', '组合优化', '回测验证', '只读数据工具']

const EXAMPLE_CATEGORIES: ExampleCategory[] = [
  {
    label: '策略与选股',
    items: [
      { title: '内置策略', prompt: '有哪些内置策略？分别适合什么行情？' },
      { title: '选股筛选', prompt: '帮我筛选连续放量、涨幅超5%的股票。' },
    ],
  },
  {
    label: '行情与市场',
    items: [
      { title: '个股走势', prompt: '看下 600519 最近走势和关键风险。' },
      { title: '市场概览', prompt: '总结今天市场概览，给出板块和情绪线索。' },
    ],
  },
  {
    label: '量化分析',
    items: [
      { title: '单因子分析', prompt: '分析一下 momentum_20d 这个因子最近半年的 IC 表现。' },
      { title: '多因子对比', prompt: '帮我对比一下 rsi_14、macd_hist 和 momentum_60d 这几个因子的 IC 表现，哪个更强。' },
      { title: '多因子合成', prompt: '把 rsi_14 和 macd_hist 按 IC 加权，给这些股票合成打分排名。' },
    ],
  },
  {
    label: '组合与回测',
    items: [
      { title: '组合优化', prompt: '用风险平价方法给这几只股票算组合权重。' },
      { title: '策略回测', prompt: '帮我跑个回测验证这个策略最近表现。' },
    ],
  },
]

function AgentAvatar() {
  return (
    <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-accent/15 text-accent">
      <Bot className="h-3.5 w-3.5" />
    </div>
  )
}

function ToolTraceList({ tools }: { tools?: ToolTrace[] }) {
  if (!tools?.length) return null
  return (
    <div className="mb-2 space-y-1">
      {tools.map((t, j) => (
        <details key={`${t.name}-${j}`} className="rounded bg-elevated/60 px-2 py-1">
          <summary className="flex cursor-pointer items-center gap-1 text-[11px] text-muted">
            <Wrench className="h-3 w-3" />
            {t.name}
          </summary>
          <pre className="mt-1 max-h-40 overflow-auto text-[10px] text-muted">
            {JSON.stringify(t.result ?? t.args, null, 2)}
          </pre>
        </details>
      ))}
    </div>
  )
}

function MessageBubble({
  msg,
  streaming,
  isLatest,
  onRetry,
}: {
  msg: ChatMsg
  streaming: boolean
  isLatest: boolean
  onRetry: () => void
}) {
  if (msg.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-card bg-accent/20 px-3 py-2 text-xs text-foreground whitespace-pre-wrap">
          {msg.content}
        </div>
      </div>
    )
  }

  const failed = msg.content.includes('[错误]') || msg.content.includes('[请求失败]')
  return (
    <div className="flex justify-start gap-2">
      <AgentAvatar />
      <div className="max-w-[80%] rounded-card border border-border bg-surface px-3 py-2 text-xs text-foreground">
        <ToolTraceList tools={msg.tools} />
        {msg.content ? (
          <MarkdownRenderer content={msg.content} />
        ) : streaming && isLatest ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" />
        ) : (
          <span className="text-muted">(无回复)</span>
        )}
        {failed && !streaming && (
          <button
            onClick={onRetry}
            className="mt-2 inline-flex items-center gap-1 rounded-btn bg-elevated px-2 py-1 text-[11px] text-muted hover:text-secondary"
          >
            <RotateCcw className="h-3 w-3" />
            重试
          </button>
        )}
      </div>
    </div>
  )
}

function WelcomeScreen({ disabled, onExample }: { disabled: boolean; onExample: (prompt: string) => void }) {
  return (
    <div className="flex min-h-[42vh] flex-col items-center justify-center gap-5 px-2 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-accent/15 text-accent">
        <Bot className="h-6 w-6" />
      </div>
      <div>
        <div className="text-sm font-semibold text-foreground">AI 助手</div>
        <div className="mt-1 text-xs text-muted">询问策略、行情、因子、组合，必要时会调用面板只读工具。</div>
      </div>
      <div className="flex flex-wrap justify-center gap-1.5">
        {CAPABILITY_CHIPS.map(label => (
          <span
            key={label}
            className="rounded-full border border-border bg-elevated px-2.5 py-1 text-[11px] text-muted"
          >
            {label}
          </span>
        ))}
      </div>
      <div className="grid w-full max-w-2xl grid-cols-1 gap-3 text-left sm:grid-cols-2">
        {EXAMPLE_CATEGORIES.map(cat => (
          <div key={cat.label} className="space-y-1.5">
            <div className="px-1 text-[11px] font-medium text-secondary">{cat.label}</div>
            {cat.items.map(ex => (
              <button
                key={ex.title}
                disabled={disabled}
                onClick={() => onExample(ex.prompt)}
                className="block w-full rounded-card border border-border bg-surface px-3 py-2 text-left hover:bg-elevated disabled:opacity-50"
              >
                <div className="text-xs font-medium text-foreground">{ex.title}</div>
                <div className="mt-1 text-[11px] leading-relaxed text-muted">{ex.prompt}</div>
              </button>
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}

function applyAgentEvent(prev: ChatMsg[], evt: AgentEvent, attemptIdRef: { current: string | null }): ChatMsg[] {
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
    if (idx >= 0) tools[idx] = { ...tools[idx], result: evt.result }
    nextLast.tools = tools
  } else if (evt.type === 'error') {
    nextLast.content += `\n[错误] ${evt.message}`
  } else if (evt.type === 'cancelled') {
    nextLast.content += nextLast.content ? '\n[已停止]' : '[已停止]'
  }
  const next = [...prev]
  next[lastIdx] = nextLast
  return next
}

export function Agent() {
  const [msgs, setMsgs] = useState<ChatMsg[]>(() => loadAgentChat())
  const [sessions, setSessions] = useState<AgentSession[]>([])
  const [sessionId, setSessionId] = useState<string>()
  const [input, setInput] = useState('')
  const [profileId, setProfileId] = useState<string>()
  const [streaming, setStreaming] = useState(false)
  const [attachment, setAttachment] = useState<DocumentEnvelope | null>(null)
  const [readingFile, setReadingFile] = useState(false)
  const [searchParams, setSearchParams] = useSearchParams()
  const scrollRef = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const silentAbortRef = useRef<AbortController | null>(null)
  const attemptIdRef = useRef<string | null>(null)
  const watchSessionRef = useRef<string | null>(null)
  const urlSessionId = searchParams.get('session') || undefined

  useEffect(() => {
    if (streaming || sessionId) return
    saveAgentChat(msgs.map(m => ({ role: m.role, content: m.content })))
  }, [msgs, sessionId, streaming])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [msgs, streaming])

  useEffect(() => {
    api.agentSessions()
      .then(({ sessions: rows }) => {
        setSessions(rows)
        if (!urlSessionId) return
        const match = rows.find(s => s.session_id === urlSessionId)
        if (!match) {
          setSearchParams({}, { replace: true })
        } else if (match.last_attempt_status === 'running') {
          void reconnect(urlSessionId)
        } else {
          void loadSession(urlSessionId, true)
        }
      })
      .catch(() => setSessions([]))
    // 初次进入时恢复 URL session（含在跑 attempt 的重连）；之后由显式切换动作维护。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!urlSessionId || urlSessionId === sessionId) return
    if (!sessions.some(s => s.session_id === urlSessionId)) return
    openSession(urlSessionId, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlSessionId, sessions, sessionId])

  function setSessionInUrl(id?: string, replace = false) {
    setSearchParams(id ? { session: id } : {}, { replace })
  }

  async function refreshSessions(selectId?: string) {
    const { sessions: rows } = await api.agentSessions()
    setSessions(rows)
    if (selectId) {
      setSessionId(selectId)
      setSessionInUrl(selectId)
    }
  }

  function abortLocalWatch(silent = true) {
    const ctrl = abortRef.current
    if (!ctrl) return
    if (silent) silentAbortRef.current = ctrl
    ctrl.abort()
  }

  async function loadSession(id: string, replaceUrl = false) {
    abortLocalWatch(true)
    const { messages } = await api.agentSessionMessages(id)
    setSessionId(id)
    setSessionInUrl(id, replaceUrl)
    setMsgs(messages.map(m => ({ role: m.role, content: m.content })))
  }

  async function reconnect(id: string) {
    abortLocalWatch(true)
    const { messages } = await api.agentSessionMessages(id)
    setSessionId(id)
    setSessionInUrl(id, true)
    setMsgs(messages.map(m => ({ role: m.role, content: m.content })))
    setStreaming(true)
    const ctrl = new AbortController()
    abortRef.current = ctrl
    watchSessionRef.current = id
    let bubbleAdded = false
    try {
      for await (const evt of api.agentWatch(id, ctrl.signal)) {
        if (watchSessionRef.current !== id) continue
        if (!bubbleAdded) {
          bubbleAdded = true
          setMsgs(prev => [...prev, { role: 'assistant', content: '', tools: [] }])
        }
        setMsgs(prev => applyAgentEvent(prev, evt, attemptIdRef))
      }
      void refreshSessions(id)
    } catch (e) {
      if ((e as Error).name === 'AbortError') return
      if (bubbleAdded) {
        setMsgs(prev => {
          const next = [...prev]
          const last = next[next.length - 1]
          if (last?.role === 'assistant') last.content += `\n[请求失败] ${(e as Error).message}`
          return next
        })
      }
    } finally {
      const isCurrentWatch = abortRef.current === ctrl && watchSessionRef.current === id
      if (abortRef.current === ctrl) abortRef.current = null
      if (silentAbortRef.current === ctrl) silentAbortRef.current = null
      if (isCurrentWatch) {
        watchSessionRef.current = null
        attemptIdRef.current = null
        setStreaming(false)
      }
    }
  }

  function openSession(id: string, replaceUrl = false) {
    const match = sessions.find(s => s.session_id === id)
    if (match?.last_attempt_status === 'running') {
      void reconnect(id)
    } else {
      void loadSession(id, replaceUrl)
    }
  }

  async function newSession() {
    abortLocalWatch(true)
    const s = await api.createAgentSession('新对话')
    setMsgs([])
    await refreshSessions(s.session_id)
  }

  async function sendPrompt(prompt: string) {
    const text = prompt.trim()
    if (!text || streaming) return
    const content = attachment
      ? `${text}\n\n## 用户附件（只读上下文）\n文件: ${attachment.title}\n类型: ${attachment.kind}\n\n${attachment.text}`
      : text
    let activeSessionId = sessionId
    if (!activeSessionId) {
      const s = await api.createAgentSession(text.slice(0, 50))
      activeSessionId = s.session_id
      setSessionId(activeSessionId)
      setSessionInUrl(activeSessionId)
      setSessions(prev => [s, ...prev])
    }
    const currentSessionId = activeSessionId

    const fullHistory: AgentMsg[] = [
      ...msgs.map(m => ({ role: m.role, content: m.content })),
      { role: 'user', content, display_content: text },
    ]
    const history = fullHistory.slice(-50)
    setMsgs(prev => [...prev, { role: 'user', content: text }, { role: 'assistant', content: '', tools: [] }])
    setInput('')
    setAttachment(null)
    setStreaming(true)
    const ctrl = new AbortController()
    abortRef.current = ctrl
    watchSessionRef.current = currentSessionId

    try {
      const { attempt_id } = await api.agentSend(currentSessionId, history, profileId)
      attemptIdRef.current = attempt_id
      for await (const evt of api.agentWatch(currentSessionId, ctrl.signal)) {
        if (watchSessionRef.current !== currentSessionId) continue
        setMsgs(prev => applyAgentEvent(prev, evt, attemptIdRef))
      }
      void refreshSessions(currentSessionId)
    } catch (e) {
      if ((e as Error).name === 'AbortError') {
        if (silentAbortRef.current === ctrl) return
        setMsgs(prev => {
          const next = [...prev]
          const last = next[next.length - 1]
          if (last?.role === 'assistant') last.content += last.content ? '\n[已停止]' : '[已停止]'
          return next
        })
        return
      }
      setMsgs(prev => {
        const next = [...prev]
        const last = next[next.length - 1]
        const message = (e as Error).message
        const busy = message.includes('仍在运行') || message.includes('already running')
        if (last?.role === 'assistant') last.content += `\n[请求失败] ${busy ? '上一轮回复仍在运行，请稍后重试' : message}`
        return next
      })
    } finally {
      const isCurrentWatch = abortRef.current === ctrl && watchSessionRef.current === currentSessionId
      if (abortRef.current === ctrl) abortRef.current = null
      if (silentAbortRef.current === ctrl) silentAbortRef.current = null
      if (isCurrentWatch) {
        watchSessionRef.current = null
        attemptIdRef.current = null
        setStreaming(false)
      }
    }
  }

  async function send() {
    await sendPrompt(input)
  }

  function clear() {
    abortLocalWatch(true)
    clearAgentChat()
    setMsgs([])
    setSessionId(undefined)
    setSessionInUrl(undefined)
  }

  function cancelStream() {
    const attemptId = attemptIdRef.current
    if (attemptId) void api.cancelAgentAttempt(attemptId).catch(() => undefined)
    abortLocalWatch(false)
  }

  async function attachFile(file: File) {
    setReadingFile(true)
    try {
      setAttachment(await api.readDocument(file))
    } finally {
      setReadingFile(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  async function renameCurrentSession() {
    if (!sessionId) return
    const current = sessions.find(s => s.session_id === sessionId)
    const title = window.prompt('会话名称', current?.title ?? '')
    if (!title?.trim()) return
    const updated = await api.renameAgentSession(sessionId, title.trim())
    setSessions(prev => prev.map(s => (s.session_id === updated.session_id ? updated : s)))
  }

  async function deleteCurrentSession() {
    if (!sessionId || !window.confirm('删除当前会话？')) return
    abortLocalWatch(true)
    await api.deleteAgentSession(sessionId)
    setSessionId(undefined)
    setMsgs([])
    setSessionInUrl(undefined)
    await refreshSessions()
  }

  function retryAt(index: number) {
    for (let i = index - 1; i >= 0; i--) {
      if (msgs[i]?.role === 'user') {
        void sendPrompt(msgs[i].content)
        return
      }
    }
  }

  function exportMarkdown() {
    if (!msgs.length) return
    const lines = [`# AI 助手对话`, '', `导出时间：${new Date().toLocaleString()}`, '']
    for (const msg of msgs) {
      lines.push(`## ${msg.role === 'user' ? '用户' : '助手'}`, '', msg.content || '(无回复)', '')
      if (msg.tools?.length) {
        lines.push('<details><summary>工具调用</summary>', '', '```json')
        lines.push(JSON.stringify(msg.tools, null, 2))
        lines.push('```', '', '</details>', '')
      }
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `agent-chat-${new Date().toISOString().slice(0, 10)}.md`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="flex h-[calc(100vh-3rem)] flex-col p-4">
      <div className="flex items-center justify-between gap-3">
        <PageHeader title="AI 助手" subtitle="多轮对话 · 可调用面板数据工具" />
        <div className="flex items-center gap-2">
            <select
              value={sessionId ?? ''}
              onChange={e => {
                if (e.target.value) openSession(e.target.value)
                else clear()
              }}
            className="h-8 max-w-40 rounded-input border border-border bg-elevated px-2 text-xs text-foreground md:hidden"
          >
            <option value="">本地草稿</option>
            {sessions.map(s => (
              <option key={s.session_id} value={s.session_id}>{s.title || s.session_id}</option>
            ))}
          </select>
          <button
            onClick={() => void newSession()}
            className="flex h-8 items-center gap-1 rounded-btn bg-elevated px-2 text-xs text-muted hover:text-secondary"
          >
            <Plus className="h-3.5 w-3.5" />
            新建
          </button>
          <button
            onClick={() => void renameCurrentSession()}
            disabled={!sessionId}
            className="flex h-8 items-center gap-1 rounded-btn bg-elevated px-2 text-xs text-muted hover:text-secondary disabled:opacity-40"
          >
            <Pencil className="h-3.5 w-3.5" />
            改名
          </button>
          <AiProviderSelector entry="agent" value={profileId} onChange={setProfileId} />
          <button
            onClick={exportMarkdown}
            disabled={msgs.length === 0}
            className="flex h-8 items-center gap-1 rounded-btn bg-elevated px-2 text-xs text-muted hover:text-secondary disabled:opacity-40"
          >
            <Download className="h-3.5 w-3.5" />
            导出
          </button>
          <button
            onClick={sessionId ? () => void deleteCurrentSession() : clear}
            disabled={!sessionId && msgs.length === 0}
            className="flex h-8 items-center gap-1 rounded-btn bg-elevated px-2 text-xs text-muted hover:text-secondary"
          >
            <Trash2 className="h-3.5 w-3.5" />
            {sessionId ? '删除' : '清空'}
          </button>
        </div>
      </div>

      <div className="mt-3 flex min-h-0 flex-1 overflow-hidden border-t border-border">
        <aside className="hidden w-56 shrink-0 border-r border-border py-3 pr-3 md:block">
          <button
            onClick={clear}
            className={`mb-2 flex w-full items-center justify-between rounded-btn px-2 py-2 text-left text-xs ${
              !sessionId ? 'bg-accent/15 text-foreground' : 'text-muted hover:bg-elevated hover:text-secondary'
            }`}
          >
            <span>本地草稿</span>
            {!sessionId && msgs.length > 0 && <span className="text-[10px] text-muted">{msgs.length}</span>}
          </button>
          <div className="space-y-1 overflow-auto">
            {sessions.map(s => (
                <button
                  key={s.session_id}
                  onClick={() => openSession(s.session_id)}
                className={`w-full rounded-btn px-2 py-2 text-left ${
                  s.session_id === sessionId
                    ? 'bg-accent/15 text-foreground'
                    : 'text-muted hover:bg-elevated hover:text-secondary'
                }`}
              >
                <div className="truncate text-xs font-medium">{s.title || s.session_id}</div>
                <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-muted">
                  <span className="truncate">{new Date(s.updated_at).toLocaleString()}</span>
                  <span className="shrink-0">{s.message_count}</span>
                </div>
              </button>
            ))}
          </div>
        </aside>

        <div ref={scrollRef} className="min-w-0 flex-1 space-y-3 overflow-auto py-3 md:pl-3">
          {msgs.length === 0 && (
            <WelcomeScreen disabled={streaming} onExample={sendPrompt} />
          )}
          {msgs.map((m, i) => (
            <MessageBubble
              key={i}
              msg={m}
              streaming={streaming}
              isLatest={i === msgs.length - 1}
              onRetry={() => retryAt(i)}
            />
          ))}
        </div>
      </div>

      <div className="flex items-end gap-2 border-t border-border pt-3">
        <div className="min-w-0 flex-1">
          {attachment && (
            <div className="mb-2 flex max-w-full items-center gap-2 rounded-btn border border-border bg-elevated px-2 py-1 text-xs text-muted">
              <Paperclip className="h-3.5 w-3.5 shrink-0" />
              <span className="truncate">{attachment.title}</span>
              <span className="shrink-0 text-[10px]">{attachment.char_count} 字</span>
              <button onClick={() => setAttachment(null)} className="ml-auto shrink-0 hover:text-secondary">
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          )}
          <textarea
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault()
                send()
              }
            }}
            rows={2}
            placeholder="输入消息，Enter 发送 / Shift+Enter 换行"
            className="w-full resize-none rounded-input border border-border bg-elevated px-2.5 py-2 text-xs outline-none focus:border-accent/60"
          />
        </div>
        <input
          ref={fileRef}
          type="file"
          className="hidden"
          accept=".txt,.md,.csv,.xlsx,.xls,.pdf"
          onChange={e => {
            const file = e.target.files?.[0]
            if (file) void attachFile(file)
          }}
        />
        <button
          disabled={streaming || readingFile}
          onClick={() => fileRef.current?.click()}
          className="flex h-9 items-center gap-1.5 rounded-btn bg-elevated px-3 text-xs text-muted hover:text-secondary disabled:opacity-40"
        >
          {readingFile ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Paperclip className="h-3.5 w-3.5" />}
          附件
        </button>
        <button
          disabled={!streaming && !input.trim()}
          onClick={streaming ? cancelStream : send}
          className={`flex h-9 items-center gap-1.5 rounded-btn px-4 text-xs font-medium disabled:opacity-40 ${
            streaming ? 'bg-danger/15 text-danger hover:bg-danger/20' : 'bg-accent/90 text-base hover:bg-accent'
          }`}
        >
          {streaming ? <Square className="h-3.5 w-3.5" /> : <Send className="h-3.5 w-3.5" />}
          {streaming ? '停止' : '发送'}
        </button>
      </div>
    </div>
  )
}
