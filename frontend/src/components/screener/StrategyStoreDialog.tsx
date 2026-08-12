import { motion, AnimatePresence } from 'framer-motion'
import { X, Store, Hammer, Download, Zap } from 'lucide-react'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'

interface Props {
  open: boolean
  onClose: () => void
}

/**
 * 获取策略（占位）
 * 目标：不定时更新更多策略。功能正在建设中，当前仅占位。
 */
export function StrategyStoreDialog({ open, onClose }: Props) {
  const backdrop = useDialogBackdrop(onClose)
  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
          {...backdrop}
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.95, y: 10 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.95, y: 10 }}
            transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
            className="w-[560px] max-h-[78vh] bg-surface border border-border rounded-card shadow-xl flex flex-col"
          >
            {/* 标题 */}
            <div className="flex items-center justify-between px-4 py-2.5 border-b border-border shrink-0">
              <span className="flex items-center gap-1.5 text-sm font-medium text-foreground">
                <Store className="h-4 w-4 text-accent" />
                获取策略
              </span>
              <button onClick={onClose} className="p-1 rounded hover:bg-elevated transition-colors cursor-pointer">
                <X className="h-4 w-4 text-muted" />
              </button>
            </div>

            {/* 建设中占位内容 */}
            <div className="flex-1 flex flex-col items-center justify-center px-6 py-14 text-center">
              <div className="relative mb-5">
                <div className="absolute inset-0 blur-2xl bg-amber-400/20 rounded-full" />
                <div className="relative h-16 w-16 rounded-2xl bg-amber-400/10 border border-amber-400/30 flex items-center justify-center">
                  <Hammer className="h-8 w-8 text-amber-400" />
                </div>
              </div>

              <h3 className="text-base font-semibold text-foreground mb-1.5">
                不定时更新更多策略
              </h3>
              <p className="text-sm text-muted leading-relaxed max-w-[380px]">
                敬请期待。
              </p>

              <div className="mt-6 flex items-center gap-4 text-[11px] text-muted">
                <span className="flex items-center gap-1">
                  <Download className="h-3.5 w-3.5" />
                  一键下载
                </span>
                <span className="w-px h-3 bg-border" />
                <span className="flex items-center gap-1">
                  <Zap className="h-3.5 w-3.5" />
                  无需配置 即可使用
                </span>
              </div>
            </div>

            {/* 底部关闭 */}
            <div className="flex justify-end px-4 py-2.5 border-t border-border shrink-0">
              <button
                onClick={onClose}
                className="inline-flex items-center gap-1.5 h-7 px-4 rounded-btn
                  border border-border bg-surface text-xs font-medium text-secondary
                  hover:text-accent hover:border-accent/50 transition-colors cursor-pointer"
              >
                知道了
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
