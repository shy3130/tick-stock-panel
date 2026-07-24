/**
 * 股票列表表格骨架（自选/策略页共享）。
 *
 * 职责：表头渲染（读列配置 label/align + 可排序三态指示器）、表体遍历、sticky 表头。
 * 不内置任何业务逻辑：单元格内容（含 symbol 列交互、操作列、ext 列）由调用方通过
 * renderCell / renderExtraCol 注入。这样两个页面的特有交互得以保留，同时表头能力一致。
 */
import { cloneElement, isValidElement, useRef, type ReactElement, type ReactNode } from 'react'
import { useVirtualizer, type VirtualItem } from '@tanstack/react-virtual'
import type { ColumnConfig } from '@/lib/list-columns'
import { UNSORTABLE_KEYS } from '@/lib/stock-table'
import { VIRTUAL_LIST_THRESHOLD, useParentScroll } from '@/components/virtual-list/useParentScroll'
import type { SortState } from './useTableSort'

export type { SortState }

export interface StockDataTableProps {
  columns: ColumnConfig[]
  rows: any[]
  /** 单元格渲染回调。返回 null 时回退到内置纯数据列渲染。 */
  renderCell: (r: any, col: ColumnConfig) => ReactNode
  /** 行 key（默认取 r.symbol） */
  rowKey?: (r: any) => string | number
  /** 行 className（默认含 hover） */
  rowClassName?: (r: any) => string
  /** 表头是否 sticky（自选页需要，策略页不需要） */
  headerSticky?: boolean
  /** 最小表格宽度，默认按列数计算 */
  minWidth?: number
  /** 排序：外部受控时传入（含当前 sort 与 toggle）；不传则表头不可排序 */
  sort?: SortState | null
  onSortToggle?: (colId: string) => void
  /** 追加在每行末尾的额外单元格（如自选页的操作列） */
  renderExtraCol?: (r: any) => ReactNode
  /** 追加的表头单元格（对应 renderExtraCol） */
  extraHeader?: ReactNode
  /** 自定义表头单元格内容覆盖（如日k眼睛按钮）。返回 undefined 则用 col.label */
  renderHeaderContent?: (col: ColumnConfig) => ReactNode | undefined
  /** 外层容器 className */
  className?: string
}

function alignThClass(align: ColumnConfig['align']): string {
  if (align === 'right') return 'px-3 py-2.5 font-medium text-right'
  if (align === 'center') return 'px-3 py-2.5 font-medium text-center'
  return 'px-3 py-2.5 font-medium'
}

export function StockDataTable({
  columns, rows, renderCell,
  rowKey = (r: any) => r.symbol,
  rowClassName = () => 'border-t border-border hover:bg-elevated/50',
  headerSticky = false,
  minWidth,
  sort,
  onSortToggle,
  renderExtraCol,
  extraHeader,
  renderHeaderContent,
  className = 'rounded-card border border-border overflow-x-auto',
}: StockDataTableProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const visibleColumns = columns.filter(c => c.visible)
  const computedMinWidth = minWidth ?? Math.max(900, visibleColumns.length * 110)
  const virtualized = rows.length > VIRTUAL_LIST_THRESHOLD
  const { getScrollElement, scrollMargin } = useParentScroll(containerRef, virtualized)
  const rowVirtualizer = useVirtualizer({
    count: virtualized ? rows.length : 0,
    getScrollElement,
    estimateSize: () => 56,
    getItemKey: index => rowKey(rows[index]),
    overscan: 10,
    scrollMargin,
  })
  const virtualRows = virtualized ? rowVirtualizer.getVirtualItems() : []
  const totalSize = virtualized ? rowVirtualizer.getTotalSize() : 0
  const firstVirtualRow = virtualRows[0]
  const lastVirtualRow = virtualRows[virtualRows.length - 1]
  const topPadding = firstVirtualRow ? firstVirtualRow.start - scrollMargin : 0
  const bottomPadding = lastVirtualRow
    ? totalSize - (lastVirtualRow.end - scrollMargin)
    : totalSize
  const columnCount = visibleColumns.length + (renderExtraCol || extraHeader ? 1 : 0)

  const isColSortable = (col: ColumnConfig): boolean => {
    // 排序能力由调用方是否提供 onSortToggle 决定；sort 是否为 null 只影响当前指示器
    if (!onSortToggle) return false
    if (col.source.type === 'builtin' && UNSORTABLE_KEYS.has(col.source.key)) return false
    return true
  }

  const theadClass = headerSticky
    ? 'sticky top-0 z-10 bg-surface after:absolute after:inset-x-0 after:bottom-0 after:h-px after:bg-border'
    : 'bg-elevated'

  const renderRow = (r: any, virtualRow?: VirtualItem) => (
    <tr
      key={rowKey(r)}
      ref={virtualRow ? rowVirtualizer.measureElement : undefined}
      data-index={virtualRow?.index}
      className={`transition-colors duration-150 ease-smooth group ${rowClassName(r)}`}
    >
      {visibleColumns.map(col => {
        // renderCell 返回的 <td> 无 key, 这里补上避免 React key 警告
        const cell = renderCell(r, col)
        return isValidElement(cell)
          ? cloneElement(cell as ReactElement, { key: col.id })
          : cell
      })}
      {renderExtraCol && renderExtraCol(r)}
    </tr>
  )

  return (
    <div ref={containerRef} className={className}>
      <table className="w-full text-sm" style={{ minWidth: computedMinWidth }}>
        <thead className={theadClass}>
          <tr className="text-left text-secondary">
            {visibleColumns.map(col => {
              const sortable = isColSortable(col)
              const isSorted = sort?.key === col.id
              const dir = isSorted ? sort!.dir : null
              const contentOverride = renderHeaderContent?.(col)
              return (
                <th
                  key={col.id}
                  className={`${alignThClass(col.align)} ${sortable ? 'cursor-pointer select-none group' : ''}`}
                  onClick={sortable ? () => onSortToggle!(col.id) : undefined}
                >
                  {contentOverride !== undefined ? contentOverride : col.label}
                  {sortable && (
                    <span className="inline-block ml-1 text-[10px] opacity-30 group-hover:opacity-60 transition-opacity">
                      {isSorted ? (dir === 'asc' ? '↑' : '↓') : '↕'}
                    </span>
                  )}
                </th>
              )
            })}
            {extraHeader && (
              <th className="px-3 py-2.5 font-medium text-right">{extraHeader}</th>
            )}
          </tr>
        </thead>
        <tbody>
          {virtualized && topPadding > 0 && (
            <tr aria-hidden="true">
              <td colSpan={columnCount} className="p-0 border-0" style={{ height: topPadding }} />
            </tr>
          )}
          {virtualized
            ? virtualRows.map(virtualRow => renderRow(rows[virtualRow.index], virtualRow))
            : rows.map((r: any) => renderRow(r))}
          {virtualized && bottomPadding > 0 && (
            <tr aria-hidden="true">
              <td colSpan={columnCount} className="p-0 border-0" style={{ height: bottomPadding }} />
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
