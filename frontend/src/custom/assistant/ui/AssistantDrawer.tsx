/**
 * AI 助手面板 — 非模态, 从页面最右缘滑入(right: 0), 默认 720px 宽,
 * 左缘拖拽可调宽并持久化到 localStorage。
 *
 * 非模态是核心体验决策: 用户边看行情边问, 页面主体不遮挡、不解锁滚动。
 * 拖拽调宽沿用仓库 60fps 手法: 拖动期间直接改 DOM style.width, 松手才
 * 提交 React state + 持久化。视觉全部走设计令牌; 动画用全站统一缓动。
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { AnimatePresence, motion } from 'framer-motion'
import {
  ArrowDown,
  Gift,
  History,
  Plus,
  SendHorizontal,
  Settings2,
  Sparkles,
  Square,
  Trash2,
  X,
} from 'lucide-react'
import { cn } from '@/lib/cn'
import {
  fetchAssistantStatus,
  fetchAssistantSuggests,
  type AssistantStatus,
  type QuickSuggest,
} from '../client'
import {
  closeAssistant,
  deleteSession,
  newSession,
  retryLast,
  selectSession,
  sendMessage,
  stopSending,
  useAssistantStore,
} from '../store'
import { AssistantMessageView } from './messages'

const EASE_SMOOTH: [number, number, number, number] = [0.16, 1, 0.3, 1]

const WIDTH_STORAGE_KEY = 'assistant.width.v1'
const DEFAULT_WIDTH = 720
const MIN_WIDTH = 480

const RH_PROMO_URL = 'https://www.runninghub.ai/zh-cn/call-api/llm/models?source=github&inviteCode=edt5wh7c'
const RH_PROMO_DISMISS_KEY = 'assistant.rh_promo.dismissed_at.v1'
const RH_PROMO_SNOOZE_MS = 7 * 24 * 60 * 60 * 1000

function loadWidth(): number {
  try {
    const raw = localStorage.getItem(WIDTH_STORAGE_KEY)
    const value = raw ? Number.parseInt(raw, 10) : Number.NaN
    return Number.isFinite(value) && value >= MIN_WIDTH ? value : DEFAULT_WIDTH
  } catch {
    return DEFAULT_WIDTH
  }
}

function saveWidth(width: number) {
  try {
    localStorage.setItem(WIDTH_STORAGE_KEY, String(Math.round(width)))
  } catch { /* 存储不可用时仅内存态 */ }
}

function clampWidth(width: number): number {
  // 右缘贴齐, 至少给页面主体留 64px 上下文。
  const maxWidth = Math.max(MIN_WIDTH, window.innerWidth - 64)
  return Math.min(Math.max(width, MIN_WIDTH), maxWidth)
}

export function AssistantDrawer() {
  const { open, sending, sessions, activeId } = useAssistantStore()
  const active = sessions.find(s => s.id === activeId) ?? null
  const messages = active?.messages ?? []

  return createPortal(
    <AnimatePresence>
      {open && (
        <DrawerPanel key="assistant-drawer" sending={sending} messages={messages} />
      )}
    </AnimatePresence>,
    document.body,
  )
}

type DrawerMessages = ReturnType<typeof useAssistantStore>['sessions'][number]['messages']

