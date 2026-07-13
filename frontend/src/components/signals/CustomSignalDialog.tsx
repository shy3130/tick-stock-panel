import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, Plus, Save, Search, X } from 'lucide-react'
import { api, type CustomSignal, type CustomSignalCondition, type CustomSignalFieldGroup } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

interface Props {
  open: boolean
  signal?: CustomSignal | null
  defaultKind?: CustomSignal['kind']
  onClose: () => void
  onSaved?: (signal: CustomSignal) => void
}

const emptySignal = (kind: CustomSignal['kind'] = 'exit'): CustomSignal => ({
  id: '', name: '', kind, enabled: true,
  conditions: [{ left: 'close', op: '>', right: 'field:ma20', leftDays: 0, rightDays: 0 }],
})

export function CustomSignalDialog({ open, signal, defaultKind = 'exit', onClose, onSaved }: Props) {
  const qc = useQueryClient()
  const options = useQuery({ queryKey: QK.customSignalsOptions, queryFn: api.customSignalsOptions, enabled: open })

  const [draft, setDraft] = useState<CustomSignal>(() => emptySignal(defaultKind))
  const [error, setError] = useState('')

  const fields = options.data?.fields ?? []
  const groups = options.data?.groups
  const maxDays = options.data?.maxDays ?? 60
  const operators = options.data?.operators ?? ['>', '>=', '<', '<=', '==', '!=']
  const editing = !!signal

  useEffect(() => {
    if (!open) return
    setDraft(signal ? { ...signal, conditions: signal.conditions.map(c => ({ ...c })) } : emptySignal(defaultKind))
    setError('')
  }, [open, signal, defaultKind])

  const save = useMutation({
    mutationFn: () => {
      const d = draft
      if (!d.id.trim()) throw new Error('请输入信号标识')
      if (!/^[a-z0-9_]{1,40}$/.test(d.id)) throw new Error('标识仅允许小写字母、数字、下划线（1-40字符）')
      if (!d.name.trim()) throw new Error('请输入信号名称')
      if (d.conditions.length === 0) throw new Error('至少需要一个条件')
      for (const c of d.conditions) {
        if (!c.left || !c.op || c.right === '') throw new Error('条件填写不完整')
      }
      return api.customSignalSave(d)
    },
    onSuccess: res => {
      qc.invalidateQueries({ queryKey: QK.customSignals })
      onSaved?.(res.signal)
      onClose()
    },
    onError: err => setError(String((err as any)?.message ?? err)),
  })

  const updateCond = (idx: number, patch: Partial<CustomSignalCondition>) =>
    setDraft(d => ({ ...d, conditions: d.conditions.map((c, i) => i === idx ? { ...c, ...patch } : c) }))
  const addCond = () => setDraft(d => ({ ...d, conditions: [...d.conditions, { left: 'close', op: '>', right: '0', leftDays: 0, rightDays: 0 }] }))
  const removeCond = (idx: number) => setDraft(d => ({ ...d, conditions: d.conditions.filter((_, i) => i !== idx) }))

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-[70] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4"
          onClick={onClose}
        >
          <motion.div
            role="dialog"
            aria-modal="true"
            initial={{ opacity: 0, scale: 0.95, y: 10 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.95, y: 10 }}
            transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
            className="w-full max-w-3xl max-h-[88vh] bg-surface/95 backdrop-blur-xl border border-border/50 rounded-2xl shadow-2xl flex flex-col overflow-hidden"
            onClick={e => e.stopPropagation()}
          >
            <div className="flex items-center justify-between gap-3 border-b border-border/50 px-5 py-4">
              <div>
                <h3 className="text-sm font-semibold text-foreground">{editing ? '编辑自定义信号' : '新建自定义信号'}</h3>
                <p className="mt-1 text-[11px] text-muted">标识保存后不可修改，如需更换请新建。自定义信号保存为 csg_* 列。</p>
              </div>
              <button onClick={onClose} className="rounded-lg p-1.5 text-muted transition-colors hover:bg-elevated hover:text-foreground">
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="flex-1 overflow-y-auto px-5 py-5 space-y-5">
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                <label className="space-y-1.5">
                  <span className="text-[11px] text-muted">信号标识</span>
                  <input
                    value={draft.id}
                    disabled={editing}
                    onChange={e => setDraft(d => ({ ...d, id: e.target.value.replace(/[^a-z0-9_]/g, '') }))}
                    placeholder="如 low_touches_ma5"
                    className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground disabled:opacity-60"
                  />
                </label>
                <label className="space-y-1.5">
                  <span className="text-[11px] text-muted">信号名称</span>
                  <input value={draft.name} onChange={e => setDraft(d => ({ ...d, name: e.target.value }))} placeholder="如 跌至MA5" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
                </label>
                <label className="space-y-1.5">
                  <span className="text-[11px] text-muted">类型</span>
                  <select value={draft.kind} onChange={e => setDraft(d => ({ ...d, kind: e.target.value as CustomSignal['kind'] }))} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground">
                    <option value="entry">入场</option>
                    <option value="exit">出场</option>
                    <option value="both">出入通用</option>
                  </select>
                </label>
              </div>

              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] text-muted">条件（多条件为「且」关系）</span>
                  <button onClick={addCond} className="inline-flex items-center gap-1 text-[11px] text-accent hover:text-accent/80 cursor-pointer">
                    <Plus className="h-3 w-3" />添加条件
                  </button>
                </div>
                <div className="space-y-2 rounded-card border border-border/70 bg-base/50 p-3">
                  {draft.conditions.map((c, i) => (
                    <div key={i} className="flex flex-wrap items-center gap-1.5">
                      <span className="text-[10px] text-muted/60 w-5 text-right shrink-0">{i === 0 ? '当' : '且'}</span>

                      {/* 左操作数: 前N日 + 字段(弹出选择) */}
                      <DaysInput value={c.leftDays ?? 0} max={maxDays} onChange={v => updateCond(i, { leftDays: v })} />
                      <FieldPicker value={c.left} fields={fields} groups={groups} onChange={v => updateCond(i, { left: v })} />

                      {/* 运算符 */}
                      <select value={c.op} onChange={e => updateCond(i, { op: e.target.value })} className="w-11 h-7 px-0.5 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50">
                        {operators.map(op => <option key={op} value={op}>{op}</option>)}
                      </select>

                      {/* 右操作数: 前N日(仅字段) + 字段/常量(弹出选择) */}
                      <RightValueInput cond={c} fields={fields} groups={groups} maxDays={maxDays}
                        onChangeRight={v => updateCond(i, { right: v })}
                        onChangeDays={v => updateCond(i, { rightDays: v })} />

                      {draft.conditions.length > 1 && (
                        <button onClick={() => removeCond(i)} className="p-1 rounded text-muted hover:text-danger hover:bg-danger/10 cursor-pointer">
                          <X className="h-3 w-3" />
                        </button>
                      )}
                    </div>
                  ))}
                </div>
                <p className="text-[10px] text-muted/60 px-1">
                  每个操作数左侧的 <span className="text-foreground/70">最新</span> 按钮可点击切换为「前N日」(取 N 个交易日前的值)。例:收盘价(最新) &gt; 收盘价(前1日) = 上涨。带偏移的条件仅盘后/回测生效, 盘中实时跳过。
                </p>
              </div>

              {error && <div className="rounded-btn border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">{error}</div>}
            </div>

            <div className="flex justify-end gap-2 border-t border-border/50 px-5 py-4">
              <button onClick={onClose} className="px-4 py-1.5 rounded-btn bg-elevated text-secondary text-xs">取消</button>
              <button onClick={() => save.mutate()} disabled={save.isPending} className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-btn bg-amber-500/90 text-base text-xs font-medium disabled:opacity-50">
                <Save className="h-3.5 w-3.5" />保存
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

