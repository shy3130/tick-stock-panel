import { useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { ArrowRight, Plus, Search, X } from 'lucide-react'
import type { CustomSignalCondition, CustomSignalFieldGroup } from '@/lib/api'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'

// 多行「字段 + 运算符 + 值」条件编辑器 (受控), 从自定义信号弹窗提取共用:
//   - 自定义信号 (CustomSignalDialog): 完整形态, 含「前N日」偏移
//   - 策略设置·叠加条件 (StrategySettingsDialog): hideDays 隐藏偏移
// 条件语义与后端 custom_signals / overlay_filter 同构: 多行 AND、字符串字段
// 仅 contains/==/!=、数值字段六种比较运算符。
export const OP_LABELS: Record<string, string> = { contains: '包含' }

export interface ConditionEditorOptions {
  fields: { key: string; label: string }[]
  groups?: CustomSignalFieldGroup[]
  stringFields: string[]
  operators?: string[]
  stringOperators?: string[]
  maxDays?: number
  /** 隐藏「前N日」偏移控件 (叠加条件不支持偏移, 始终当日值) */
  hideDays?: boolean
}

interface Props {
  conditions: CustomSignalCondition[]
  onChange: (next: CustomSignalCondition[]) => void
  options: ConditionEditorOptions
  /** 行数上限 (后端约束 8) */
  max?: number
  /** 条件区标题 (默认「条件（多条件为「且」关系）」); 传空串隐藏 */
  title?: string
  /** 标题行右侧追加内容 (如 AI 生成按钮), 与内置「添加条件」并排 */
  headerExtra?: React.ReactNode
  /** 允许删到 0 条 (叠加条件默认即空 = 不叠加; 自定义信号至少 1 条, 保持默认 false) */
  allowEmpty?: boolean
}

export function ConditionEditor({ conditions, onChange, options, max = 8, title, headerExtra, allowEmpty = false }: Props) {
  const {
    fields, groups, stringFields,
    operators = ['>', '>=', '<', '<=', '==', '!='],
    stringOperators = ['contains', '==', '!='],
    maxDays = 60, hideDays = false,
  } = options

  const updateCond = (idx: number, patch: Partial<CustomSignalCondition>) =>
    onChange(conditions.map((c, i) => i === idx ? { ...c, ...patch } : c))
  // 切换左字段时若跨越 数值↔字符串 类型, 运算符/右值语义不再成立, 一并复位
  const changeLeft = (idx: number, left: string) =>
    onChange(conditions.map((c, i) => {
      if (i !== idx) return c
      const wasStr = stringFields.includes(c.left)
      const isStr = stringFields.includes(left)
      if (wasStr === isStr) return { ...c, left }
      return isStr
        ? { ...c, left, op: 'contains', right: '', rightDays: 0 }
        : { ...c, left, op: '>', right: '0', rightDays: 0 }
    }))
  const addCond = () =>
    onChange([...conditions, { left: 'close', op: '>', right: '0', leftDays: 0, rightDays: 0 }])
  const removeCond = (idx: number) => onChange(conditions.filter((_, i) => i !== idx))

  return (
    <div className="space-y-2">
      {title !== '' && (
        <div className="flex items-center justify-between gap-2">
          <span className="text-[11px] text-muted">{title ?? '条件（多条件为「且」关系）'}</span>
          <div className="flex items-center gap-2 shrink-0">
            {headerExtra}
            <button
              onClick={addCond}
              disabled={conditions.length >= max}
              className="inline-flex items-center gap-1 text-[11px] text-accent hover:text-accent/80 cursor-pointer disabled:opacity-40"
            >
              <Plus className="h-3 w-3" />添加条件
            </button>
          </div>
        </div>
      )}
      <div className="space-y-2 rounded-card border border-border/70 bg-base/50 p-3">
        {conditions.length === 0 && (
          <div className="text-[11px] text-muted/70 py-1.5">无条件 — 不叠加过滤</div>
        )}
        {conditions.map((c, i) => {
          const isStr = stringFields.includes(c.left)
          const condOps = isStr ? stringOperators : operators
          return (
            <div key={i} className="flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] text-muted/60 w-5 text-right shrink-0">{i === 0 ? '当' : '且'}</span>

              {/* 左操作数: 前N日 + 字段(弹出选择) */}
              {!hideDays && <DaysInput value={c.leftDays ?? 0} max={maxDays} onChange={v => updateCond(i, { leftDays: v })} />}
              <FieldPicker value={c.left} fields={fields} groups={groups} onChange={v => changeLeft(i, v)} />

              {/* 运算符: 字符串字段为 包含/等于/不等于 */}
              <select value={c.op} onChange={e => updateCond(i, { op: e.target.value })} className="w-11 h-7 px-0.5 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50">
                {condOps.map(op => <option key={op} value={op}>{OP_LABELS[op] ?? op}</option>)}
              </select>

              {/* 右操作数: 字符串字段为文本字面量; 其余为 前N日(仅字段) + 字段/常量(弹出选择) */}
              {isStr ? (
                <input type="text" value={c.right} onChange={e => updateCond(i, { right: e.target.value })}
                  placeholder="概念/行业名, 如 AI" maxLength={64}
                  className="flex-1 min-w-0 h-7 px-1.5 rounded bg-base border border-border text-[11px] text-foreground focus:outline-none focus:border-accent/50" />
              ) : (
                <RightValueInput cond={c} fields={fields} groups={groups} maxDays={maxDays} hideDays={hideDays}
                  onChangeRight={v => updateCond(i, { right: v })}
                  onChangeDays={v => updateCond(i, { rightDays: v })} />
              )}

              {(conditions.length > 1 || allowEmpty) && (
                <button onClick={() => removeCond(i)} aria-label="删除条件" className="p-1 rounded text-muted hover:text-danger hover:bg-danger/10 cursor-pointer">
                  <X className="h-3 w-3" />
                </button>
              )}
            </div>
          )
        })}
      </div>
    </div>
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
  const backdrop = useDialogBackdrop(() => setOpen(false))
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
              {...backdrop}
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

function RightValueInput({ cond, fields, groups, maxDays, hideDays, onChangeRight, onChangeDays }: {
  cond: CustomSignalCondition
  fields: { key: string; label: string }[]
  groups?: CustomSignalFieldGroup[]
  maxDays: number
  hideDays?: boolean
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
          {!hideDays && <DaysInput value={cond.rightDays ?? 0} max={maxDays} onChange={onChangeDays} />}
          <FieldPicker value={fieldValue} fields={fields} groups={groups} onChange={v => onChangeRight(`field:${v}`)} />
          <button onClick={() => onChangeRight('0')} title="切换为数字" className="p-0.5 rounded text-muted hover:text-accent cursor-pointer shrink-0">
            <ArrowRight className="h-3 w-3 rotate-90" />
          </button>
        </>
      ) : (
        <>
          {/* 常量无前N日概念, 占位保持与字段模式对齐 */}
          {!hideDays && <div className="shrink-0" style={{ width: 44 }} />}
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
