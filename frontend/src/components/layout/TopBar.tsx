import { ChevronsLeft, ChevronsRight, Moon, Sun, WifiOff } from 'lucide-react'
import { toggleTheme, useTheme } from '@/lib/theme'

interface Props {
  collapsed: boolean
  forcedByViewport?: boolean
  reconnecting?: boolean
  onToggleCollapsed: () => void
}

/**
 * 主内容区顶部操作栏 —— 侧栏折叠开关 + 主题切换(从原侧栏底部移up来)。
 * 为以后的全局搜索/切股功能预留了这条横栏的位置, 但目前故意不放一个不可用的
 * 搜索框占位(半成品 UI 体验差) —— 等真正设计好"搜到股票后跳去哪个页面"这个
 * 产品问题, 再实现进来。
 */
export function TopBar({ collapsed, forcedByViewport = false, reconnecting = false, onToggleCollapsed }: Props) {
  const theme = useTheme()
  const dark = theme === 'dark'

  return (
    <header className="h-12 shrink-0 border-b border-border bg-surface flex items-center justify-between px-3">
      <button
        onClick={onToggleCollapsed}
        disabled={forcedByViewport}
        className={`flex items-center justify-center rounded-btn p-2 text-foreground/80 transition-colors duration-150 ease-smooth ${
          forcedByViewport
            ? 'opacity-50 cursor-not-allowed'
            : 'hover:bg-elevated hover:text-foreground cursor-pointer'
        }`}
        title={forcedByViewport ? '屏幕宽度不足，侧栏保持收起状态' : collapsed ? '展开侧栏' : '收起侧栏'}
        aria-label={forcedByViewport ? '屏幕宽度不足，侧栏保持收起状态' : collapsed ? '展开侧栏' : '收起侧栏'}
      >
        {collapsed ? <ChevronsRight className="h-4 w-4 shrink-0" /> : <ChevronsLeft className="h-4 w-4 shrink-0" />}
      </button>

      <div className="flex min-w-0 items-center gap-2">
        {reconnecting && (
          <div
            role="status"
            aria-live="polite"
            className="flex min-w-0 items-center gap-1.5 rounded-full border border-warning/30 bg-warning/10 px-2.5 py-1 text-[11px] font-medium text-warning"
          >
            <WifiOff className="h-3 w-3 shrink-0 animate-pulse" />
            <span className="hidden truncate sm:inline">实时连接断开 · 重连中</span>
            <span className="truncate sm:hidden">重连中</span>
          </div>
        )}
        <button
          onClick={() => toggleTheme()}
          className="flex items-center justify-center rounded-btn p-2 text-foreground/80 transition-colors duration-150 ease-smooth hover:bg-elevated hover:text-foreground cursor-pointer"
          title={dark ? '切换到亮色模式' : '切换到暗色模式'}
          aria-label={dark ? '切换到亮色模式' : '切换到暗色模式'}
        >
          {dark ? <Sun className="h-4 w-4 shrink-0" /> : <Moon className="h-4 w-4 shrink-0" />}
        </button>
      </div>
    </header>
  )
}