// ── 字段选择器: 搜索 + 分组居中对话框 ───────────────────

function FieldPicker({ value, fields, groups, onChange }: {
  value: string
  fields: { key: string; label: string }[]
  groups?: CustomSignalFieldGroup[]
  onChange: (v: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const selectedLabel = fields.find(f => f.key === value)?.label ?? value

  const filteredGroups = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q || !groups) return groups
    return groups
      .map(g => ({ ...g, fields: g.fields.filter(f => f.label.toLowerCase().includes(q) || f.key.toLowerCase().includes(q)) }))
      .filter(g => g.fields.length > 0)
  }, [groups, query])

  const filteredFields = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q || filteredGroups) return fields
    return fields.filter(f => f.label.toLowerCase().includes(q) || f.key.toLowerCase().includes(q))
  }, [fields, query, filteredGroups])

  return (
    <>
      <button
        type="button"
        onClick={() => { setQuery(''); setOpen(true) }}
        className="min-w-[80px] max-w-[180px] h-7 px-1.5 rounded bg-base border border-border text-[11px] text-foreground text-left hover:border-accent/40 transition-colors cursor-pointer truncate"
      >
        {selectedLabel}
      </button>
      {createPortal(
        <AnimatePresence>
          {open && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/50 backdrop-blur-sm p-4"
              onClick={() => setOpen(false)}
            >
              <motion.div
                initial={{ opacity: 0, scale: 0.95, y: 10 }}
                animate={{ opacity: 1, scale: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.95, y: 10 }}
                transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
                className="w-full max-w-sm bg-surface border border-border/50 rounded-2xl shadow-2xl flex flex-col overflow-hidden max-h-[70vh]"
                onClick={e => e.stopPropagation()}
              >
                {/* 标题 + 搜索 */}
                <div className="p-3 border-b border-border/50 space-y-2.5">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-medium text-foreground">选择字段</span>
                    <button onClick={() => setOpen(false)} className="rounded p-1 text-muted hover:bg-elevated hover:text-foreground transition-colors">
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                  <div className="flex items-center gap-2 px-2.5 h-8 rounded-btn bg-base border border-border">
                    <Search className="h-3.5 w-3.5 text-muted shrink-0" />
                    <input
                      autoFocus
                      value={query}
                      onChange={e => setQuery(e.target.value)}
                      placeholder="搜索字段…"
                      className="flex-1 bg-transparent text-xs text-foreground focus:outline-none"
                    />
                    {query && <button onClick={() => setQuery('')} className="text-muted hover:text-foreground"><X className="h-3 w-3" /></button>}
                  </div>
                </div>
                {/* 分组列表 */}
                <div className="flex-1 overflow-y-auto p-2">
                  {filteredGroups ? (
                    filteredGroups.length > 0 ? filteredGroups.map(g => (
                      <div key={g.key} className="mb-1">
                        <div className="px-2 py-1 text-[10px] text-muted/60 font-medium">{g.label}</div>
                        {g.fields.map(f => (
                          <button
                            key={f.key}
                            onClick={() => { onChange(f.key); setOpen(false) }}
                            className={`w-full text-left px-2.5 py-1.5 rounded text-xs transition-colors ${
                              f.key === value ? 'bg-accent/10 text-accent' : 'text-foreground/80 hover:bg-elevated'
                            }`}
                          >
                            {f.label}
                          </button>
                        ))}
                      </div>
                    )) : (
                      <div className="px-3 py-8 text-center text-xs text-muted">无匹配字段</div>
                    )
                  ) : (
                    filteredFields.map(f => (
                      <button
                        key={f.key}
                        onClick={() => { onChange(f.key); setOpen(false) }}
                        className={`w-full text-left px-2.5 py-1.5 rounded text-xs transition-colors ${
                          f.key === value ? 'bg-accent/10 text-accent' : 'text-foreground/80 hover:bg-elevated'
                        }`}
                      >
                        {f.label}
                      </button>
                    ))
                  )}
                </div>
              </motion.div>
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </>
  )
}

// ── 日期偏移控件: "最新" / "前N日" ───────────────────────

function DaysInput({ value, max, onChange }: { value: number; max: number; onChange: (v: number) => void }) {
  if (!value) {
    return (
      <button
        type="button"
        onClick={() => onChange(1)}
        title="点击切换为「前N日」(取 N 个交易日前的值)"
        className="h-7 px-2 rounded bg-base border border-border text-[11px] text-muted hover:text-accent hover:border-accent/50 transition-colors shrink-0 cursor-pointer"
      >
        最新
      </button>
    )
  }
  return (
    <div className="flex items-center h-7 rounded bg-base border border-border focus-within:border-accent/50 transition-colors shrink-0">
      <span className="pl-1.5 text-[11px] text-muted select-none">前</span>
      <input
        type="number"
        min={1}
        max={max}
        value={value}
        onChange={e => {
          const raw = e.target.value
          if (raw === '') { onChange(0); return }
          const n = Math.max(1, Math.min(max, parseInt(raw) || 1))
          onChange(n)
        }}
        title={`前 ${value} 个交易日的值`}
        className="w-7 h-full px-0 text-[11px] font-mono text-foreground text-center bg-transparent focus:outline-none [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none"
      />
      <button
        type="button"
        onClick={() => onChange(0)}
        title="切回「最新」"
        className="pr-1.5 pl-0.5 text-[11px] text-muted hover:text-accent transition-colors cursor-pointer"
      >
        日
      </button>
    </div>
  )
}

// ── 右操作数: 字段(弹出) / 常量 切换 ─────────────────────

function RightValueInput({ cond, fields, groups, maxDays, onChangeRight, onChangeDays }: {
  cond: CustomSignalCondition
  fields: { key: string; label: string }[]
  groups?: CustomSignalFieldGroup[]
  maxDays: number
  onChangeRight: (v: string) => void
  onChangeDays: (v: number) => void
}) {
  const isField = cond.right.startsWith('field:')
  const fieldValue = isField ? cond.right.slice(6) : ''
  const numValue = isField ? '' : cond.right

  return (
    <div className="flex items-center gap-1 flex-1 min-w-0">
      {isField ? (
        <>
          <DaysInput value={cond.rightDays ?? 0} max={maxDays} onChange={onChangeDays} />
          <FieldPicker value={fieldValue} fields={fields} groups={groups} onChange={v => onChangeRight(`field:${v}`)} />
          <button onClick={() => onChangeRight('0')} title="切换为数字" className="p-0.5 rounded text-muted hover:text-accent cursor-pointer shrink-0">
            <ArrowRight className="h-3 w-3 rotate-90" />
          </button>
        </>
      ) : (
        <>
          {/* 常量无前N日概念, 占位保持与字段模式对齐 */}
          <div className="shrink-0" style={{ width: 44 }} />
          <input type="number" value={numValue} onChange={e => onChangeRight(e.target.value)} step="any"
            className="flex-1 min-w-0 h-7 px-1.5 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50" />
          <button onClick={() => onChangeRight('field:close')} title="切换为字段" className="p-0.5 rounded text-muted hover:text-accent cursor-pointer shrink-0">
            <ArrowRight className="h-3 w-3 -rotate-90" />
          </button>
        </>
      )}
    </div>
  )
}
