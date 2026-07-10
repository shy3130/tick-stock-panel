import type { ReactNode } from 'react'
import { AnimatePresence, motion } from 'framer-motion'

interface ConfirmDialogProps {
  open: boolean
  title: ReactNode
  message: ReactNode
  confirmText?: string
  cancelText?: string
  pending?: boolean
  danger?: boolean
  onCancel: () => void
  onConfirm: () => void
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmText = '确认',
  cancelText = '取消',
  pending = false,
  danger = false,
  onCancel,
  onConfirm,
}: ConfirmDialogProps) {
  return (
    <AnimatePresence>
      {open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            onClick={() => { if (!pending) onCancel() }}
          />
          <motion.div
            initial={{ opacity: 0, scale: 0.95, y: 12 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 8 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className="relative w-[90vw] max-w-[380px] rounded-card border border-border bg-base p-6 shadow-2xl"
          >
            <h3 className="mb-2 text-sm font-medium text-foreground">{title}</h3>
            <div className="mb-5 text-xs leading-relaxed text-secondary">{message}</div>
            <div className="flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={onCancel}
                disabled={pending}
                className="rounded-btn bg-elevated px-3 py-1.5 text-sm text-secondary transition-colors hover:bg-elevated/80 disabled:opacity-50"
              >
                {cancelText}
              </button>
              <button
                type="button"
                onClick={onConfirm}
                disabled={pending}
                className={
                  danger
                    ? 'rounded-btn bg-danger/15 px-3 py-1.5 text-sm font-medium text-danger transition-colors hover:bg-danger/25 disabled:opacity-50'
                    : 'rounded-btn bg-accent/90 px-3 py-1.5 text-sm font-medium text-base transition-colors hover:bg-accent disabled:opacity-50'
                }
              >
                {pending ? '处理中...' : confirmText}
              </button>
            </div>
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}
