import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { X, Loader2, Upload, Database, Plus, AlertCircle, Lock } from 'lucide-react'
import { api, type ExtDataConfig, type ExtDataField } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'
import { SectionHead, INPUT_CLS } from './dialogShared'

export function EditExtDialog({ config, onClose }: { config: ExtDataConfig; onClose: () => void }) {
  const qc = useQueryClient()
  const backdrop = useDialogBackdrop(onClose)
  const [label, setLabel] = useState(config.label)
  const [description, setDescription] = useState(config.description ?? '')
  const [fields, setFields] = useState<ExtDataField[]>([...config.fields])
  const [error, setError] = useState('')
  const detectFileRef = useRef<HTMLInputElement>(null)
  const [detecting, setDetecting] = useState(false)
  const [symbolMapping, setSymbolMapping] = useState<{ candidates: string[]; file: File } | null>(null)

  const update = useMutation({
    mutationFn: () =>
      api.extDataUpdate(config.id, {
        label: label.trim(),
        fields: fields.filter((f) => f.name.trim()),
        description: description.trim() || '',
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.extData })
      onClose()
    },
    onError: (err) => setError(String(err)),
  })

  const addField = () =>
    setFields([...fields, { name: '', dtype: 'string', label: '' }])

  const removeField = (i: number) =>
    setFields(fields.filter((_, idx) => idx !== i))

  const updateField = (i: number, key: keyof ExtDataField, val: string) =>
    setFields(fields.map((f, idx) => (idx === i ? { ...f, [key]: val } : f)))

  const valid = label.trim() && fields.some((f) => f.name.trim())

  const applyDetectedFields = (detected: { name: string; dtype: string; label: string }[]) => {
    const rest = detected.filter(f => f.name !== 'symbol')
    setFields([
      { name: 'symbol', dtype: 'string', label: '标的代码' },
      ...rest,
    ])
  }

  const handleDetectFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    e.target.value = ''
    setDetecting(true); setError(''); setSymbolMapping(null)
    api.extDataDetectFields(file)
      .then((res) => {
        const hasSym = res.symbol_candidates.length > 0
        const hasCode = res.code_candidates.length > 0
        if (hasSym || hasCode) {
          applyDetectedFields(res.fields)
        } else {
          const candidates = res.fields.map(f => f.name)
          setSymbolMapping({ candidates, file })
        }
      })
      .catch((err) => setError(String(err)))
      .finally(() => setDetecting(false))
  }

  const handleSymbolMap = (_col: string) => {
    if (!symbolMapping) return
    setDetecting(true); setError('')
    const rest = fields.filter(f => f.name !== 'symbol')
    setFields([{ name: 'symbol', dtype: 'string', label: '标的代码' }, ...rest])
    setSymbolMapping(null)
    setDetecting(false)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" {...backdrop} />
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 12 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.97, y: 8 }}
        transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
        className="relative rounded-2xl border border-border bg-surface shadow-2xl mx-4 w-full max-w-2xl max-h-[85vh] flex flex-col overflow-hidden"
      >
        <div className="px-6 pt-5 pb-4 flex items-start justify-between gap-4">
          <div className="flex items-start gap-3 min-w-0">
            <div className="mt-0.5 p-2 rounded-xl bg-amber-500/10 text-amber-500 border border-amber-500/20 shrink-0">
              <Database className="h-4 w-4" />
            </div>
            <div className="min-w-0">
              <h3 className="text-[15px] leading-6 font-semibold text-foreground">编辑扩展数据</h3>
              <div className="flex items-center gap-1.5 mt-0.5">
                <span className="text-[11px] font-mono text-muted">{config.id}</span>
                <span className={`inline-flex px-1.5 py-px rounded text-[10px] border ${
                  config.mode === 'snapshot'
                    ? 'bg-blue-500/10 border-blue-500/25 text-blue-400'
                    : 'bg-amber-500/10 border-amber-500/25 text-amber-500'
                }`}>
                  {config.mode === 'snapshot' ? '快照型' : '时序型'}
                </span>
              </div>
            </div>
          </div>
          <button
            onClick={onClose}
            aria-label="关闭"
            className="p-1.5 rounded-lg text-secondary hover:text-foreground hover:bg-elevated focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors shrink-0"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 pb-6 space-y-6">
          {/* 01 基本信息 */}
          <section>
            <SectionHead num="01" title="基本信息" />
            <div className="space-y-3">
              <div>
                <div className="text-[11px] font-medium text-secondary mb-1.5">显示名称</div>
                <input
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  placeholder={config.label}
                  className={INPUT_CLS}
                />
              </div>
              <div>
                <div className="text-[11px] font-medium text-secondary mb-1.5">描述 <span className="text-muted font-normal">可选</span></div>
                <input
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="一句话说明数据用途，展示在表卡片上"
                  className={INPUT_CLS}
                />
              </div>
            </div>
          </section>

          {/* 02 字段定义 */}
          <section>
            <SectionHead num="02" title="字段定义">
              <div className="flex items-center gap-1.5">
                <input
                  ref={detectFileRef}
                  type="file"
                  accept=".csv,.xlsx,.xls"
                  className="hidden"
                  onChange={handleDetectFile}
                />
                <button
                  onClick={() => detectFileRef.current?.click()}
                  disabled={detecting}
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] text-muted hover:text-accent hover:bg-accent/[0.06] disabled:opacity-40 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
                >
                  {detecting ? <Loader2 className="h-3 w-3 animate-spin" /> : <Upload className="h-3 w-3" />}
                  从文件导入
                </button>
                <button
                  onClick={addField}
                  className="inline-flex items-center gap-0.5 px-2 py-0.5 rounded-md text-[10px] text-muted hover:text-accent hover:bg-accent/[0.06] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
                >
                  <Plus className="h-3 w-3" />
                  添加字段
                </button>
              </div>
            </SectionHead>

            {symbolMapping && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                className="mb-2 rounded-lg border border-amber-500/30 bg-amber-500/[0.06] px-3 py-2.5 space-y-2"
              >
                <div className="text-[10px] text-amber-500 font-medium">未找到 symbol 列，请选择哪一列作为标的代码</div>
                <div className="flex flex-wrap gap-1.5">
                  {symbolMapping.candidates.map(col => (
                    <button
                      key={col}
                      onClick={() => handleSymbolMap(col)}
                      className="px-2.5 py-1 rounded-md bg-accent/10 text-accent text-[10px] font-medium hover:bg-accent/20 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
                    >
                      {col}
                    </button>
                  ))}
                  <button
                    onClick={() => setSymbolMapping(null)}
                    className="px-2.5 py-1 rounded-md bg-elevated text-muted text-[10px] hover:text-secondary focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
                  >
                    取消
                  </button>
                </div>
              </motion.div>
            )}

            <div className="space-y-1">
              {fields.map((f, i) => {
                const isBuiltin = f.name === 'symbol' || f.name === 'name'
                return (
                  <div key={i} className="flex items-center gap-2 group">
                    <input
                      value={f.label}
                      onChange={(e) => updateField(i, 'label', e.target.value)}
                      placeholder="显示名"
                      disabled={isBuiltin}
                      className={`w-24 h-8 px-2.5 rounded-lg border text-[11px] placeholder:text-muted/40 focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 transition-shadow ${
                        isBuiltin
                          ? 'bg-elevated/50 border-border/60 text-muted cursor-not-allowed'
                          : 'bg-base border-border text-foreground'
                      }`}
                    />
                    <div className="relative flex-1 min-w-0">
                      <input
                        value={f.name}
                        onChange={(e) => updateField(i, 'name', e.target.value)}
                        placeholder="字段名（英文）"
                        disabled={isBuiltin}
                        className={`w-full h-8 px-2.5 rounded-lg border text-[11px] font-mono placeholder:text-muted/40 focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 transition-shadow ${
                          isBuiltin
                            ? 'bg-elevated/50 border-border/60 text-muted cursor-not-allowed pr-7'
                            : 'bg-base border-border text-foreground pr-2.5'
                        }`}
                      />
                      {isBuiltin && (
                        <Lock className="absolute right-2 top-1/2 -translate-y-1/2 h-3 w-3 text-muted/40 pointer-events-none" />
                      )}
                    </div>
                    <select
                      value={f.dtype}
                      onChange={(e) => updateField(i, 'dtype', e.target.value)}
                      disabled={isBuiltin}
                      className={`h-8 px-2 rounded-lg border border-border text-[11px] focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 ${
                        isBuiltin ? 'bg-elevated/50 text-muted cursor-not-allowed' : 'bg-base text-foreground'
                      }`}
                    >
                      <option value="string">文本</option>
                      <option value="int">整数</option>
                      <option value="float">小数</option>
                      <option value="bool">布尔</option>
                    </select>
                    {!isBuiltin && fields.length > 3 ? (
                      <button
                        onClick={() => removeField(i)}
                        aria-label="删除字段"
                        className="p-1.5 rounded-md text-muted/40 hover:text-danger hover:bg-danger/10 opacity-0 group-hover:opacity-100 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-danger/40 transition-all"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    ) : (
                      <div className="w-[30px] shrink-0" />
                    )}
                  </div>
                )
              })}
            </div>
          </section>

          {error && (
            <div className="flex items-start gap-2 text-[11px] text-danger bg-danger/5 border border-danger/20 rounded-lg px-3 py-2.5">
              <AlertCircle className="h-3.5 w-3.5 mt-px shrink-0" />
              <span className="leading-relaxed">{error}</span>
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 px-6 py-4 border-t border-border/50 bg-elevated/30">
          <button
            onClick={onClose}
            className="px-4 py-2 rounded-lg bg-elevated text-secondary text-xs hover:bg-elevated/70 hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
          >
            取消
          </button>
          <button
            onClick={() => update.mutate()}
            disabled={!valid || update.isPending}
            className="px-5 py-2 inline-flex items-center gap-1.5 rounded-lg bg-accent text-white text-xs font-medium hover:bg-accent/90 active:translate-y-px disabled:opacity-40 disabled:pointer-events-none focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-all shadow-sm shadow-accent/20"
          >
            {update.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            {update.isPending ? '保存中…' : '保存'}
          </button>
        </div>
      </motion.div>
    </div>
  )
}
