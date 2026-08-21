import { useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Bot, ChevronRight, Clock3, Download, Filter, LineChart, Loader2, Paperclip, Pencil, Plus, RotateCcw, Send, Square, Trash2, X, Wrench } from 'lucide-react'
import { AiProviderSelector } from '@/components/AiProviderSelector'
import { PageHeader } from '@/components/PageHeader'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { api, type AgentMsg, type AgentSession, type DocumentEnvelope } from '@/lib/api'
import { clearAgentChat, loadAgentChat, saveAgentChat } from '@/lib/agentChatStore'
import { applyAgentEvent, type ChatMsg, type ToolTrace } from '@/lib/agentEvents'
import { extractPoolCard } from '@/lib/agentPoolCard'
import { stageScreenerBacktestHandoff } from '@/lib/screenerBacktestHandoff'
import { toast } from '@/components/Toast'

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
      {
        title: '筛选池回测',
        prompt: '筛选近 30 日龙虎榜上榜至少 2 次且换手率大于 3% 的股票，保存股票池后列出可用策略供我选择回测。',
      },
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

function formatElapsed(ms?: number) {
  if (typeof ms !== 'number' || !Number.isFinite(ms)) return null
  if (ms < 1_000) return `${Math.round(ms)} ms`
  return `${(ms / 1_000).toFixed(ms < 10_000 ? 1 : 0)} 秒`
}

function ToolPayload({ label, value, pending = false }: { label: string; value?: unknown; pending?: boolean }) {
  return (
    <div className="min-w-0">
      <div className="mb-1 text-[10px] font-medium tracking-wide text-muted">{label}</div>
      <pre className="max-h-40 overflow-auto rounded-md bg-background/50 px-2.5 py-2 text-[10px] leading-relaxed text-muted whitespace-pre-wrap">
        {pending ? '执行中…' : JSON.stringify(value ?? {}, null, 2)}
      </pre>
    </div>
  )
}

