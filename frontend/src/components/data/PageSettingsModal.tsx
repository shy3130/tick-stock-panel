import { useState } from 'react'
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core'
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { Check, GripVertical } from 'lucide-react'
import { storage } from '@/lib/storage'

export type CardKey =
  | 'instruments' | 'daily' | 'adj_factor' | 'enriched'
  | 'index' | 'etf' | 'minute' | 'financials' | 'regime'

interface CardDef {
  key: CardKey
  label: string
  desc: string
  /** 档位能力不足时该卡片是否默认隐藏(减少干扰) */
  defaultHiddenIfNoCap: boolean
  /** 无条件默认隐藏(用户可在设置里手动开启) */
  defaultHidden?: boolean
}

/** 数据画像卡片定义 —— 默认顺序即此数组顺序 */
export const DATA_CARD_DEFS: CardDef[] = [
  { key: 'instruments', label: '个股维表', desc: 'A 股股票元数据', defaultHiddenIfNoCap: false },
  { key: 'daily',       label: '日 K',     desc: 'A 股日K线数据',          defaultHiddenIfNoCap: false },
  { key: 'adj_factor',  label: '除权因子', desc: '复权计算因子',           defaultHiddenIfNoCap: true },
  { key: 'enriched',    label: 'Enriched', desc: '技术指标计算结果',       defaultHiddenIfNoCap: false },
  { key: 'index',       label: '指数',     desc: '主要市场指数日K',        defaultHiddenIfNoCap: false },
  { key: 'etf',         label: 'ETF',      desc: '场内交易基金日K',         defaultHiddenIfNoCap: false, defaultHidden: true },
  { key: 'minute',      label: '分钟 K',   desc: '分钟级K线(需 Pro+)',     defaultHiddenIfNoCap: true },
  { key: 'financials',  label: '财务数据', desc: '财报数据(需 Expert)',    defaultHiddenIfNoCap: true },
  { key: 'regime',      label: '市场环境', desc: '每日环境状态(本地计算)', defaultHiddenIfNoCap: false },
]

const DEFAULT_ORDER = DATA_CARD_DEFS.map(d => d.key)
/** 恢复默认时显示的卡片数量(按默认顺序取前 N 张) */
const DEFAULT_VISIBLE_COUNT = 5

const CAP_KEY_MAP: Partial<Record<CardKey, string>> = {
  adj_factor: 'adj_factor',
  minute: 'kline.minute.batch',
  financials: 'financial',
}

/**
 * 读取卡片显隐状态。结合档位能力决定默认值:
 * - 用户显式设置过 → 用设置值
 * - 未设置 + defaultHidden → 隐藏(无条件默认隐藏)
 * - 未设置 + defaultHiddenIfNoCap + 当前无能力 → 隐藏
 * - 其他 → 显示
 */
export function getCardVisibility(
  caps: Record<string, unknown> | undefined,
): Record<string, boolean> {
  const has = (capKey: string) => !capKey || !!caps?.[capKey]
  const override = storage.dataCardVisible.get({})
  const result: Record<string, boolean> = {}
  for (const def of DATA_CARD_DEFS) {
    if (def.key in override) {
      result[def.key] = override[def.key]
    } else if (def.defaultHidden) {
      result[def.key] = false
    } else {
      result[def.key] = def.defaultHiddenIfNoCap ? has(CAP_KEY_MAP[def.key] ?? '') : true
    }
  }
  return result
}

/**
 * 读取卡片显示顺序。
 * - 用户拖拽设置过 → 用设置值(过滤掉已不存在的 key, 补齐新增的 key)
 * - 未设置 → 用 DATA_CARD_DEFS 默认顺序
 */
export function getCardOrder(): CardKey[] {
  const saved = storage.dataCardOrder.get([])
  if (!saved.length) return [...DEFAULT_ORDER]
  const known = new Set<CardKey>(DEFAULT_ORDER)
  const ordered = saved.filter(k => known.has(k as CardKey)) as CardKey[]
  // 补齐新增的 key(默认顺序里新增的卡片追加到末尾)
  for (const k of DEFAULT_ORDER) {
    if (!ordered.includes(k)) ordered.push(k)
  }
  return ordered
}

