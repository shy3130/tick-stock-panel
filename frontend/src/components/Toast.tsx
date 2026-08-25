import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { cn } from '@/lib/cn'
import { TOAST_KIND } from '@/components/ui/Primitives'

// ===== 全局 toast 状态 =====
/** 可选操作链接: 点击后在 SPA 内跳转 (如「去监控中心」) */
type ToastAction = { label: string; href: string }
type ToastItem = { id: number; msg: string; kind: 'error' | 'success'; action?: ToastAction }
let _id = 0
const _listeners: Set<(items: ToastItem[]) => void> = new Set()
let _queue: ToastItem[] = []

function _emit() { _listeners.forEach(fn => fn([..._queue])) }

function _dismiss(id: number) {
  _queue = _queue.filter(t => t.id !== id)
  _emit()
}

function toast(msg: string, kind: 'error' | 'success' = 'error', action?: ToastAction) {
  const item = { id: ++_id, msg, kind, action }
  _queue = [..._queue, item]
  _emit()
  // 带操作链接的 toast 展示更久, 给用户留出点击窗口
  setTimeout(() => { _dismiss(item.id) }, action ? 8000 : 4000)
}

export { toast }

// ===== Toast 容器 — 挂在 Layout 最顶层 =====
export function ToastContainer() {
  const [items, setItems] = useState<ToastItem[]>([])
  const navigate = useNavigate()

  const sub = useCallback(() => {
    _listeners.add(setItems)
    return () => { _listeners.delete(setItems) }
  }, [])

  useEffect(sub, [sub])

  if (!items.length) return null

  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-[9999] flex flex-col gap-2">
      {items.map(t => (
        <div
          key={t.id}
          className={cn(
            'pointer-events-auto flex items-center gap-1 rounded-lg px-4 py-2.5 text-sm font-medium shadow-lg',
            'animate-in slide-in-from-bottom-2 fade-in duration-200',
            TOAST_KIND[t.kind],
          )}
        >
          <span>{t.msg}</span>
          {t.action && (
            <button
              type="button"
              onClick={() => { navigate(t.action!.href); _dismiss(t.id) }}
              className="shrink-0 rounded underline underline-offset-2 transition-opacity hover:opacity-70"
            >
              {t.action.label}
            </button>
          )}
        </div>
      ))}
    </div>
  )
}
