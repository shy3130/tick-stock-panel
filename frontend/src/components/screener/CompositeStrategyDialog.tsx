import { useState, useMemo, useEffect, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, Layers, Plus, Loader2, Search } from 'lucide-react'
import { api, type ScreenerStrategy } from '@/lib/api'

interface Props {
  open: boolean
  onClose: () => void
  onSavedId?: (id: string) => void | Promise<void>
  /** 编辑模式: 传入已有 composite 策略 id 时为 update, 否则 create */
  editStrategyId?: string | null
}

interface ChildItem {
  strategy_id: string
  weight: number
}

const SRC_MAP: Record<string, string> = { builtin: '内置', custom: '自定义', ai: 'AI' }
const BADGE_CLS: Record<string, string> = {
  builtin: 'bg-accent/10 text-accent border-accent/20',
  ai: 'bg-purple-500/10 text-purple-400 border-purple-500/20',
  custom: 'bg-amber-400/10 text-amber-400 border-amber-400/30',
}

export function CompositeStrategyDialog({ open, onClose, onSavedId, editStrategyId }: Props) {
  const isEdit = !!editStrategyId
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [strategyId, setStrategyId] = useState('')
  const [children, setChildren] = useState<ChildItem[]>([])
  const [mergeMode, setMergeMode] = useState<'union' | 'intersect'>('union')
  const [minConfirm, setMinConfirm] = useState(0)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  // 记录鼠标按下时是否落在遮罩上, 避免拖选文本时鼠标移出面板导致误关。
  const mouseDownOnBackdrop = useRef(false)

  // 拉取所有可用子策略(排除 composite 自身)
  const [available, setAvailable] = useState<ScreenerStrategy[]>([])
  const [loadingList, setLoadingList] = useState(false)

  useEffect(() => {
    if (!open) return
    // 重置表单
    setName('')
    setDescription('')
    // 创建模式自动生成 ID(composite_ + 时间戳), 编辑模式用现有 ID
    setStrategyId(isEdit ? (editStrategyId ?? '') : `composite_${Date.now().toString(36)}`)
    setChildren([])
    setMergeMode('union')
    setMinConfirm(0)
    setError('')
    setSearch('')
    setLoadingList(true)
    api.screenerStrategies()
      .then(data => {
        // 排除 composite 策略(不能嵌套)
        setAvailable((data.presets ?? []).filter(s => s.source !== 'composite'))
      })
      .catch(() => setAvailable([]))
      .finally(() => setLoadingList(false))
    // 编辑模式: 加载现有配置回显
    if (isEdit && editStrategyId) {
      api.strategyGet(editStrategyId)
        .then(detail => {
          setName(detail.name)
          setDescription(detail.description)
          setMergeMode((detail.params_defaults?.merge_mode as 'union' | 'intersect') ?? 'union')
          setMinConfirm(detail.params_defaults?.min_confirm ?? 0)
          if (detail.composite_children) {
            setChildren(detail.composite_children.map(c => ({ strategy_id: c.id, weight: c.weight })))
          }
        })
        .catch(() => {})
    }
  }, [open, isEdit, editStrategyId])

  const filteredAvailable = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    const selectedIds = new Set(children.map(c => c.strategy_id))
    return available.filter(s => {
      if (selectedIds.has(s.id)) return false
      if (!keyword) return true
      return s.name.toLowerCase().includes(keyword) || s.id.toLowerCase().includes(keyword)
    })
  }, [available, children, search])

  const addChild = (s: ScreenerStrategy) => {
    setChildren(prev => [...prev, { strategy_id: s.id, weight: 1.0 }])
  }
  const removeChild = (id: string) => {
    setChildren(prev => prev.filter(c => c.strategy_id !== id))
  }
  const updateWeight = (id: string, weight: number) => {
    setChildren(prev => prev.map(c => c.strategy_id === id ? { ...c, weight } : c))
  }

  const totalWeight = useMemo(
    () => children.reduce((sum, c) => sum + (c.weight || 0), 0),
    [children],
  )

  const normalizeWeights = () => {
    if (totalWeight <= 0) return
    setChildren(prev => prev.map(c => ({ ...c, weight: Math.round((c.weight / totalWeight) * 1000) / 1000 })))
  }

  const handleSave = async () => {
    setError('')
    if (!name.trim()) {
      setError('请输入策略名称')
      return
    }
    if (children.length === 0) {
      setError('请至少选择一个子策略')
      return
    }
    setSaving(true)
    try {
      const result = await api.strategySaveComposite({
        strategy_id: isEdit ? (editStrategyId ?? '') : strategyId.trim(),
        name: name.trim(),
        description: description.trim(),
        children: children.map(c => ({ strategy_id: c.strategy_id, weight: c.weight })),
        merge_mode: mergeMode,
        min_confirm: minConfirm,
        mode: isEdit ? 'update' : 'create',
      })
      await onSavedId?.(result.strategy_id)
      setTimeout(() => onClose(), 800)
    } catch (e: any) {
      setError(String(e?.message ?? '保存失败'))
    }
    setSaving(false)
  }

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onMouseDown={(e) => {
            mouseDownOnBackdrop.current = e.target === e.currentTarget
          }}
          onClick={(e) => {
            // 仅当按下和松开都在遮罩上才关闭, 避免面板内拖选文本误关。
            if (mouseDownOnBackdrop.current && e.target === e.currentTarget) onClose()
          }}
        >
          <motion.div
            className="relative flex max-h-[88vh] w-full max-w-3xl flex-col overflow-hidden rounded-panel border border-border bg-base shadow-2xl"
            initial={{ scale: 0.96, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.96, opacity: 0 }}
            onClick={e => e.stopPropagation()}
          >
            {/* 头部 */}
            <div className="flex items-center gap-2 border-b border-border px-4 py-3">
              <Layers className="h-4 w-4 text-teal-400" />
              <span className="text-sm font-semibold text-foreground">
                {isEdit ? '编辑叠加策略' : '创建叠加策略'}
              </span>
              <span className="text-[10px] text-muted/60">
                引用多个子策略, 合并选股与回测信号
              </span>
              <button onClick={onClose} className="ml-auto text-muted hover:text-foreground">
                <X className="h-4 w-4" />
              </button>
            </div>

            {/* 主体 */}
            <div className="flex-1 overflow-y-auto p-4 space-y-4">
              {/* 基本信息 */}
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div>
                  <label className="mb-1 block text-xs text-muted">策略 ID（自动生成）</label>
                  <input
                    value={strategyId}
                    readOnly
                    disabled
                    placeholder="composite_..."
                    className="w-full rounded-btn border border-border bg-elevated px-2 py-1.5 font-mono text-[11px] text-muted cursor-not-allowed"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs text-muted">策略名称</label>
                  <input
                    value={name}
                    onChange={e => setName(e.target.value)}
                    placeholder="我的叠加策略"
                    className="w-full rounded-btn border border-border bg-elevated px-2 py-1.5 text-xs text-foreground placeholder:text-muted/40"
                  />
                </div>
              </div>
              <div>
                <label className="mb-1 block text-xs text-muted">描述（可选）</label>
                <input
                  value={description}
                  onChange={e => setDescription(e.target.value)}
                  placeholder="说明这个叠加策略的意图"
                  className="w-full rounded-btn border border-border bg-elevated px-2 py-1.5 text-xs text-foreground placeholder:text-muted/40"
                />
              </div>

              {/* 合并模式 */}
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div>
                  <label className="mb-1 block text-xs text-muted">合并模式</label>
                  <select
                    value={mergeMode}
                    onChange={e => setMergeMode(e.target.value as 'union' | 'intersect')}
                    className="w-full rounded-btn border border-border bg-elevated px-2 py-1.5 text-xs text-foreground"
                  >
                    <option value="union">并集（任一子策略命中即入选）</option>
                    <option value="intersect">交集（至少 N 个子策略同时命中）</option>
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-xs text-muted">交集最少确认数（0=全部）</label>
                  <input
                    type="number"
                    min={0}
                    value={minConfirm}
                    onChange={e => setMinConfirm(Math.max(0, parseInt(e.target.value) || 0))}
                    disabled={mergeMode !== 'intersect'}
                    className="w-full rounded-btn border border-border bg-elevated px-2 py-1.5 text-xs text-foreground disabled:opacity-50"
                  />
                </div>
              </div>

              {/* 已选子策略 */}
              <div>
                <div className="mb-1.5 flex items-center justify-between">
                  <label className="text-xs font-medium text-foreground">
                    子策略（{children.length}）
                  </label>
                  <span className="text-[10px] text-muted flex items-center gap-1.5">
                    权重总和: {totalWeight.toFixed(2)}
                    {totalWeight > 0 && Math.abs(totalWeight - 1) > 0.001 && (
                      <button onClick={normalizeWeights} className="text-teal-400 hover:text-teal-300 underline underline-offset-2">归一</button>
                    )}
                  </span>
                </div>
                <div className="space-y-1.5">
                  {children.length === 0 && (
                    <div className="rounded-btn border border-dashed border-border px-3 py-4 text-center text-xs text-muted">
                      从下方列表选择子策略
                    </div>
                  )}
                  {children.map(c => {
                    const s = available.find(a => a.id === c.strategy_id)
                    return (
                      <div key={c.strategy_id} className="flex items-center gap-2 rounded-btn border border-border bg-elevated px-2 py-1.5">
                        <span className="flex-1 truncate text-xs text-foreground">{s?.name ?? c.strategy_id}</span>
                        {s?.source && (
                          <span className={`rounded border px-1 text-[8px] ${BADGE_CLS[s.source] ?? ''}`}>
                            {SRC_MAP[s.source] ?? s.source}
                          </span>
                        )}
                        <input
                          type="number"
                          step={0.05}
                          min={0}
                          value={c.weight}
                          onChange={e => updateWeight(c.strategy_id, parseFloat(e.target.value) || 0)}
                          className="w-16 rounded border border-border bg-base px-1.5 py-0.5 text-[11px] text-foreground"
                        />
                        <button onClick={() => removeChild(c.strategy_id)} className="text-danger/60 hover:text-danger">
                          <X className="h-3 w-3" />
                        </button>
                      </div>
                    )
                  })}
                </div>
              </div>

              {/* 可选子策略 */}
              <div>
                <label className="mb-1.5 block text-xs font-medium text-foreground">可选策略</label>
                <div className="relative mb-1.5">
                  <Search className="absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted/50" />
                  <input
                    value={search}
                    onChange={e => setSearch(e.target.value)}
                    placeholder="搜索策略名称或 ID"
                    className="w-full rounded-btn border border-border bg-elevated py-1.5 pl-7 pr-2 text-xs text-foreground placeholder:text-muted/40"
                  />
                </div>
                <div className="max-h-48 space-y-1 overflow-y-auto rounded-btn border border-border bg-elevated p-1.5">
                  {loadingList && (
                    <div className="flex items-center justify-center gap-1.5 py-3 text-xs text-muted">
                      <Loader2 className="h-3 w-3 animate-spin" /> 加载中
                    </div>
                  )}
                  {!loadingList && filteredAvailable.length === 0 && (
                    <div className="py-3 text-center text-xs text-muted">无可用策略</div>
                  )}
                  {filteredAvailable.map(s => (
                    <button
                      key={s.id}
                      onClick={() => addChild(s)}
                      className="flex w-full items-center gap-1.5 rounded px-2 py-1 text-left text-xs text-foreground hover:bg-accent/10"
                    >
                      <Plus className="h-3 w-3 shrink-0 text-teal-400" />
                      <span className="flex-1 truncate">{s.name}</span>
                      {s.source && (
                        <span className={`rounded border px-1 text-[8px] ${BADGE_CLS[s.source] ?? ''}`}>
                          {SRC_MAP[s.source] ?? s.source}
                        </span>
                      )}
                      <span className="font-mono text-[9px] text-muted/50">{s.id}</span>
                    </button>
                  ))}
                </div>
              </div>

              {error && (
                <div className="rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
                  {error}
                </div>
              )}
            </div>

            {/* 底部 */}
            <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
              <button
                onClick={onClose}
                className="rounded-btn border border-border px-3 py-1.5 text-xs text-muted hover:text-foreground"
              >
                取消
              </button>
              <button
                onClick={handleSave}
                disabled={saving}
                className="inline-flex items-center gap-1.5 rounded-btn bg-teal-500/15 px-3 py-1.5 text-xs font-medium text-teal-400 border border-teal-500/30 hover:bg-teal-500/25 disabled:opacity-50"
              >
                {saving && <Loader2 className="h-3 w-3 animate-spin" />}
                {isEdit ? '保存修改' : '创建'}
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
