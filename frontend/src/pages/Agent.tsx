import { useEffect, useRef, useState } from 'react'
import { Loader2, Send, Trash2, Wrench } from 'lucide-react'
import { AiProviderSelector } from '@/components/AiProviderSelector'
import { PageHeader } from '@/components/PageHeader'
import { api, type AgentMsg } from '@/lib/api'
import { clearAgentChat, loadAgentChat, saveAgentChat } from '@/lib/agentChatStore'

interface ToolTrace {
  name: string
  args?: unknown
  result?: unknown
}

interface ChatMsg extends AgentMsg {
  tools?: ToolTrace[]
}

export function Agent() {
  const [msgs, setMsgs] = useState<ChatMsg[]>(() => loadAgentChat())
  const [input, setInput] = useState('')
  const [profileId, setProfileId] = useState<string>()
  const [streaming, setStreaming] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    saveAgentChat(msgs.map(m => ({ role: m.role, content: m.content })))
  }, [msgs])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [msgs, streaming])

  async function send() {
    const text = input.trim()
    if (!text || streaming) return

    const history: AgentMsg[] = [
      ...msgs.map(m => ({ role: m.role, content: m.content })),
      { role: 'user', content: text },
    ]
    setMsgs(prev => [...prev, { role: 'user', content: text }, { role: 'assistant', content: '', tools: [] }])
    setInput('')
    setStreaming(true)

    try {
      for await (const evt of api.agentStream(history, profileId)) {
        setMsgs(prev => {
          const next = [...prev]
          const last = next[next.length - 1]
          if (last?.role !== 'assistant') return prev

          if (evt.type === 'delta') last.content += evt.content
          else if (evt.type === 'tool_call') last.tools = [...(last.tools ?? []), { name: evt.name, args: evt.args }]
          else if (evt.type === 'tool_result') {
            const tools = [...(last.tools ?? [])]
            const index = tools.map(t => t.name).lastIndexOf(evt.name)
            if (index >= 0) tools[index] = { ...tools[index], result: evt.result }
            last.tools = tools
          } else if (evt.type === 'error') {
            last.content += `\n[错误] ${evt.message}`
          }
          return next
        })
      }
    } catch (e) {
      setMsgs(prev => {
        const next = [...prev]
        const last = next[next.length - 1]
        if (last?.role === 'assistant') last.content += `\n[请求失败] ${(e as Error).message}`
        return next
      })
    } finally {
      setStreaming(false)
    }
  }

  function clear() {
    clearAgentChat()
    setMsgs([])
  }

  return (
    <div className="flex h-[calc(100vh-3rem)] flex-col p-4">
      <div className="flex items-center justify-between gap-3">
        <PageHeader title="AI 助手" subtitle="多轮对话 · 可调用面板数据工具" />
        <div className="flex items-center gap-2">
          <AiProviderSelector entry="agent" value={profileId} onChange={setProfileId} />
          <button
            onClick={clear}
            className="flex h-8 items-center gap-1 rounded-btn bg-elevated px-2 text-xs text-muted hover:text-secondary"
          >
            <Trash2 className="h-3.5 w-3.5" />
            清空
          </button>
        </div>
      </div>

      <div ref={scrollRef} className="flex-1 space-y-3 overflow-auto py-3">
        {msgs.length === 0 && (
          <div className="pt-10 text-center text-xs text-muted">
            问点什么，比如"有哪些内置策略"或"看下 600519 最近走势"
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={m.role === 'user' ? 'flex justify-end' : 'flex justify-start'}>
            <div
              className={`max-w-[80%] rounded-card px-3 py-2 text-xs text-foreground whitespace-pre-wrap ${
                m.role === 'user' ? 'bg-accent/20' : 'border border-border bg-surface'
              }`}
            >
              {m.tools && m.tools.length > 0 && (
                <div className="mb-1.5 space-y-1">
                  {m.tools.map((t, j) => (
                    <details key={j} className="rounded bg-elevated/60 px-2 py-1">
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
              )}
              {m.content || (m.role === 'assistant' && streaming && i === msgs.length - 1 ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" />
              ) : null)}
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-end gap-2 border-t border-border pt-3">
        <textarea
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              send()
            }
          }}
          rows={2}
          placeholder="输入消息，Enter 发送 / Shift+Enter 换行"
          className="flex-1 resize-none rounded-input border border-border bg-elevated px-2.5 py-2 text-xs outline-none focus:border-accent/60"
        />
        <button
          disabled={streaming || !input.trim()}
          onClick={send}
          className="flex h-9 items-center gap-1.5 rounded-btn bg-accent/90 px-4 text-xs font-medium text-base hover:bg-accent disabled:opacity-40"
        >
          {streaming ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
          发送
        </button>
      </div>
    </div>
  )
}
