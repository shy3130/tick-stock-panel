import { useState, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, Settings2, RotateCcw, Save, ChevronDown, Filter, Star, TrendingUp, Sparkles, Download } from 'lucide-react'
import { api, type StrategyDetail, type StrategyParamDef } from '@/lib/api'
import { BUILTIN_COLUMNS } from '@/lib/watchlist-columns'
import { color } from '@/lib/colors'
import { SignalPicker } from './SignalPicker'
import { SignalTriggerActions } from '@/components/signals/SignalTriggerActions'
import { Modal } from '@/components/Modal'

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
})

interface Props {
  strategyId: string | null
  onClose: () => void
  onSaved?: (displayLimit: number | null) => void
  onAiModify?: () => void
  onDeleted?: () => void
}

// ===== 可折叠区域 =====
function Section({ icon: Icon, title, accent, defaultOpen = true, children, extra }: {
  icon?: React.ComponentType<{ className?: string }>
  title: string
  accent?: string
  defaultOpen?: boolean
  children: React.ReactNode
  extra?: React.ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="rounded-xl border border-border/15 bg-surface/20 overflow-hidden">
      <div className="flex items-center gap-2 px-3.5 py-2 hover:bg-surface/30 transition-colors">
        <button
          onClick={() => setOpen(v => !v)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left cursor-pointer"
        >
          <ChevronDown className={`h-3 w-3 text-muted/40 transition-transform duration-200 ${open ? '' : '-rotate-90'}`} />
          {Icon && <Icon className={`h-3.5 w-3.5 ${accent ?? 'text-muted'}`} />}
          <span className="text-[11px] font-medium text-foreground/70">{title}</span>
        </button>
        {extra && <div className="ml-auto flex items-center gap-1">{extra}</div>}
      </div>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="overflow-hidden"
          >
            <div className="px-3.5 pb-3 pt-0.5 space-y-2">
              {children}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}


// ===== 区间字段（最小 ~ 最大） =====
function RangeField({ label, minVal, maxVal, onMinChange, onMaxChange, unit, step }: {
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
      <span className="text-[11px] text-secondary w-16 shrink-0 text-right">{label}</span>
      <input
        type="number"
        value={minVal ?? ''}
        onChange={e => onMinChange(e.target.value === '' ? null : Number(e.target.value))}
        placeholder="最小"
        step={step}
        className="w-20 px-1.5 py-0.5 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50"
      />
      <span className="text-[10px] text-muted">~</span>
      <input
        type="number"
        value={maxVal ?? ''}
        onChange={e => onMaxChange(e.target.value === '' ? null : Number(e.target.value))}
        placeholder="最大"
        step={step}
        className="w-20 px-1.5 py-0.5 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50"
      />
      {unit && <span className="text-[10px] text-muted shrink-0">{unit}</span>}
    </div>
  )
}

// 板块标签
const ALL_BOARDS = ['沪主板', '深主板', '创业板', '科创板', '北交所']

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
        <span className="text-[11px] text-secondary w-16 shrink-0 text-right">{def.label}</span>
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
      </div>
    )
  }
  if (def.type === 'select' && def.options) {
    return (
      <div className="flex items-center gap-2">
        <span className="text-[11px] text-secondary w-16 shrink-0 text-right">{def.label}</span>
        <select
          value={value ?? def.default}
          onChange={e => onChange(e.target.value)}
          className="w-24 px-1.5 py-0.5 rounded bg-base border border-border text-[11px] font-mono text-foreground focus:outline-none focus:border-accent/50"
        >
          {def.options.map(o => <option key={o} value={o}>{o}</option>)}
        </select>
      </div>
    )
  }

  return (
    <div className="flex items-center gap-2">
      <span className="text-[11px] text-secondary w-16 shrink-0 text-right">{def.label}</span>
      <input
        type="number"
        value={value ?? def.default}
        onChange={e => onChange(e.target.value === '' ? def.default : Number(e.target.value))}
        step={def.step ?? 0.1}
        min={def.min}
        max={def.max}
        className="w-20 px-1.5 py-0.5 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50"
      />
      {def.min != null && def.max != null && (
        <span className="text-[10px] text-muted">{def.min}~{def.max}</span>
      )}
    </div>
  )
}

