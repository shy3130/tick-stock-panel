import { useCallback, useEffect, useState } from 'react'
import { cn } from '@/lib/cn'
import { TOAST_KIND } from '@/components/ui/Primitives'

// ===== 全局 toast 状态 =====
type ToastItem = { id: number; msg: string; kind: 'error' | 'success' }
let _id = 0
const _listeners: Set<(items: ToastItem[]) => void> = new Set()
let _queue: ToastItem[] = []

function _emit() { _listeners.forEach(fn => fn([..._queue])) }

function toast(msg: string, kind: 'error' | 'success' = 'error') {
  const item = { id: ++_id, msg, kind }
  _queue = [..._queue, item]
  _emit()
  setTimeout(() => { _queue = _queue.filter(t => t.id !== item.id); _emit() }, 4000)
}

export { toast }

// ===== Toast 容器 — 挂在 Layout 最顶层 =====
export function ToastContainer() {
  const [items, setItems] = useState<ToastItem[]>([])

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
            'pointer-events-auto rounded-lg px-4 py-2.5 text-sm font-medium shadow-lg',
            'animate-in slide-in-from-bottom-2 fade-in duration-200',
            TOAST_KIND[t.kind],
          )}
        >
          {t.msg}
        </div>
      ))}
    </div>
  )
}
