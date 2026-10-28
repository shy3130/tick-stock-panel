/**
 * 通用列表列自定义组件。
 *
 * 布局：
 * - 上半区「已启用」：已启用的列，@dnd-kit 拖拽排序
 * - 下半区「内置列」：按业务分组折叠
 * - 底部「扩展数据列」：复用 ext_data schema，按需添加字段
 */
import React, { useState, useCallback, useEffect, useMemo, useRef } from 'react'
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
import { X, GripVertical, Plus, ChevronDown, ChevronRight, Database, Settings2, Search } from 'lucide-react'
import { api } from '@/lib/api'
import { useQuery } from '@tanstack/react-query'
import { QK } from '@/lib/queryKeys'
import type { ColumnConfig, ColumnGroup, ExtColumnDisplayConfig, CandleColumnConfig, IntradayColumnConfig, AuctionBarWidth, ChartLinkConfig } from '@/lib/list-columns'
import { resolveCandleConfig, resolveIntradayConfig, hasAuctionVolumeField, isAuctionActive, AUCTION_WIDTH_RATIOS, resolveChartLink, LINK_HEIGHT_BOUNDS, restoreColumnsFromBaseline } from '@/lib/list-columns'
import { MiniIntraday } from '@/components/stock-table/MiniIntraday'
import { MiniCandlestick } from '@/components/stock-table/MiniCandlestick'
import type { MinuteKlineRow, KlineRow } from '@/lib/api'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'

export interface ListColumnCustomizerProps {
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
  /**
   * 是否禁用遮罩背景模糊 (backdrop-blur)。默认 false。
   * 信息条场景 (宿主含 Canvas K线/分时图) 传 true：blur 会对 Canvas 做逐帧 GPU 合成，
   * 是抽屉打开/滑动卡顿的主因；改纯半透明遮罩后合成层锐减。
   * 策略/自选页背后是 DOM 表格，blur 开销可忽略，保持默认。
   */
  disableBackdropBlur?: boolean
  /**
   * 「默认方案」快照的读写接口 (可选)。传入后头部出现「保存为默认方案」按钮,
   * 且「恢复默认」回到这份快照 (未保存过时回退内置默认)。
   * 不传则只提供内置默认恢复 (兼容旧调用方)。
   */
  baselineStore?: {
    get: () => ColumnConfig[] | null
    set: (cols: ColumnConfig[]) => void
  }
}

/** 判断扩展数据字段类型是否为数字(int/float/double/number/decimal 等)。
 * 旧列 source 无 fieldType 时默认 true(放宽), 让旧列也能配置数字格式 ——
 * 若列实际非数字, 渲染时 typeof val==='number' 判断会跳过格式化, 无副作用。 */
/** 数字输入: 聚焦期间完全自由编辑 (随意清空/输入中间态), 失焦时统一收敛 —
 *  合法数字钳制到 [min, max] 后提交; 空或非数字恢复聚焦前的数字。
 *  受控父值在聚焦期间不干扰草稿, 避免逐位输入被即时钳制/回填打断。 */
export function RangeNumberInput({
  value, min, max, step, onCommit, className,
}: {
  value: number
  min: number
  max: number
  step?: number
  onCommit: (v: number) => void
  className?: string
}) {
  const [draft, setDraft] = useState<string | null>(null)
  // 同步 ref: onBlur 在 setState 之后触发, 闭包里的 draft 会过期
  const draftRef = useRef<string | null>(null)
  const setDraftBoth = (v: string | null) => {
    draftRef.current = v
    setDraft(v)
  }

  return (
    <input
      type="number" min={min} max={max} step={step}
      value={draft ?? String(value)}
      onFocus={() => setDraftBoth(String(value))}
      onChange={e => setDraftBoth(e.target.value)}
      onBlur={() => {
        const raw = draftRef.current
        setDraftBoth(null)
        if (raw == null) return
        const num = Number(raw)
        // 空 / 非数字 → 不提交, 草稿清空后自动显示聚焦前的数字
        if (raw.trim() === '' || !Number.isFinite(num)) return
        onCommit(Math.max(min, Math.min(max, num)))
      }}
      onKeyDown={e => {
        if (e.key === 'Enter') e.currentTarget.blur()
        if (e.key === 'Escape') {
          setDraftBoth(null)  // 丢弃草稿 → onBlur 读到 null, 恢复原值
          e.currentTarget.blur()
        }
      }}
      className={className}
    />
  )
}

function isNumericFieldType(ft?: string): boolean {
  if (!ft) return true
  const t = ft.toLowerCase()
  // 明确是文本类则不显示
  if (['str', 'string', 'text', 'char', 'varchar', 'date', 'time', 'bool', 'boolean'].some(k => t.includes(k))) {
    return false
  }
  return true
}

/** 模块级空数组常量：extSchema 未加载时用，避免 `?? []` 每次创建新引用导致 useMemo 失效。 */
const EMPTY_EXT_TABLES: readonly { id: string; label: string; mode: string; columns: { name: string; label: string; type: string }[] }[] = []

/** 分时列「效果预览」固定样例数据 (零网络依赖): 6 根分钟K 围绕昨收 10 波动,
 *  竞价价格/匹配量/未匹配量各给少量点 — 配置面板据此即时反映当前配置效果 */
const PREVIEW_ROWS: MinuteKlineRow[] = [
  { datetime: '2026-10-20 09:31:00', open: 9.96, high: 9.97, low: 9.95, close: 9.96, volume: 320, amount: 3187 },
  { datetime: '2026-10-20 09:32:00', open: 9.90, high: 9.91, low: 9.89, close: 9.90, volume: 540, amount: 5346 },
  { datetime: '2026-10-20 09:33:00', open: 9.94, high: 9.95, low: 9.93, close: 9.94, volume: 210, amount: 2077 },
  { datetime: '2026-10-20 09:34:00', open: 10.02, high: 10.03, low: 10.01, close: 10.02, volume: 760, amount: 7615 },
  { datetime: '2026-10-20 09:35:00', open: 10.08, high: 10.09, low: 10.07, close: 10.08, volume: 900, amount: 9072 },
  { datetime: '2026-10-20 09:36:00', open: 10.05, high: 10.06, low: 10.04, close: 10.05, volume: 430, amount: 4321 },
]
/** 竞价预览序列: 价格 3 点 (<2 点会退化, 预览需要成线) + 匹配量/未匹配量各 2 点 */
const PREVIEW_AUCTION = {
  price: [['09:15:00', 9.92], ['09:20:00', 9.98], ['09:25:00', 10.02]] as [string, number][],
  match: [['09:15:00', 120], ['09:25:00', 480]] as [string, number][],
  unmatched: [['09:15:00', 260], ['09:25:00', -180]] as [string, number][],
}
/** 日k列「效果预览」固定样例数据 (零网络依赖): 12 根日 K, 涨跌交替、
 *  成交量有高有低 — 配置面板据此即时反映宽高与量带效果 */
