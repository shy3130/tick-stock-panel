/**
 * AI 配置徽标右侧的助手入口 — DOM 锚定实现, 不改核心文件。
 *
 * 核心侧栏头部没有扩展槽位; 为保持「零核心修改」, 此以
 * a[href='/settings?tab=ai'](核心 AIConfigBadge) 为锚点, 把一个
 * fixed 小按钮 portal 到 document.body, 定位到徽标行最右侧,
 * 用 ResizeObserver(徽标 + aside) 与 window resize 跟随布局。
 * 并给锚点行注入右内边距(RESERVED_PR), 让模型名 truncate 提前收尾、
 * 状态点与按钮互不覆盖 — 否则长模型名会延伸到按钮下面。
 * 锚点不存在或不可见(侧栏收起/隐藏、核心改版)时不渲染 — fail-closed。
 */
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { MessagesSquare } from 'lucide-react'
import { cn } from '@/lib/cn'
import { toggleAssistant, useAssistantStore } from '../store'

const ANCHOR_SELECTOR = "a[href='/settings?tab=ai']"
const BTN = 22 // 按钮边长(px)
// 徽标行右侧预留的按钮位: 按钮贴 rect.right-8, 宽 BTN, 留 2px 间隔
const RESERVED_PR = `${BTN + 10}px`

interface AnchorSpot {
  left: number
  top: number
  visible: boolean
}

function measure(): AnchorSpot {
  const anchor = document.querySelector(ANCHOR_SELECTOR)
  if (!anchor) return { left: 0, top: 0, visible: false }
  const rect = anchor.getBoundingClientRect()
  // 零宽(隐藏)或移出视口(移动端抽屉关闭)时不可见。
  if (rect.width <= 0 || rect.left < 0 || rect.right > window.innerWidth) {
    return { left: 0, top: 0, visible: false }
  }
  return {
    left: rect.right - BTN - 8, // 8px 对齐徽标行 pr-2 右内边距
    top: rect.top + rect.height / 2 - BTN / 2,
    visible: true,
  }
}

export function AiConfigEntry() {
  const { open } = useAssistantStore()
  const [spot, setSpot] = useState<AnchorSpot>(() => measure())

  useEffect(() => {
    let frame = 0
    let pollTimer = 0
    let styledAnchor: HTMLElement | null = null
    const observer = new ResizeObserver(() => sync())

    const sync = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        setSpot(prev => {
          const next = measure()
          const unchanged = prev.visible === next.visible
            && (!next.visible || (Math.abs(prev.left - next.left) < 0.5 && Math.abs(prev.top - next.top) < 0.5))
          return unchanged ? prev : next
        })
      })
    }

    /** 给锚点行预留按钮位; 锚点更换/卸载时还原(行内样式不与核心类冲突)。 */
    const reserveSpace = (el: HTMLElement | null) => {
      if (styledAnchor === el) return
      if (styledAnchor) styledAnchor.style.paddingRight = ''
      styledAnchor = el
      if (el) el.style.paddingRight = RESERVED_PR
    }

    const attach = (): boolean => {
      const anchorEl = document.querySelector(ANCHOR_SELECTOR)
      if (!anchorEl) return false
      reserveSpace(anchorEl as HTMLElement)
      observer.observe(anchorEl)
      const aside = anchorEl.closest('aside')
      if (aside) observer.observe(aside)
      return true
    }
    // 锚点可能晚于本组件挂载(设置/偏好加载后) — 轮询直到出现为止。
    if (!attach()) {
      pollTimer = window.setInterval(() => {
        if (attach()) window.clearInterval(pollTimer)
      }, 400)
    }
    window.addEventListener('resize', sync)
    sync()

    return () => {
      reserveSpace(null)
      observer.disconnect()
      window.removeEventListener('resize', sync)
      cancelAnimationFrame(frame)
      if (pollTimer) window.clearInterval(pollTimer)
    }
  }, [])

  if (!spot.visible) return null

  return createPortal(
    <button
      type="button"
      onClick={toggleAssistant}
      title="打开 AI 助手 (⌘K / Ctrl+K)"
      aria-label="打开 AI 助手"
      style={{ left: spot.left, top: spot.top, width: BTN, height: BTN }}
      className={cn(
        'fixed z-[55] flex cursor-pointer items-center justify-center rounded-md',
        'transition-colors duration-150 ease-smooth',
        open
          ? 'bg-purple-400/15 text-purple-400'
          : 'text-muted hover:bg-elevated/70 hover:text-purple-400',
      )}
    >
      <MessagesSquare className="h-3.5 w-3.5" />
    </button>,
    document.body,
  )
}
