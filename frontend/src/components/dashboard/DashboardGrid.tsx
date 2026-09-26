/**
 * 看板网格 — react-grid-layout v2 封装: 12 列吸附网格, 编辑态拖拽换位/八向调宽高。
 *
 * 设计要点:
 * - 编辑入口与编辑控制组(添加组件/恢复默认/完成)在页面头部「重载」右侧,
 *   由 Dashboard 页持有 editing 状态传入; 本组件只负责网格渲染与编辑态覆盖层。
 * - 编辑态整卡可拖(不限定把手, 顶部被遮挡也能从任意位置拖起);
 *   组件内容 pointer-events:none 阻断内部点击/悬停(编辑时不误触行情链接),
 *   外链 iframe 同样被阻断 — 拖拽不会被 iframe 吞掉。
 * - 缩放八向: 四边通长细条 + 四角直角标线(样式在 grid.css, 挂 .dash-grid-editing);
 *   西/北向缩放由 RGL 联动平移 x/y 固定对侧边。
 * - 吸附: 拖拽/缩放全程按网格单元吸附, 拖动时占位格实时标出落点(grid.css 美化),
 *   松手按 200ms 过渡滑入格子。
 * - <768px 不挂网格, 组件按顺序纵排(RGL 只服务桌面宽度)。
 * - onLayoutChange 仅编辑态提交(挂载期 RGL 的规范化回调被忽略, 避免
 *   非编辑状态下布局意外写回)。
 */
import { useCallback, useMemo, type ReactNode } from 'react'
import GridLayout, { useContainerWidth, type Layout } from 'react-grid-layout'
import 'react-grid-layout/css/styles.css'
import './grid.css'
import { X } from 'lucide-react'
import { cn } from '@/lib/cn'
import { GRID_COLS, GRID_MARGIN, GRID_ROW_HEIGHT, compactLayout, normalizeLayout, type DashboardItem } from './layout'
import { DEFAULT_LAYOUT, isKnownWidgetType, minSizeOf, widgetDef } from './registry'
import type { WidgetCtx } from './registry'

export function normalizeDashboardLayout(blob: unknown): DashboardItem[] {
  return normalizeLayout(blob, DEFAULT_LAYOUT, isKnownWidgetType, minSizeOf)
}

export interface DashboardGridProps {
  ctx: WidgetCtx
  items: DashboardItem[]
  onItemsChange: (items: DashboardItem[]) => void
  /** 编辑态由 Dashboard 页头部控制组持有, 受控传入 */
  editing: boolean
}

export function DashboardGrid({ ctx, items, onItemsChange, editing }: DashboardGridProps) {
  const { width, containerRef, mounted } = useContainerWidth({ measureBeforeMount: true })

  const rglLayout = useMemo(
    () => items.map(it => {
      const min = minSizeOf(it.t)
      return { i: it.i, x: it.x, y: it.y, w: it.w, h: it.h, minW: min.w, minH: min.h }
    }),
    [items],
  )

  const handleLayoutChange = useCallback((layout: Layout) => {
    // RGL 回传全量布局(含 compact 后坐标); props 不在 layout 里, 从当前 items 续接。
    // compactLayout 兜底消重叠后再写回持久化(与 normalizeLayout 加载侧同一防线),
    // 确保存进后端的坐标永不互相压盖。
    const map = new Map(layout.map(l => [l.i, l]))
    const next = items.map(it => {
      const l = map.get(it.i)
      return l ? { ...it, x: l.x, y: l.y, w: l.w, h: l.h } : it
    })
    onItemsChange(compactLayout(next))
  }, [items, onItemsChange])

  const removeItem = useCallback((id: string) => {
    onItemsChange(items.filter(it => it.i !== id))
  }, [items, onItemsChange])

  const renderWidget = (it: DashboardItem): ReactNode => {
    const def = widgetDef(it.t)
    return def ? def.render(ctx, it) : null
  }

  const isMobile = mounted && width < 768

  return (
    <div ref={containerRef as unknown as React.RefObject<HTMLDivElement>} className={cn('dash-grid relative', editing && 'dash-grid-editing')}>
      {/* 编辑态操作提示 (入口与控制组在页面头部「重载」右侧) */}
      {editing && (
        <div className="mb-1.5 text-[11px] text-muted">
          拖动组件任意位置移动 · 悬停卡片四边/四角调宽高 · 自动吸附网格 · 改动自动保存
        </div>
      )}

      {!mounted ? (
        <div className="h-32" />
      ) : isMobile ? (
        /* 移动端: 不挂网格, 纵排 */
        <div className="space-y-1.5">
          {items.map(it => (
            <div key={it.i} className="min-h-16">{renderWidget(it)}</div>
          ))}
        </div>
      ) : (
        <GridLayout
          width={width}
          layout={rglLayout}
          gridConfig={{ cols: GRID_COLS, rowHeight: GRID_ROW_HEIGHT, margin: [GRID_MARGIN, GRID_MARGIN], containerPadding: null, maxRows: Infinity }}
          dragConfig={{
            enabled: editing,
            // 整卡可拖(不限把手); ✕ 移除钮不触发拖拽 — RGL 自动并入 .react-resizable-handle
            cancel: '.dash-widget-remove',
            threshold: 3,
          }}
          resizeConfig={{ enabled: editing, handles: ['n', 'e', 's', 'w', 'ne', 'nw', 'se', 'sw'] }}
          onLayoutChange={editing ? handleLayoutChange : undefined}
        >
          {items.map(it => {
            const def = widgetDef(it.t)
            const Icon = def?.icon
            return (
              <div
                key={it.i}
                className={cn(
                  // overflow-hidden 根除内容溢出压盖相邻组件(卡片内容自然高度可能大于格子);
                  editing && 'cursor-grab select-none rounded-card ring-1 ring-accent/30 hover:ring-accent/60 active:cursor-grabbing',
                )}
                style={{ overflow: 'hidden' }}
              >
                {editing && (
                  <div className="absolute inset-x-0 top-0 z-10 flex h-6 items-center justify-between rounded-t-card bg-surface/95 pl-2 pr-5 text-[10px] text-secondary shadow-sm">
                    <span className="flex min-w-0 items-center gap-1">
                      {Icon && <Icon className="h-3 w-3 shrink-0 text-accent" />}
                      <span className="truncate">{def?.label ?? it.t}{it.t === 'ext-link' && it.p?.title ? ` · ${it.p.title}` : ''}</span>
                    </span>
                    <button
                      type="button"
                      onClick={() => removeItem(it.i)}
                      className="dash-widget-remove flex h-4 w-4 shrink-0 items-center justify-center rounded text-muted hover:bg-danger/15 hover:text-danger"
                      title="移除组件"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                )}
                {/* 编辑态阻断组件内部交互(点击/悬停/iframe), 拖拽由外层网格项接管;
                    卡片撑满格子(h-full), 内容超出格子时在本组件边界内裁切 */}
                <div className={cn('h-full [&>div]:h-full', editing && 'pointer-events-none')}>{renderWidget(it)}</div>
              </div>
            )
          })}
        </GridLayout>
      )}
    </div>
  )
}
