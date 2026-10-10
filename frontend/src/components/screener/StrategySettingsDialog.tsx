import { useState, useEffect, useCallback, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useQuery } from '@tanstack/react-query'
import { X, Settings2, RotateCcw, Save, Filter, Star, TrendingUp, Sparkles, Download, Layers, Plus, Trash2, SlidersHorizontal } from 'lucide-react'
import { api, type StrategyDetail, type StrategyParamDef, type CompositeChildInfo, type ScoringDirection, type CustomSignalCondition } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toPercentages, normalizeWeights } from '@/lib/weights'
import { BUILTIN_COLUMNS } from '@/lib/watchlist-columns'
import { color } from '@/lib/colors'
import { SignalPicker } from './SignalPicker'
import { SignalTriggerActions } from '@/components/signals/SignalTriggerActions'
import { ConditionEditor } from '@/components/signals/ConditionEditor'
import { Modal } from '@/components/Modal'
import { ScoringEditor } from '@/components/ScoringEditor'

// 内置列名 → 中文标签
const FIELD_LABEL: Record<string, string> = {}
for (const c of BUILTIN_COLUMNS) {
  if (c.source.type === 'builtin') FIELD_LABEL[c.source.key] = c.label
}
// enriched 列名别名
Object.assign(FIELD_LABEL, {
  change_pct: '涨跌幅', consecutive_limit_ups: '连板',
  momentum_60d: '60D动量', turnover_rate: '换手率',
  rsi_14: 'RSI14', rsi_6: 'RSI6', rsi_24: 'RSI24',
  vol_ratio_5d: '量比', vol_ratio_20d: '20日量比',
  macd_dif: 'MACD-DIF', macd_dea: 'MACD-DEA', macd_hist: 'MACD柱',
  boll_upper: '布林上轨', boll_lower: '布林下轨',
  ma20_bias: 'MA20乖离率',
})

interface Props {
  strategyId: string | null
  onClose: () => void
  onSaved?: (displayLimit: number | null) => void
  onAiModify?: () => void
  onDeleted?: () => void
}

// ===== 叠加条件支持判定: 与后端 _OVERLAY_UNSUPPORTED_BACKENDS 同集 =====
// composite 已有自身叠加合并语义, 分钟策略帧不带 enriched 列 (ext_*/因子不可见)。
function overlaySupported(d: StrategyDetail | null): boolean {
  return !!d && !['composite', 'minute_filter'].includes(d.execution_backend)
}

// ===== 设置分区 (左侧导航) =====
type SettingsPage = 'screen' | 'scoring' | 'trading' | 'children'

// ===== 基线快照: 用于「未保存改动」提示 (分区独立比较) =====
interface SnapshotInput {
  name: string
  desc: string
  limit: number | null
  bf: Record<string, any>
  enabled: boolean
  params: Record<string, any>
  overlay: CustomSignalCondition[]
  scoring: Record<string, number>
  dirs: Record<string, ScoringDirection>
  sl: number | null
  mh: number | null
  tp: number | null
  ts: number | null
  ta: number | null
  td: number | null
  en: string[]
  ex: string[]
  children: { id: string; w: number }[]
}

// 各字段的归一化口径必须与加载/保存路径一致, 否则会误报"有改动":
// overlay 按 left/op/right/偏移天数 归一; 子策略权重按整数百分比比较;
// 风控小数按 abs 归一 (百分比输入往返 -0.08 → 0.08, 引擎按 abs 消费, 不算改动)。
function buildSnapshot(v: SnapshotInput) {
  const absRisk = (x: number | null) => (x == null ? null : Math.abs(x))
  return {
    info: JSON.stringify({ name: v.name, desc: v.desc, limit: v.limit ?? null }),
    screen: JSON.stringify({
      bf: { ...v.bf, enabled: v.enabled },
      params: v.params,
      overlay: v.overlay.map(c => ({ left: c.left, op: c.op, right: c.right, leftDays: c.leftDays ?? 0, rightDays: c.rightDays ?? 0 })),
    }),
    scoring: JSON.stringify({ s: v.scoring, d: v.dirs }),
    trading: JSON.stringify({ sl: absRisk(v.sl), mh: v.mh, tp: absRisk(v.tp), ts: absRisk(v.ts), ta: absRisk(v.ta), td: absRisk(v.td), en: v.en, ex: v.ex }),
    children: JSON.stringify(v.children),
  }
}
type Snapshot = ReturnType<typeof buildSnapshot>

// 页面切换时的入场动画 (tailwindcss-animate); 常驻挂载避免 ScoringEditor 草稿丢失
const PAGE_ANIM = 'animate-in fade-in-0 slide-in-from-bottom-1 duration-150'

// ===== 策略参数/基础参数通用字段行 =====
const FIELD_LABEL_CLS = 'w-[88px] shrink-0 text-right text-[11px] leading-4 text-secondary'
// 参数标签通常更长 (如「近60日最少涨停次数」), 单独放宽避免括号内折行
// 参数标签通常更长 (如「近60日最少涨停次数」), 单独放宽; keep-all 让断行只发生在空格处, 避免拆断括号
const PARAM_LABEL_CLS = 'w-[116px] shrink-0 text-right text-[11px] leading-4 text-secondary break-keep [overflow-wrap:anywhere]'
const FIELD_INPUT_CLS = 'h-7 rounded-[6px] border border-border bg-base text-xs font-mono text-foreground focus:outline-none focus:border-accent/50'

// ===== 区间字段（最小 ~ 最大） =====
export function RangeField({ label, minVal, maxVal, onMinChange, onMaxChange, unit, step }: {
  label: string
  minVal: any
  maxVal: any
  onMinChange: (v: any) => void
  onMaxChange: (v: any) => void
  unit?: string
  step?: string
}) {
  return (
    <div className="flex items-center gap-1.5">
      <span className={FIELD_LABEL_CLS}>{label}</span>
      <input
        type="number"
        value={minVal ?? ''}
        onChange={e => onMinChange(e.target.value === '' ? null : Number(e.target.value))}
        placeholder="最小"
        step={step}
        className={`${FIELD_INPUT_CLS} w-[84px] px-2 text-center`}
      />
      <span className="text-[10px] text-muted">~</span>
      <input
        type="number"
        value={maxVal ?? ''}
        onChange={e => onMaxChange(e.target.value === '' ? null : Number(e.target.value))}
        placeholder="最大"
        step={step}
        className={`${FIELD_INPUT_CLS} w-[84px] px-2 text-center`}
      />
      {unit && <span className="w-4 shrink-0 text-[10px] text-muted">{unit}</span>}
    </div>
  )
}