function ToolTraceList({ tools, elapsedMs }: { tools?: ToolTrace[]; elapsedMs?: number }) {
  const navigate = useNavigate()
  if (!tools?.length) return null
  const totalElapsed = formatElapsed(elapsedMs)
  return (
    <details className="group mb-3 overflow-hidden rounded-input border border-border/80 bg-elevated/40">
      <summary className="flex cursor-pointer list-none items-center gap-1.5 px-2.5 py-2 text-[11px] text-muted hover:bg-elevated">
        <ChevronRight className="h-3.5 w-3.5 transition-transform group-open:rotate-90" />
        <Wrench className="h-3.5 w-3.5 text-accent" />
        <span className="font-medium text-secondary">工具调用链路</span>
        <span className="rounded-full bg-background/60 px-1.5 py-0.5 text-[10px]">{tools.length} 步</span>
        {totalElapsed && (
          <span className="ml-auto inline-flex items-center gap-1 tabular-nums">
            <Clock3 className="h-3 w-3" />
            总耗时 {totalElapsed}
          </span>
        )}
      </summary>
      <div className="divide-y divide-border/70 border-t border-border/70">
        {tools.map((tool, index) => {
          const toolElapsed = formatElapsed(tool.elapsed_ms)
          const pending = tool.result === undefined
          return (
            <section key={`${tool.name}-${index}`} className="px-2.5 py-2.5">
              <div className="mb-2 flex items-center gap-2">
                <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-accent/15 text-[9px] font-semibold text-accent">
                  {index + 1}
                </span>
                <code className="min-w-0 truncate text-[11px] font-medium text-secondary">{tool.name}</code>
                <span className="ml-auto shrink-0 tabular-nums text-[10px] text-muted">
                  {toolElapsed ?? (pending ? '执行中…' : '—')}
                </span>
              </div>
              <div className="grid gap-2 sm:grid-cols-2">
                <ToolPayload label="输入" value={tool.args} />
                <ToolPayload label="输出" value={tool.result} pending={pending} />
              </div>
              {tool.name === 'screen_stock_pool' && !pending && (() => {
                const card = extractPoolCard(tool.result)
                if (!card) return null
                const handlePoolBacktest = () => {
                  if (!card.previewSymbols.length) {
                    toast('预览为空，无法送回测', 'error')
                    return
                  }
                  const staged = stageScreenerBacktestHandoff({ target: 'strategy', symbols: card.previewSymbols, asOf: card.as_of })
                  if (!staged) {
                    toast('无法送入回测', 'error')
                    return
                  }
                  navigate('/backtest')
                }
                return (
                  <div className="mt-2 rounded-input border border-border bg-elevated/60 px-2.5 py-2">
                    <div className="flex items-center gap-2 text-[10px] text-muted">
                      <Filter className="h-3 w-3 shrink-0 text-accent" />
                      <span>服务端股票池 {card.total} 只 · as_of {card.as_of}</span>
                      <span className="ml-auto shrink-0 font-mono">{card.pool_id}</span>
                    </div>
                    <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                      <button type="button" onClick={() => navigate('/screener')}
                        className="inline-flex items-center gap-1 h-7 px-2 rounded-lg bg-elevated border border-border text-[10px] text-secondary hover:text-foreground">
                        <Filter className="h-3 w-3" />打开条件选股
                      </button>
                      <button type="button" onClick={handlePoolBacktest}
                        className="inline-flex items-center gap-1 h-7 px-2 rounded-lg bg-elevated border border-border text-[10px] text-secondary hover:text-foreground">
                        <LineChart className="h-3 w-3" />送回测
                      </button>
                    </div>
                    <p className="mt-1 text-[10px] leading-relaxed text-muted/70">
                      完整股票池不在模型上下文，按钮仅用预览（前 {card.previewSymbols.length} 只）。
                    </p>
                  </div>
                )
              })()}
            </section>
          )
        })}
      </div>
    </details>
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
        <div className="max-w-[80%] rounded-input bg-accent/15 px-3 py-2 text-xs text-foreground whitespace-pre-wrap">
          {msg.content}
        </div>
      </div>
    )
  }

  const failed = msg.content.includes('[错误]') || msg.content.includes('[请求失败]')
  return (
    <div className="flex justify-start gap-2">
      <AgentAvatar />
      <div className="panel max-w-[80%] px-3 py-2 text-xs text-foreground">
        <ToolTraceList tools={msg.tools} elapsedMs={msg.elapsed_ms} />
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
        <div className="mt-1 text-xs text-muted">只读研究：询问策略、行情、因子、组合，必要时调用面板工具。不荐股、不下单。</div>
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
                className="panel block w-full px-3 py-2 text-left hover:bg-elevated disabled:opacity-50"
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


export function Agent() {
  const [msgs, setMsgs] = useState<ChatMsg[]>(() => loadAgentChat())
  const [sessions, setSessions] = useState<AgentSession[]>([])
  const [sessionId, setSessionId] = useState<string>()
  const [input, setInput] = useState('')
  const [profileId, setProfileId] = useState<string>()
  const [streaming, setStreaming] = useState(false)
  const [attachment, setAttachment] = useState<DocumentEnvelope | null>(null)

  const [readingFile, setReadingFile] = useState(false)
  const [agentRuntime, setAgentRuntime] = useState<'python' | 'pi' | null>(null)
  const [searchParams, setSearchParams] = useSearchParams()
  const scrollRef = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const silentAbortRef = useRef<AbortController | null>(null)
  const attemptIdRef = useRef<string | null>(null)
  const watchSessionRef = useRef<string | null>(null)
  const urlSessionId = searchParams.get('session') || undefined


  useEffect(() => {
    void api.agentRuntime().then(r => setAgentRuntime(r.runtime)).catch(() => undefined)
  }, [])

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
    setMsgs(messages.map(m => ({
      role: m.role,
      content: m.content,
      tools: m.tool_traces,
      elapsed_ms: m.elapsed_ms,
    })))
  }

  async function reconnect(id: string) {
    abortLocalWatch(true)
    const { messages } = await api.agentSessionMessages(id)
    setSessionId(id)
    setSessionInUrl(id, true)
    setMsgs(messages.map(m => ({
      role: m.role,
      content: m.content,
      tools: m.tool_traces,
      elapsed_ms: m.elapsed_ms,
    })))
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
    const content = attachment?.text.trim()
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
    <div className="workspace-page h-[calc(100vh-3rem)]">
      <PageHeader
        title="AI 助手"
        subtitle={agentRuntime ? `多轮对话 · 可调用面板数据工具 · 运行时 ${agentRuntime}` : '多轮对话 · 可调用面板数据工具'}
        right={
          <div className="workspace-toolbar">
            <select
              value={sessionId ?? ''}
              onChange={e => {
                if (e.target.value) openSession(e.target.value)
                else clear()
              }}
              className="control max-w-40 text-xs md:hidden"
            >
              <option value="">本地草稿</option>
              {sessions.map(s => (
                <option key={s.session_id} value={s.session_id}>{s.title || s.session_id}</option>
              ))}
            </select>
            <button onClick={() => void newSession()} className="btn-secondary !h-8 text-xs">
              <Plus className="h-3.5 w-3.5" />
              新建
            </button>
            <button
              onClick={() => void renameCurrentSession()}
              disabled={!sessionId}
              className="btn-ghost !h-8 text-xs"
            >
              <Pencil className="h-3.5 w-3.5" />
              改名
            </button>
            <AiProviderSelector entry="agent" value={profileId} onChange={setProfileId} />
            <button
              onClick={exportMarkdown}
              disabled={msgs.length === 0}
              className="btn-ghost !h-8 text-xs"
            >
              <Download className="h-3.5 w-3.5" />
              导出
            </button>
            <button
              onClick={sessionId ? () => void deleteCurrentSession() : clear}
              disabled={!sessionId && msgs.length === 0}
              className="btn-ghost !h-8 text-xs"
            >
              <Trash2 className="h-3.5 w-3.5" />
              {sessionId ? '删除' : '清空'}
            </button>
          </div>
        }
      />

      <div className="flex min-h-0 flex-1 overflow-hidden">
        <aside className="hidden w-56 shrink-0 border-r border-border p-3 md:block">
          <button
            onClick={clear}
            className={`mb-2 flex w-full items-center justify-between rounded-btn px-2 py-2 text-left text-xs ${
              !sessionId ? 'bg-accent/15 text-foreground' : 'text-muted hover:bg-elevated hover:text-secondary'
            }`}
          >
            <span>本地草稿</span>
            {!sessionId && msgs.length > 0 && <span className="text-[10px] text-muted num">{msgs.length}</span>}
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
                  <span className="shrink-0 num">{s.message_count}</span>
                </div>
              </button>
            ))}
          </div>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-auto p-3">
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

          <div className="workspace-toolbar items-end border-t border-border p-3">
            <div className="min-w-0 flex-1">
              {attachment && (
                <div className="mb-2 max-w-full rounded-input border border-border bg-elevated px-2 py-1.5 text-xs text-muted">
                  <div className="flex items-center gap-2">
                    <Paperclip className="h-3.5 w-3.5 shrink-0" />
                    <span className="truncate">{attachment.title}</span>
                    <span className="shrink-0 text-[10px] num">{attachment.char_count} 字</span>
                    <button onClick={() => setAttachment(null)} className="btn-ghost ml-auto !h-auto shrink-0 !p-0.5">
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                  {attachment.warnings.length > 0 && (
                    <div className="mt-1 text-[10px] text-warning">
                      {attachment.text
                        ? attachment.warnings.join('；')
                        : '未提取到文本（可能是扫描件）；当前未启用 OCR。'}
                    </div>
                  )}
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
                className="control min-h-16 w-full resize-none !h-auto py-2 text-xs"
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
              className="btn-secondary"
            >
              {readingFile ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Paperclip className="h-3.5 w-3.5" />}
              附件
            </button>
            <button
              disabled={!streaming && !input.trim()}
              onClick={streaming ? cancelStream : send}
              className={streaming ? 'btn-secondary border-danger/30 text-danger hover:bg-danger/10' : 'btn-primary'}
            >
              {streaming ? <Square className="h-3.5 w-3.5" /> : <Send className="h-3.5 w-3.5" />}
              {streaming ? '停止' : '发送'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
