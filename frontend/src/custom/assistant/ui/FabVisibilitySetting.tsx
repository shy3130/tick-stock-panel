/**
 * 设置→系统「界面」区的「显示悬浮球」开关 — 经 settings.system.section 插槽渲染。
 *
 * 样式与核心 System 面板 ToggleRow 同款 (h-5 w-9 胶囊开关 + label/desc 两行),
 * 开关状态走 assistant store 的 fabVisible (localStorage 持久化, 默认开启)。
 * 删除助手扩展即整行消失, 核心零依赖。
 */
import { cn } from '@/lib/cn'
import { setFabVisible, useAssistantStore } from '../store'

export function FabVisibilitySetting() {
  const { fabVisible } = useAssistantStore()
  return (
    <div className="flex items-center justify-between gap-4 py-2">
      <div className="min-w-0">
        <div className="text-sm text-foreground">AI 助手悬浮球</div>
        <div className="text-[11px] text-muted truncate">侧栏「AI 助手」行与 ⌘K 快捷键不受影响</div>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={fabVisible}
        aria-label="显示 AI 助手悬浮球"
        onClick={() => setFabVisible(!fabVisible)}
        className={cn(
          'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors duration-200',
          fabVisible ? 'border-accent/50 bg-accent' : 'border-border bg-elevated hover:border-muted',
        )}
      >
        <span
          className={cn(
            'inline-block h-3.5 w-3.5 rounded-full border border-black/5 bg-white shadow-sm transition-transform duration-200',
            fabVisible ? 'translate-x-[18px]' : 'translate-x-0.5',
          )}
        />
      </button>
    </div>
  )
}