// 板块标签
export const ALL_BOARDS = ['沪主板', '深主板', '创业板', '科创板', '北交所']

// 风控百分比字段: 输入/显示百分比 (5 = 5%), 存储正小数 (0.05)。
// 引擎与回测抽屉显示均按 abs 消费, 与历史负值 (-0.05) 兼容。
function RiskPctField({ label, value, onChange, min, max, step = 0.5, unit = '%' }: {
  label: string
  value: number | null
  onChange: (v: number | null) => void
  min?: number
  max?: number
  step?: number
  unit?: string
}) {
  const display = value == null ? '' : String(Math.round(Math.abs(value) * 10000) / 100)
  return (
    <div className="flex items-center gap-2">
      <span className={FIELD_LABEL_CLS}>{label}</span>
      <input
        type="number"
        value={display}
        step={step}
        min={min}
        max={max}
        placeholder="未设置"
        onChange={e => onChange(e.target.value === '' ? null : Math.abs(Number(e.target.value)) / 100)}
        className={`${FIELD_INPUT_CLS} w-[76px] px-2 text-center`}
      />
      <span className="text-[10px] text-muted">{unit}</span>
    </div>
  )
}

// 策略参数字段
function ParamField({ def, value, onChange }: {
  def: StrategyParamDef
  value: any
  onChange: (v: any) => void
}) {
  if (def.type === 'bool') {
    const checked = value === true || value === 'true' || value === 'True'
    return (
      <div className="flex items-center gap-2">
        <span className={PARAM_LABEL_CLS} title={def.label}>{def.label}</span>
        <button
          type="button"
          onClick={() => onChange(!checked)}
          className={`relative inline-flex h-4 w-7 items-center rounded-full transition-colors duration-200 cursor-pointer ${
            checked ? 'bg-accent' : 'bg-elevated'
          }`}
          aria-pressed={checked}
        >
          <span className={`inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform duration-200 ${
            checked ? 'translate-x-[14px]' : 'translate-x-0.5'
          }`} />
        </button>
        <span className="text-[10px] text-muted/60">{checked ? '开' : '关'}</span>
      </div>
    )
  }
  if (def.type === 'select' && def.options) {
    return (
      <div className="flex items-center gap-2">
        <span className={PARAM_LABEL_CLS} title={def.label}>{def.label}</span>
        <select
          value={value ?? def.default}
          onChange={e => onChange(e.target.value)}
          className={`${FIELD_INPUT_CLS} w-full max-w-[240px] px-1.5`}
        >
          {def.options.map(o => <option key={o} value={o}>{o}</option>)}
        </select>
      </div>
    )
  }
  if (def.type === 'string') {
    return (
      <div className="flex items-center gap-2">
        <span className={PARAM_LABEL_CLS} title={def.label}>{def.label}</span>
        <input
          type="text"
          value={value ?? def.default ?? ''}
          onChange={e => onChange(e.target.value)}
          className={`${FIELD_INPUT_CLS} min-w-0 flex-1 px-2`}
        />
      </div>
    )
  }

  return (
    <div className="flex items-center gap-2">
      <span className={PARAM_LABEL_CLS} title={def.label}>{def.label}</span>
      <input
        type="number"
        value={value ?? def.default}
        onChange={e => onChange(e.target.value === '' ? def.default : Number(e.target.value))}
        step={def.step ?? 0.1}
        min={def.min}
        max={def.max}
        className={`${FIELD_INPUT_CLS} w-24 px-2 text-center`}
      />
      {def.min != null && def.max != null && (
        <span className="whitespace-nowrap text-[10px] text-muted/60">{def.min}~{def.max}</span>
      )}
    </div>
  )
}

// ===== 内容卡片 (分区页内的静态卡片, 取代原三列里的可折叠 Section) =====
function Card({ icon: Icon, title, accent, extra, children, dim = false }: {
  icon?: React.ComponentType<{ className?: string }>
  title: string
  accent?: string
  extra?: React.ReactNode
  children: React.ReactNode
  /** 只淡化卡片主体, 头部操作 (如启用开关) 保持可点 */
  dim?: boolean
}) {
  return (
    <div className="overflow-hidden rounded-xl border border-border/20 bg-surface/20">
      <div className="flex items-center gap-2 border-b border-border/15 bg-elevated/20 px-4 py-2.5">
        {Icon && <Icon className={`h-3.5 w-3.5 shrink-0 ${accent ?? 'text-muted'}`} />}
        <span className="text-xs font-medium text-foreground/80">{title}</span>
        {extra && <div className="ml-auto flex items-center gap-2">{extra}</div>}
      </div>
      <div className={`px-4 py-3 transition-opacity duration-200 ${dim ? 'opacity-25 pointer-events-none' : ''}`}>
        {children}
      </div>
    </div>
  )
}

// 后端/周期显示名 (左侧导航底部信息栏)
const BACKEND_LABEL: Record<string, string> = {
  polars_expr: 'Polars 表达式',
  matrix_native: '矩阵原生',
  python_history_legacy: '历史兼容',
  composite: '叠加融合',
  minute_filter: '分钟过滤',
}
const TF_LABEL: Record<string, string> = { '1d': '日线', '1m': '分钟' }

