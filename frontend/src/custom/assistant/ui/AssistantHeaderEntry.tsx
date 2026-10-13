/**
 * 侧栏头部「AI 助手」快捷行 — 经 layout.header.quick 插槽渲染。
 *
 * 与核心的数据源/AI 配置/数据行同几何 (icon+label+右侧元信息), 点击
 * toggleAssistant 展开抽屉; 右侧 ⌘K 提示全局快捷键。行内自带顶部发丝线,
 * 删除本扩展即整行消失, 核心零依赖。
 */
import { MessagesSquare } from 'lucide-react'
import { cn } from '@/lib/cn'
import { toggleAssistant, useAssistantStore } from '../store'

export function AssistantHeaderEntry() {
  const { open } = useAssistantStore()
  return (
    <button
      type="button"
      onClick={toggleAssistant}
      title="AI 助手 (⌘K)"
      aria-label="打开 AI 助手"
      className={cn(
        'group flex h-7 flex-1 items-center justify-center gap-1.5 rounded-md border transition-colors duration-150',
        open
          ? 'border-accent/40 bg-accent/10 text-accent'
          : 'border-border/60 bg-elevated/25 text-secondary hover:border-border hover:bg-elevated hover:text-foreground',
      )}
    >
      <MessagesSquare
        className={cn(
          'h-3 w-3 shrink-0 transition-colors',
          open ? 'text-accent' : 'text-muted group-hover:text-foreground',
        )}
      />
      <span className="text-[11px] leading-none">AI 助手</span>
    </button>
  )
}
