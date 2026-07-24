/**
 * 回测预热期徽标 — 点击弹出说明气泡。
 *
 * 解释「回测开头几个月没有交易」这一高频疑问: 技术指标需要历史数据预热,
 * 系统会自动在回测起点之前多取约 120 天 (≈4 个月) 数据; 若本地数据恰好从
 * 起点才开始, 开头几个月指标算不出、信号不触发, 属正常现象。
 *
 * 实现要点:
 *   - 点击触发 (非 hover), 移动端友好
 *   - 用 createPortal 渲染到 body, 绕开父容器 overflow 裁剪 (回测配置面板有 overflow-y-auto)
 *   - 全屏透明遮罩点击关闭 + ESC 关闭
 *   - 气泡位置 = 锚点 rect 实时计算, 自动判断向左/向右展开避免溢出屏幕
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { Info } from 'lucide-react'

interface Pos { top: number; left: number }

export function WarmupBadge() {
  const [open, setOpen] = useState(false)
  const anchorRef = useRef<HTMLButtonElement>(null)
  const [pos, setPos] = useState<Pos>({ top: 0, left: 0 })

  // 打开时根据锚点 rect 计算气泡位置 (向下方弹出)
  useLayoutEffect(() => {
    if (!open || !anchorRef.current) return
    const rect = anchorRef.current.getBoundingClientRect()
    const POPUP_W = 272
    const GAP = 8
    // 优先左对齐锚点; 右侧不够则右对齐; 兜底贴左边
    let left = rect.left
    if (left + POPUP_W > window.innerWidth - 8) {
      left = rect.right - POPUP_W
    }
    left = Math.max(8, left)
    setPos({ top: rect.bottom + GAP, left })
  }, [open])

  // ESC 关闭
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open])

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        onClick={() => setOpen(o => !o)}
        className="inline-flex items-center gap-0.5 rounded-full px-1.5 text-[10px] text-amber-500/70 transition-colors hover:bg-amber-400/10 hover:text-amber-500"
        title="为什么开头可能没交易?"
      >
        <Info className="h-3 w-3" strokeWidth={1.5} />
        预热 ≥120 天
      </button>

      {createPortal(
        <AnimatePresence>
          {open && (
            <>
              {/* 全屏透明遮罩: 点击关闭 */}
              <div
                className="fixed inset-0 z-[60]"
                onClick={() => setOpen(false)}
              />
              {/* 气泡: 绝对定位到 body, 绕开 overflow 裁剪 */}
              <motion.div
                initial={{ opacity: 0, y: -4, scale: 0.96 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, y: -4, scale: 0.96 }}
                transition={{ duration: 0.15 }}
                style={{ position: 'fixed', top: pos.top, left: pos.left, width: 272 }}
                className="z-[70] rounded-btn border border-border bg-surface p-3 text-[11px] leading-relaxed text-secondary shadow-2xl"
                onClick={e => e.stopPropagation()}
              >
                <div className="mb-1.5 font-medium text-foreground">为什么开头几个月可能没有交易?</div>
                <p className="text-muted">
                  技术指标 (MA / MACD / RSI 等) 需要历史数据才能算出。系统会自动在回测起点之前多取约
                  <span className="font-medium text-amber-300"> 120 天 (≈4 个月)</span> 数据做预热。
                </p>
                <p className="mt-1.5 text-muted">
                  若本地数据恰好从回测起点才开始, 开头几个月指标算不出、信号不触发,
                  <span className="text-secondary"> 属正常现象, 不是 bug</span>。等数据攒够后自然开始产生交易。
                </p>
                <div className="mt-2 border-t border-border/60 pt-2 text-muted">
                  <span className="text-secondary">解决:</span> 把历史数据补到回测起点之前至少半年, 或把起点往后挪。
                </div>
                {/* 小箭头指向锚点 */}
                <div
                  className="absolute -top-1 h-2 w-2 rotate-45 border-l border-t border-border bg-surface"
                  style={{ left: 12 }}
                />
              </motion.div>
            </>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </>
  )
}
