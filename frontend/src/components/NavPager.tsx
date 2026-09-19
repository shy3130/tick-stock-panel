import { AnimatePresence, motion } from 'framer-motion'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import type { ListNav } from '@/lib/useListNav'

interface NavPagerProps {
  /** useListNav 的返回值; 不足两项或当前项不在列表中时自行不渲染 */
  nav: ListNav<unknown>
  /** 按钮文案的宾语: 股票用「上一只/下一只」, 交易回放用「上一笔/下一笔」 */
  prevLabel: string
  nextLabel: string
}

/** 顶栏的列表导航控件 (上一项 / n·N / 下一项), 与 useListNav 配套 */
export function NavPager({ nav, prevLabel, nextLabel }: NavPagerProps) {
  if (!nav.navEnabled) return null
  return (
    <>
      <span className="mx-0.5 shrink-0 text-muted/20">|</span>
      <button
        onClick={() => nav.go(-1)}
        title={`${prevLabel} (←)`}
        aria-label={prevLabel}
        className="p-1 rounded-btn text-secondary hover:text-foreground hover:bg-elevated transition-colors cursor-pointer"
      >
        <ChevronLeft className="h-3.5 w-3.5" />
      </button>
      <span className="shrink-0 font-mono text-[11px] text-secondary tabular-nums whitespace-nowrap">
        {nav.navIdx + 1} / {nav.navTotal}
      </span>
      <button
        onClick={() => nav.go(1)}
        title={`${nextLabel} (→)`}
        aria-label={nextLabel}
        className="p-1 rounded-btn text-secondary hover:text-foreground hover:bg-elevated transition-colors cursor-pointer"
      >
        <ChevronRight className="h-3.5 w-3.5" />
      </button>
    </>
  )
}

/** 首↔尾循环弱提示 (自显 ~1.5s, 不引全局 Toast); 置于弹窗容器内绝对定位 */
export function NavWrapToast({ message }: { message: string | null }) {
  return (
    <AnimatePresence>
      {message && (
        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 8 }}
          transition={{ duration: 0.2 }}
          className="pointer-events-none absolute bottom-4 left-1/2 z-30 -translate-x-1/2 rounded-full border border-border bg-surface/95 px-3 py-1.5 text-[11px] text-secondary shadow-lg backdrop-blur"
        >
          {message}
        </motion.div>
      )}
    </AnimatePresence>
  )
}
