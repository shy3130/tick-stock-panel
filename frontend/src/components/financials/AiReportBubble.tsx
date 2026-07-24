import { useState, useRef, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Loader2, Check, AlertCircle } from 'lucide-react'
import { useActiveTasks, restoreDialog } from '@/lib/aiReportStore'
import type { ActiveTask } from '@/lib/aiReportStore'

/**
 * AI 分析任务全局气泡容器 —— 玻璃拟态卡片,挂在网页右侧。
 *
 * 拖拽丝滑的关键(60fps):
 *   - 位置用 transform: translate3d 存储(走 GPU 合成层,不触发 layout/paint)
 *   - 拖动期间直接操作 DOM.style.transform,完全绕开 React setState 重渲染
 *   - 拖动结束才同步一次 state + 持久化 localStorage
 *   - 拖动时给容器加 .dragging 类,禁用所有 transition,消除回弹延迟
 *
 * 视觉:
 *   - 玻璃拟态(frosted glass):半透明 + backdrop-blur + 细边框 + 内发光
 *   - 固定宽度,内容居中,多任务竖向堆叠
 *   - 生成中:柔和呼吸光环(非刺眼 ping)
 *   - hover:展开操作区,带平滑过渡
 */

const BUBBLE_W = 148          // 卡片固定宽度(紧凑单行版)
const EDGE_MARGIN = 12        // 距视口边缘最小间距

export function AiReportBubble() {
  const activeTasks = useActiveTasks()
  const containerRef = useRef<HTMLDivElement>(null)
  // pos 只在拖拽结束时更新一次(用于初始化/持久化),拖拽过程不触发它
  const [pos, setPos] = useState<{ x: number; y: number }>(() => loadPos())

  // ===== 拖拽(纯 DOM 操作,60fps) =====
  const draggingRef = useRef(false)
  const dragData = useRef({ mx: 0, my: 0, ox: 0, oy: 0 })  // 鼠标起点 + 元素起点
  const movedRef = useRef(false)                             // 本次是否真的移动了(区分点击)
  // 记录 pointerdown 时命中的卡片回调(松手时若未拖动则触发它 = 点击)
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
    const nx = dragData.current.ox + dx
    const ny = dragData.current.oy + dy
    const c = clamp(nx, ny)
    applyTransform(c.x, c.y)   // ← 直接改 DOM,不走 React,丝滑
  }, [clamp, applyTransform])

  const onPointerUp = useCallback(() => {
    if (!draggingRef.current) return
    draggingRef.current = false
    const el = containerRef.current
    if (el) el.classList.remove('dragging')
    if (movedRef.current) {
      // 拖动结束 → 持久化位置
      setPos(prev => {
        const transform = el?.style.transform ?? ''
        const m = transform.match(/translate3d\(([-\d.]+)px,\s*([-\d.]+)px/)
        const finalPos = m ? { x: parseFloat(m[1]), y: parseFloat(m[2]) } : prev
        savePos(finalPos)
        return finalPos
      })
    } else {
      // 未移动 → 视为点击,触发卡片回调
      const fn = clickTargetRef.current
      clickTargetRef.current = null
      fn?.()
    }
  }, [])

  // 窗口尺寸变化时确保不越界
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

  // 初始化 transform(pos 变化时同步,如 resize / 首次挂载)
  useEffect(() => {
    applyTransform(pos.x, pos.y)
  }, [pos.x, pos.y, applyTransform])

  if (activeTasks.length === 0) return null

  return (
    <div
      ref={containerRef}
      className="ai-bubble-root fixed z-[60] select-none cursor-grab active:cursor-grabbing"
      style={{
        width: `${BUBBLE_W}px`,
        transform: `translate3d(${pos.x}px, ${pos.y}px, 0)`,
        touchAction: 'none',
        // 拖动时禁用过渡(通过 .dragging 类控制);静止时用 transition 让 resize/吸附有动画
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

      {/* 内联样式:拖动时禁用过渡,确保 1:1 跟手 */}
      <style>{`
        .ai-bubble-root.dragging { transition: none !important; }
      `}</style>
    </div>
  )
}

// ===== 单个胶囊卡片(紧凑玻璃拟态) =====
function BubbleItem({ task, isLast, onPointerDown }: {
  task: ActiveTask
  isLast: boolean
  onPointerDown: () => void
}) {
  const isWorking = task.phase === 'loading' || task.phase === 'streaming'
  const isError = task.phase === 'error'

  // 状态配色
  const accent = isWorking
    ? 'from-purple-500/25 to-fuchsia-500/20 text-purple-300 border-purple-300/40 shadow-[0_6px_24px_-10px_rgba(168,85,247,0.5)]'
    : isError
      ? 'from-red-500/20 to-red-500/10 text-red-300 border-red-300/40 shadow-[0_6px_20px_-10px_rgba(239,68,68,0.4)]'
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
        title={isWorking ? '生成中,点击恢复对话框' : isError ? '分析失败,点击重试' : '点击查看报告'}
        className={`group relative flex w-full cursor-pointer items-center gap-1.5 overflow-hidden rounded-lg border bg-gradient-to-br px-2 py-1.5 backdrop-blur-xl transition-all duration-200 hover:scale-[1.02] active:scale-[0.99] ${accent}`}
      >
        {/* 生成中:顶部进度流光 */}
        {isWorking && (
          <div className="absolute inset-x-0 top-0 h-px overflow-hidden">
            <div className="h-full w-1/2 bg-gradient-to-r from-transparent via-purple-200 to-transparent animate-bubble-progress" />
          </div>
        )}

        {/* 状态图标 */}
        <span className="flex h-4 w-4 items-center justify-center shrink-0">
          {isWorking ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : isError ? (
            <AlertCircle className="h-3 w-3" />
          ) : (
            <Check className="h-3 w-3" />
          )}
        </span>

        {/* 标的名(单行) */}
        <span className="flex-1 min-w-0 text-[11px] font-medium text-foreground leading-none truncate">
          {task.name || task.symbol}
        </span>

        {/* 状态后缀 */}
        <span className="shrink-0 text-[9px] leading-none">
          {isWorking ? (
            <span className="text-purple-300/80">分析中</span>
          ) : isError ? (
            <span className="text-red-300/80">失败</span>
          ) : (
            <span className="text-emerald-300/80">点击查看</span>
          )}
        </span>
      </div>

      {/* 内联关键帧:进度条流动 */}
      <style>{`
        @keyframes bubble-progress {
          0% { transform: translateX(-100%); }
          100% { transform: translateX(300%); }
        }
        .animate-bubble-progress { animation: bubble-progress 1.6s ease-in-out infinite; }
      `}</style>
    </motion.div>
  )
}

// ===== 位置持久化 =====
const POS_KEY = 'ai_bubble_pos'
function loadPos(): { x: number; y: number } {
  // 默认:右下角(距右边缘 EDGE_MARGIN,距底部留出空间避开右下角元素)
  const defaultX = Math.max(EDGE_MARGIN, window.innerWidth - BUBBLE_W - EDGE_MARGIN)
  const defaultY = Math.max(EDGE_MARGIN, window.innerHeight - 200)
  try {
    const v = localStorage.getItem(POS_KEY)
    if (v) {
      const p = JSON.parse(v)
      if (typeof p.x === 'number' && typeof p.y === 'number') {
        // 钳制到当前视口(防止保存的位置在缩小后的窗口外)
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