function DrawerPanel({
  sending,
  messages,
}: {
  sending: boolean
  messages: DrawerMessages
}) {
  const { sessions, activeId } = useAssistantStore()
  const [status, setStatus] = useState<AssistantStatus | null>(null)
  const [suggests, setSuggests] = useState<QuickSuggest[]>([])
  const [width, setWidth] = useState(() => clampWidth(loadWidth()))
  const panelRef = useRef<HTMLElement>(null)
  const draggingRef = useRef(false)

  useEffect(() => {
    let cancelled = false
    fetchAssistantStatus().then(s => { if (!cancelled) setStatus(s) }).catch(() => {})
    fetchAssistantSuggests().then(list => { if (!cancelled) setSuggests(list) }).catch(() => {})
    return () => { cancelled = true }
  }, [])

  // 窗口缩放后重新夹取宽度, 保证面板不越出视口。
  useEffect(() => {
    const onResize = () => setWidth(prev => clampWidth(prev))
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // 拖拽期间直接改 DOM(60fps), 松手才提交 state + 持久化。
  const onHandlePointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault()
    event.currentTarget.setPointerCapture(event.pointerId)
    draggingRef.current = true
  }
  const onHandlePointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!draggingRef.current) return
    const el = panelRef.current
    if (el) el.style.width = `${clampWidth(window.innerWidth - event.clientX)}px`
  }
  const onHandlePointerEnd = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!draggingRef.current) return
    draggingRef.current = false
    const finalWidth = clampWidth(window.innerWidth - event.clientX)
    setWidth(finalWidth)
    saveWidth(finalWidth)
  }

  return (
    <motion.aside
      ref={panelRef}
      initial={{ x: 32, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      exit={{ x: 32, opacity: 0 }}
      transition={{ duration: 0.26, ease: EASE_SMOOTH }}
      style={{ right: 0, width }}
      className={cn(
        'fixed inset-y-0 z-[60] flex min-w-0 flex-col border-l border-border',
        'bg-surface/95 shadow-2xl backdrop-blur-xl',
      )}
      role="complementary"
      aria-label="AI 助手"
    >
      <DrawerHeader status={status} sessions={sessions} activeId={activeId} sending={sending} />
      <MessageList messages={messages} sending={sending} status={status} suggests={suggests} />
      <InputArea sending={sending} blocked={status ? !status.supports_tools : false} />

      {/* 左缘拖拽调宽 */}
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="调整宽度"
        onPointerDown={onHandlePointerDown}
        onPointerMove={onHandlePointerMove}
        onPointerUp={onHandlePointerEnd}
        onLostPointerCapture={onHandlePointerEnd}
        className="group/handle absolute inset-y-0 left-0 z-10 w-1.5 cursor-col-resize touch-none"
        title="拖拽调整宽度"
      >
        <span className="absolute left-1/2 top-1/2 h-10 w-[3px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-accent/50 opacity-0 transition-opacity duration-150 ease-smooth group-hover/handle:opacity-100" />
      </div>
    </motion.aside>
  )
}

/** RunningHub 推广条: 嵌在抽屉标题行中间, 单行跑马灯横向滚动(悬停暂停),
 * 琥珀色高亮(深浅主题各取一档), 可点击跳转邀请链接。
 * 点 X 后一周(7 天)内不再显示 —— 时间戳持久化到 localStorage; DrawerPanel 随抽屉
 * 开合挂载/卸载, 每次点开对话框重新检查是否已到期, 到期后自动恢复。 */
function RhPromoBanner() {
  const [dismissed, setDismissed] = useState(() => {
    try {
      const ts = Number(localStorage.getItem(RH_PROMO_DISMISS_KEY))
      return Number.isFinite(ts) && Date.now() - ts < RH_PROMO_SNOOZE_MS
    } catch { return false }
  })
  if (dismissed) return null
  const snooze = () => {
    setDismissed(true)
    try { localStorage.setItem(RH_PROMO_DISMISS_KEY, String(Date.now())) } catch { /* 存储不可用时仅本次关闭 */ }
  }
  const copy = (key: string) => (
    <a
      key={key}
      href={RH_PROMO_URL}
      target="_blank"
      rel="noreferrer"
      className="flex shrink-0 items-center gap-1 pr-8 text-[10px] leading-relaxed text-secondary transition-colors hover:text-foreground"
    >
      <span className="font-medium text-amber-600 dark:text-amber-400">Claude、ChatGPT、Gemini</span>
      <span>等国际模型直连稳定不掉线，</span>
      <span className="font-medium text-amber-600 dark:text-amber-400">最高优惠 80%</span>
      <span>，</span>
      <span className="font-medium text-amber-600 dark:text-amber-400">通过此链接注册赠送 1000 RH 积分</span>
      <span>🎁</span>
    </a>
  )
  // 窗口(overflow-hidden)裁剪滚动内容, 礼包图标与 X 按钮在窗口外, 永不被文字盖住;
  // 轨道 w-max 按内容撑宽, 份数为偶数时 translateX(-50%) 恰好位移整份文案宽度,
  // 循环回跳视觉无缝。4 份保证极宽抽屉下窗口内也始终有内容。
  return (
    <div className="group flex min-w-0 flex-1 items-center gap-1.5 rounded-btn border border-amber-400/30 bg-amber-400/15 py-1 pl-2 pr-1 dark:border-amber-400/25 dark:bg-amber-400/10">
      <Gift className="h-3 w-3 shrink-0 text-amber-600 dark:text-amber-400" />
      <div className="min-w-0 flex-1 overflow-hidden">
        <div className="flex w-max animate-[rh-marquee_26s_linear_infinite] whitespace-nowrap motion-reduce:animate-none group-hover:[animation-play-state:paused]">
          {['a', 'b', 'c', 'd'].map(copy)}
        </div>
      </div>
      <button
        onClick={snooze}
        title="关闭推广 (一周内不再显示)"
        aria-label="关闭推广 (一周内不再显示)"
        className="shrink-0 rounded-btn p-0.5 text-muted transition-colors hover:bg-elevated hover:text-foreground"
      >
        <X className="h-3 w-3" />
      </button>
    </div>
  )
}

function DrawerHeader({
  status,
  sessions,
  activeId,
  sending,
}: {
  status: AssistantStatus | null
  sessions: ReturnType<typeof useAssistantStore>['sessions']
  activeId: string
  sending: boolean
}) {
  const [menuOpen, setMenuOpen] = useState(false)

  return (
    <div className="relative flex h-14 shrink-0 items-center gap-2 border-b border-border px-4">
      <Sparkles className="h-4 w-4 shrink-0 text-accent" />
      <span className="shrink-0 text-sm font-semibold text-foreground">AI 助手</span>
      {status?.model && (
        <span
          className="min-w-0 truncate rounded-btn bg-elevated px-1.5 py-0.5 font-mono text-[10px] text-muted"
          title={`模型: ${status.model}\n供应商: ${status.provider ?? '-'}`}
        >
          {status.model}
        </span>
      )}
      <RhPromoBanner />
      <div className="ml-auto flex shrink-0 items-center gap-0.5">
        <div className="relative">
          <IconButton title="历史会话" onClick={() => setMenuOpen(v => !v)} disabled={sending}>
            <History className="h-4 w-4" />
          </IconButton>
          {menuOpen && <SessionMenu sessions={sessions} activeId={activeId} onDone={() => setMenuOpen(false)} />}
        </div>
        <IconButton title="新对话" onClick={() => { newSession(); setMenuOpen(false) }} disabled={sending}>
          <Plus className="h-4 w-4" />
        </IconButton>
        <IconButton title="关闭 (Esc)" onClick={closeAssistant}>
          <X className="h-4 w-4" />
        </IconButton>
      </div>
    </div>
  )
}

function SessionMenu({
  sessions,
  activeId,
  onDone,
}: {
  sessions: ReturnType<typeof useAssistantStore>['sessions']
  activeId: string
  onDone: () => void
}) {
  return (
    <>
      <div className="fixed inset-0 z-[61]" onClick={onDone} />
      <div className="absolute right-0 top-full z-[62] mt-1 max-h-72 w-60 overflow-y-auto rounded-card border border-border bg-surface p-1 shadow-xl">
        {sessions.length === 0 && (
          <div className="px-2 py-1.5 text-xs text-muted">暂无历史会话</div>
        )}
        {sessions.map(session => (
          <div
            key={session.id}
            className={cn(
              'group flex items-center gap-1 rounded-btn px-2 py-1.5 text-xs transition-colors duration-150 ease-smooth',
              session.id === activeId ? 'bg-elevated text-foreground' : 'text-secondary hover:bg-elevated/60 hover:text-foreground',
            )}
          >
            <button
              type="button"
              className="min-w-0 flex-1 cursor-pointer truncate text-left"
              onClick={() => { selectSession(session.id); onDone() }}
              title={session.title}
            >
              {session.title || '新对话'}
            </button>
            <button
              type="button"
              className="hidden cursor-pointer text-muted transition-colors duration-150 ease-smooth hover:text-danger group-hover:block"
              onClick={() => deleteSession(session.id)}
              title="删除会话"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        ))}
      </div>
    </>
  )
}

function IconButton({
  title,
  onClick,
  disabled,
  children,
}: {
  title: string
  onClick: () => void
  disabled?: boolean
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      disabled={disabled}
      onClick={onClick}
      className="flex h-8 w-8 cursor-pointer items-center justify-center rounded-btn text-secondary transition-colors duration-150 ease-smooth hover:bg-elevated hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
    >
      {children}
    </button>
  )
}

function MessageList({
  messages,
  sending,
  status,
  suggests,
}: {
  messages: DrawerMessages
  sending: boolean
  status: AssistantStatus | null
  suggests: QuickSuggest[]
}) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const pinnedRef = useRef(true)
  const [showJump, setShowJump] = useState(false)

  useLayoutEffect(() => {
    const el = scrollRef.current
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight
  }, [messages])

  const onScroll = () => {
    const el = scrollRef.current
    if (!el) return
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight
    pinnedRef.current = distance < 80
    setShowJump(distance >= 240)
  }

  const jumpToBottom = () => {
    const el = scrollRef.current
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }

  return (
    <div className="relative min-h-0 flex-1">
      <div ref={scrollRef} onScroll={onScroll} className="h-full space-y-4 overflow-y-auto px-5 py-4">
        {messages.length === 0 ? (
          <EmptyState status={status} suggests={suggests} />
        ) : (
          messages.map(message => (
            <AssistantMessageView key={message.id} message={message} onRetry={sending ? undefined : retryLast} />
          ))
        )}
      </div>
      <AnimatePresence>
        {showJump && (
          <motion.button
            type="button"
            initial={{ opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 4 }}
            transition={{ duration: 0.15, ease: EASE_SMOOTH }}
            onClick={jumpToBottom}
            className="absolute bottom-3 left-1/2 flex h-7 w-7 -translate-x-1/2 cursor-pointer items-center justify-center rounded-full border border-border bg-elevated text-secondary shadow-md transition-colors duration-150 ease-smooth hover:text-foreground"
            title="回到底部"
          >
            <ArrowDown className="h-3.5 w-3.5" />
          </motion.button>
        )}
      </AnimatePresence>
    </div>
  )
}

