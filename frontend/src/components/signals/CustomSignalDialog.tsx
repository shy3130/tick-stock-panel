import { useEffect, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, Bot, Loader2, Plus, Save, X } from 'lucide-react'
import { AiExecutionMetaBadge } from '@/components/AiExecutionMetaBadge'
import { AiProviderSelector } from '@/components/AiProviderSelector'
import { api, type CustomSignal, type CustomSignalCondition, type AiExecutionMeta } from '@/lib/api'
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
  conditions: [{ left: 'close', op: '>', right: 'field:ma20' }],
})

export function CustomSignalDialog({ open, signal, defaultKind = 'exit', onClose, onSaved }: Props) {
  const qc = useQueryClient()
  const options = useQuery({ queryKey: QK.customSignalsOptions, queryFn: api.customSignalsOptions, enabled: open })

  const [aiText, setAiText] = useState('')
  const [aiProfileId, setAiProfileId] = useState<string>()
  const [aiMeta, setAiMeta] = useState<AiExecutionMeta | null>(null)
  const [aiError, setAiError] = useState('')
  const [draft, setDraft] = useState<CustomSignal>(() => emptySignal(defaultKind))
  const [error, setError] = useState('')

  const fields = options.data?.fields ?? []
  const operators = options.data?.operators ?? ['>', '>=', '<', '<=', '==', '!=']
  const editing = !!signal

  useEffect(() => {
    if (!open) return
    setDraft(signal ? { ...signal, conditions: signal.conditions.map(c => ({ ...c })) } : emptySignal(defaultKind))
    setAiText('')
    setAiMeta(null)
    setAiError('')
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
    onError: err => setError(err instanceof Error ? err.message : String(err)),
  })
  const aiDraft = useMutation({
    mutationFn: () => {
      const text = aiText.trim()
      if (!text || text.length > 500) throw new Error('请输入 1-500 字的自然语言描述')
      return api.customSignalAiDraft(text, aiProfileId)
    },
    onSuccess: res => {
      setDraft(d => ({ ...d, ...res.draft, conditions: res.draft.conditions.map(c => ({ ...c })) }))
      setAiMeta(res.ai_meta ?? null)
      setAiError('')
    },
    onError: err => setAiError(err instanceof Error ? err.message : String(err)),
  })


  const updateCond = (idx: number, patch: Partial<CustomSignalCondition>) => {
    setDraft(d => ({ ...d, conditions: d.conditions.map((c, i) => i === idx ? { ...c, ...patch } : c) }))
  }
  const addCond = () => setDraft(d => ({ ...d, conditions: [...d.conditions, { left: 'close', op: '>', right: '0' }] }))
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
              {!editing && (
                <section className="rounded-card border border-accent/30 bg-accent/5 p-3 space-y-2">
                  <div className="flex items-center justify-between gap-2">
                    <div>
                      <div className="text-xs font-medium text-foreground">AI 辅助生成草稿</div>
                      <div className="text-[11px] text-accent font-medium">AI 仅生成草稿，核对后点击保存才会写入</div>
                    </div>
                    <AiProviderSelector entry="custom_signal_draft" value={aiProfileId} onChange={setAiProfileId} compact />
                  </div>
                  <textarea
                    value={aiText}
                    maxLength={500}
                    onChange={e => setAiText(e.target.value)}
                    placeholder="例如：收盘价低于 MA20 时作为卖出信号"
                    className="min-h-16 w-full rounded-btn border border-border bg-base px-3 py-2 text-xs text-foreground"
                  />
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[10px] text-muted">{aiText.length}/500</span>
                    <button
                      onClick={() => aiDraft.mutate()}
                      disabled={aiDraft.isPending || !aiText.trim()}
                      className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn bg-accent text-base text-xs font-medium disabled:opacity-50"
                    >
                      {aiDraft.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Bot className="h-3.5 w-3.5" />}
                      生成草稿
                    </button>
                  </div>
                  {aiError && <div className="rounded-btn border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">{aiError}</div>}
                  {aiMeta && <AiExecutionMetaBadge meta={aiMeta} />}
                </section>
              )}
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
                    <option value="entry">买入</option>
                    <option value="exit">卖出</option>
                    <option value="both">买卖通用</option>
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
                    <div key={i} className="flex items-center gap-1.5">
                      <span className="text-[10px] text-muted/60 w-6 text-right shrink-0">{i === 0 ? '当' : '且'}</span>
                      <select value={c.left} onChange={e => updateCond(i, { left: e.target.value })} className="w-32 h-7 px-1.5 rounded bg-base border border-border text-[11px] text-foreground focus:outline-none focus:border-accent/50">
                        {fields.map(f => <option key={f.key} value={f.key}>{f.label}</option>)}
                      </select>
                      <select value={c.op} onChange={e => updateCond(i, { op: e.target.value })} className="w-12 h-7 px-1 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50">
                        {operators.map(op => <option key={op} value={op}>{op}</option>)}
                      </select>
                      <RightValueInput cond={c} fields={fields} onChange={v => updateCond(i, { right: v })} />
                      {draft.conditions.length > 1 && (
                        <button onClick={() => removeCond(i)} className="p-1 rounded text-muted hover:text-danger hover:bg-danger/10 cursor-pointer">
                          <X className="h-3 w-3" />
                        </button>
                      )}
                    </div>
                  ))}
                </div>
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

function RightValueInput({ cond, fields, onChange }: { cond: CustomSignalCondition; fields: { key: string; label: string }[]; onChange: (v: string) => void }) {
  const isField = cond.right.startsWith('field:')
  const fieldValue = isField ? cond.right.slice(6) : ''
  const numValue = isField ? '' : cond.right

  return (
    <div className="flex items-center gap-1">
      {isField ? (
        <>
          <select value={fieldValue} onChange={e => onChange(`field:${e.target.value}`)} className="w-32 h-7 px-1.5 rounded bg-base border border-border text-[11px] text-foreground focus:outline-none focus:border-accent/50">
            {fields.map(f => <option key={f.key} value={f.key}>{f.label}</option>)}
          </select>
          <button onClick={() => onChange('0')} title="切换为数字" className="p-0.5 rounded text-muted hover:text-accent cursor-pointer">
            <ArrowRight className="h-3 w-3 rotate-90" />
          </button>
        </>
      ) : (
        <>
          <input type="number" value={numValue} onChange={e => onChange(e.target.value)} step="any" className="w-24 h-7 px-1.5 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50" />
          <button onClick={() => onChange('field:close')} title="切换为字段" className="p-0.5 rounded text-muted hover:text-accent cursor-pointer">
            <ArrowRight className="h-3 w-3 -rotate-90" />
          </button>
        </>
      )}
    </div>
  )
}