export function StrategySettingsDialog({ strategyId, onClose, onSaved, onAiModify, onDeleted }: Props) {
  const [detail, setDetail] = useState<StrategyDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [resetting, setResetting] = useState(false)

  // 编辑状态
  const [strategyName, setStrategyName] = useState('')
  const [strategyDesc, setStrategyDesc] = useState('')
  const [basicFilter, setBasicFilter] = useState<Record<string, any>>({})
  const [params, setParams] = useState<Record<string, any>>({})
  const [scoring, setScoring] = useState<Record<string, number>>({})
  const [scoringDirections, setScoringDirections] = useState<Record<string, ScoringDirection>>({})
  const [stopLoss, setStopLoss] = useState<number | null>(null)
  const [maxHoldDays, setMaxHoldDays] = useState<number | null>(null)
  // 风控字段 (止盈/移动止损/回撤止盈): 与回测抽屉同口径, 输入百分比、存储小数
  const [takeProfit, setTakeProfit] = useState<number | null>(null)
  const [trailingStop, setTrailingStop] = useState<number | null>(null)
  const [ttpActivate, setTtpActivate] = useState<number | null>(null)
  const [ttpDrawdown, setTtpDrawdown] = useState<number | null>(null)
  const [entrySignals, setEntrySignals] = useState<string[]>([])
  const [exitSignals, setExitSignals] = useState<string[]>([])
  const [displayLimit, setDisplayLimit] = useState<number | null>(null)
  const [basicFilterEnabled, setBasicFilterEnabled] = useState(true)
  // 叠加条件 (每策略 overlay 硬过滤; 仅日线 polars_expr / matrix_native 支持)
  const [overlayFilter, setOverlayFilter] = useState<CustomSignalCondition[]>([])
  // 叠加策略: 子策略列表与权重(composite 专属, 编辑权重后随 override 保存)
  const [compositeChildren, setCompositeChildren] = useState<CompositeChildInfo[]>([])
  // 点击子策略名打开其配置编辑(composite 专属; 子策略必非 composite, 不会再嵌套)
  const [editingChildId, setEditingChildId] = useState<string | null>(null)
  // 可选子策略列表 + 添加面板开关(composite 设置用)
  const [allStrategies, setAllStrategies] = useState<{ id: string; name: string; source?: string }[]>([])
  const [showAddChild, setShowAddChild] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [deleteError, setDeleteError] = useState('')
  // 当前分区页 + 未保存改动基线
  const [page, setPage] = useState<SettingsPage>('screen')
  const [baseline, setBaseline] = useState<Snapshot | null>(null)

  // 辅助：更新 basicFilter 某个 key
  const setBF = useCallback((key: string, value: any) => {
    setBasicFilter(prev => ({ ...prev, [key]: value }))
  }, [])

  // 叠加条件字段选项 (与自定义信号同一端点: 基础列 + 注册表因子 + 扩展数据)
  const overlayOptions = useQuery({
    queryKey: QK.customSignalsOptions,
    queryFn: api.customSignalsOptions,
    enabled: overlaySupported(detail),
  })
  // matrix 策略的矩阵 fields 是 float 数组, 字符串扩展字段进不了回测矩阵 → 不提供
  const isMatrixBackend = detail?.execution_backend === 'matrix_native'
  const overlayStringFields = isMatrixBackend ? [] : (overlayOptions.data?.stringFields ?? [])
  const overlayFields = isMatrixBackend
    ? (overlayOptions.data?.fields ?? []).filter(f => !(overlayOptions.data?.stringFields ?? []).includes(f.key))
    : (overlayOptions.data?.fields ?? [])
  const overlayGroups = isMatrixBackend
    ? (overlayOptions.data?.groups ?? [])
        .map(g => ({ ...g, fields: g.fields.filter(f => !(overlayOptions.data?.stringFields ?? []).includes(f.key)) }))
        .filter(g => g.fields.length > 0)
    : overlayOptions.data?.groups

  // 加载策略详情 → 编辑状态 (初始加载与「重置默认」后共用, 保证两边归一化口径一致)
  const loadIntoState = useCallback((d: StrategyDetail) => {
    setDetail(d)
    setStrategyName(d.name ?? '')
    setStrategyDesc(d.description ?? '')
    // 确保 boards 有默认值
    const bf = { ...d.basic_filter }
    if (!bf.boards) bf.boards = ALL_BOARDS
    const enabled = d.basic_filter?.enabled !== false
    setBasicFilter(bf)
    setBasicFilterEnabled(enabled)
    setParams(d.params_defaults)
    setScoring(d.scoring)
    setScoringDirections(d.scoring_directions ?? {})
    setStopLoss(d.stop_loss)
    setMaxHoldDays(d.max_hold_days)
    setTakeProfit(d.take_profit)
    setTrailingStop(d.trailing_stop)
    setTtpActivate(d.trailing_take_profit_activate)
    setTtpDrawdown(d.trailing_take_profit_drawdown)
    setEntrySignals(d.entry_signals ?? [])
    setExitSignals(d.exit_signals ?? [])
    const overlay = (d.overlay_filter ?? []).map(c => ({ leftDays: 0, rightDays: 0, ...c }))
    setOverlayFilter(overlay)
    setDisplayLimit(d.display_limit ?? null)
    // 存储的小数权重 → 滑块百分比口径
    const children = (() => {
      const list = d.composite_children ?? []
      const pcts = toPercentages(list.map(c => c.weight))
      return list.map((c, i) => ({ ...c, weight: pcts[i] }))
    })()
    setCompositeChildren(children)
    setPage(d.source === 'composite' ? 'children' : 'screen')
    setBaseline(buildSnapshot({
      name: d.name ?? '', desc: d.description ?? '', limit: d.display_limit ?? null,
      bf, enabled, params: d.params_defaults, overlay,
      scoring: d.scoring, dirs: d.scoring_directions ?? {},
      sl: d.stop_loss, mh: d.max_hold_days,
      tp: d.take_profit, ts: d.trailing_stop,
      ta: d.trailing_take_profit_activate, td: d.trailing_take_profit_drawdown,
      en: d.entry_signals ?? [], ex: d.exit_signals ?? [],
      children: children.map(c => ({ id: c.id, w: Math.round(c.weight) })),
    }))
  }, [])

  // 加载策略详情
  useEffect(() => {
    if (!strategyId) return
    setEditingChildId(null)
    setLoading(true)
    api.strategyGet(strategyId)
      .then(d => {
        loadIntoState(d)
        // composite 策略: 加载全部可选子策略(排除自身和其他 composite)供添加
        if (d.source === 'composite') {
          api.screenerStrategies().then(data => {
            setAllStrategies((data.presets ?? []).filter(s => s.id !== strategyId && s.source !== 'composite'))
          }).catch(() => setAllStrategies([]))
        }
      })
      .catch(() => setDetail(null))
      .finally(() => setLoading(false))
  }, [strategyId, loadIntoState])

  // ===== 未保存改动检测 (分区维度) =====
  const currentSnapshot = useMemo(() => buildSnapshot({
    name: strategyName, desc: strategyDesc, limit: displayLimit,
    bf: basicFilter, enabled: basicFilterEnabled, params,
    overlay: overlayFilter, scoring, dirs: scoringDirections,
    sl: stopLoss, mh: maxHoldDays,
    tp: takeProfit, ts: trailingStop, ta: ttpActivate, td: ttpDrawdown,
    en: entrySignals, ex: exitSignals,
    children: compositeChildren.map(c => ({ id: c.id, w: Math.round(c.weight) })),
  }), [strategyName, strategyDesc, displayLimit, basicFilter, basicFilterEnabled, params, overlayFilter, scoring, scoringDirections, stopLoss, maxHoldDays, takeProfit, trailingStop, ttpActivate, ttpDrawdown, entrySignals, exitSignals, compositeChildren])

  const dirty = useMemo(() => {
    if (!baseline) return { info: false, screen: false, scoring: false, trading: false, children: false }
    return {
      info: currentSnapshot.info !== baseline.info,
      screen: currentSnapshot.screen !== baseline.screen,
      scoring: currentSnapshot.scoring !== baseline.scoring,
      trading: currentSnapshot.trading !== baseline.trading,
      children: currentSnapshot.children !== baseline.children,
    }
  }, [currentSnapshot, baseline])
  const dirtyAny = dirty.info || dirty.screen || dirty.scoring || dirty.trading || dirty.children

  // 叠加策略: 滑块百分比口径, 允许总和 ≠100, 保存时自动按比例归一
  const compositeTotal = compositeChildren.reduce((s, c) => s + (c.weight || 0), 0)
  const removeCompositeChild = (id: string) => {
    setCompositeChildren(prev => prev.filter(c => c.id !== id))
  }
  const addCompositeChild = (s: { id: string; name: string; source?: string }) => {
    // 首个子策略独占 100%, 后续默认 10% (与因子编辑口径一致)
    setCompositeChildren(prev => [...prev, { id: s.id, name: s.name, source: s.source ?? '', weight: prev.length === 0 ? 100 : 10 }])
    setShowAddChild(false)
  }

  // 保存
  const handleSave = async () => {
    if (!strategyId) return
    setSaving(true)
    try {
      await api.strategySaveConfig(strategyId, {
        name: strategyName,
        description: strategyDesc,
        basic_filter: { ...basicFilter, enabled: basicFilterEnabled },
        params,
        ...(detail?.source !== 'composite' ? {
          scoring,
          scoring_directions: scoringDirections,
          scoring_replace: true,
        } : {}),
        stop_loss: stopLoss,
        take_profit: takeProfit,
        trailing_stop: trailingStop,
        trailing_take_profit_activate: ttpActivate,
        trailing_take_profit_drawdown: ttpDrawdown,
        max_hold_days: maxHoldDays,
        entry_signals: entrySignals,
        exit_signals: exitSignals,
        display_limit: displayLimit,
        // 叠加条件: 仅支持的策略类型随保存提交 (composite/minute 后端会拒绝);
        // 含「前N日」偏移 (leftDays/rightDays), 与信号库同一套条件结构
        ...(overlaySupported(detail) ? {
          overlay_filter: overlayFilter.map(c => ({ left: c.left, op: c.op, right: c.right, leftDays: c.leftDays ?? 0, rightDays: c.rightDays ?? 0 })),
        } : {}),
        // 叠加策略: 子策略权重(composite 专属, 走 override.children 持久化)
        ...(detail?.source === 'composite'
          ? { children: (() => {
              // 滑块百分比 → 归一小数权重再持久化
              const normalized = normalizeWeights(compositeChildren.map(c => c.weight))
              return compositeChildren.map((c, i) => ({ strategy_id: c.id, weight: normalized[i] }))
            })() }
          : {}),
      })
      onSaved?.(displayLimit)
      onClose()
    } finally {
      setSaving(false)
    }
  }

  // 重置
  const handleReset = async () => {
    if (!strategyId) return
    setResetting(true)
    try {
      await api.strategyResetConfig(strategyId)
      // 重新加载默认值 (走同一归一化路径, 基线一并刷新)
      const d = await api.strategyGet(strategyId)
      loadIntoState(d)
    } finally {
      setResetting(false)
    }
  }

  const handleDelete = async () => {
    if (!strategyId) return
    setDeleting(true)
    setDeleteError('')
    try {
      await api.strategyDelete(strategyId)
      onDeleted?.()
      onClose()
      setShowDeleteConfirm(false)
    } catch (e: any) {
      // request() 已弹 toast, 这里再在确认弹窗内显式提示, 并保持弹窗打开让用户知晓删除失败。
      setDeleteError(String(e?.message ?? '删除失败,请重试'))
    } finally { setDeleting(false) }
  }

  const handleDownload = async () => {
    if (!strategyId || !detail || (detail.source !== 'ai' && detail.source !== 'custom')) return
    const src = await api.strategyGetSource(strategyId)
    const blob = new Blob([src.code], { type: 'text/x-python;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${strategyId}.py`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  }

  // ===== 左侧导航分区 =====
  const isComposite = detail?.source === 'composite'
  const pages = useMemo(() => {
    if (!detail) return []
    return isComposite
      ? [{ id: 'children' as const, label: '子策略与权重', icon: Layers, accent: 'text-teal-400' }]
      : [
          { id: 'screen' as const, label: '策略条件', icon: Filter, accent: 'text-sky-400' },
          { id: 'scoring' as const, label: '策略权重', icon: Star, accent: 'text-amber-400' },
          { id: 'trading' as const, label: '纪律与触发', icon: TrendingUp, accent: 'text-emerald-400' },
        ]
  }, [detail, isComposite])

  // 导航徽标: 各分区核心数量一览
  const navBadges: Record<SettingsPage, string | number | null> = {
    screen: overlaySupported(detail) && overlayFilter.length > 0 ? overlayFilter.length : null,
    scoring: !isComposite && Object.keys(scoring).length > 0 ? Object.keys(scoring).length : null,
    trading: (entrySignals.length || exitSignals.length) ? `${entrySignals.length}/${exitSignals.length}` : null,
    children: compositeChildren.length > 0 ? compositeChildren.length : null,
  }

  const scoreCount = Object.keys(scoring).length

  if (!strategyId) return null

  return (
    <>
    <Modal
      onClose={() => {
        // 子策略编辑弹窗打开期间(Esc 会同时到达两层的 document 监听), 只关最上层的子编辑
        if (editingChildId) return
        onClose()
      }}
      labelledBy="strategy-settings-title"
      overlayClassName="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      panelClassName="w-[1200px] max-w-[95vw] max-h-[88vh] bg-surface/95 backdrop-blur-xl border border-border/50 rounded-2xl shadow-2xl flex flex-col overflow-hidden"
    >
          {/* 标题 */}
          <div className="flex items-center justify-between px-5 py-3 border-b border-border/50">
            <div className="flex items-center gap-2.5">
              <Settings2 className="h-4 w-4 text-accent" />
              <span id="strategy-settings-title" className="text-sm font-semibold text-foreground">{detail?.name ?? strategyId}</span>
              {detail && <span className="text-[10px] px-1.5 py-0.5 rounded bg-elevated text-muted">{{ custom: '自定义', ai: 'AI', composite: '叠加' }[detail.source] ?? detail.source}</span>}
              <span className="text-[10px] text-muted/40 font-mono">{strategyId}</span>
            </div>
            <div className="flex items-center gap-2">
              {detail && (detail.source === 'ai' || detail.source === 'custom') && (
                <button
                  aria-label="下载策略"
                  title="下载策略"
                  onClick={handleDownload}
                  className="inline-flex items-center gap-1.5 h-7 px-2.5 rounded-lg border border-border/60 bg-surface text-xs text-secondary hover:text-accent hover:border-accent/30 transition-colors cursor-pointer"
                >
                  <Download className="h-3.5 w-3.5" />
                  下载策略
                </button>
              )}
              <button aria-label="关闭" onClick={onClose} className="p-1.5 rounded-lg hover:bg-elevated transition-colors cursor-pointer"><X className="h-4 w-4 text-muted" /></button>
            </div>
          </div>

          {loading ? (
            <div className="flex flex-1 items-center justify-center py-16"><div className="w-6 h-6 border-2 border-accent/30 border-t-accent rounded-full animate-spin" /></div>
          ) : detail ? (
            <>
              {/* 名称 + 描述 + 显示上限 */}
              <div className="flex items-center gap-4 border-b border-border/40 px-5 py-2.5">
                <div className="flex min-w-0 flex-[3] items-center gap-2">
                  <span className="shrink-0 text-[10px] uppercase tracking-wider text-muted/50">名称</span>
                  <input type="text" value={strategyName} onChange={e => setStrategyName(e.target.value)}
                    className="h-8 min-w-0 flex-1 rounded-lg bg-base border-0 ring-1 ring-border/30 px-3 text-sm font-medium text-foreground focus:outline-none focus:ring-2 focus:ring-accent/30 transition-shadow" />
                </div>
                <div className="flex min-w-0 flex-[4] items-center gap-2">
                  <span className="shrink-0 text-[10px] uppercase tracking-wider text-muted/50">描述</span>
                  <input type="text" value={strategyDesc} onChange={e => setStrategyDesc(e.target.value)}
                    className="h-8 min-w-0 flex-1 rounded-lg bg-base border-0 ring-1 ring-border/30 px-3 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-accent/30 transition-shadow" />
                </div>
                <div className="flex shrink-0 items-center gap-1.5">
                  <span className="text-[10px] text-muted/50">显示上限</span>
                  <input type="number" value={displayLimit ?? ''} onChange={e => setDisplayLimit(e.target.value ? Number(e.target.value) : null)} step={1} min={10} max={200} placeholder="不限"
                    className="h-8 w-16 rounded-lg bg-base border border-border/40 px-1.5 text-xs font-mono text-foreground text-center focus:outline-none focus:border-accent/50" />
                  <span className="text-[10px] text-muted/50">只</span>
                </div>
              </div>

              {/* 主区: 左侧分区导航 + 右侧内容页 */}
              <div className="flex min-h-0 flex-1">
                <nav className="flex w-44 shrink-0 flex-col gap-0.5 overflow-y-auto border-r border-border/40 px-2.5 py-3">
                  {pages.map(p => {
                    const Icon = p.icon
                    const activePage = page === p.id
                    const badge = navBadges[p.id]
                    const pageDirty = dirty[p.id]
                    return (
                      <button
                        key={p.id}
                        type="button"
                        aria-current={activePage ? 'page' : undefined}
                        onClick={() => setPage(p.id)}
                        className={`flex h-9 items-center gap-2 rounded-lg border px-2.5 text-left transition-colors cursor-pointer ${
                          activePage
                            ? 'border-accent/25 bg-accent/10 text-accent'
                            : 'border-transparent text-secondary hover:bg-elevated/60 hover:text-foreground'
                        }`}
                      >
                        <Icon className={`h-3.5 w-3.5 shrink-0 ${activePage ? 'text-accent' : p.accent}`} />
                        <span className="min-w-0 flex-1 truncate text-xs font-medium">{p.label}</span>
                        {badge != null && <span className="shrink-0 font-mono text-[10px] text-muted/70">{badge}</span>}
                        {pageDirty && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400" title="有未保存的改动" />}
                      </button>
                    )
                  })}
                  {/* 底部: 策略元信息 */}
                  <div className="mt-auto space-y-1 border-t border-border/20 px-1 pt-3" hidden={pages.length === 0}>
                    <div className="flex items-center justify-between text-[10px] text-muted/50">
                      <span>执行后端</span>
                      <span className="font-mono text-muted/70">{BACKEND_LABEL[detail.execution_backend] ?? detail.execution_backend}</span>
                    </div>
                    {detail.timeframes.length > 0 && (
                      <div className="flex items-center justify-between text-[10px] text-muted/50">
                        <span>周期</span>
                        <span className="text-muted/70">{detail.timeframes.map(t => TF_LABEL[t] ?? t).join(' / ')}</span>
                      </div>
                    )}
                  </div>
                </nav>

                <div className="min-w-0 flex-1 overflow-y-auto px-5 py-4">
                  {/* 策略条件: 基础参数 / 策略参数 / 叠加条件 */}
                  <div className={page === 'screen' ? PAGE_ANIM : 'hidden'}>
                    <div className="space-y-3">
                      <p className="text-[11px] leading-5 text-muted/70">决定哪些股票进入候选：基础过滤 → 策略参数 → 叠加条件，逐层收紧。</p>
                      <div className="grid grid-cols-2 items-start gap-3">
                        <Card
                          icon={SlidersHorizontal}
                          title="基础参数"
                          accent="text-sky-400"
                          dim={!basicFilterEnabled}
                          extra={<>
                            <span className="text-[10px] text-muted/60">{basicFilterEnabled ? '已启用' : '已关闭'}</span>
                            <button
                              type="button"
                              onClick={() => setBasicFilterEnabled(v => !v)}
                              aria-pressed={basicFilterEnabled}
                              aria-label="启用基础参数过滤"
                              className={`relative inline-flex h-4 w-7 items-center rounded-full transition-colors duration-200 cursor-pointer ${basicFilterEnabled ? 'bg-sky-500' : 'bg-elevated'}`}
                            >
                              <span className={`inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform duration-200 ${basicFilterEnabled ? 'translate-x-[14px]' : 'translate-x-0.5'}`} />
                            </button>
                          </>}
                        >
                          <div className="space-y-2">
                            <RangeField label="价格" minVal={basicFilter.price_min} maxVal={basicFilter.price_max} onMinChange={v => setBF('price_min', v)} onMaxChange={v => setBF('price_max', v)} unit="元" step="1" />
                            <RangeField label="流通市值" minVal={basicFilter.float_cap_min != null ? basicFilter.float_cap_min / 1e8 : null} maxVal={basicFilter.float_cap_max != null ? basicFilter.float_cap_max / 1e8 : null} onMinChange={v => setBF('float_cap_min', v != null ? v * 1e8 : null)} onMaxChange={v => setBF('float_cap_max', v != null ? v * 1e8 : null)} unit="亿" step="5" />
                            <RangeField label="成交额" minVal={basicFilter.amount_min != null ? basicFilter.amount_min / 1e8 : null} maxVal={basicFilter.amount_max != null ? basicFilter.amount_max / 1e8 : null} onMinChange={v => setBF('amount_min', v != null ? v * 1e8 : null)} onMaxChange={v => setBF('amount_max', v != null ? v * 1e8 : null)} unit="亿" step="0.5" />
                            <RangeField label="换手率" minVal={basicFilter.turnover_min} maxVal={basicFilter.turnover_max} onMinChange={v => setBF('turnover_min', v)} onMaxChange={v => setBF('turnover_max', v)} unit="%" step="0.5" />
                            <div className="flex items-start gap-2 pt-0.5">
                              <span className={`${FIELD_LABEL_CLS} pt-0.5`}>板块</span>
                              <div className="flex flex-wrap gap-1">
                                {ALL_BOARDS.map(b => {
                                  const boards: string[] = basicFilter.boards ?? ALL_BOARDS
                                  const active = boards.includes(b)
                                  return (
                                    <button key={b} onClick={() => { const cur: string[] = basicFilter.boards ?? ALL_BOARDS; const next = active ? cur.filter(x => x !== b) : [...cur, b]; setBF('boards', next.length === 0 ? ALL_BOARDS : next) }}
                                      className={`px-2 py-1 rounded-md text-[11px] font-medium border transition-colors cursor-pointer ${active ? `${color.select.border} ${color.select.bgLight} ${color.select.text}` : `border-border bg-base text-muted ${color.select.borderHover}`}`}>{b}</button>
                                  )
                                })}
                              </div>
                            </div>
                            <div className="flex items-center gap-2">
                              <span className={FIELD_LABEL_CLS}>ST</span>
                              <button onClick={() => setBF('exclude_st', !basicFilter.exclude_st)}
                                className={`px-2 py-1 rounded-md text-[11px] font-medium border transition-colors cursor-pointer ${basicFilter.exclude_st ? 'border-danger/40 bg-danger/10 text-danger' : 'border-border bg-base text-muted hover:border-danger/30'}`}>{basicFilter.exclude_st ? '排除' : '包含'}</button>
                            </div>
                          </div>
                        </Card>

                        <Card
                          icon={Settings2}
                          title="策略参数"
                          accent="text-muted"
                          extra={detail.params.length > 0 ? <span className="text-[10px] text-muted/60">{detail.params.length} 项</span> : undefined}
                        >
                          {detail.params.length > 0 ? (
                            <div className="space-y-2">
                              {detail.params.map(p => <ParamField key={p.id} def={p} value={params[p.id]} onChange={v => setParams({ ...params, [p.id]: v })} />)}
                            </div>
                          ) : (
                            <div className="py-4 text-center text-xs text-muted/60">该策略无可调参数</div>
                          )}
                        </Card>
                      </div>

                      {/* 叠加条件: 每策略 overlay 硬过滤, 不改策略代码 */}
                      <Card
                        icon={Filter}
                        title="叠加条件"
                        accent="text-sky-400"
                        extra={overlaySupported(detail) && overlayFilter.length > 0
                          ? <span className="text-[10px] text-muted/60">{overlayFilter.length} 条</span>
                          : undefined}
                      >
                        {overlaySupported(detail) ? (
                          <div className="space-y-3">
                            <ConditionEditor
                              conditions={overlayFilter}
                              onChange={setOverlayFilter}
                              options={{ fields: overlayFields, groups: overlayGroups, stringFields: overlayStringFields }}
                              title="叠加过滤（多条件为「且」关系，硬过滤）"
                              allowEmpty
                            />
                            <div className="text-[11px] leading-5 text-muted/70">
                              叠加条件直接过滤策略候选，策略 / 回测 / 监控一致生效；只影响新入场，不触发已持仓卖出；字段可点「最新」切换为「前N日」（按交易日回看，上限 60 日，与信号库同口径）；字段数据缺失的日期不入选（如扩展数据未回补的交易日）。
                              {isMatrixBackend && ' matrix 策略不支持字符串字段条件。'}
                            </div>
                          </div>
                        ) : (
                          <div className="py-2 text-xs text-muted">
                            当前策略类型不支持叠加条件（composite 请对子策略单独配置，分钟策略不支持）。
                          </div>
                        )}
                      </Card>
                    </div>
                  </div>

                  {/* 策略权重 */}
                  <div className={page === 'scoring' ? PAGE_ANIM : 'hidden'}>
                    <div className="space-y-3">
                      <p className="text-[11px] leading-5 text-muted/70">对通过筛选的候选按因子加权打分排序；评分不改变是否入选，只决定排序先后。权重保存时自动归一。</p>
                      <Card
                        icon={Star}
                        title="策略权重"
                        accent="text-amber-400"
                        extra={scoreCount > 0 ? <span className="text-[10px] text-muted/60">{scoreCount} 个因子</span> : undefined}
                      >
                        <ScoringEditor
                          key={detail.id}
                          value={scoring}
                          directions={scoringDirections}
                          fallbackLabels={FIELD_LABEL}
                          onChange={(nextScoring, nextDirections) => {
                            setScoring(nextScoring)
                            setScoringDirections(nextDirections)
                          }}
                        />
                      </Card>
                    </div>
                  </div>

                  {/* 纪律与触发 */}
                  <div className={page === 'trading' ? PAGE_ANIM : 'hidden'}>
                    <div className="space-y-3">
                      <p className="text-[11px] leading-5 text-muted/70">控制买卖时点：出入场触发器对回测与监控生效（任一命中即触发）；策略扫描不消费触发器，仍按策略自身规则。风控与持仓纪律作为策略默认值保存，回测 / 优化 / 走查打开时自动继承。</p>
                      <Card icon={TrendingUp} title="风控与持仓纪律" accent="text-emerald-400">
                        <div className="space-y-2">
                          <div className="grid grid-cols-2 gap-x-6 gap-y-2">
                            <RiskPctField label="止损" value={stopLoss} onChange={setStopLoss} min={0} max={99} />
                            <RiskPctField label="止盈" value={takeProfit} onChange={setTakeProfit} min={1} max={500} />
                            <RiskPctField label="移动止损" value={trailingStop} onChange={setTrailingStop} min={0.5} max={50} />
                            <RiskPctField
                              label="回撤止盈启动"
                              value={ttpActivate}
                              min={1} max={200}
                              onChange={n => {
                                setTtpActivate(n)
                                // 联动: 回撤不能超过启动值 (与回测抽屉同规则)
                                if (n != null && ttpDrawdown != null && ttpDrawdown > n) setTtpDrawdown(n)
                              }}
                            />
                            <RiskPctField label="回撤止盈回撤" value={ttpDrawdown} onChange={setTtpDrawdown} min={0.5} max={50} unit="点" />
                            <div className="flex items-center gap-2">
                              <span className={FIELD_LABEL_CLS}>最长持仓</span>
                              <input type="number" value={maxHoldDays ?? ''} onChange={e => setMaxHoldDays(e.target.value === '' ? null : Number(e.target.value))} step={1} min={1}
                                className={`${FIELD_INPUT_CLS} w-[76px] px-2 text-center`} />
                              <span className="text-[10px] text-muted">天{maxHoldDays == null && '（不限）'}</span>
                            </div>
                          </div>
                          <div className="text-[10px] leading-4 text-muted/70">回撤止盈：涨幅达到「启动」后启用，从高点回撤「回撤」个百分点即卖出；留空 = 不启用对应纪律。</div>
                        </div>
                      </Card>

                      <div className="grid grid-cols-2 items-start gap-3">
                        <Card
                          icon={TrendingUp}
                          title="入场触发器"
                          accent="text-accent"
                          extra={<SignalTriggerActions kind="entry" signals={entrySignals} onChange={setEntrySignals} buttonClassName="rounded-md border border-border bg-base p-1 text-muted transition-colors cursor-pointer" iconClassName="h-3 w-3" />}
                        >
                          <div className="space-y-2">
                            <SignalPicker signals={entrySignals} onChange={setEntrySignals} kind="entry" options={{ variant: 'dialog' }} />
                            <div className="text-[10px] leading-4 text-muted/70">任一入场点满足即进入候选。</div>
                          </div>
                        </Card>

                        <Card
                          icon={TrendingUp}
                          title="出场触发器"
                          accent="text-warning"
                          extra={<SignalTriggerActions kind="exit" signals={exitSignals} onChange={setExitSignals} buttonClassName="rounded-md border border-border bg-base p-1 text-muted transition-colors cursor-pointer" iconClassName="h-3 w-3" />}
                        >
                          <div className="space-y-2">
                            <SignalPicker signals={exitSignals} onChange={setExitSignals} kind="exit" options={{ variant: 'dialog' }} />
                            <div className="text-[10px] leading-4 text-muted/70">任一出场点满足即触发出场。</div>
                          </div>
                        </Card>
                      </div>
                    </div>
                  </div>

                  {/* 叠加策略: 子策略列表 + 权重 (composite 专属) */}
                  <div className={page === 'children' ? PAGE_ANIM : 'hidden'}>
                    {isComposite && (() => {
                      const SRC_LABEL: Record<string, string> = { custom: '自定义', ai: 'AI' }
                      const SRC_CLS: Record<string, string> = {
                        custom: 'border-amber-400/25 bg-amber-400/10 text-amber-400',
                        ai: 'border-purple-500/25 bg-purple-500/10 text-purple-400',
                      }
                      const selectedIds = new Set(compositeChildren.map(c => c.id))
                      const candidates = allStrategies.filter(s => !selectedIds.has(s.id))
                      return (
                      <div className="space-y-3">
                        <p className="text-[11px] leading-5 text-muted/70">叠加策略按权重融合子策略的结果；点击子策略名可单独编辑其配置。</p>
                        <div className="rounded-xl border border-teal-500/20 bg-teal-500/[0.03] overflow-hidden">
                          <div className="flex items-center gap-2 border-b border-teal-500/15 bg-teal-500/[0.06] px-4 py-2.5">
                            <Layers className="h-3.5 w-3.5 text-teal-400" />
                            <span className="text-xs font-medium text-foreground/80">子策略与权重</span>
                            <div className="ml-auto flex items-center gap-2">
                              <span className="text-[10px] text-muted flex items-center gap-1.5">
                                共 {compositeChildren.length} 个 · 权重
                                <span className={`font-mono ${compositeChildren.length > 0 && compositeTotal !== 100 ? 'text-amber-400' : 'text-emerald-400'}`}>
                                  {compositeTotal}%
                                </span>
                                {compositeChildren.length > 0 && compositeTotal !== 100 && (
                                  <span className="text-amber-400/60">(保存时自动按比例归一)</span>
                                )}
                              </span>
                              <button onClick={() => setShowAddChild(v => !v)} className="inline-flex items-center gap-1 h-6 px-2 rounded-lg border border-teal-500/30 bg-teal-500/10 text-[11px] text-teal-400 hover:bg-teal-500/20 cursor-pointer">
                                <Plus className="h-3 w-3" />添加
                              </button>
                            </div>
                          </div>
                          <div className="space-y-3 px-4 py-3">
                            {/* 添加子策略面板 */}
                            {showAddChild && (
                              <div className="rounded-lg border border-border bg-base/60 p-2 space-y-1 max-h-56 overflow-y-auto">
                                {candidates.length === 0 ? (
                                  <div className="text-[11px] text-muted py-2 text-center">无可添加的策略</div>
                                ) : candidates.map(s => (
                                  <button key={s.id} onClick={() => addCompositeChild(s)} className="flex w-full items-center gap-1.5 rounded px-2 py-1 text-left hover:bg-teal-500/10 cursor-pointer">
                                    <Plus className="h-3 w-3 shrink-0 text-teal-400" />
                                    <span className="flex-1 truncate text-xs text-foreground">{s.name}</span>
                                    {s.source && (
                                      <span className={`rounded border px-1 text-[8px] ${SRC_CLS[s.source] ?? ''}`}>{SRC_LABEL[s.source] ?? s.source}</span>
                                    )}
                                  </button>
                                ))}
                              </div>
                            )}
                            {compositeChildren.length === 0 ? (
                              <div className="text-xs text-muted py-6 text-center">暂无子策略，点击右上角「添加」选择</div>
                            ) : (
                              <div className="space-y-1.5">
                                {compositeChildren.map((c, i) => (
                                  <div key={c.id} className="flex items-center gap-3 rounded-lg bg-base/60 px-3 py-2">
                                    <span className="w-5 shrink-0 font-mono text-[10px] text-muted/50">{i + 1}</span>
                                    <div className="min-w-0 flex-1">
                                      <div className="flex items-center gap-1.5">
                                        <button
                                          type="button"
                                          onClick={() => setEditingChildId(c.id)}
                                          title="点击编辑该子策略的配置"
                                          className="truncate text-left text-xs font-medium text-foreground transition-colors hover:text-accent cursor-pointer"
                                        >
                                          {c.name || c.id}
                                        </button>
                                        {c.source && (
                                          <span className={`shrink-0 rounded border px-1 text-[8px] ${SRC_CLS[c.source] ?? ''}`}>{SRC_LABEL[c.source] ?? c.source}</span>
                                        )}
                                      </div>
                                      <div className="font-mono text-[10px] text-muted/50">{c.id}</div>
                                    </div>
                                    <div className="flex shrink-0 items-center gap-2">
                                      <input
                                        type="range"
                                        min={0}
                                        max={100}
                                        step={1}
                                        value={c.weight}
                                        onChange={e => setCompositeChildren(prev => prev.map((p, j) => j === i ? { ...p, weight: parseInt(e.target.value) || 0 } : p))}
                                        className="h-1 w-36 cursor-pointer accent-teal-400"
                                        aria-label={`${c.name || c.id}权重`}
                                      />
                                      <span className="w-9 text-right font-mono text-[10px] text-muted">{Math.round(c.weight)}%</span>
                                      <button onClick={() => removeCompositeChild(c.id)} className="p-1 text-danger/50 hover:text-danger cursor-pointer" aria-label={`移除${c.name || c.id}`}>
                                        <Trash2 className="h-3 w-3" />
                                      </button>
                                    </div>
                                  </div>
                                ))}
                              </div>
                            )}
                            <div className="border-t border-border/30 pt-2 text-[10px] text-muted/60">
                              提示: 权重按相对比例生效, 保存时自动归一; 修改后点底部「保存设置」生效。
                            </div>
                          </div>
                        </div>
                      </div>
                      )
                    })()}
                  </div>
                </div>
              </div>
            </>
          ) : (
            <div className="flex flex-1 items-center justify-center py-16 text-sm text-muted">加载失败</div>
          )}

          {/* 底部按钮 */}
          <div className="flex items-center justify-between border-t border-border/50 bg-surface/50 px-5 py-3">
            <div className="flex items-center gap-2">
              <button onClick={handleReset} disabled={resetting}
                className="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg border border-border bg-surface text-xs text-secondary hover:text-danger hover:border-danger/30 transition-colors cursor-pointer disabled:opacity-50">
                <RotateCcw className="h-3.5 w-3.5" />{resetting ? '重置中…' : '重置默认'}
              </button>
              {(detail?.source === 'ai' || detail?.source === 'custom' || detail?.source === 'composite') && (
                <button onClick={() => { setDeleteError(''); setShowDeleteConfirm(true) }}
                  className="text-[10px] text-danger hover:text-danger/80 transition-colors">删除策略</button>
              )}
            </div>
            <div className="flex items-center gap-2">
              {dirtyAny && (
                <span className="mr-1 inline-flex items-center gap-1.5 text-[11px] text-amber-400/90" title="改动需点击「保存设置」后生效">
                  <span className="h-1.5 w-1.5 rounded-full bg-amber-400" />有未保存的改动
                </span>
              )}
              {onAiModify && (detail?.source === 'ai' || detail?.source === 'custom') && (
                <button onClick={onAiModify}
                  className="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg border border-amber-400/30 bg-amber-400/8 text-amber-400 text-xs font-medium hover:bg-amber-400/15 transition-colors cursor-pointer">
                  <Sparkles className="h-3.5 w-3.5" />AI 修改
                </button>
              )}
              <button onClick={handleSave} disabled={saving}
                className="inline-flex items-center gap-1.5 h-8 px-4 rounded-lg bg-accent text-white text-xs font-semibold hover:bg-accent/90 transition-colors cursor-pointer disabled:opacity-50">
                <Save className="h-3.5 w-3.5" />{saving ? '保存中…' : '保存设置'}
              </button>
            </div>
          </div>
    </Modal>

    {/* 删除确认弹窗 — 必须放 Modal 外: Modal 面板有 backdrop-blur (为 fixed 后代建立定位上下文)
        + overflow-hidden, 放里面会导致本应全屏居中的确认框相对面板定位并被裁剪/错位。 */}
    {showDeleteConfirm && (
      <AnimatePresence>
        <motion.div
          initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
          className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 backdrop-blur-sm"
          onClick={() => setShowDeleteConfirm(false)}
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.95 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: 0.95 }}
            className="w-[380px] bg-surface border border-border/50 rounded-2xl shadow-2xl p-6"
            onClick={e => e.stopPropagation()}
          >
            <div className="text-center space-y-3">
              <div className="w-10 h-10 rounded-full bg-danger/10 flex items-center justify-center mx-auto">
                <span className="text-danger text-lg">!</span>
              </div>
              <div>
                <div className="text-sm font-semibold text-foreground">删除策略</div>
                <div className="text-xs text-muted mt-1">确定要删除「{detail?.name ?? strategyId}」吗？</div>
              </div>
              <div className="text-[11px] text-danger/70 bg-danger/[0.04] rounded-lg px-3 py-2 border border-danger/10">
                删除后无法恢复，策略文件、配置和关联数据将被永久清除。
              </div>
              {deleteError && (
                <div className="text-[11px] text-danger bg-danger/10 rounded-lg px-3 py-2 border border-danger/20">
                  {deleteError}
                </div>
              )}
              <div className="flex gap-2 pt-2">
                <button onClick={() => setShowDeleteConfirm(false)}
                  className="flex-1 h-8 rounded-lg border border-border text-xs text-secondary hover:text-foreground">取消</button>
                <button onClick={handleDelete} disabled={deleting}
                  className="flex-1 h-8 rounded-lg bg-danger text-white text-xs font-medium hover:bg-danger/90 disabled:opacity-50">
                  {deleting ? '删除中...' : '确认删除'}
                </button>
              </div>
            </div>
          </motion.div>
        </motion.div>
      </AnimatePresence>
    )}

    {/* 子策略配置编辑 — 同删除确认弹窗一样必须放 Modal 外 (面板 backdrop-blur 会为
        fixed 后代建立定位上下文)。渲染在主 Modal 之后, 同 z-50 自然覆盖其上。 */}
    <StrategySettingsDialog
      strategyId={editingChildId}
      onClose={() => setEditingChildId(null)}
      onSaved={() => {
        // 子策略可能改名: 拉最新名称同步到列表 (参数 override 按策略 ID 生效, 无需重建叠加)
        if (!editingChildId) return
        api.strategyGet(editingChildId)
          .then(d => setCompositeChildren(prev =>
            prev.map(c => c.id === editingChildId ? { ...c, name: d.name ?? c.name } : c),
          ))
          .catch(() => {})
      }}
      onDeleted={() => {
        setCompositeChildren(prev => prev.filter(c => c.id !== editingChildId))
        setEditingChildId(null)
      }}
    />
    </>

  )
}