function EmptyState({ status, suggests }: { status: AssistantStatus | null; suggests: QuickSuggest[] }) {
  const navigate = useNavigate()

  if (status && !status.configured) {
    return (
      <div className="rounded-card border border-warning/30 bg-warning/5 p-4 text-sm">
        <div className="flex items-center gap-1.5 font-medium text-warning">
          <Settings2 className="h-4 w-4" />
          AI 未配置
        </div>
        <p className="mt-1.5 text-secondary">AI 助手需要先配置 AI 供应商和 API Key 才能对话。</p>
        <button
          type="button"
          onClick={() => navigate('/settings?tab=ai')}
          className="mt-2 cursor-pointer rounded-btn bg-accent px-3 py-1.5 text-xs text-white transition-colors duration-150 ease-smooth hover:bg-accent/90"
        >
          去设置页配置
        </button>
      </div>
    )
  }

  if (status && !status.supports_tools) {
    return (
      <div className="rounded-card border border-warning/30 bg-warning/5 p-4 text-sm">
        <div className="flex items-center gap-1.5 font-medium text-warning">
          <Settings2 className="h-4 w-4" />
          当前供应商不支持工具调用
        </div>
        <p className="mt-1.5 text-secondary">
          AI 助手依赖工具调用能力({status.provider} 不支持), 请在设置页切换为 OpenAI 兼容模型。
        </p>
        <button
          type="button"
          onClick={() => navigate('/settings?tab=ai')}
          className="mt-2 cursor-pointer rounded-btn bg-accent px-3 py-1.5 text-xs text-white transition-colors duration-150 ease-smooth hover:bg-accent/90"
        >
          去设置页调整
        </button>
      </div>
    )
  }

  if (!suggests.length) {
    return <p className="pt-8 text-center text-xs text-muted">问我任何关于策略、因子、回测或数据能力的问题。</p>
  }

  return (
    <div className="space-y-3 pt-6">
      <p className="text-center text-xs text-muted">试试这些:</p>
      <div className="grid grid-cols-1 gap-2">
        {suggests.map(suggest => (
          <button
            key={suggest.id}
            type="button"
            onClick={() => sendMessage(suggest.prompt)}
            className="cursor-pointer rounded-card border border-border bg-base/60 px-3 py-2.5 text-left text-xs text-secondary transition-all duration-150 ease-smooth hover:border-accent/40 hover:bg-elevated hover:text-foreground"
          >
            {suggest.label}
          </button>
        ))}
      </div>
    </div>
  )
}

