/**
 * 通用列表列自定义组件。
 *
 * 布局：
 * - 上半区「已启用」：已启用的列，@dnd-kit 拖拽排序
 * - 下半区「内置列」：按业务分组折叠
 * - 底部「扩展数据列」：复用 ext_data schema，按需添加字段
 */
import React, { useState, useCallback, useEffect, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  DndContext, closestCenter, KeyboardSensor, PointerSensor,
  useSensor, useSensors, type DragEndEvent,
} from '@dnd-kit/core'
import {
  arrayMove, SortableContext, sortableKeyboardCoordinates,
  useSortable, verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { X, GripVertical, Plus, ChevronDown, ChevronRight, Database, Settings2, Search, Eye, EyeOff } from 'lucide-react'
import { api } from '@/lib/api'
import { useQuery } from '@tanstack/react-query'
import { QK } from '@/lib/queryKeys'
import type { ColumnConfig, ColumnGroup, ExtColumnDisplayConfig, CandleColumnConfig, IntradayColumnConfig } from '@/lib/list-columns'
import { resolveCandleConfig, resolveIntradayConfig } from '@/lib/list-columns'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'

interface ListColumnCustomizerProps {
  columns: ColumnConfig[]
  groups: ColumnGroup[]
  onChange: (columns: ColumnConfig[]) => void
  open: boolean
  onClose: () => void
  title?: string
  builtinSectionLabel?: string
  extColumnAlign?: 'left' | 'center' | 'right'
  extFieldFilter?: (field: { name: string; label: string; type: string }) => boolean
  /** 是否显示扩展数据列区块（默认 true；信息条等无法渲染 ext 数据的场景设为 false）。 */
  showExtColumns?: boolean
  /** 是否显示「单独显示」勾选项（默认 false；仅信息条场景启用，让某列独占一行）。 */
  showStandaloneToggle?: boolean
}

/** 判断扩展数据字段类型是否为数字(int/float/double/number/decimal 等)。
 * 旧列 source 无 fieldType 时默认 true(放宽), 让旧列也能配置数字格式 ——
 * 若列实际非数字, 渲染时 typeof val==='number' 判断会跳过格式化, 无副作用。 */
function isNumericFieldType(ft?: string): boolean {
  if (!ft) return true
  const t = ft.toLowerCase()
  // 明确是文本类则不显示
  if (['str', 'string', 'text', 'char', 'varchar', 'date', 'time', 'bool', 'boolean'].some(k => t.includes(k))) {
    return false
  }
  return true
}

function SortableActiveCol({ col, onRemove, onConfig, configOpen, extTableLabel, extConfig, candleConfig: candlePanel, intradayConfig: intradayPanel, strategiesConfig, showStandaloneToggle, onToggleStandalone }: {
  col: ColumnConfig
  onRemove: (id: string) => void
  onConfig: (id: string | null) => void
  configOpen: boolean
  extTableLabel: string
  extConfig: React.ReactNode
  candleConfig: React.ReactNode
  intradayConfig: React.ReactNode
  strategiesConfig: React.ReactNode
  showStandaloneToggle?: boolean
  onToggleStandalone?: (id: string) => void
}) {
  const {
    attributes, listeners, setNodeRef, transform, transition, isDragging,
  } = useSortable({ id: col.id })
  const isExt = col.source.type === 'ext'
  const isCandle = col.source.type === 'builtin' && col.source.key === 'candle'
  const isIntraday = col.source.type === 'builtin' && col.source.key === 'intraday'
  const isStrategies = col.source.type === 'builtin' && col.source.key === 'strategies'
  const hasConfig = isExt || isCandle || isIntraday || isStrategies

  return (
    <>
      <div
        ref={setNodeRef}
        style={{
          transform: CSS.Transform.toString(transform),
          transition,
          opacity: isDragging ? 0.5 : 1,
          zIndex: isDragging ? 10 : undefined,
        }}
        className={`flex items-center gap-2 px-2 py-1.5 rounded group ${
          isDragging ? 'bg-elevated shadow-lg' : 'hover:bg-elevated/50'
        }`}
      >
        <span
          {...attributes}
          {...listeners}
          className="cursor-grab active:cursor-grabbing text-muted hover:text-foreground transition-colors shrink-0"
        >
          <GripVertical className="h-3.5 w-3.5" />
        </span>
        <span className="flex-1 text-xs text-foreground truncate">
          {isExt && col.source.type === 'ext'
            ? `${col.label}（${extTableLabel}）`
            : col.label}
        </span>
        {showStandaloneToggle && (
          <button
            onClick={() => onToggleStandalone?.(col.id)}
            className={`flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] transition-colors shrink-0 ${
              col.standalone
                ? 'text-accent bg-accent/10'
                : 'text-muted hover:text-secondary opacity-0 group-hover:opacity-100'
            }`}
            title={col.standalone ? '取消单独显示' : '单独一行显示'}
          >
            {col.standalone ? '单独' : '单行'}
          </button>
        )}
        {hasConfig && (
          <button
            onClick={() => onConfig(configOpen ? null : col.id)}
            className={`transition-all ${configOpen ? 'text-accent' : 'opacity-0 group-hover:opacity-100 text-muted hover:text-accent'}`}
            title="配置"
          >
            <Settings2 className="h-3 w-3" />
          </button>
        )}
        <button
          onClick={() => onRemove(col.id)}
          className="opacity-0 group-hover:opacity-100 text-muted hover:text-danger transition-all shrink-0"
          title="隐藏"
        >
          <EyeOff className="h-3 w-3" />
        </button>
      </div>
      {hasConfig && configOpen && (isExt ? extConfig : isCandle ? candlePanel : isIntraday ? intradayPanel : strategiesConfig)}
    </>
  )
}

export function ListColumnCustomizer({
  columns,
  groups,
  onChange,
  open,
  onClose,
  title = '自定义列',
  builtinSectionLabel = '内置列',
  extColumnAlign = 'center',
  extFieldFilter,
  showExtColumns = true,
  showStandaloneToggle = false,
}: ListColumnCustomizerProps) {
  const extSchema = useQuery({
    queryKey: QK.extDataSchemaAll,
    queryFn: api.extDataSchemaAll,
    enabled: open && showExtColumns,
    staleTime: 60_000,
  })
  const backdrop = useDialogBackdrop(onClose)

  const [searchQuery, setSearchQuery] = useState('')
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set())
  const [configOpenId, setConfigOpenId] = useState<string | null>(null)
  const [expandedTables, setExpandedTables] = useState<Set<string>>(new Set())

  const colById = useMemo(() => new Map(columns.map(c => [c.id, c])), [columns])
  const keyToId = useMemo(() => {
    const m = new Map<string, string>()
    for (const c of columns) {
      if (c.source.type === 'builtin' || c.source.type === 'computed') m.set(c.source.key, c.id)
    }
    return m
  }, [columns])

  const activeCols = useMemo(() => columns.filter(c => !c.pinned && c.visible), [columns])

  const toggleVisible = useCallback((colId: string) => {
    onChange(columns.map(c =>
      c.id === colId && !c.pinned ? { ...c, visible: !c.visible } : c
    ))
  }, [columns, onChange])

  const toggleStandalone = useCallback((colId: string) => {
    onChange(columns.map(c =>
      c.id === colId ? { ...c, standalone: !c.standalone } : c
    ))
  }, [columns, onChange])

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )

  const handleDragEnd = useCallback((event: DragEndEvent) => {
    const { active, over } = event
    if (!over || active.id === over.id) return
    const reorderCols = columns.filter(c => !c.pinned)
    const pinnedCols = columns.filter(c => c.pinned)
    const ids = reorderCols.map(c => c.id)
    const oldIdx = ids.indexOf(active.id as string)
    const newIdx = ids.indexOf(over.id as string)
    if (oldIdx < 0 || newIdx < 0) return
    const reordered = arrayMove(reorderCols, oldIdx, newIdx)
    onChange([...pinnedCols, ...reordered])
  }, [columns, onChange])

  const addExtColumn = useCallback((configId: string, fieldName: string, fieldLabel?: string, fieldType?: string) => {
    const colId = `ext:${configId}:${fieldName}`
    if (columns.some(c => c.id === colId)) {
      toggleVisible(colId)
      return
    }
    const newCol: ColumnConfig = {
      id: colId,
      source: { type: 'ext', configId, fieldName, fieldLabel, fieldType },
      label: fieldLabel || fieldName,
      visible: true,
      align: extColumnAlign,
    }
    const actionIdx = columns.findIndex(c => c.id === 'builtin:action')
    if (actionIdx >= 0) {
      const next = [...columns]
      next.splice(actionIdx, 0, newCol)
      onChange(next)
    } else {
      onChange([...columns, newCol])
    }
  }, [columns, extColumnAlign, onChange, toggleVisible])

  const hideColumn = useCallback((colId: string) => {
    onChange(columns.map(c => c.id === colId ? { ...c, visible: false } : c))
  }, [columns, onChange])

  const updateExtDisplay = useCallback((colId: string, patch: Partial<ExtColumnDisplayConfig>) => {
    onChange(columns.map(c => {
      if (c.id !== colId) return c
      return { ...c, extDisplay: { displayMode: 'tag', ...(c.extDisplay || {}), ...patch } }
    }))
  }, [columns, onChange])

  const resetExtDisplay = useCallback((colId: string) => {
    onChange(columns.map(c => {
      if (c.id !== colId) return c
      const { extDisplay, ...rest } = c
      return rest
    }))
  }, [columns, onChange])

  const updateCandleConfig = useCallback((colId: string, patch: Partial<CandleColumnConfig>) => {
    onChange(columns.map(c => {
      if (c.id !== colId) return c
      return { ...c, candleConfig: { ...c.candleConfig, ...patch } }
    }))
  }, [columns, onChange])

  const resetCandleConfig = useCallback((colId: string) => {
    onChange(columns.map(c => {
      if (c.id !== colId) return c
      const { candleConfig, ...rest } = c
      return rest
    }))
  }, [columns, onChange])

  const updateIntradayConfig = useCallback((colId: string, patch: Partial<IntradayColumnConfig>) => {
    onChange(columns.map(c => {
      if (c.id !== colId) return c
      return { ...c, intradayConfig: { ...c.intradayConfig, ...patch } }
    }))
  }, [columns, onChange])

  const resetIntradayConfig = useCallback((colId: string) => {
    onChange(columns.map(c => {
      if (c.id !== colId) return c
      const { intradayConfig, ...rest } = c
      return rest
    }))
  }, [columns, onChange])

  const toggleGroup = useCallback((groupId: string) => {
    setExpandedGroups(prev => {
      const next = new Set(prev)
      if (next.has(groupId)) next.delete(groupId)
      else next.add(groupId)
      return next
    })
  }, [])

  const toggleTableExpand = useCallback((tableId: string) => {
    setExpandedTables(prev => {
      const next = new Set(prev)
      if (next.has(tableId)) next.delete(tableId)
      else next.add(tableId)
      return next
    })
  }, [])

  const extTables = extSchema.data?.items ?? []
  const extTableLabelMap = new Map(extTables.map(t => [t.id, t.label]))

  const query = searchQuery.trim().toLowerCase()
  const filteredGroups = useMemo(() => {
    if (!query) return groups
    return groups.filter(g =>
      g.label.includes(query) ||
      g.keys.some(k => {
        const id = keyToId.get(k)
        return id && (colById.get(id)?.label ?? '').toLowerCase().includes(query)
      })
    )
  }, [groups, query, keyToId, colById])

  useEffect(() => {
    if (!open) { setConfigOpenId(null); setSearchQuery('') }
  }, [open])

  useEffect(() => {
    if (query) setExpandedGroups(new Set(filteredGroups.map(g => g.id)))
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query])

  const renderExtConfig = (col: ColumnConfig) => (
    <motion.div
      initial={{ height: 0, opacity: 0 }}
      animate={{ height: 'auto', opacity: 1 }}
      exit={{ height: 0, opacity: 0 }}
      transition={{ duration: 0.15 }}
      className="overflow-hidden"
    >
      <div className="pl-10 pr-3 py-2 space-y-2 border-l-2 border-accent/20 ml-[18px]">
        <label className="flex items-center gap-2 text-xs">
          <span className="text-secondary w-16 shrink-0">显示模式</span>
          <select
            value={col.extDisplay?.displayMode ?? 'tag'}
            onChange={e => updateExtDisplay(col.id, { displayMode: e.target.value as 'tag' | 'text' })}
            className="flex-1 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 focus:outline-none focus:border-accent/50"
          >
            <option value="tag">标签</option>
            <option value="text">纯文本</option>
          </select>
        </label>
        {(col.extDisplay?.displayMode ?? 'tag') === 'tag' && (
          <label className="flex items-center gap-2 text-xs">
            <span className="text-secondary w-16 shrink-0">分隔符</span>
            <input
              type="text"
              value={col.extDisplay?.separator ?? ''}
              onChange={e => updateExtDisplay(col.id, { separator: e.target.value })}
              placeholder="默认：、,，;；-"
              className="flex-1 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 placeholder:text-muted focus:outline-none focus:border-accent/50"
            />
          </label>
        )}
        <label className="flex items-center gap-2 text-xs">
          <span className="text-secondary w-16 shrink-0">最大列宽</span>
          <input
            type="text"
            value={col.extDisplay?.maxWidth ?? ''}
            onChange={e => updateExtDisplay(col.id, { maxWidth: e.target.value })}
            placeholder="如 200px"
            className="flex-1 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 placeholder:text-muted focus:outline-none focus:border-accent/50"
          />
        </label>
        {(col.extDisplay?.displayMode ?? 'tag') === 'tag' && (
          <label className="flex items-center gap-2 text-xs">
            <span className="text-secondary w-16 shrink-0">显示前N个</span>
            <input
              type="number" min={0}
              value={col.extDisplay?.maxTags ?? ''}
              onChange={e => {
                const v = e.target.value ? Number(e.target.value) : undefined
                updateExtDisplay(col.id, { maxTags: v, ...(v ? {} : { hiddenIndices: undefined }) })
              }}
              placeholder="0=全部"
              className="flex-1 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 placeholder:text-muted focus:outline-none focus:border-accent/50"
            />
          </label>
        )}
        {(col.extDisplay?.displayMode ?? 'tag') === 'tag' && (col.extDisplay?.maxTags ?? 0) > 0 && (
          <div className="flex items-center gap-2 text-xs">
            <span className="text-secondary w-16 shrink-0">显示位置</span>
            <div className="flex flex-wrap gap-1">
              {Array.from({ length: col.extDisplay!.maxTags! }, (_, i) => {
                const hidden = col.extDisplay?.hiddenIndices?.includes(i)
                return (
                  <button
                    key={i}
                    onClick={() => {
                      const cur = col.extDisplay?.hiddenIndices ?? []
                      const next = hidden ? cur.filter(x => x !== i) : [...cur, i]
                      updateExtDisplay(col.id, { hiddenIndices: next.length ? next : undefined })
                    }}
                    className={`w-6 h-6 rounded text-[10px] font-medium transition-colors ${
                      hidden ? 'bg-elevated text-muted line-through' : 'bg-accent/15 text-accent'
                    }`}
                  >
                    {i + 1}
                  </button>
                )
              })}
            </div>
          </div>
        )}
        {(col.extDisplay?.displayMode ?? 'tag') === 'tag' && (
          <label className="flex items-center gap-2 text-xs">
            <span className="text-secondary w-16 shrink-0">排列方向</span>
            <div className="flex rounded overflow-hidden border border-border">
              <button
                onClick={() => updateExtDisplay(col.id, { tagLayout: 'horizontal' })}
                className={`px-3 py-1 text-xs transition-colors ${
                  (col.extDisplay?.tagLayout ?? 'horizontal') === 'horizontal'
                    ? 'bg-accent/15 text-accent' : 'bg-elevated text-secondary hover:text-foreground'
                }`}
              >横向</button>
              <button
                onClick={() => updateExtDisplay(col.id, { tagLayout: 'vertical' })}
                className={`px-3 py-1 text-xs transition-colors border-l border-border ${
                  col.extDisplay?.tagLayout === 'vertical'
                    ? 'bg-accent/15 text-accent' : 'bg-elevated text-secondary hover:text-foreground'
                }`}
              >竖向</button>
            </div>
          </label>
        )}
        {/* 数字格式化配置: 千分位 + 单位换算 + 小数位(仅 number 类型字段) */}
        {col.source.type === 'ext' && isNumericFieldType(col.source.fieldType) && (
          <>
            <div className="border-t border-border/40 pt-2 mt-1 text-[10px] text-muted">数字格式</div>
            <label className="flex items-center gap-2 text-xs">
              <span className="text-secondary w-16 shrink-0">千分位</span>
              <button
                type="button"
                onClick={() => updateExtDisplay(col.id, { thousandSeparator: !col.extDisplay?.thousandSeparator })}
                className={`relative inline-flex h-4 w-7 items-center rounded-full transition-colors duration-200 cursor-pointer ${
                  col.extDisplay?.thousandSeparator ? 'bg-accent' : 'bg-elevated'
                }`}
                aria-pressed={!!col.extDisplay?.thousandSeparator}
              >
                <span className={`inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform duration-200 ${
                  col.extDisplay?.thousandSeparator ? 'translate-x-[14px]' : 'translate-x-0.5'
                }`} />
              </button>
              <span className="text-[10px] text-muted">如 1,234,567</span>
            </label>
            <label className="flex items-center gap-2 text-xs">
              <span className="text-secondary w-16 shrink-0">单位换算</span>
              <select
                value={col.extDisplay?.unitConvert ?? 'none'}
                onChange={e => updateExtDisplay(col.id, { unitConvert: e.target.value as 'none' | 'wan' | 'yi' | 'auto' })}
                className="flex-1 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 focus:outline-none focus:border-accent/50"
              >
                <option value="none">不换算</option>
                <option value="wan">万 (÷1万)</option>
                <option value="yi">亿 (÷1亿)</option>
                <option value="auto">自动 (≥亿用亿, ≥万用万)</option>
              </select>
            </label>
            {(col.extDisplay?.unitConvert ?? 'none') !== 'none' && (
              <label className="flex items-center gap-2 text-xs">
                <span className="text-secondary w-16 shrink-0">小数位</span>
                <input
                  type="number" min={0} max={6} step={1}
                  value={col.extDisplay?.unitDecimals ?? 2}
                  onChange={e => updateExtDisplay(col.id, { unitDecimals: Math.max(0, Math.min(6, Number(e.target.value) || 0)) })}
                  className="w-16 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 text-center focus:outline-none focus:border-accent/50"
                />
                <span className="text-[10px] text-muted">换算后保留几位</span>
              </label>
            )}
          </>
        )}
        {col.extDisplay && (
          <div className="flex justify-end pt-1">
            <button onClick={() => resetExtDisplay(col.id)} className="text-[10px] text-muted hover:text-foreground transition-colors">
              恢复默认
            </button>
          </div>
        )}
      </div>
    </motion.div>
  )

  // 策略列配置（精简版：仅显示数量/位置/排列方向，复用 extDisplay 存储）
  const renderStrategiesConfig = (col: ColumnConfig) => (
    <motion.div
      initial={{ height: 0, opacity: 0 }}
      animate={{ height: 'auto', opacity: 1 }}
      exit={{ height: 0, opacity: 0 }}
      transition={{ duration: 0.15 }}
      className="overflow-hidden"
    >
      <div className="pl-10 pr-3 py-2 space-y-2 border-l-2 border-accent/20 ml-[18px]">
        <label className="flex items-center gap-2 text-xs">
          <span className="text-secondary w-16 shrink-0">显示前N个</span>
          <input
            type="number" min={0}
            value={col.extDisplay?.maxTags ?? ''}
            onChange={e => {
              const v = e.target.value ? Number(e.target.value) : undefined
              updateExtDisplay(col.id, { maxTags: v, ...(v ? {} : { hiddenIndices: undefined }) })
            }}
            placeholder="0=全部"
            className="flex-1 h-7 rounded bg-elevated border border-border text-foreground text-xs placeholder:text-muted focus:outline-none focus:border-accent/50"
          />
        </label>
        {(col.extDisplay?.maxTags ?? 0) > 0 && (
          <div className="flex items-center gap-2 text-xs">
            <span className="text-secondary w-16 shrink-0">显示位置</span>
            <div className="flex flex-wrap gap-1">
              {Array.from({ length: col.extDisplay!.maxTags! }, (_, i) => {
                const hidden = col.extDisplay?.hiddenIndices?.includes(i)
                return (
                  <button
                    key={i}
                    onClick={() => {
                      const cur = col.extDisplay?.hiddenIndices ?? []
                      const next = hidden ? cur.filter(x => x !== i) : [...cur, i]
                      updateExtDisplay(col.id, { hiddenIndices: next.length ? next : undefined })
                    }}
                    className={`w-6 h-6 rounded text-[10px] font-medium transition-colors ${
                      hidden ? 'bg-elevated text-muted line-through' : 'bg-accent/15 text-accent'
                    }`}
                  >
                    {i + 1}
                  </button>
                )
              })}
            </div>
          </div>
        )}
        <label className="flex items-center gap-2 text-xs">
          <span className="text-secondary w-16 shrink-0">排列方向</span>
          <div className="flex rounded overflow-hidden border border-border">
            <button
              onClick={() => updateExtDisplay(col.id, { tagLayout: 'horizontal' })}
              className={`px-3 py-1 text-xs transition-colors ${
                (col.extDisplay?.tagLayout ?? 'horizontal') === 'horizontal'
                  ? 'bg-accent/15 text-accent' : 'bg-elevated text-secondary hover:text-foreground'
              }`}
            >横向</button>
            <button
              onClick={() => updateExtDisplay(col.id, { tagLayout: 'vertical' })}
              className={`px-3 py-1 text-xs transition-colors border-l border-border ${
                col.extDisplay?.tagLayout === 'vertical'
                  ? 'bg-accent/15 text-accent' : 'bg-elevated text-secondary hover:text-foreground'
              }`}
            >竖向</button>
          </div>
        </label>
        {col.extDisplay && (
          <div className="flex justify-end pt-1">
            <button onClick={() => resetExtDisplay(col.id)} className="text-[10px] text-muted hover:text-foreground transition-colors">
              恢复默认
            </button>
          </div>
        )}
      </div>
    </motion.div>
  )

  const renderCandleConfig = (col: ColumnConfig) => {
    const cfg = resolveCandleConfig(col.candleConfig)
    // 数值输入: onChange 存原始值(不钳制, 允许自由输入), onBlur 钳制边界
    const numInput = (
      field: keyof CandleColumnConfig,
      label: string,
    ) => (
      <label key={field} className="flex items-center gap-2 text-xs">
        <span className="text-secondary w-16 shrink-0">{label}</span>
        <input
          type="number"
          value={col.candleConfig?.[field] ?? cfg[field]}
          onChange={e => {
            const raw = e.target.value
            // 空字符串 → 存 undefined (回退到默认值显示); 否则存原始数字 (不钳制)
            updateCandleConfig(col.id, { [field]: raw === '' ? undefined : Number(raw) } as Partial<CandleColumnConfig>)
          }}
          onBlur={e => {
            const raw = e.target.value
            // 失焦时钳制: 过大取上限、过小取最小值
            const merged = resolveCandleConfig({ ...col.candleConfig, [field]: raw === '' ? undefined : Number(raw) })
            updateCandleConfig(col.id, { [field]: merged[field] } as Partial<CandleColumnConfig>)
          }}
          className="flex-1 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 focus:outline-none focus:border-accent/50 tabular-nums"
        />
      </label>
    )
    return (
      <motion.div
        initial={{ height: 0, opacity: 0 }}
        animate={{ height: 'auto', opacity: 1 }}
        exit={{ height: 0, opacity: 0 }}
        transition={{ duration: 0.15 }}
        className="overflow-hidden"
      >
        <div className="pl-10 pr-3 py-2 space-y-2 border-l-2 border-accent/20 ml-[18px]">
          {numInput('days', '日k天数')}
          {numInput('enabledWidth', '开启宽度')}
          {numInput('enabledHeight', '开启高度')}
          {numInput('disabledWidth', '收起宽度')}
          {numInput('disabledHeight', '收起高度')}
          <div className="text-[10px] text-muted leading-relaxed pt-0.5">
            宽度 40–300 / 高度 32–200 / 天数 1–60，越界自动钳制到边界
          </div>
          {col.candleConfig && (
            <div className="flex justify-end pt-1">
              <button onClick={() => resetCandleConfig(col.id)} className="text-[10px] text-muted hover:text-foreground transition-colors">
                恢复默认
              </button>
            </div>
          )}
        </div>
      </motion.div>
    )
  }

  const renderIntradayConfig = (col: ColumnConfig) => {
    const cfg = resolveIntradayConfig(col.intradayConfig)
    const numInput = (
      field: keyof IntradayColumnConfig,
      label: string,
    ) => (
      <label key={field} className="flex items-center gap-2 text-xs">
        <span className="text-secondary w-16 shrink-0">{label}</span>
        <input
          type="number"
          value={col.intradayConfig?.[field] ?? cfg[field]}
          onChange={e => {
            const raw = e.target.value
            updateIntradayConfig(col.id, { [field]: raw === '' ? undefined : Number(raw) } as Partial<IntradayColumnConfig>)
          }}
          onBlur={e => {
            const raw = e.target.value
            const merged = resolveIntradayConfig({ ...col.intradayConfig, [field]: raw === '' ? undefined : Number(raw) })
            updateIntradayConfig(col.id, { [field]: merged[field] } as Partial<IntradayColumnConfig>)
          }}
          className="flex-1 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 focus:outline-none focus:border-accent/50 tabular-nums"
        />
      </label>
    )
    return (
      <motion.div
        initial={{ height: 0, opacity: 0 }}
        animate={{ height: 'auto', opacity: 1 }}
        exit={{ height: 0, opacity: 0 }}
        transition={{ duration: 0.15 }}
        className="overflow-hidden"
      >
        <div className="pl-10 pr-3 py-2 space-y-2 border-l-2 border-accent/20 ml-[18px]">
          {numInput('width', '宽度')}
          {numInput('height', '高度')}
          <div className="text-[10px] text-muted leading-relaxed pt-0.5">
            宽度 60–300 / 高度 32–200，越界自动钳制到边界
          </div>
          {col.intradayConfig && (
            <div className="flex justify-end pt-1">
              <button onClick={() => resetIntradayConfig(col.id)} className="text-[10px] text-muted hover:text-foreground transition-colors">
                恢复默认
              </button>
            </div>
          )}
        </div>
      </motion.div>
    )
  }

  const renderCheckbox = (checked: boolean) => (
    <span className={`shrink-0 w-4 h-4 rounded flex items-center justify-center transition-colors ${
      checked ? 'bg-accent text-white' : 'border border-border text-transparent group-hover:border-muted'
    }`}>
      <svg viewBox="0 0 16 16" className="w-2.5 h-2.5" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 8.5L6.5 12L13 4" />
      </svg>
    </span>
  )

  const renderBuiltinRow = (col: ColumnConfig) => (
    <div
      key={col.id}
      className="flex items-center gap-2 w-full px-2 py-1.5 rounded hover:bg-elevated/50 text-left group transition-colors"
    >
      <button onClick={() => toggleVisible(col.id)} className="flex items-center gap-2 flex-1 min-w-0">
        {renderCheckbox(col.visible)}
        <span className={`flex-1 text-xs truncate ${col.visible ? 'text-foreground' : 'text-muted'}`}>{col.label}</span>
      </button>
      {showStandaloneToggle && col.visible && (
        <button
          onClick={() => toggleStandalone(col.id)}
          className={`flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] transition-colors shrink-0 ${
            col.standalone
              ? 'text-accent bg-accent/10'
              : 'text-muted hover:text-secondary'
          }`}
          title={col.standalone ? '取消单独显示' : '单独一行显示'}
        >
          {col.standalone ? '单独' : '单行'}
        </button>
      )}
    </div>
  )

  const renderExtFieldRow = (configId: string, field: { name: string; label: string; type: string }) => {
    const colId = `ext:${configId}:${field.name}`
    const existing = columns.find(c => c.id === colId)
    const checked = !!existing?.visible
    return (
      <button
        key={field.name}
        onClick={() => addExtColumn(configId, field.name, field.label, field.type)}
        className="flex items-center gap-2 w-full px-2 py-1.5 rounded hover:bg-elevated/50 text-left group transition-colors"
      >
        {renderCheckbox(checked)}
        <span className={`flex-1 text-xs truncate ${checked ? 'text-foreground' : 'text-muted'}`}>{field.label || field.name}</span>
        <span className="text-[10px] text-muted shrink-0">{field.type}</span>
      </button>
    )
  }

  return (
    <AnimatePresence>
      {open && (
        <div className="fixed inset-0 z-50 flex justify-end">
          <motion.div
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="absolute inset-0 bg-black/50 backdrop-blur-sm"
            {...backdrop}
          />
          <motion.div
            initial={{ x: '100%' }} animate={{ x: 0 }} exit={{ x: '100%' }}
            transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
            className="relative w-[360px] max-w-[90vw] h-full bg-base border-l border-border shadow-2xl flex flex-col"
          >
            <div className="flex items-center justify-between px-4 py-3 border-b border-border">
              <h3 className="text-sm font-medium text-foreground">{title}</h3>
              <button onClick={onClose} className="p-1 rounded hover:bg-elevated text-muted hover:text-foreground transition-colors">
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="px-3 pt-3 pb-1">
              <div className="relative">
                <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted" />
                <input
                  type="text"
                  value={searchQuery}
                  onChange={e => setSearchQuery(e.target.value)}
                  placeholder="搜索列名..."
                  className="w-full h-8 pl-8 pr-3 rounded-lg bg-elevated border border-border text-xs text-foreground placeholder:text-muted focus:outline-none focus:border-accent/50 transition-colors"
                />
              </div>
            </div>

            <div className="flex-1 overflow-y-auto px-3 py-2 space-y-1">
              {activeCols.length > 0 && (
                <div>
                  <div className="flex items-center gap-1.5 px-1 py-1.5">
                    <Eye className="h-3 w-3 text-accent/70" />
                    <span className="text-[10px] font-semibold text-accent/80 uppercase tracking-wider">已启用</span>
                    <span className="text-[10px] text-muted">{activeCols.length} 列 · 拖拽排序</span>
                  </div>
                  <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
                    <SortableContext items={activeCols.map(c => c.id)} strategy={verticalListSortingStrategy}>
                      {activeCols.map(col => (
                        <SortableActiveCol
                          key={col.id}
                          col={col}
                          onRemove={hideColumn}
                          onConfig={setConfigOpenId}
                          configOpen={configOpenId === col.id}
                          extTableLabel={col.source.type === 'ext' ? (extTableLabelMap.get(col.source.configId) || col.source.configId) : ''}
                          extConfig={renderExtConfig(col)}
                          candleConfig={renderCandleConfig(col)}
                          intradayConfig={renderIntradayConfig(col)}
                          strategiesConfig={renderStrategiesConfig(col)}
                          showStandaloneToggle={showStandaloneToggle}
                          onToggleStandalone={toggleStandalone}
                        />
                      ))}
                    </SortableContext>
                  </DndContext>
                </div>
              )}

              <div className="border-t border-border my-1" />

              <div>
                <div className="flex items-center gap-1.5 px-1 py-1.5">
                  <Plus className="h-3 w-3 text-muted" />
                  <span className="text-[10px] font-semibold text-muted uppercase tracking-wider">{builtinSectionLabel}</span>
                </div>

                {filteredGroups.map(group => {
                  const isExpanded = expandedGroups.has(group.id)
                  const groupCols = group.keys
                    .map(k => keyToId.get(k))
                    .filter((id): id is string => !!id)
                    .map(id => colById.get(id))
                    .filter((c): c is ColumnConfig => !!c)
                  if (groupCols.length === 0) return null
                  const visCount = groupCols.filter(c => c.visible).length

                  return (
                    <div key={group.id}>
                      <button
                        onClick={() => toggleGroup(group.id)}
                        className="flex items-center gap-1.5 w-full px-1 py-1.5 rounded hover:bg-elevated/30 text-left transition-colors"
                      >
                        {isExpanded ? <ChevronDown className="h-3 w-3 text-muted" /> : <ChevronRight className="h-3 w-3 text-muted" />}
                        {group.icon && <span className="text-[11px]">{group.icon}</span>}
                        <span className="text-[11px] font-medium text-secondary">{group.label}</span>
                        <span className="text-[10px] text-muted ml-auto">{visCount}/{groupCols.length}</span>
                      </button>
                      <AnimatePresence>
                        {isExpanded && (
                          <motion.div
                            initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }}
                            exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.12 }}
                            className="overflow-hidden"
                          >
                            <div className="space-y-0.5 pb-0.5">
                              {groupCols.map(col => renderBuiltinRow(col))}
                            </div>
                          </motion.div>
                        )}
                      </AnimatePresence>
                    </div>
                  )
                })}
              </div>

              {showExtColumns && extTables.length > 0 && (
                <div className="pt-1 border-t border-border mt-1">
                  <div className="flex items-center gap-1.5 px-1 py-1.5">
                    <Database className="h-3 w-3 text-accent/70" />
                    <span className="text-[10px] font-semibold text-accent/80 uppercase tracking-wider">扩展数据列</span>
                  </div>
                  <div className="space-y-0.5">
                    {extTables.map(table => {
                      const isExpanded = expandedTables.has(table.id)
                      const dataFields = table.columns
                        .filter(c => !['symbol', 'code', 'date'].includes(c.name))
                        .filter(c => extFieldFilter ? extFieldFilter(c) : true)
                      const activeCount = dataFields.filter(f => {
                        const colId = `ext:${table.id}:${f.name}`
                        return columns.some(c => c.id === colId && c.visible)
                      }).length
                      return (
                        <div key={table.id}>
                          <button
                            onClick={() => toggleTableExpand(table.id)}
                            className="flex items-center gap-1.5 w-full px-1 py-1.5 rounded hover:bg-elevated/30 text-left transition-colors"
                          >
                            {isExpanded ? <ChevronDown className="h-3 w-3 text-muted" /> : <ChevronRight className="h-3 w-3 text-muted" />}
                            <Database className="h-3 w-3 text-accent shrink-0" />
                            <span className="text-[11px] font-medium text-secondary flex-1">{table.label}</span>
                            <span className="text-[10px] text-muted">{table.mode === 'snapshot' ? '快照' : '时序'}</span>
                            {activeCount > 0 && (
                              <span className="text-[10px] text-accent font-medium">{activeCount}</span>
                            )}
                          </button>
                          <AnimatePresence>
                            {isExpanded && (
                              <motion.div
                                initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }}
                                exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.15 }}
                                className="overflow-hidden"
                              >
                                <div className="space-y-0.5 pb-0.5">
                                  {dataFields.map(field => renderExtFieldRow(table.id, field))}
                                </div>
                              </motion.div>
                            )}
                          </AnimatePresence>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )}

              {showExtColumns && extTables.length === 0 && extSchema.isSuccess && (
                <div className="text-xs text-muted text-center py-4">
                  暂无扩展数据表，可在「数据」页面创建
                </div>
              )}
            </div>
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}
