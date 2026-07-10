import { useCallback, useEffect, useState } from 'react'

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
    <div
      role="status"
      aria-live="polite"
      aria-atomic="false"
      className="fixed bottom-4 right-4 z-[9999] flex flex-col gap-2 pointer-events-none"
    >
      {items.map(t => (
        <div
          key={t.id}
          className={`pointer-events-auto px-4 py-2.5 rounded-lg shadow-lg text-sm font-medium animate-in slide-in-from-bottom-2 fade-in duration-200 ${
            t.kind === 'error'
              ? 'bg-red-500/90 text-white'
              : 'bg-emerald-500/90 text-white'
          }`}
        >
          {t.msg}
        </div>
      ))}
    </div>
  )
}
