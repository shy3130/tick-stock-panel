/**
 * AI 助手悬浮球 — 可拖动, 位置持久化(localStorage)。
 *
 * 交互约定: 按住移动超过阈值(6px)进入拖拽, 拖拽期间直接改 DOM style
 * (60fps 手法, 与仓库 AiReportBubble 一致), 松手才提交 state + 持久化;
 * 未超过阈值视为点击, 切换助手面板。生成中显示脉冲状态点。
 */
import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { motion } from 'framer-motion'
import { Sparkles } from 'lucide-react'
import { cn } from '@/lib/cn'
import { toggleAssistant, useAssistantStore } from '../store'

const POS_STORAGE_KEY = 'assistant.fab.pos.v1'
const SIZE = 44
const DRAG_THRESHOLD = 6

interface BallPos {
  x: number
  y: number
}

function clampPos(pos: BallPos): BallPos {
  const maxX = Math.max(0, window.innerWidth - SIZE)
  const maxY = Math.max(0, window.innerHeight - SIZE)
  return { x: Math.min(Math.max(pos.x, 0), maxX), y: Math.min(Math.max(pos.y, 0), maxY) }
}

function loadPos(): BallPos {
  try {
    const raw = localStorage.getItem(POS_STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as { x?: unknown; y?: unknown }
      if (typeof parsed.x === 'number' && typeof parsed.y === 'number') {
        return clampPos({ x: parsed.x, y: parsed.y })
      }
    }
  } catch { /* 存储不可用时用默认位置 */ }
  return { x: window.innerWidth - SIZE - 24, y: window.innerHeight - SIZE - 152 }
}

function savePos(pos: BallPos) {
  try {
    localStorage.setItem(POS_STORAGE_KEY, JSON.stringify(pos))
  } catch { /* 存储不可用时仅内存态 */ }
}

export function AssistantFloatingButton() {
  const { open, sending } = useAssistantStore()
  const [pos, setPos] = useState<BallPos>(() => loadPos())
  const ballRef = useRef<HTMLButtonElement>(null)
  const dragRef = useRef({ active: false, moved: false, offsetX: 0, offsetY: 0 })
  const suppressClickRef = useRef(false)

  // 视口变化后把悬浮球夹回可见区域。
  useEffect(() => {
    const onResize = () => setPos(prev => clampPos(prev))
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const onPointerDown = (event: React.PointerEvent<HTMLButtonElement>) => {
    const rect = ballRef.current?.getBoundingClientRect()
    if (!rect) return
    event.currentTarget.setPointerCapture(event.pointerId)
    dragRef.current = {
      active: true,
      moved: false,
      offsetX: event.clientX - rect.left,
      offsetY: event.clientY - rect.top,
    }
  }
  const onPointerMove = (event: React.PointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current
    if (!drag.active) return
    const el = ballRef.current
    if (!el) return
    const next = clampPos({ x: event.clientX - drag.offsetX, y: event.clientY - drag.offsetY })
    if (!drag.moved) {
      if (Math.abs(next.x - pos.x) + Math.abs(next.y - pos.y) <= DRAG_THRESHOLD) return
      drag.moved = true
    }
    // 拖拽期间直接改 DOM, 不触发 React 渲染。
    el.style.left = `${next.x}px`
    el.style.top = `${next.y}px`
  }
  const onPointerUp = () => {
    const drag = dragRef.current
    if (!drag.active) return
    drag.active = false
    if (!drag.moved) return
    suppressClickRef.current = true
    const rect = ballRef.current?.getBoundingClientRect()
    if (rect) {
      const final = clampPos({ x: rect.left, y: rect.top })
      setPos(final)
      savePos(final)
    }
  }
  const onClick = () => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false
      return
    }
    toggleAssistant()
  }

  // Portal 到 body: 挂载点在侧栏 aside 内, 移动端 aside 带 transform,
  // 会让 fixed 相对 aside 定位并被 overflow-hidden 裁剪。
  return createPortal(
    <motion.button
      ref={ballRef}
      type="button"
      initial={{ scale: 0.6, opacity: 0 }}
      animate={{ scale: 1, opacity: 1 }}
      transition={{ type: 'spring', stiffness: 400, damping: 26 }}
      whileHover={{ scale: 1.06 }}
      whileTap={{ scale: 0.94 }}
      onClick={onClick}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onLostPointerCapture={onPointerUp}
      title="AI 助手 (⌘K / Ctrl+K · 可拖动)"
      aria-label="AI 助手"
      style={{ left: pos.x, top: pos.y, width: SIZE, height: SIZE }}
      className={cn(
        // z-30 = 常驻入口层: 在页面内容之上, 但低于 z-40 遮罩与 z-50 弹窗 —
        // 弹窗打开时悬浮球被盖住, 不再悬浮遮挡弹窗内容 (与 AiConfigEntry 同层)。
        'fixed z-30 flex touch-none cursor-pointer items-center justify-center',
        'rounded-full border border-white/15 bg-gradient-to-br from-accent to-accent/80',
        'text-white shadow-[0_8px_24px_rgba(59,130,246,0.45)] backdrop-blur-sm',
        'transition-shadow duration-150 ease-smooth',
        open && 'ring-2 ring-accent/60 ring-offset-2 ring-offset-surface',
      )}
    >
      <Sparkles className="h-5 w-5" />
      {sending && (
        <span
          className="absolute -right-0.5 -top-0.5 h-3 w-3 animate-pulse rounded-full border-2 border-surface bg-warning"
          title="生成中"
        />
      )}
    </motion.button>,
    document.body,
  )
}