export function PageSettingsModal({
  caps,
}: {
  caps: Record<string, unknown> | undefined
}) {
  const [visible, setVisible] = useState<Record<string, boolean>>(() => getCardVisibility(caps))
  const [order, setOrder] = useState<CardKey[]>(() => getCardOrder())

  const persistVisible = (next: Record<string, boolean>) => {
    setVisible(next)
    storage.dataCardVisible.set(next)
    window.dispatchEvent(new CustomEvent('data-card-visible-change'))
  }
  const persistOrder = (next: CardKey[]) => {
    setOrder(next)
    storage.dataCardOrder.set(next)
    window.dispatchEvent(new CustomEvent('data-card-visible-change'))
  }

  const toggle = (key: CardKey) => persistVisible({ ...visible, [key]: !(visible[key] ?? true) })

  const reset = () => {
    // 恢复默认: 默认顺序 + 仅勾选前 5 张卡片, 其余隐藏
    const defaultOrder = [...DEFAULT_ORDER]
    const defaultVisible: Record<string, boolean> = {}
    defaultOrder.forEach((k, i) => { defaultVisible[k] = i < DEFAULT_VISIBLE_COUNT })
    storage.dataCardVisible.set(defaultVisible)
    storage.dataCardOrder.set(defaultOrder)
    setVisible(defaultVisible)
    setOrder(defaultOrder)
    window.dispatchEvent(new CustomEvent('data-card-visible-change'))
  }

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event
    if (!over || active.id === over.id) return
    const oldIdx = order.indexOf(active.id as CardKey)
    const newIdx = order.indexOf(over.id as CardKey)
    if (oldIdx < 0 || newIdx < 0) return
    persistOrder(arrayMove(order, oldIdx, newIdx))
  }

  // 按 order 排序卡片定义
  const defByKey = new Map(DATA_CARD_DEFS.map(d => [d.key, d]))
  const orderedDefs = order.map(k => defByKey.get(k)!).filter(Boolean)

  return (
    <div className="space-y-2.5">
      <p className="text-xs text-secondary leading-relaxed">
        拖动手柄调整卡片顺序,勾选控制显隐。未勾选的卡片将隐藏,不影响数据本身。
      </p>
      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragEnd={handleDragEnd}
      >
        <SortableContext items={order} strategy={verticalListSortingStrategy}>
          <div className="space-y-1.5">
            {orderedDefs.map((def) => {
              const on = visible[def.key] ?? true
              return (
                <SortableCardRow
                  key={def.key}
                  id={def.key}
                  label={def.label}
                  desc={def.desc}
                  on={on}
                  onToggle={() => toggle(def.key)}
                />
              )
            })}
          </div>
        </SortableContext>
      </DndContext>
      <div className="flex items-center justify-end pt-1">
        <button
          onClick={reset}
          className="px-2 py-0.5 rounded-btn text-[10px] text-secondary hover:text-foreground transition-colors"
        >
          恢复默认
        </button>
      </div>
    </div>
  )
}

// ── 可拖拽的卡片行 ──
function SortableCardRow({
  id, label, desc, on, onToggle,
}: {
  id: CardKey
  label: string
  desc: string
  on: boolean
  onToggle: () => void
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id })

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.6 : 1,
    zIndex: isDragging ? 10 : undefined,
  }

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`flex items-center gap-2 rounded-card border px-3 py-2 transition-colors ${
        isDragging ? 'bg-elevated shadow-lg' : ''
      } ${on ? 'border-accent/40 bg-accent/[0.05]' : 'border-border bg-base/30'}`}
    >
      {/* 拖拽手柄 */}
      <button
        type="button"
        {...attributes}
        {...listeners}
        className="cursor-grab active:cursor-grabbing text-muted hover:text-foreground transition-colors shrink-0"
        title="拖动排序"
      >
        <GripVertical className="h-4 w-4" />
      </button>
      {/* 显隐勾选 */}
      <button
        type="button"
        onClick={onToggle}
        className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors ${
          on ? 'bg-accent border-accent' : 'bg-base border-border'
        }`}
        role="checkbox"
        aria-checked={on}
      >
        {on && <Check className="h-3 w-3 text-white" strokeWidth={3} />}
      </button>
      <div className="min-w-0 flex-1">
        <div className="text-xs font-medium text-foreground">{label}</div>
        <div className="text-[10px] text-muted leading-snug">{desc}</div>
      </div>
    </div>
  )
}
