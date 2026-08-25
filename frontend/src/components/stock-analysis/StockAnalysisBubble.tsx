import { useState, useRef, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Loader2, Check, AlertCircle, X } from 'lucide-react'
import { useBubbleTasks, restoreDialog, cancelAnalysis } from '@/lib/stockAnalysisStore'
import type { ActiveTask } from '@/lib/stockAnalysisStore'

/**
 * AI 个股分析任务全局气泡 —— 与财务分析胶囊并列,蓝色主题区分。
 * 挂在右侧,拖拽丝滑(逻辑同 AiReportBubble,独立状态池)。
 *
 * 与财务胶囊的差异:蓝色系 + "个股分析中"文案,避免与财务分析混淆。
 */

const BUBBLE_W = 148
const EDGE_MARGIN = 12

export function StockAnalysisBubble() {
  const activeTasks = useBubbleTasks()
  const containerRef = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ x: number; y: number }>(() => loadPos())

  const draggingRef = useRef(false)
  const dragData = useRef({ mx: 0, my: 0, ox: 0, oy: 0 })
  const movedRef = useRef(false)
  const clickTargetRef = useRef<(() => void) | null>(null)

  const applyTransform = useCallback((x: number, y: number) => {
    const el = containerRef.current
    if (el) el.style.transform = `translate3d(${x}px, ${y}px, 0)`
  }, [])

  const clamp = useCallback((x: number, y: number) => {
    const maxX = window.innerWidth - BUBBLE_W - EDGE_MARGIN
    const maxY = window.innerHeight - 80
    return {
      x: Math.max(EDGE_MARGIN, Math.min(maxX, x)),
      y: Math.max(EDGE_MARGIN, Math.min(maxY, y)),
    }
  }, [])

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    draggingRef.current = true
    movedRef.current = false
    dragData.current = { mx: e.clientX, my: e.clientY, ox: pos.x, oy: pos.y }
    const el = containerRef.current
    if (el) el.classList.add('dragging')
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  }, [pos.x, pos.y])

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    if (!draggingRef.current) return
    const dx = e.clientX - dragData.current.mx
    const dy = e.clientY - dragData.current.my
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) movedRef.current = true
    const c = clamp(dragData.current.ox + dx, dragData.current.oy + dy)
    applyTransform(c.x, c.y)
  }, [clamp, applyTransform])

  const onPointerUp = useCallback(() => {
    if (!draggingRef.current) return
    draggingRef.current = false
    const el = containerRef.current
    if (el) el.classList.remove('dragging')
    if (movedRef.current) {
      setPos(prev => {
        const transform = el?.style.transform ?? ''
        const m = transform.match(/translate3d\(([-\d.]+)px,\s*([-\d.]+)px/)
        const finalPos = m ? { x: parseFloat(m[1]), y: parseFloat(m[2]) } : prev
        savePos(finalPos)
        return finalPos
      })
    } else {
      const fn = clickTargetRef.current
      clickTargetRef.current = null
      fn?.()
    }
  }, [])

  useEffect(() => {
    const onResize = () => {
      setPos(prev => {
        const c = clamp(prev.x, prev.y)
        if (c.x !== prev.x || c.y !== prev.y) {
          applyTransform(c.x, c.y)
          return c
        }
        return prev
      })
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [clamp, applyTransform])

  useEffect(() => { applyTransform(pos.x, pos.y) }, [pos.x, pos.y, applyTransform])

  if (activeTasks.length === 0) return null

  // 默认位置偏下,避免与财务胶囊(右下)重叠
  return (
    <div
      ref={containerRef}
      className="sa-bubble-root fixed z-[60] select-none cursor-grab active:cursor-grabbing"
      style={{
        width: `${BUBBLE_W}px`,
        transform: `translate3d(${pos.x}px, ${pos.y}px, 0)`,
        touchAction: 'none',
        transition: 'transform 0.2s cubic-bezier(0.16, 1, 0.3, 1)',
      }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
    >
      <AnimatePresence mode="popLayout">
        {activeTasks.map((task, i) => (
          <BubbleItem
            key={task.id}
            task={task}
            isLast={i === activeTasks.length - 1}
            onPointerDown={() => { clickTargetRef.current = () => restoreDialog(task.id) }}
          />
        ))}
      </AnimatePresence>
      <style>{`.sa-bubble-root.dragging { transition: none !important; }`}</style>
    </div>
  )
}

function BubbleItem({ task, isLast, onPointerDown }: {
  task: ActiveTask
  isLast: boolean
  onPointerDown: () => void
}) {
  const isWorking = task.phase === 'loading' || task.phase === 'streaming'
  const isError = task.phase === 'error'
  const isCancelled = task.phase === 'cancelled'

  // 蓝色系(区别于财务分析的紫色)
  const accent = isWorking
    ? 'from-sky-500/25 to-blue-500/20 text-sky-300 border-sky-300/40 shadow-[0_6px_24px_-10px_rgba(14,165,233,0.5)]'
    : isError
      ? 'from-red-500/20 to-red-500/10 text-red-300 border-red-300/40 shadow-[0_6px_20px_-10px_rgba(239,68,68,0.4)]'
      : isCancelled
        ? 'from-muted/20 to-muted/10 text-secondary border-border/40'
        : 'from-emerald-500/20 to-emerald-500/10 text-emerald-300 border-emerald-300/40 shadow-[0_6px_20px_-10px_rgba(16,185,129,0.35)]'

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.9, y: -8 }}
      animate={{ opacity: 1, scale: 1, y: 0 }}
      exit={{ opacity: 0, scale: 0.9, y: -8 }}
      transition={{ type: 'spring', damping: 22, stiffness: 300 }}
      className={isLast ? '' : 'mb-1.5'}
    >
      <div
        onPointerDown={onPointerDown}
        role="button"
        tabIndex={0}
        title={isWorking ? '个股分析中,点击恢复;点 × 取消' : isError ? '分析失败,点击重试' : isCancelled ? '已取消,点击查看' : '点击查看个股分析报告'}
        className={`group relative flex w-full cursor-pointer items-center gap-1.5 overflow-hidden rounded-lg border bg-gradient-to-br px-2 py-1.5 backdrop-blur-xl transition-all duration-200 hover:scale-[1.02] active:scale-[0.99] ${accent}`}
      >
        {isWorking && (
          <div className="absolute inset-x-0 top-0 h-px overflow-hidden">
            <div className="h-full w-1/2 bg-gradient-to-r from-transparent via-sky-200 to-transparent animate-sa-bubble-progress" />
          </div>
        )}
        <span className="flex h-4 w-4 items-center justify-center shrink-0">
          {isWorking ? <Loader2 className="h-3 w-3 animate-spin" />
            : isError ? <AlertCircle className="h-3 w-3" />
            : isCancelled ? <X className="h-3 w-3" />
            : <Check className="h-3 w-3" />}
        </span>
        <span className="flex-1 min-w-0 text-[11px] font-medium text-foreground leading-none truncate">
          {task.name || task.symbol}
        </span>
        <span className="shrink-0 text-[9px] leading-none">
          {isWorking ? <span className="text-sky-300/80">个股分析</span>
            : isError ? <span className="text-red-300/80">失败</span>
            : isCancelled ? <span className="text-secondary/80">已取消</span>
            : <span className="text-emerald-300/80">点击查看</span>}
        </span>
        {isWorking && (
          <button
            type="button"
            aria-label="取消分析"
            className="shrink-0 grid h-4 w-4 place-items-center rounded text-sky-200/80 hover:bg-sky-400/20 hover:text-sky-100"
            onPointerDown={(e) => { e.stopPropagation(); e.preventDefault() }}
            onClick={(e) => { e.stopPropagation(); void cancelAnalysis(task.id) }}
          >
            <X className="h-3 w-3" />
          </button>
        )}
      </div>
      <style>{`
        @keyframes sa-bubble-progress { 0% { transform: translateX(-100%); } 100% { transform: translateX(300%); } }
        .animate-sa-bubble-progress { animation: sa-bubble-progress 1.6s ease-in-out infinite; }
      `}</style>
    </motion.div>
  )
}

const POS_KEY = 'sa_bubble_pos'
function loadPos(): { x: number; y: number } {
  // 默认右下,偏上一点(避开财务胶囊的右下位置)
  const defaultX = Math.max(EDGE_MARGIN, window.innerWidth - BUBBLE_W - EDGE_MARGIN)
  const defaultY = Math.max(EDGE_MARGIN, window.innerHeight - 320)
  try {
    const v = localStorage.getItem(POS_KEY)
    if (v) {
      const p = JSON.parse(v)
      if (typeof p.x === 'number' && typeof p.y === 'number') {
        return {
          x: Math.max(EDGE_MARGIN, Math.min(window.innerWidth - BUBBLE_W - EDGE_MARGIN, p.x)),
          y: Math.max(EDGE_MARGIN, Math.min(window.innerHeight - 80, p.y)),
        }
      }
    }
  } catch { /* ignore */ }
  return { x: defaultX, y: defaultY }
}
function savePos(p: { x: number; y: number }) {
  try { localStorage.setItem(POS_KEY, JSON.stringify(p)) } catch { /* ignore */ }
}