const PREVIEW_KLINES: KlineRow[] = [
  { date: '2026-09-24', open: 9.78, high: 9.86, low: 9.74, close: 9.84, volume: 2100 },
  { date: '2026-09-25', open: 9.85, high: 9.98, low: 9.82, close: 9.95, volume: 3500 },
  { date: '2026-09-26', open: 9.96, high: 10.04, low: 9.90, close: 9.92, volume: 1800 },
  { date: '2026-09-29', open: 9.90, high: 9.96, low: 9.80, close: 9.83, volume: 2600 },
  { date: '2026-09-30', open: 9.82, high: 9.88, low: 9.76, close: 9.86, volume: 1400 },
  { date: '2026-10-08', open: 9.88, high: 10.10, low: 9.86, close: 10.06, volume: 5200 },
  { date: '2026-10-09', open: 10.08, high: 10.18, low: 10.02, close: 10.14, volume: 4300 },
  { date: '2026-10-12', open: 10.15, high: 10.16, low: 10.01, close: 10.05, volume: 3100 },
  { date: '2026-10-13', open: 10.04, high: 10.08, low: 9.94, close: 9.97, volume: 2400 },
  { date: '2026-10-14', open: 9.96, high: 10.02, low: 9.92, close: 10.00, volume: 1900 },
  { date: '2026-10-15', open: 10.00, high: 10.12, low: 9.99, close: 10.10, volume: 4600 },
  { date: '2026-10-16', open: 10.11, high: 10.22, low: 10.06, close: 10.18, volume: 5800 },
]

function SortableActiveCol({ col, onRemove, onConfig, configOpen, extTableLabel, extConfig, candleConfig: candlePanel, intradayConfig: intradayPanel, strategiesConfig, showStandaloneToggle, onToggleStandalone }: {
  col: ColumnConfig
  onRemove: (id: string) => void
  onConfig: (id: string | null) => void
  configOpen: boolean
  extTableLabel: string
  extConfig: () => React.ReactNode
  candleConfig: () => React.ReactNode
  intradayConfig: () => React.ReactNode
  strategiesConfig: () => React.ReactNode
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
  const label = isExt
    ? `${col.label}（${extTableLabel}）`
    : col.label

  return (
    <>
      <div
        ref={setNodeRef}
        data-col-row={col.id}
        style={{
          transform: CSS.Transform.toString(transform),
          transition,
          opacity: isDragging ? 0.5 : 1,
          zIndex: isDragging ? 10 : undefined,
        }}
        className={`flex items-center gap-2 px-2 py-1.5 rounded-lg group border ${
          isDragging ? 'bg-elevated shadow-lg border-transparent'
            : configOpen ? 'bg-accent/8 border-accent/30'
              : 'border-transparent hover:bg-elevated/50'
        }`}
      >
        <span
          {...attributes}
          {...listeners}
          className="cursor-grab active:cursor-grabbing text-muted hover:text-foreground transition-colors shrink-0"
        >
          <GripVertical className="h-3.5 w-3.5" />
        </span>
        <button
          onClick={() => hasConfig && onConfig(configOpen ? null : col.id)}
          disabled={!hasConfig}
          className={`flex-1 min-w-0 text-left ${hasConfig ? 'cursor-pointer' : 'cursor-default'}`}
          title={hasConfig ? '点击配置' : undefined}
        >
          <span className="block text-xs text-foreground truncate">{label}</span>
        </button>
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
            className={`shrink-0 p-1 rounded transition-colors ${configOpen ? 'text-accent bg-accent/10' : 'text-muted hover:text-accent hover:bg-elevated'}`}
            title={configOpen ? '收起配置' : '配置'}
          >
            <Settings2 className="h-3.5 w-3.5" />
          </button>
        )}
        <button
          onClick={() => onRemove(col.id)}
          className="shrink-0 p-1 rounded text-muted hover:text-danger hover:bg-elevated transition-colors"
          title="移出列表"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      {hasConfig && configOpen && (
        <div className="mb-1">{isExt ? extConfig() : isCandle ? candlePanel() : isIntraday ? intradayPanel() : strategiesConfig()}</div>
      )}
    </>
  )
}

