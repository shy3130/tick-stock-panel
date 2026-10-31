import { motion } from 'framer-motion'
import { X } from 'lucide-react'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'

export function SettingsModal({ title, onClose, children, width = 'max-w-md', contentClassName = 'min-h-0 flex-1 overflow-y-auto p-4 sm:p-5' }: {
  title: string
  onClose: () => void
  children: React.ReactNode
  /** 弹窗最大宽度 (Tailwind max-w-* 类), 默认 max-w-md */
  width?: string
  /** 有固定页签/操作栏时, 由内容组件管理唯一的滚动区。 */
  contentClassName?: string
}) {
  const backdrop = useDialogBackdrop(onClose)
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" {...backdrop} />
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 12 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.97, y: 8 }}
        transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        // 百分比跟随 #root 的缩放补偿高度, vh 会在界面放大后把底部推出视口。
        className={`relative rounded-dialog border border-border bg-surface shadow-2xl mx-4 w-full ${width} max-h-[92%] flex flex-col overflow-hidden`}
      >
        <div className="flex items-center justify-between px-5 py-3 border-b border-border shrink-0">
          <h3 className="text-sm font-medium text-foreground">{title}</h3>
          <button onClick={onClose} aria-label="关闭" className="p-1 rounded hover:bg-elevated text-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className={contentClassName}>
          {children}
        </div>
      </motion.div>
    </div>
  )
}
