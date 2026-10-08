import { useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Save, Sparkles, X } from 'lucide-react'
import { api, type CustomSignal } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'
import { ConditionEditor } from './ConditionEditor'

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
  const backdrop = useDialogBackdrop(onClose)
  const options = useQuery({ queryKey: QK.customSignalsOptions, queryFn: api.customSignalsOptions, enabled: open })

  const [draft, setDraft] = useState<CustomSignal>(() => emptySignal(defaultKind))
  const [error, setError] = useState('')

  // AI 生成条件
  const [aiOpen, setAiOpen] = useState(false)
  const [aiDesc, setAiDesc] = useState('')
  const [aiLoading, setAiLoading] = useState(false)
  const [aiError, setAiError] = useState('')
  const [aiConfigured, setAiConfigured] = useState<boolean | null>(null)
  const checkedAi = useRef(false)

  const fields = options.data?.fields ?? []
  const groups = options.data?.groups
  const maxDays = options.data?.maxDays ?? 60
  const operators = options.data?.operators ?? ['>', '>=', '<', '<=', '==', '!=']
  const stringFields = options.data?.stringFields ?? []
  const stringOperators = options.data?.stringOperators ?? ['contains', '==', '!=']
  const editing = !!signal

  useEffect(() => {
    if (!open) return
    setDraft(signal ? { ...signal, conditions: signal.conditions.map(c => ({ ...c })) } : emptySignal(defaultKind))
    setError('')
    setAiOpen(false); setAiDesc(''); setAiError(''); setAiLoading(false)
  }, [open, signal, defaultKind])

  // 打开时检查一次 AI 是否已配置（复用策略构建器逻辑）
  useEffect(() => {
    if (!open || checkedAi.current) return
    checkedAi.current = true
    api.strategyAiStatus()
      .then(s => setAiConfigured(s.configured))
      .catch(() => setAiConfigured(false))
  }, [open])

  const generateByAI = async () => {
    const desc = aiDesc.trim()
    if (!desc) { setAiError('请先描述信号思路'); return }
    setAiLoading(true)
    setAiError('')
    try {
      const res = await api.customSignalsAiGenerate(desc)
      setDraft(d => ({
        ...d,
        name: d.name.trim() ? d.name : res.name,
        conditions: res.conditions.map(c => ({ ...c, leftDays: c.leftDays ?? 0, rightDays: c.rightDays ?? 0 })),
      }))
    } catch (err: any) {
      setAiError(String(err?.message ?? err))
    } finally {
      setAiLoading(false)
    }
  }

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

  const updateConditions = (conditions: CustomSignal['conditions']) =>
    setDraft(d => ({ ...d, conditions }))

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-[70] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4"
          {...backdrop}
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
                <ConditionEditor
                  conditions={draft.conditions}
                  onChange={updateConditions}
                  options={{ fields, groups, stringFields, operators, stringOperators, maxDays }}
                  headerExtra={
                    <button
                      onClick={() => setAiOpen(o => !o)}
                      className={`inline-flex items-center gap-1 text-[11px] cursor-pointer transition-colors ${aiOpen ? 'text-amber-400' : 'text-amber-400/80 hover:text-amber-400'}`}
                    >
                      <Sparkles className="h-3 w-3" />AI 生成条件
                    </button>
                  }
                />
                {aiOpen && (
                  <div className="rounded-card border border-amber-400/30 bg-amber-400/5 p-3 space-y-2">
                    <div className="flex items-center gap-2">
                      <Sparkles className="h-3.5 w-3.5 text-amber-400 shrink-0" />
                      <span className="text-[11px] text-amber-300">描述信号思路，AI 将生成条件组合</span>
                    </div>
                    {aiConfigured === false ? (
                      <div className="text-xs text-amber-400/80">
                        AI 未配置，无法生成信号。{' '}
                        <a href="/settings?tab=ai" className="underline hover:text-amber-300">去设置页配置 API Key</a>
                      </div>
                    ) : (
                      <>
                        <textarea
                          value={aiDesc}
                          onChange={e => setAiDesc(e.target.value)}
                          placeholder="例如：收盘价回踩20日均线，且量比≥2 放量"
                          rows={2}
                          className="w-full rounded-btn border border-border bg-base px-3 py-2 text-xs text-foreground focus:outline-none focus:border-amber-400/50 resize-none"
                        />
                        {aiError && <div className="text-xs text-danger">{aiError}</div>}
                        <div className="flex justify-end">
                          <button
                            onClick={generateByAI}
                            disabled={aiLoading}
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn bg-amber-500/90 text-white text-xs font-medium disabled:opacity-50 cursor-pointer"
                          >
                            {aiLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                            {aiLoading ? '生成中…' : '生成条件'}
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                )}
                <p className="text-[10px] text-muted/60 px-1">
                  每个操作数左侧的 <span className="text-foreground/70">最新</span> 按钮可点击切换为「前N日」(取 N 个交易日前的值)。例:收盘价(最新) &gt; 收盘价(前1日) = 上涨。带偏移的条件仅盘后/回测生效, 盘中实时跳过。
                </p>
              </div>

              {error && <div className="rounded-btn border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">{error}</div>}
            </div>

            <div className="flex justify-end gap-2 border-t border-border/50 px-5 py-4">
              <button onClick={onClose} className="px-4 py-1.5 rounded-btn bg-elevated text-secondary text-xs">取消</button>
              <button onClick={() => save.mutate()} disabled={save.isPending} className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-btn bg-amber-500/90 text-white text-xs font-medium disabled:opacity-50">
                <Save className="h-3.5 w-3.5" />保存
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