function InputArea({ sending, blocked }: { sending: boolean; blocked: boolean }) {
  const [text, setText] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const canSend = !sending && !blocked && text.trim().length > 0

  const submit = () => {
    if (!canSend) return
    sendMessage(text)
    setText('')
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      submit()
    }
  }

  const autoGrow = () => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 144)}px`
  }

  return (
    <div className="shrink-0 border-t border-border bg-surface px-4 pb-3 pt-2.5">
      <div className="flex items-end gap-2 rounded-card border border-border bg-base focus-within:border-accent/50">
        <textarea
          ref={textareaRef}
          value={text}
          rows={1}
          disabled={blocked}
          placeholder={blocked ? '请先在设置页配置 AI' : '提问, Enter 发送 / Shift+Enter 换行'}
          onChange={e => { setText(e.target.value); autoGrow() }}
          onKeyDown={onKeyDown}
          className="max-h-36 min-h-[38px] flex-1 resize-none bg-transparent px-3 py-2 text-sm text-foreground outline-none placeholder:text-muted disabled:cursor-not-allowed"
        />
        {sending ? (
          <button
            type="button"
            onClick={stopSending}
            title="停止生成"
            aria-label="停止生成"
            className="mr-1.5 mb-1.5 flex h-8 w-8 cursor-pointer items-center justify-center rounded-btn bg-elevated text-secondary transition-colors duration-150 ease-smooth hover:text-foreground"
          >
            <Square className="h-3.5 w-3.5 fill-current" />
          </button>
        ) : (
          <button
            type="button"
            onClick={submit}
            disabled={!canSend}
            title="发送"
            aria-label="发送"
            className="mr-1.5 mb-1.5 flex h-8 w-8 cursor-pointer items-center justify-center rounded-btn bg-accent text-white transition-all duration-150 ease-smooth hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <SendHorizontal className="h-4 w-4" />
          </button>
        )}
      </div>
      <div className="mt-1.5 px-1 text-[10px] text-muted">
        ⌘K / Ctrl+K 呼出 · 左缘可拖拽调宽 · 回答基于本地数据, 不构成投资建议
      </div>
    </div>
  )
}
