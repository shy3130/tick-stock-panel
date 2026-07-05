import { useEffect, useRef, useState } from 'react'
import { Bot, Loader2, Send, Trash2, Wrench } from 'lucide-react'
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

export function Agent() {
  const [msgs, setMsgs] = useState<ChatMsg[]>(() => loadAgentChat())
  const [input, setInput] = useState('')
  const [profileId, setProfileId] = useState<string>()
  const [streaming, setStreaming] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (streaming) return
    saveAgentChat(msgs.map(m => ({ role: m.role, content: m.content })))
  }, [msgs, streaming])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [msgs, streaming])

  async function sendPrompt(prompt: string) {
    const text = prompt.trim()
    if (!text || streaming) return

    const fullHistory: AgentMsg[] = [
      ...msgs.map(m => ({ role: m.role, content: m.content })),
      { role: 'user', content: text },
    ]
    const history = fullHistory.slice(-50)
    setMsgs(prev => [...prev, { role: 'user', content: text }, { role: 'assistant', content: '', tools: [] }])
    setInput('')
    setStreaming(true)

    try {
      for await (const evt of api.agentStream(history, profileId)) {
        setMsgs(prev => {
          const lastIdx = prev.length - 1
          const last = prev[lastIdx]
          if (last?.role !== 'assistant') return prev

          const nextLast: ChatMsg = { ...last, tools: last.tools ? [...last.tools] : [] }
          if (evt.type === 'delta') {
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
          }
          const next = [...prev]
          next[lastIdx] = nextLast
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

  async function send() {
    await sendPrompt(input)
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
          <WelcomeScreen disabled={streaming} onExample={sendPrompt} />
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
              {m.content ? (
                m.content
              ) : m.role === 'assistant' && streaming && i === msgs.length - 1 ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" />
              ) : m.role === 'assistant' ? (
                <span className="text-muted">(无回复)</span>
              ) : null}
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-end gap-2 border-t border-border pt-3">
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