export function ListColumnCustomizerInner({
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
  disableBackdropBlur = false,
  baselineStore,
}: ListColumnCustomizerProps) {
  const extSchema = useQuery({
    queryKey: QK.extDataSchemaAll,
    queryFn: api.extDataSchemaAll,
    enabled: open && showExtColumns,
    staleTime: 60_000,
  })
  const backdrop = useDialogBackdrop(onClose)

  /** 时序扩展表的生效粒度: schema 透出值优先; 旧后端无该字段时退回 time_field 判定。
 *  snapshot 表 (无历史分区) 与未知表 → null, 不提供序列形态配置。 */
function extGranularityOf(
  tables: readonly { id: string; mode?: string; time_field?: string | null; granularity?: 'intraday' | 'daily' | null }[],
  configId: string,
): 'intraday' | 'daily' | null {
  const t = tables.find(x => x.id === configId)
  if (!t || t.mode !== 'timeseries') return null
  return t.granularity ?? (t.time_field ? 'intraday' : 'daily')
}

// 多盘表识别用 ref 镜像 (schema 查询数据), 让 addExtColumn 保持稳定引用
  const extTablesRef = useRef<readonly { id: string; time_field?: string | null; granularity?: 'intraday' | 'daily' | null }[]>([])
  extTablesRef.current = extSchema.data?.items ?? []

  // columns/onChange 用 ref 镜像，让下方多数 useCallback 空依赖（addExtColumn 例外，
  // 依赖 props.extColumnAlign）。callback 引用稳定后，columns 变更不再导致
  // SortableActiveCol 等子组件因 props 变化而无谓重渲染。
  const columnsRef = useRef(columns)
  columnsRef.current = columns
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange

  const [searchQuery, setSearchQuery] = useState('')
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set())
  const [configOpenId, setConfigOpenId] = useState<string | null>(null)
  const [expandedTables, setExpandedTables] = useState<Set<string>>(new Set())
  /** 「保存为默认方案」成功时刻 — 只用于给按钮一个短暂的成功反馈 */
  const [baselineSavedAt, setBaselineSavedAt] = useState<number | null>(null)
  /** 恢复默认的二次确认弹窗 */
  const [confirmReset, setConfirmReset] = useState(false)
  const hasBaseline = !!baselineStore?.get()

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
    const cols = columnsRef.current
    onChangeRef.current(cols.map(c =>
      c.id === colId && !c.pinned ? { ...c, visible: !c.visible } : c
    ))
  }, [])

  const toggleStandalone = useCallback((colId: string) => {
    const cols = columnsRef.current
    onChangeRef.current(cols.map(c =>
      c.id === colId ? { ...c, standalone: !c.standalone } : c
    ))
  }, [])

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )

  const handleDragEnd = useCallback((event: DragEndEvent) => {
    const { active, over } = event
    if (!over || active.id === over.id) return
    const cols = columnsRef.current
    const reorderCols = cols.filter(c => !c.pinned)
    const pinnedCols = cols.filter(c => c.pinned)
    const ids = reorderCols.map(c => c.id)
    const oldIdx = ids.indexOf(active.id as string)
    const newIdx = ids.indexOf(over.id as string)
    if (oldIdx < 0 || newIdx < 0) return
    const reordered = arrayMove(reorderCols, oldIdx, newIdx)
    onChangeRef.current([...pinnedCols, ...reordered])
  }, [])

  const addExtColumn = useCallback((configId: string, fieldName: string, fieldLabel?: string, fieldType?: string) => {
    const cols = columnsRef.current
    const colId = `ext:${configId}:${fieldName}`
    if (cols.some(c => c.id === colId)) {
      toggleVisible(colId)
      return
    }
    // 日内多盘表 (生效粒度 intraday, 如集合竞价) 的数值列: 添加即默认分时式迷你图
    // (与「分时」列同尺寸 150×80), 文字形态在列配置里按需切换; 日级表默认文字值
    const multiSnapshot = !!extTablesRef.current.find(
      t => t.id === configId
        && (t.granularity === 'intraday' || (t.granularity == null && t.time_field)),
    )
    const newCol: ColumnConfig = {
      id: colId,
      source: { type: 'ext', configId, fieldName, fieldLabel, fieldType },
      label: fieldLabel || fieldName,
      visible: true,
      align: multiSnapshot && isNumericFieldType(fieldType) ? 'center' : extColumnAlign,
      ...(multiSnapshot && isNumericFieldType(fieldType)
        ? { extDisplay: { displayMode: 'text' as const, snapshotRender: 'line' as const, chartWidth: 150, chartHeight: 80 } }
        : {}),
    }
    const actionIdx = cols.findIndex(c => c.id === 'builtin:action')
    if (actionIdx >= 0) {
      const next = [...cols]
      next.splice(actionIdx, 0, newCol)
      onChangeRef.current(next)
    } else {
      onChangeRef.current([...cols, newCol])
    }
  }, [extColumnAlign, toggleVisible])

  const hideColumn = useCallback((colId: string) => {
    const cols = columnsRef.current
    onChangeRef.current(cols.map(c => c.id === colId ? { ...c, visible: false } : c))
  }, [])

  const updateExtDisplay = useCallback((colId: string, patch: Partial<ExtColumnDisplayConfig>) => {
    const cols = columnsRef.current
    onChangeRef.current(cols.map(c => {
      if (c.id !== colId) return c
      return { ...c, extDisplay: { displayMode: 'tag', ...(c.extDisplay || {}), ...patch } }
    }))
  }, [])

  const resetExtDisplay = useCallback((colId: string) => {
    const cols = columnsRef.current
    onChangeRef.current(cols.map(c => {
      if (c.id !== colId) return c
      const { extDisplay, ...rest } = c
      return rest
    }))
  }, [])

  /** 恢复默认: 优先回到用户保存的「默认方案」快照; 没保存过才回退内置默认。
   *  快照恢复规则见 restoreColumnsFromBaseline (纯函数, 已单测)。 */
  const resetAllColumns = useCallback(() => {
    const prev = columnsRef.current
    setConfigOpenId(null)

    const restored = restoreColumnsFromBaseline(prev, baselineStore?.get() ?? null)
    if (restored) {
      onChangeRef.current(restored)
      return
    }

    // 无快照: 内置列回到 groups 声明的默认显隐, 清空所有列级配置
    const defaultVisible = new Set<string>()
    for (const g of groups) for (const k of g.keys) {
      const id = keyToId.get(k)
      if (id) defaultVisible.add(id)
    }
    const next = prev.map(c => {
      const { candleConfig, intradayConfig, extDisplay, standalone, ...rest } = c
      if (c.pinned) return c
      if (c.source.type === 'ext') return { ...rest, visible: false }
      return { ...rest, visible: defaultVisible.has(c.id) }
    })
    onChangeRef.current(next)
  }, [groups, keyToId, baselineStore])

  /** 把当前列配置存为新的「默认方案」快照 */
  const saveAsBaseline = useCallback(() => {
    if (!baselineStore) return
    baselineStore.set(columnsRef.current.filter(c => !c.pinned))
    setBaselineSavedAt(Date.now())
  }, [baselineStore])

  const updateCandleConfig = useCallback((colId: string, patch: Partial<CandleColumnConfig>) => {
    const cols = columnsRef.current
    onChangeRef.current(cols.map(c => {
      if (c.id !== colId) return c
      return { ...c, candleConfig: { ...c.candleConfig, ...patch } }
    }))
  }, [])

  const resetCandleConfig = useCallback((colId: string) => {
    const cols = columnsRef.current
    onChangeRef.current(cols.map(c => {
      if (c.id !== colId) return c
      const { candleConfig, ...rest } = c
      return rest
    }))
  }, [])

  const updateIntradayConfig = useCallback((colId: string, patch: Partial<IntradayColumnConfig>) => {
    const cols = columnsRef.current
    onChangeRef.current(cols.map(c => {
      if (c.id !== colId) return c
      return { ...c, intradayConfig: { ...c.intradayConfig, ...patch } }
    }))
  }, [])

  // 联动配置写入: 只 map 分时一列 (link 的唯一宿主), 一次 onChange; 不改 visible
  const updateChartLink = useCallback((patch: Partial<ChartLinkConfig>) => {
    const cols = columnsRef.current
    onChangeRef.current(cols.map(c => {
      if (!(c.source.type === 'builtin' && c.source.key === 'intraday')) return c
      return { ...c, intradayConfig: { ...c.intradayConfig, link: { ...c.intradayConfig?.link, ...patch } } }
    }))
  }, [])

  const resetIntradayConfig = useCallback((colId: string) => {
    const cols = columnsRef.current
    onChangeRef.current(cols.map(c => {
      if (c.id !== colId) return c
      // 保留 link: 联动是配对级意图, 重置分时自身字段不应静默改变日k渲染。
      // 仅剩 {link} 时 intradayConfig 仍为真值, 「恢复默认」按钮继续显示且幂等
      // (与既有空对象保留行为一致); 彻底取消联动走共享块的高度联动开关。
      const link = c.intradayConfig?.link
      if (link) return { ...c, intradayConfig: { link } }
      const { intradayConfig, ...rest } = c
      return rest
    }))
  }, [])

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

  const extTables = extSchema.data?.items ?? EMPTY_EXT_TABLES
  const extTableLabelMap = useMemo(() => new Map(extTables.map(t => [t.id, t.label])), [extTables])

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
        {/* 时序表的序列形态: 日内多盘 (集合竞价等) / 日级别 (近N日) 按生效粒度分流 */}
        {col.source.type === 'ext' && extGranularityOf(extTables, (col.source as { configId: string }).configId) === 'intraday' && (
          <>
            <div className="border-t border-border/40 pt-2 mt-1 text-[10px] text-muted">日内多盘</div>
            <label className="flex items-center gap-2 text-xs">
              <span className="text-secondary w-16 shrink-0">展示形态</span>
              <select
                value={col.extDisplay?.snapshotRender ?? 'text'}
                onChange={e => updateExtDisplay(col.id, { snapshotRender: e.target.value as 'text' | 'bar' | 'line' })}
                className="flex-1 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 focus:outline-none focus:border-accent/50"
              >
                <option value="line">折线图 (分时式)</option>
                <option value="bar">柱状图 (逐盘幅度)</option>
                <option value="text">文字 (选定时刻值)</option>
              </select>
            </label>
            {(col.extDisplay?.snapshotRender ?? 'text') === 'text' ? (
              <label className="flex items-center gap-2 text-xs">
                <span className="text-secondary w-16 shrink-0">取盘时刻</span>
                <input
                  type="time"
                  value={(col.extDisplay?.snapshotAt ?? '').slice(0, 5)}
                  onChange={e => updateExtDisplay(col.id, { snapshotAt: e.target.value ? `${e.target.value}:00` : undefined })}
                  title="取该时刻或之前最近一盘的值; 留空=末盘定型值"
                  className="flex-1 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 focus:outline-none focus:border-accent/50"
                />
                <span className="text-[10px] text-muted shrink-0">留空=末盘</span>
              </label>
            ) : (
              <div className="flex items-center gap-2 text-xs">
                <label className="flex items-center gap-1">
                  <span className="text-secondary shrink-0">宽</span>
                  <RangeNumberInput
                    value={col.extDisplay?.chartWidth ?? 150}
                    min={60} max={300} step={10}
                    onCommit={v => updateExtDisplay(col.id, { chartWidth: v })}
                    className="w-16 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 text-center focus:outline-none focus:border-accent/50"
                  />
                </label>
                <label className="flex items-center gap-1">
                  <span className="text-secondary shrink-0">高</span>
                  <RangeNumberInput
                    value={col.extDisplay?.chartHeight ?? 80}
                    min={24} max={160} step={4}
                    onCommit={v => updateExtDisplay(col.id, { chartHeight: v })}
                    className="w-16 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 text-center focus:outline-none focus:border-accent/50"
                  />
                </label>
                <span className="text-[10px] text-muted">px · 当天全序列</span>
              </div>
            )}
          </>
        )}
        {col.source.type === 'ext' && extGranularityOf(extTables, (col.source as { configId: string }).configId) === 'daily' && (
          <>
            <div className="border-t border-border/40 pt-2 mt-1 text-[10px] text-muted">日级序列</div>
            <label className="flex items-center gap-2 text-xs">
              <span className="text-secondary w-16 shrink-0">展示形态</span>
              <select
                value={col.extDisplay?.snapshotRender ?? 'text'}
                onChange={e => updateExtDisplay(col.id, { snapshotRender: e.target.value as 'text' | 'bar' | 'line' })}
                className="flex-1 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 focus:outline-none focus:border-accent/50"
              >
                <option value="bar">柱状图 (近N日每日值)</option>
                <option value="line">折线图 (近N日)</option>
                <option value="text">文字 (最新值)</option>
              </select>
            </label>
            {(col.extDisplay?.snapshotRender ?? 'text') !== 'text' && (
              <div className="flex items-center gap-2 text-xs">
                <label className="flex items-center gap-1">
                  <span className="text-secondary shrink-0">天数</span>
                  <RangeNumberInput
                    value={col.extDisplay?.chartDays ?? 10}
                    min={3} max={60} step={1}
                    onCommit={v => updateExtDisplay(col.id, { chartDays: v })}
                    className="w-16 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 text-center focus:outline-none focus:border-accent/50"
                  />
                </label>
                <label className="flex items-center gap-1">
                  <span className="text-secondary shrink-0">宽</span>
                  <RangeNumberInput
                    value={col.extDisplay?.chartWidth ?? 150}
                    min={60} max={300} step={10}
                    onCommit={v => updateExtDisplay(col.id, { chartWidth: v })}
                    className="w-16 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 text-center focus:outline-none focus:border-accent/50"
                  />
                </label>
                <label className="flex items-center gap-1">
                  <span className="text-secondary shrink-0">高</span>
                  <RangeNumberInput
                    value={col.extDisplay?.chartHeight ?? 80}
                    min={24} max={160} step={4}
                    onCommit={v => updateExtDisplay(col.id, { chartHeight: v })}
                    className="w-16 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 text-center focus:outline-none focus:border-accent/50"
                  />
                </label>
                <span className="text-[10px] text-muted">px · 近N日全序列</span>
              </div>
            )}
          </>
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

  // 日k / 分时 高度联动共享块: 在两个面板各渲染一次, 状态同读 resolveChartLink(columns)、
  // 同写分时一列的 intradayConfig.link —— 单一副本, 两面板不可能显示不一致。
  // host 决定开启联动时的种子高度取哪个面板列的当前高度 (不静默改另一列)。
  // 只管高度: 量带各列自管 (开关在哪个面板就管哪个图), 不做跨列共享开关。
  const renderLinkControls = (host: 'candle' | 'intraday') => {
    const link = resolveChartLink(columns)
    const candleCol = columns.find(c => c.source.type === 'builtin' && c.source.key === 'candle')
    const intradayCol = columns.find(c => c.source.type === 'builtin' && c.source.key === 'intraday')
    const candleRes = resolveCandleConfig(candleCol?.candleConfig, link)
    const intradayRes = resolveIntradayConfig(intradayCol?.intradayConfig, link)
    const linkOn = link?.linkHeight === true
    const seedHeight = host === 'candle' ? candleRes.enabledHeight : intradayRes.height
    // 统一高度显示值: link.height 为有限数时钳制显示; 脏值回退种子 (提交后恢复统一)
    const unifiedHeight = typeof link?.height === 'number' && Number.isFinite(link.height)
      ? Math.min(LINK_HEIGHT_BOUNDS.max, Math.max(LINK_HEIGHT_BOUNDS.min, link.height))
      : seedHeight

    const toggleCls = (on: boolean) => `relative inline-flex h-4 w-7 items-center rounded-full transition-colors duration-200 cursor-pointer ${
      on ? 'bg-accent' : 'bg-elevated'
    }`
    const knobCls = (on: boolean) => `inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform duration-200 ${
      on ? 'translate-x-[14px]' : 'translate-x-0.5'
    }`

    return (
      <>
        <div className="border-t border-border/40 pt-2 mt-1 text-[10px] text-muted">
          {host === 'candle' ? '与分时联动' : '与日k联动'}
        </div>
        <div className="flex items-center justify-between">
          <div className="text-[10px] text-muted">统一高度</div>
          <button
            type="button"
            onClick={() => updateChartLink(linkOn ? { linkHeight: false } : { linkHeight: true, height: seedHeight })}
            className={toggleCls(linkOn)}
            aria-pressed={linkOn}
            aria-label="统一日k与分时的展开高度"
          >
            <span className={knobCls(linkOn)} />
          </button>
        </div>
        {linkOn ? (
          <>
            <label className="flex items-center gap-2 text-xs">
              <span className="text-secondary w-16 shrink-0">统一高度</span>
              <RangeNumberInput
                value={unifiedHeight}
                min={LINK_HEIGHT_BOUNDS.min} max={LINK_HEIGHT_BOUNDS.max}
                onCommit={v => updateChartLink({ height: v })}
                className="flex-1 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 focus:outline-none focus:border-accent/50 tabular-nums"
              />
            </label>
            <div className="text-[10px] text-muted leading-relaxed">
              两列共用展开高度；宽度与收起高度仍在各自列设置。
            </div>
          </>
        ) : (
          <div className="text-[10px] text-muted leading-relaxed">
            当前 日k {candleRes.enabledHeight} · 分时 {intradayRes.height}，开启后统一。
          </div>
        )}
        {host === 'candle' && !intradayCol?.visible && (
          <div className="text-[10px] text-muted leading-relaxed">分时列当前未显示，联动配置仍会保存。</div>
        )}
        {host === 'intraday' && !candleCol?.visible && (
          <div className="text-[10px] text-muted leading-relaxed">日k列当前未显示，联动配置仍会保存。</div>
        )}
      </>
    )
  }

  const renderCandleConfig = (col: ColumnConfig) => {
    // resolved 值必须传入 link: 「开启高度」显示 / 预览高度 / 联动 off 态提示都
    // 需要 link 生效后的值 (漏传会显示旧高度而非联动高度)
    const link = resolveChartLink(columns)
    const linkOn = link?.linkHeight === true
    const cfg = resolveCandleConfig(col.candleConfig, link)
    // 数值输入: onChange 存原始值(不钳制, 允许自由输入), onBlur 钳制边界
    const numInput = (
      field: 'enabledWidth' | 'enabledHeight' | 'disabledWidth' | 'disabledHeight' | 'days',
      label: string,
    ) => {
      // 高度联动开启时「开启高度」只读 (由联动统一设置, 旧值未删, 关闭即还原)
      const locked = field === 'enabledHeight' && linkOn
      return (
        <label key={field} className="flex items-center gap-2 text-xs">
          <span className="text-secondary w-16 shrink-0">{label}</span>
          <input
            type="number"
            value={locked ? cfg.enabledHeight : col.candleConfig?.[field] ?? cfg[field]}
            readOnly={locked}
            title={locked ? '由联动统一设置' : undefined}
            onChange={e => {
              if (locked) return // 只读态不提交: 显示值是联动高度, 写入会覆盖旧 enabledHeight
              const raw = e.target.value
              // 空字符串 → 存 undefined (回退到默认值显示); 否则存原始数字 (不钳制)
              updateCandleConfig(col.id, { [field]: raw === '' ? undefined : Number(raw) } as Partial<CandleColumnConfig>)
            }}
            onBlur={e => {
              if (locked) return // 只读 input 失焦仍触发 blur: 不跳过会把联动高度写进旧字段, 关闭联动后无法无损还原
              const raw = e.target.value
              // 失焦时钳制: 过大取上限、过小取最小值
              const merged = resolveCandleConfig({ ...col.candleConfig, [field]: raw === '' ? undefined : Number(raw) }, link)
              updateCandleConfig(col.id, { [field]: merged[field] } as Partial<CandleColumnConfig>)
            }}
            className={`flex-1 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 focus:outline-none focus:border-accent/50 tabular-nums${locked ? ' opacity-60' : ''}`}
          />
        </label>
      )
    }
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
          {renderLinkControls('candle')}
          {/* 成交量副图: 本列自管 (与分时各自独立) */}
          <div className="border-t border-border/40 pt-2 mt-1 flex items-center justify-between">
            <div className="text-[10px] text-muted">成交量副图</div>
            <button
              type="button"
              onClick={() => updateCandleConfig(col.id, { volume: !cfg.volume })}
              className={`relative inline-flex h-4 w-7 items-center rounded-full transition-colors duration-200 cursor-pointer ${cfg.volume ? 'bg-accent' : 'bg-elevated'}`}
              aria-pressed={cfg.volume}
              aria-label="显示成交量副图"
            >
              <span className={`inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform duration-200 ${cfg.volume ? 'translate-x-[14px]' : 'translate-x-0.5'}`} />
            </button>
          </div>
          <div className="text-[10px] text-muted leading-relaxed">
            开启后在价格区下方显示成交量柱；分时副图在分时列单独设置。
          </div>
          {/* 效果预览: 固定样例 K, 宽高与量带即时反映 (阈值与列表同源: 带宽 ≥3px 即画) */}
          <div className="border-t border-border/40 pt-2 mt-1">
            <div className="text-[10px] text-muted mb-1.5">效果预览（示例日K，配置改动即时反映）</div>
            <div className="rounded bg-base border border-border/60 flex items-center justify-center overflow-hidden">
              <MiniCandlestick
                rows={PREVIEW_KLINES}
                width={Math.min(cfg.enabledWidth, 260)}
                height={cfg.enabledHeight}
                volume={cfg.volume}
              />
            </div>
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
    // 传 link 让高度/量带显示联动生效值 (link 宿主就是本列, 同一副本)
    const link = resolveChartLink(columns)
    const linkOn = link?.linkHeight === true
    const cfg = resolveIntradayConfig(col.intradayConfig, link)
    const numInput = (
      field: 'width' | 'height',
      label: string,
    ) => {
      // 高度联动开启时「高度」只读 (由联动统一设置, 旧值未删, 关闭即还原)
      const locked = field === 'height' && linkOn
      return (
        <label key={field} className="flex items-center gap-2 text-xs">
          <span className="text-secondary w-16 shrink-0">{label}</span>
          <input
            type="number"
            value={locked ? cfg.height : col.intradayConfig?.[field] ?? cfg[field]}
            readOnly={locked}
            title={locked ? '由联动统一设置' : undefined}
            onChange={e => {
              if (locked) return // 只读态不提交: 显示值是联动高度, 写入会覆盖旧 height
              const raw = e.target.value
              updateIntradayConfig(col.id, { [field]: raw === '' ? undefined : Number(raw) } as Partial<IntradayColumnConfig>)
            }}
            onBlur={e => {
              if (locked) return // 只读 input 失焦仍触发 blur: 不跳过会把联动高度写进旧字段, 关闭联动后无法无损还原
              const raw = e.target.value
              const merged = resolveIntradayConfig({ ...col.intradayConfig, [field]: raw === '' ? undefined : Number(raw) }, link)
              updateIntradayConfig(col.id, { [field]: merged[field] } as Partial<IntradayColumnConfig>)
            }}
            className={`flex-1 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 focus:outline-none focus:border-accent/50 tabular-nums${locked ? ' opacity-60' : ''}`}
          />
        </label>
      )
    }

    // 竞价叠加: 仅「时序 + 配置了时间列」的表可选 (竞价是日内多盘序列,
    // 每日截面表没有时间轴, 无法画轨迹)。
    const auction = col.intradayConfig?.auction
    const updateAuction = (patch: Partial<NonNullable<IntradayColumnConfig['auction']>>) =>
      updateIntradayConfig(col.id, { auction: { ...auction, ...patch } })
    // R2 强制联动: 竞价已配量字段 → 量带本身恒显示 (渲染侧竞价量柱本就不受
    // volume 开关控制)。开关只控制分钟量柱, 保持可用; forced 用于开关提示与预览
    const forced = hasAuctionVolumeField(auction)
    const multiTables = extTables.filter(t => (t as { time_field?: string | null }).time_field != null)
    const auctionTable = multiTables.find(t => t.id === auction?.configId)
    // schema-all 的列类型键是 dtype (api.ts 的 type 标注为历史遗留), 两者都认
    const numericFields = (auctionTable?.columns ?? []).filter(c =>
      isNumericFieldType((c as { dtype?: string; type?: string }).dtype ?? (c as { type?: string }).type))
    const fieldSelect = (
      label: string,
      key: 'priceField' | 'matchField' | 'unmatchedField',
      hint?: string,
    ) => (
      <label className="flex items-center gap-2 text-xs">
        <span className="text-secondary w-16 shrink-0">{label}</span>
        <select
          value={auction?.[key] ?? ''}
          onChange={e => updateAuction({ [key]: e.target.value || undefined })}
          className="flex-1 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 focus:outline-none focus:border-accent/50"
        >
          <option value="">{hint ?? '不显示'}</option>
          {numericFields.map(f => (
            <option key={f.name} value={f.name}>{f.label || f.name}</option>
          ))}
        </select>
      </label>
    )

    // 预览竞价序列: 按当前字段选择裁剪样例数据 (未启用/未配表/未配价格 → null,
    // 预览无窄栏; 只配价格 → 窄栏价格线无量带)
    const previewAuction = isAuctionActive(auction)
      ? {
          price: PREVIEW_AUCTION.price,
          ...(auction?.matchField ? { match: PREVIEW_AUCTION.match } : {}),
          ...(auction?.unmatchedField ? { unmatched: PREVIEW_AUCTION.unmatched } : {}),
        }
      : null

    // 一键添加竞价数据列 (已配字段的 chip): 复用 addExtColumn — 去重(已存在则切换
    // 显示)、插入位置、时序表默认迷你图都现成。叠加窄栏看形态, 数据列扫全场/排序。
    const fieldMeta = new Map((auctionTable?.columns ?? []).map(c => [c.name, c]))
    const auctionDataCols = ([
      ['priceField', '价格'],
      ['matchField', '匹配量'],
      ['unmatchedField', '未匹配量'],
    ] as const).flatMap(([key, name]) => {
      const field = auction?.[key]
      if (!field || !auction?.configId) return []
      const meta = fieldMeta.get(field) as { label?: string; dtype?: string; type?: string } | undefined
      const colId = `ext:${auction.configId}:${field}`
      return [{
        key, name, field, colId,
        label: meta?.label || field,
        type: meta?.dtype ?? meta?.type,
        added: columns.some(c => c.id === colId && c.visible),
      }]
    })

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
          {renderLinkControls('intraday')}

          {/* 成交量副图: 本列自管 (与日k各自独立)。R2: 竞价配了量字段时量带本身
              恒显示 — 开关只控制分钟量柱, 不禁用 */}
          <div className="border-t border-border/40 pt-2 mt-1 flex items-center justify-between">
            <div className="text-[10px] text-muted">成交量副图</div>
            <button
              type="button"
              onClick={() => updateIntradayConfig(col.id, { volume: !cfg.volume })}
              className={`relative inline-flex h-4 w-7 items-center rounded-full transition-colors duration-200 cursor-pointer ${cfg.volume ? 'bg-accent' : 'bg-elevated'}`}
              aria-pressed={cfg.volume}
              aria-label="显示成交量副图"
            >
              <span className={`inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform duration-200 ${cfg.volume ? 'translate-x-[14px]' : 'translate-x-0.5'}`} />
            </button>
          </div>
          <div className="text-[10px] text-muted leading-relaxed">
            开启后在价格区下方显示分钟成交量柱；日k量带在日k列单独设置。
            {forced && ' 已配竞价量字段时量带始终显示（竞价量柱不受此开关影响）。'}
          </div>

          <div className="border-t border-border/40 pt-2 mt-1 flex items-center justify-between">
            <div className="text-[10px] text-muted">集合竞价叠加</div>
            <button
              type="button"
              onClick={() => updateAuction({ enabled: !auction?.enabled })}
              className={`relative inline-flex h-4 w-7 items-center rounded-full transition-colors duration-200 cursor-pointer ${
                auction?.enabled ? 'bg-accent' : 'bg-elevated'
              }`}
              aria-pressed={!!auction?.enabled}
              aria-label="显示集合竞价窄栏"
            >
              <span className={`inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform duration-200 ${
                auction?.enabled ? 'translate-x-[14px]' : 'translate-x-0.5'
              }`} />
            </button>
          </div>
          {auction?.enabled && (
            <>
              <label className="flex items-center gap-2 text-xs">
                <span className="text-secondary w-16 shrink-0">扩展表</span>
                <select
                  value={auction?.configId ?? ''}
                  onChange={e => {
                    const next = e.target.value || undefined
                    // 仅切换表时才清空字段; 选回同一张表不动已有配置
                    if (next === auction?.configId) return
                    updateAuction({ configId: next, priceField: undefined, matchField: undefined, unmatchedField: undefined })
                  }}
                  className="flex-1 h-7 rounded bg-elevated border border-border text-foreground text-xs px-2 focus:outline-none focus:border-accent/50"
                >
                  <option value="">选择时序扩展表</option>
                  {multiTables.map(t => (
                    <option key={t.id} value={t.id}>{t.label}</option>
                  ))}
                </select>
              </label>
              {multiTables.length === 0 && (
                <div className="text-[10px] text-amber-500 leading-relaxed">
                  暂无可用的日内多盘扩展表 — 需先在「数据 → 扩展数据」创建时序表并配置时间列（如集合竞价）。
                </div>
              )}
              {auction?.configId && (
                <>
                  {fieldSelect('价格字段', 'priceField', '选择价格字段')}
                  {fieldSelect('匹配量', 'matchField')}
                  {fieldSelect('未匹配量', 'unmatchedField')}
                  {/* 栏宽三档 (R3): 只改窄栏占宽, 分桶数随宽度自适应 */}
                  <div className="flex items-center gap-2 text-xs">
                    <span className="text-secondary w-16 shrink-0">栏宽</span>
                    <div className="flex rounded overflow-hidden border border-border">
                      {([['narrow', '窄'], ['standard', '标准'], ['wide', '宽']] as [AuctionBarWidth, string][]).map(([v, text], i) => (
                        <button
                          key={v}
                          onClick={() => updateAuction({ width: v })}
                          className={`px-3 py-1 text-xs transition-colors ${i > 0 ? 'border-l border-border' : ''} ${
                            cfg.auctionWidth === v
                              ? 'bg-accent/15 text-accent' : 'bg-elevated text-secondary hover:text-foreground'
                          }`}
                        >{text}</button>
                      ))}
                    </div>
                  </div>
                  <div className="text-[10px] text-muted leading-relaxed">
                    竞价栏约占总宽 {Math.round(AUCTION_WIDTH_RATIOS[cfg.auctionWidth] * 100)}%，价格与分时共用同一纵轴；配置量字段后竞价量柱画在底部量带。
                  </div>
                  {/* 竞价数据列: 一键把已配字段加为独立可排序列 (chip 态=列已显示,
                      再点隐藏; 复用 addExtColumn 的去重与插入逻辑) */}
                  {auctionDataCols.length > 0 && (
                    <div className="border-t border-border/40 pt-2 mt-1">
                      <div className="text-[10px] text-muted mb-1.5">竞价数据列（可排序，取末盘值）</div>
                      <div className="flex flex-wrap gap-1.5">
                        {auctionDataCols.map(c => (
                          <button
                            key={c.key}
                            type="button"
                            onClick={() => addExtColumn(auction!.configId!, c.field, c.label, c.type)}
                            className={`px-2 py-1 rounded border text-[10px] transition-colors cursor-pointer ${
                              c.added
                                ? 'bg-accent/15 border-accent/40 text-accent'
                                : 'bg-elevated border-border text-secondary hover:text-foreground'
                            }`}
                            aria-pressed={c.added}
                          >
                            {c.added ? `✓ ${c.name}列` : `＋ ${c.name}列`}
                          </button>
                        ))}
                      </div>
                      <div className="text-[10px] text-muted leading-relaxed mt-1">
                        窄栏叠加看竞价形态与价格对比；数据列可排序、可拖到任意位置，适合盘前扫全市场。再次点击显示/隐藏该列。
                      </div>
                    </div>
                  )}
                </>
              )}
            </>
          )}

          {/* 效果预览 (R4): 固定样例数据渲染真组件, 配置改动即时反映 (所见即所得) */}
          <div className="border-t border-border/40 pt-2 mt-1">
            <div className="text-[10px] text-muted mb-1.5">效果预览（示例数据，配置改动即时反映）</div>
            <div className="rounded bg-base border border-border/60 flex items-center justify-center overflow-hidden">
              <MiniIntraday
                rows={PREVIEW_ROWS}
                prevClose={10}
                width={Math.min(cfg.width, 260)}
                height={cfg.height}
                volume={cfg.volume || forced}
                auctionWidth={cfg.auctionWidth}
                auction={previewAuction}
              />
            </div>
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
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <motion.div
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className={`absolute inset-0 bg-black/50 ${disableBackdropBlur ? '' : 'backdrop-blur-sm'}`}
            {...backdrop}
          />
          <motion.div
            initial={{ scale: 0.97, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} exit={{ scale: 0.97, opacity: 0 }}
            transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
            className="relative w-[900px] max-w-[94vw] max-h-[min(720px,82vh)] bg-base border border-border rounded-card shadow-2xl flex flex-col overflow-hidden"
          >
            {/* 头部: 标题 + 全局搜索 + 已选计数 + 关闭 */}
            <div className="flex items-center gap-3 px-4 py-2.5 border-b border-border shrink-0">
              <h3 className="text-sm font-medium text-foreground shrink-0">{title}</h3>
              <div className="relative flex-1 max-w-[300px]">
                <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted" />
                <input
                  type="text"
                  value={searchQuery}
                  onChange={e => setSearchQuery(e.target.value)}
                  placeholder="搜索列名 / 扩展字段…"
                  className="w-full h-8 pl-8 pr-3 rounded-lg bg-elevated border border-border text-xs text-foreground placeholder:text-muted focus:outline-none focus:border-accent/50 transition-colors"
                />
              </div>
              <span className="ml-auto text-[10px] text-muted tabular-nums shrink-0">已选 {activeCols.length} 列</span>
              {baselineStore && (
                <button
                  onClick={saveAsBaseline}
                  className="shrink-0 h-7 px-2.5 rounded-btn bg-elevated border border-border text-[11px] text-secondary hover:text-foreground hover:border-accent/50 transition-colors"
                  title="把当前列配置存为默认方案；之后「恢复默认」回到这份设置"
                >
                  {baselineSavedAt && Date.now() - baselineSavedAt < 2000 ? '✓ 已保存' : '保存为默认方案'}
                </button>
              )}
              <button onClick={onClose} className="p-1 rounded hover:bg-elevated text-muted hover:text-foreground transition-colors shrink-0">
                <X className="h-4 w-4" />
              </button>
            </div>

            {/* 两栏: 左=所有可选列, 右=已选择 (配置在已选行下方就地展开)。
                弹窗高度自适应内容: min-h 下限防列少时过扁, 超过 max-h 时本行
                收缩、两栏列表各自滚动。 */}
            <div className="flex-1 flex min-h-[260px]">
              {/* ── 左栏: 所有列 ── */}
              <div className="w-[340px] shrink-0 border-r border-border flex flex-col min-h-0 bg-surface/20">
                <div className="flex items-center justify-between px-3 py-1.5 border-b border-border/50 shrink-0">
                  <span className="text-[10px] font-semibold text-muted uppercase tracking-wider">{builtinSectionLabel}</span>
                  <span className="text-[10px] text-muted">勾选即加入</span>
                </div>
                <div className="flex-1 overflow-y-auto px-2 py-1.5 space-y-1">
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
                            <span className="text-[11px] font-medium text-secondary flex-1 truncate">{table.label}</span>
                            <span className="text-[10px] text-muted px-1.5 py-0.5 rounded bg-elevated shrink-0">
                              {table.mode === 'snapshot' ? '快照' : '时序'}
                            </span>
                            <span className="text-[10px] text-muted tabular-nums w-8 text-right shrink-0">
                              {activeCount}/{dataFields.length}
                            </span>
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
              </div>

              {/* ── 右栏: 已选择 (拖拽排序 + 就地配置) ── */}
              <div className="flex-1 flex flex-col min-w-0 min-h-0">
                <div className="flex items-center justify-between px-3 py-1.5 border-b border-border/50 shrink-0">
                  <span className="text-[10px] font-semibold text-accent/80 uppercase tracking-wider">已选择 · 拖拽排序</span>
                  <div className="flex items-center gap-2">
                    <span className="text-[10px] text-muted tabular-nums">{activeCols.length} 列</span>
                    <button
                      onClick={() => setConfirmReset(true)}
                      className="text-[10px] text-muted hover:text-foreground transition-colors"
                    >
                      恢复默认
                    </button>
                  </div>
                </div>
                <div className="flex-1 overflow-y-auto px-2 py-1.5 space-y-0.5">
                  {activeCols.length === 0 && (
                    <div className="text-xs text-muted text-center py-10">还没有选择任何列，从左侧勾选加入</div>
                  )}
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
                          extConfig={() => renderExtConfig(col)}
                          candleConfig={() => renderCandleConfig(col)}
                          intradayConfig={() => renderIntradayConfig(col)}
                          strategiesConfig={() => renderStrategiesConfig(col)}
                          showStandaloneToggle={showStandaloneToggle}
                          onToggleStandalone={toggleStandalone}
                        />
                      ))}
                    </SortableContext>
                  </DndContext>
                </div>
              </div>
            </div>

            {/* 恢复默认二次确认: 覆盖在主弹窗内 (不叠第二层遮罩), 明确说清恢复目标。
                用条件渲染而非 AnimatePresence — 嵌套在弹窗 motion 树里时退出动画
                不保证完成, 曾出现点取消后弹窗不关闭。 */}
            {confirmReset && (
              <div
                className="absolute inset-0 z-10 flex items-center justify-center bg-black/55"
                onClick={() => setConfirmReset(false)}
              >
                <div
                  className="w-[360px] rounded-card border border-border bg-base p-4 shadow-2xl"
                  onClick={e => e.stopPropagation()}
                >
                  <h4 className="text-sm font-medium text-foreground mb-1.5">确认恢复默认？</h4>
                  <p className="text-[11px] text-secondary leading-relaxed">
                    {hasBaseline
                      ? '将回到你保存的「默认方案」——列的顺序、显隐和所有列配置都会恢复成保存时的样子。'
                      : '还没有保存过默认方案，将回到内置默认：清空所有列配置、扩展列移出列表。'}
                  </p>
                  <p className="text-[11px] text-warning mt-1.5">
                    当前未保存的调整会丢失{hasBaseline ? '' : '，且无法撤销'}。
                  </p>
                  <div className="flex justify-end gap-2 mt-3.5">
                    <button
                      onClick={() => setConfirmReset(false)}
                      className="px-3 py-1.5 rounded-btn bg-elevated text-secondary hover:bg-elevated/80 text-xs transition-colors"
                    >
                      取消
                    </button>
                    <button
                      onClick={() => { resetAllColumns(); setConfirmReset(false) }}
                      className="px-3 py-1.5 rounded-btn bg-danger/90 text-white text-xs font-medium hover:bg-danger transition-colors"
                    >
                      {hasBaseline ? '恢复到默认方案' : '恢复内置默认'}
                    </button>
                  </div>
                </div>
              </div>
            )}
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}
