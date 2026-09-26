/**
 * 看板网格 — react-grid-layout v2 封装: 12 列吸附网格, 编辑态拖拽换位/角柄调宽高。
 *
 * 设计要点:
 * - 浏览态零编辑元素(视觉与固定布局时代一致); 编辑态在每个组件顶部叠加
 *   半透明把手条(拖拽手柄 + 删除), dragConfig.handle 限定只有把手可拖,
 *   组件内部链接/点击在编辑态不受影响。
 * - <768px 不挂网格, 组件按顺序纵排(RGL 只服务桌面宽度)。
 * - onLayoutChange 仅编辑态提交(挂载期 RGL 的规范化回调被忽略, 避免
 *   非编辑状态下布局意外写回)。
 */
import { useCallback, useMemo, useState, type ReactNode } from 'react'
import GridLayout, { useContainerWidth, type Layout } from 'react-grid-layout'
import 'react-grid-layout/css/styles.css'
import { Check, GripVertical, RotateCcw, X } from 'lucide-react'
import { cn } from '@/lib/cn'
import { GRID_COLS, GRID_MARGIN, GRID_ROW_HEIGHT, cloneItems, normalizeLayout, type DashboardItem } from './layout'
import { DEFAULT_LAYOUT, isKnownWidgetType, minSizeOf, widgetDef } from './registry'
import type { WidgetCtx } from './registry'

export function normalizeDashboardLayout(blob: unknown): DashboardItem[] {
  return normalizeLayout(blob, DEFAULT_LAYOUT, isKnownWidgetType, minSizeOf)
}

export interface DashboardGridProps {
  ctx: WidgetCtx
  items: DashboardItem[]
  onItemsChange: (items: DashboardItem[]) => void
}

export function DashboardGrid({ ctx, items, onItemsChange }: DashboardGridProps) {
  const [editing, setEditing] = useState(false)
  const { width, containerRef, mounted } = useContainerWidth({ measureBeforeMount: true })

  const rglLayout = useMemo(
    () => items.map(it => {
      const min = minSizeOf(it.t)
      return { i: it.i, x: it.x, y: it.y, w: it.w, h: it.h, minW: min.w, minH: min.h }
    }),
    [items],
  )

  const handleLayoutChange = useCallback((layout: Layout) => {
    // RGL 回传全量布局(含 compact 后坐标); props 不在 layout 里, 从当前 items 续接
    const map = new Map(layout.map(l => [l.i, l]))
    const next = items.map(it => {
      const l = map.get(it.i)
      return l ? { ...it, x: l.x, y: l.y, w: l.w, h: l.h } : it
    })
    onItemsChange(next)
  }, [items, onItemsChange])

  const removeItem = useCallback((id: string) => {
    onItemsChange(items.filter(it => it.i !== id))
  }, [items, onItemsChange])

  const resetDefault = useCallback(() => {
    onItemsChange(cloneItems(DEFAULT_LAYOUT))
  }, [onItemsChange])

  const renderWidget = (it: DashboardItem): ReactNode => {
    const def = widgetDef(it.t)
    return def ? def.render(ctx, it) : null
  }

  const isMobile = mounted && width < 768

  return (
    <div ref={containerRef as unknown as React.RefObject<HTMLDivElement>} className="relative">
      {/* 编辑入口/工具条: 非编辑态淡出悬浮, hover 显现 */}
      <div className={cn(
        'group/edit mb-1.5 flex items-center gap-2 transition-opacity',
        !editing && 'opacity-0 pointer-events-none hover:opacity-100 focus-within:opacity-100',
        !editing && 'group-hover/edit:opacity-100 group-hover/edit:pointer-events-auto',
      )}>
        {!editing ? (
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground hover:border-accent/40"
            title="拖拽调整组件位置与宽高"
          >
            <GripVertical className="h-3 w-3" />自定义布局
          </button>
        ) : (
          <>
            <span className="text-[11px] text-muted">拖动顶部把手移动 · 右下角柄调宽高, 自动吸附网格</span>
            <span className="flex-1" />
            <button
              type="button"
              onClick={resetDefault}
              className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground"
            >
              <RotateCcw className="h-3 w-3" />恢复默认
            </button>
            <button
              type="button"
              onClick={() => setEditing(false)}
              className="inline-flex items-center gap-1 rounded-btn bg-accent px-2.5 py-1 text-[11px] font-medium text-white transition-colors hover:bg-accent/90"
            >
              <Check className="h-3 w-3" />完成
            </button>
          </>
        )}
      </div>

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
          dragConfig={{ enabled: editing, handle: '.dash-widget-handle', threshold: 3 }}
          resizeConfig={{ enabled: editing, handles: ['se'] }}
          onLayoutChange={editing ? handleLayoutChange : undefined}
        >
          {items.map(it => {
            const def = widgetDef(it.t)
            const Icon = def?.icon
            return (
              <div key={it.i} className={cn(editing && 'rounded-card ring-1 ring-accent/25')}>
                {editing && (
                  <div className="dash-widget-handle absolute inset-x-0 top-0 z-10 flex h-6 cursor-grab items-center justify-between rounded-t-card bg-surface/95 px-2 text-[10px] text-secondary shadow-sm active:cursor-grabbing">
                    <span className="flex min-w-0 items-center gap-1">
                      {Icon && <Icon className="h-3 w-3 shrink-0 text-accent" />}
                      <span className="truncate">{def?.label ?? it.t}{it.t === 'ext-link' && it.p?.title ? ` · ${it.p.title}` : ''}</span>
                    </span>
                    <button
                      type="button"
                      onClick={() => removeItem(it.i)}
                      className="flex h-4 w-4 shrink-0 items-center justify-center rounded text-muted hover:bg-danger/15 hover:text-danger"
                      title="移除组件"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                )}
                {renderWidget(it)}
              </div>
            )
          })}
        </GridLayout>
      )}
    </div>
  )
}