// 评分权重字段
function ScoringField({ col, weight, pct, editing, onChange }: {
  col: string; weight: number; pct: number; editing: boolean; onChange: (v: number) => void
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-[11px] text-secondary w-16 shrink-0 text-right">{FIELD_LABEL[col] ?? col}</span>
      {editing ? (
        <input
          type="range"
          value={weight}
          onChange={e => onChange(Number(e.target.value))}
          min={0} max={100} step={1}
          className="flex-1 h-1 accent-amber-400 cursor-pointer"
        />
      ) : (
        <div className="flex-1 h-1.5 bg-elevated rounded-full overflow-hidden">
          <div className="h-full bg-amber-400/70 rounded-full transition-all duration-300" style={{ width: `${Math.min(pct, 100)}%` }} />
        </div>
      )}
      <span className="w-10 text-right text-[10px] font-mono text-muted">{editing ? weight : `${pct}%`}</span>
    </div>
  )
}

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
  const [stopLoss, setStopLoss] = useState<number | null>(null)
  const [maxHoldDays, setMaxHoldDays] = useState<number | null>(null)
  const [entrySignals, setEntrySignals] = useState<string[]>([])
  const [exitSignals, setExitSignals] = useState<string[]>([])
  const [displayLimit, setDisplayLimit] = useState<number | null>(null)
  const [basicFilterEnabled, setBasicFilterEnabled] = useState(true)
  const [editingScoring, setEditingScoring] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [deleteError, setDeleteError] = useState('')

  // 辅助：更新 basicFilter 某个 key
  const setBF = useCallback((key: string, value: any) => {
    setBasicFilter(prev => ({ ...prev, [key]: value }))
  }, [])

  // 加载策略详情
  useEffect(() => {
    if (!strategyId) return
    setLoading(true)
    api.strategyGet(strategyId)
      .then(d => {
        setDetail(d)
        setStrategyName(d.name ?? '')
        setStrategyDesc(d.description ?? '')
        // 确保 boards 有默认值
        const bf = { ...d.basic_filter }
        if (!bf.boards) bf.boards = ALL_BOARDS
        setBasicFilter(bf)
        setParams(d.params_defaults)
        setScoring(Object.fromEntries(Object.entries(d.scoring).map(([k, v]) => [k, Math.round((v as number) * 100)])))
        setStopLoss(d.stop_loss)
        setMaxHoldDays(d.max_hold_days)
        setEntrySignals(d.entry_signals ?? [])
        setExitSignals(d.exit_signals ?? [])
        setDisplayLimit(d.display_limit ?? null)
        setBasicFilterEnabled(d.basic_filter?.enabled !== false)
      })
      .catch(() => setDetail(null))
      .finally(() => setLoading(false))
  }, [strategyId])

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
        scoring: Object.fromEntries(Object.entries(scoring).map(([k, v]) => [k, +(v / 100).toFixed(4)])),
        stop_loss: stopLoss,
        max_hold_days: maxHoldDays,
        entry_signals: entrySignals,
        exit_signals: exitSignals,
        display_limit: displayLimit,
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
      // 重新加载默认值
      const d = await api.strategyGet(strategyId)
      setDetail(d)
      setStrategyName(d.name ?? '')
      setStrategyDesc(d.description ?? '')
      const bf = { ...d.basic_filter }
      if (!bf.boards) bf.boards = ALL_BOARDS
      setBasicFilter(bf)
      setParams(d.params_defaults)
      setScoring(Object.fromEntries(Object.entries(d.scoring).map(([k, v]) => [k, Math.round((v as number) * 100)])))
        setStopLoss(d.stop_loss)
        setMaxHoldDays(d.max_hold_days)
        setEntrySignals(d.entry_signals ?? [])
        setExitSignals(d.exit_signals ?? [])
        setDisplayLimit(d.display_limit ?? null)
        setBasicFilterEnabled(d.basic_filter?.enabled !== false)
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

  if (!strategyId) return null

  return (
    <>
    <Modal
      onClose={onClose}
      labelledBy="strategy-settings-title"
      overlayClassName="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      panelClassName="w-[980px] max-h-[88vh] bg-surface/95 backdrop-blur-xl border border-border/50 rounded-2xl shadow-2xl flex flex-col overflow-hidden"
    >
          {/* 标题 */}
          <div className="flex items-center justify-between px-5 py-3 border-b border-border/50">
            <div className="flex items-center gap-2.5">
              <Settings2 className="h-4 w-4 text-accent" />
              <span id="strategy-settings-title" className="text-sm font-semibold text-foreground">{detail?.name ?? strategyId}</span>
              {detail && <span className="text-[10px] px-1.5 py-0.5 rounded bg-elevated text-muted">{{ builtin: '内置', custom: '自定义', ai: 'AI' }[detail.source] ?? detail.source}</span>}
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

          {/* 内容 */}
          <div className="flex-1 overflow-y-auto px-5 py-5 space-y-5">
            {loading ? (
              <div className="flex items-center justify-center py-16"><div className="w-6 h-6 border-2 border-accent/30 border-t-accent rounded-full animate-spin" /></div>
            ) : detail ? (
              <>
                {/* 名称 + 描述 + 显示上限 */}
                <div className="flex items-end gap-4">
                  <div className="flex-1 space-y-2">
                    <div className="flex items-center gap-2">
                      <span className="text-[10px] text-muted/50 uppercase tracking-wider w-8 shrink-0">名称</span>
                      <input type="text" value={strategyName} onChange={e => setStrategyName(e.target.value)}
                        className="flex-1 h-8 px-3 rounded-lg bg-base border-0 ring-1 ring-border/30 text-sm font-medium text-foreground focus:outline-none focus:ring-2 focus:ring-accent/30 transition-shadow" />
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="text-[10px] text-muted/50 uppercase tracking-wider w-8 shrink-0">描述</span>
                      <input type="text" value={strategyDesc} onChange={e => setStrategyDesc(e.target.value)}
                        className="flex-1 h-8 px-3 rounded-lg bg-base border-0 ring-1 ring-border/30 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-accent/30 transition-shadow" />
                    </div>
                  </div>
                  <div className="flex items-center gap-1.5 pb-0.5 shrink-0">
                    <span className="text-[10px] text-muted/50">显示上限</span>
                    <input type="number" value={displayLimit ?? ''} onChange={e => setDisplayLimit(e.target.value ? Number(e.target.value) : null)} step={1} min={10} max={200} placeholder="不限"
                      className="w-14 h-8 px-1.5 rounded-lg bg-base border border-border/40 text-xs font-mono text-foreground text-center focus:outline-none focus:border-accent/50" />
                    <span className="text-[10px] text-muted/50">只</span>
                  </div>
                </div>

                {/* 三列 */}
                <div className="grid grid-cols-3 gap-5 items-start">
                  {/* 列1：选股条件 */}
                    <Section icon={Filter} title="基础参数" accent="text-sky-400">
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-[10px] text-muted">启用基础参数过滤</span>
                        <button onClick={() => setBasicFilterEnabled(v => !v)}
                          className={`relative w-8 h-[18px] rounded-full transition-colors ${basicFilterEnabled ? 'bg-sky-500' : 'bg-border'}`}>
                          <span className={`absolute top-0.5 w-3.5 h-3.5 rounded-full bg-white transition-transform ${basicFilterEnabled ? 'left-[16px]' : 'left-0.5'}`} />
                        </button>
                      </div>
                      <div className={`space-y-2 transition-opacity duration-200 ${basicFilterEnabled ? '' : 'opacity-25 pointer-events-none'}`}>
                      <RangeField label="价格" minVal={basicFilter.price_min} maxVal={basicFilter.price_max} onMinChange={v => setBF('price_min', v)} onMaxChange={v => setBF('price_max', v)} unit="元" step="1" />
                      <RangeField label="流通市值" minVal={basicFilter.float_cap_min != null ? basicFilter.float_cap_min / 1e8 : null} maxVal={basicFilter.float_cap_max != null ? basicFilter.float_cap_max / 1e8 : null} onMinChange={v => setBF('float_cap_min', v != null ? v * 1e8 : null)} onMaxChange={v => setBF('float_cap_max', v != null ? v * 1e8 : null)} unit="亿" step="5" />
                      <RangeField label="成交额" minVal={basicFilter.amount_min != null ? basicFilter.amount_min / 1e8 : null} maxVal={basicFilter.amount_max != null ? basicFilter.amount_max / 1e8 : null} onMinChange={v => setBF('amount_min', v != null ? v * 1e8 : null)} onMaxChange={v => setBF('amount_max', v != null ? v * 1e8 : null)} unit="亿" step="0.5" />
                      <RangeField label="换手率" minVal={basicFilter.turnover_min} maxVal={basicFilter.turnover_max} onMinChange={v => setBF('turnover_min', v)} onMaxChange={v => setBF('turnover_max', v)} unit="%" step="0.5" />
                      <div className="space-y-1.5">
                        <div className="flex items-start gap-1.5">
                          <span className="text-[11px] text-secondary w-16 shrink-0 text-right pt-0.5">板块</span>
                          <div className="flex flex-wrap gap-0.5">
                            {ALL_BOARDS.map(b => {
                              const boards: string[] = basicFilter.boards ?? ALL_BOARDS
                              const active = boards.includes(b)
                              return (
                                <button key={b} onClick={() => { const cur: string[] = basicFilter.boards ?? ALL_BOARDS; const next = active ? cur.filter(x => x !== b) : [...cur, b]; setBF('boards', next.length === 0 ? ALL_BOARDS : next) }}
                                  className={`px-1.5 py-0.5 rounded text-[10px] font-medium border transition-colors cursor-pointer ${active ? `${color.select.border} ${color.select.bgLight} ${color.select.text}` : `border-border bg-base text-muted ${color.select.borderHover}`}`}>{b}</button>
                              )
                            })}
                          </div>
                        </div>
                        <div className="flex items-center gap-1.5">
                          <span className="text-[11px] text-secondary w-16 shrink-0 text-right">ST</span>
                          <button onClick={() => setBF('exclude_st', !basicFilter.exclude_st)}
                            className={`px-1.5 py-0.5 rounded text-[10px] font-medium border transition-colors cursor-pointer ${basicFilter.exclude_st ? 'border-danger/40 bg-danger/10 text-danger' : 'border-border bg-base text-muted hover:border-danger/30'}`}>{basicFilter.exclude_st ? '排除' : '包含'}</button>
                        </div>
                      </div>
                    </div>
                  </Section>

                  {/* 列2：策略参数 */}
                  <div className="space-y-3">
                    {detail.params.length > 0 ? (
                      <Section icon={Settings2} title="策略参数" accent="text-muted">
                        <div className="space-y-1.5">
                          {detail.params.map(p => <ParamField key={p.id} def={p} value={params[p.id]} onChange={v => setParams({ ...params, [p.id]: v })} />)}
                        </div>
                      </Section>
                    ) : (
                      <div className="rounded-xl border border-border/15 bg-surface/20 px-3.5 py-4 text-[11px] text-muted/50 text-center">无策略参数</div>
                    )}
                  </div>

                  {/* 列3：评分 + 交易 */}
                  <div className="space-y-3">
                    <Section icon={Star} title="评分权重" accent="text-amber-400">
                      {Object.entries(scoring).length > 0 ? (() => {
                        const total = Object.values(scoring).reduce((a: number, b: number) => a + b, 0) || 1
                        return (
                          <div className="space-y-2">
                            {Object.entries(scoring).map(([col, w]) => {
                              const pct = Math.round((w / total) * 100)
                              return (
                                <ScoringField key={col} col={col} weight={w} pct={pct}
                                  editing={editingScoring}
                                  onChange={v => setScoring({ ...scoring, [col]: Math.max(0, v) })} />
                              )
                            })}
                            <div className="flex items-center justify-between pt-1.5 border-t border-border/10">
                              <div className="flex items-center gap-1.5 text-[10px] text-muted">
                                <span>总和</span>
                                <span className={`font-mono font-medium text-xs ${editingScoring ? (total === 100 ? color.ok : color.scoreWarn) : color.ok}`}>{editingScoring ? total : '100'}</span>
                                <span className="text-muted/40">自动归权计算</span>
                              </div>
                              <button
                                onClick={() => {
                                  if (editingScoring) {
                                    // 确认：归一化到 100
                                    const sum = Object.values(scoring).reduce((a: number, b: number) => a + b, 0) || 1
                                    const norm = Object.fromEntries(
                                      Object.entries(scoring).map(([k, v]) => [k, Math.round((v / sum) * 100)])
                                    )
                                    // 修正舍入误差
                                    const newSum = Object.values(norm).reduce((a: number, b: number) => a + b, 0)
                                    if (newSum !== 100) {
                                      const keys = Object.keys(norm)
                                      norm[keys[0]] += (100 - newSum)
                                    }
                                    setScoring(norm)
                                  } else {
                                    // 进入编辑：展开为 0-100 范围
                                    const sum = Object.values(scoring).reduce((a: number, b: number) => a + b, 0) || 1
                                    setScoring(Object.fromEntries(
                                      Object.entries(scoring).map(([k, v]) => [k, Math.round((v / sum) * 100)])
                                    ))
                                  }
                                  setEditingScoring(v => !v)
                                }}
                                className="text-[10px] text-accent/80 hover:text-accent cursor-pointer"
                              >{editingScoring ? '确定' : '设置'}</button>
                            </div>
                          </div>
                        )
                      })() : <div className="text-[11px] text-muted">未配置</div>}
                    </Section>

                    <Section icon={TrendingUp} title="交易参数" accent="text-emerald-400">
                      <div className="space-y-2">
                        <div className="flex items-center gap-1.5">
                          <span className="text-[11px] text-secondary w-12 shrink-0">止损</span>
                          <input type="number" value={stopLoss ?? ''} onChange={e => setStopLoss(e.target.value === '' ? null : Number(e.target.value))} step={0.01} min={-0.5} max={0}
                            className="w-16 h-6 px-1.5 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50" />
                          <span className="text-[10px] text-muted">{stopLoss != null ? `${(stopLoss * 100).toFixed(1)}%` : '—'}</span>
                        </div>
                        <div className="flex items-center gap-1.5">
                          <span className="text-[11px] text-secondary w-12 shrink-0">持有</span>
                          <input type="number" value={maxHoldDays ?? ''} onChange={e => setMaxHoldDays(e.target.value === '' ? null : Number(e.target.value))} step={1} min={1}
                            className="w-16 h-6 px-1.5 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50" />
                          <span className="text-[10px] text-muted">天</span>
                        </div>
                        <div className="text-[11px] text-muted pt-1 border-t border-border/10">
                          <span className="text-secondary">入场 </span><span className="text-foreground/70">{entrySignals.length > 0 ? `${entrySignals.length} 个触发器` : '无'}</span>
                          <span className="text-secondary ml-3">出场 </span><span className="text-foreground/70">{exitSignals.length > 0 ? `${exitSignals.length} 个触发器` : '无'}</span>
                        </div>
                      </div>
                    </Section>

                    <Section
                      icon={TrendingUp}
                      title="入场触发器"
                      accent="text-accent"
                      defaultOpen={false}
                      extra={<SignalTriggerActions kind="entry" signals={entrySignals} onChange={setEntrySignals} buttonClassName="rounded-md border border-border bg-base p-1 text-muted transition-colors cursor-pointer" iconClassName="h-3 w-3" />}
                    >
                      <SignalPicker signals={entrySignals} onChange={setEntrySignals} kind="entry" variant="dialog" />
                      <div className="text-[10px] leading-4 text-muted/70">任一入场点满足即进入候选。</div>
                    </Section>

                    <Section
                      icon={TrendingUp}
                      title="出场触发器"
                      accent="text-warning"
                      defaultOpen={false}
                      extra={<SignalTriggerActions kind="exit" signals={exitSignals} onChange={setExitSignals} buttonClassName="rounded-md border border-border bg-base p-1 text-muted transition-colors cursor-pointer" iconClassName="h-3 w-3" />}
                    >
                      <SignalPicker signals={exitSignals} onChange={setExitSignals} kind="exit" variant="dialog" />
                      <div className="text-[10px] leading-4 text-muted/70">任一出场点满足即触发出场。</div>
                    </Section>

                    <div className="rounded-xl border border-amber-400/20 bg-amber-400/[0.04] px-3 py-2 text-[10px] leading-4 text-muted">
                      出入场触发器保存后对<b className="text-secondary">回测和监控</b>生效;选股扫描仍按策略本身的筛选规则,不受此影响。
                    </div>

                    {detail.alerts.length > 0 && (
                      <Section icon={Settings2} title="提醒" accent="text-muted">
                        <div className="space-y-1">
                          {detail.alerts.map((a, i) => (
                            <div key={i} className="text-[10px] text-secondary">{a.message} <span className="text-muted font-mono">{a.op ? `${FIELD_LABEL[a.field] ?? a.field} ${a.op} ${a.value}` : FIELD_LABEL[a.field] ?? a.field}</span></div>
                          ))}
                        </div>
                      </Section>
                    )}
                  </div>
                </div>
              </>
            ) : (
              <div className="flex items-center justify-center py-16 text-sm text-muted">加载失败</div>
            )}
          </div>

          {/* 底部按钮 */}
          <div className="flex items-center justify-between px-5 py-3 border-t border-border/50 bg-surface/50">
            <div className="flex items-center gap-2">
              <button onClick={handleReset} disabled={resetting}
                className="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg border border-border bg-surface text-xs text-secondary hover:text-danger hover:border-danger/30 transition-colors cursor-pointer disabled:opacity-50">
                <RotateCcw className="h-3.5 w-3.5" />{resetting ? '重置中…' : '重置默认'}
              </button>
              {(detail?.source === 'ai' || detail?.source === 'custom') && (
                <button onClick={() => { setDeleteError(''); setShowDeleteConfirm(true) }}
                  className="text-[10px] text-muted/40 hover:text-danger transition-colors">删除策略</button>
              )}
            </div>
            <div className="flex items-center gap-2">
              {(detail?.source === 'ai' || detail?.source === 'custom') && (
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
    </>

  )
}
