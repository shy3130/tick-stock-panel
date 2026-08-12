import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { X, Loader2, Upload } from 'lucide-react'
import { api, type ExtDataConfig, type ExtDataField } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'

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
        className="relative rounded-2xl border border-border bg-surface shadow-2xl mx-4 w-full max-w-lg max-h-[85vh] flex flex-col overflow-hidden"
      >
        <div className="flex items-center justify-between px-5 py-3 border-b border-border">
          <h3 className="text-sm font-medium text-foreground">编辑扩展数据</h3>
          <button onClick={onClose} className="p-0.5 rounded hover:bg-elevated text-secondary">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          <div className="grid grid-cols-[1fr_1fr] gap-3">
            <div>
              <label className="text-[10px] text-muted mb-1 block">标识符</label>
              <div className="h-8 px-3 rounded-btn bg-elevated/50 border border-border text-xs text-muted font-mono flex items-center">
                {config.id}
              </div>
            </div>
            <div>
              <label className="text-[10px] text-muted mb-1 block">数据类型</label>
              <div className={`h-8 px-3 rounded-btn border text-xs flex items-center ${
                config.mode === 'snapshot'
                  ? 'bg-blue-500/10 border-blue-500/30 text-blue-400'
                  : 'bg-amber-500/10 border-amber-500/30 text-amber-400'
              }`}>
                {config.mode === 'snapshot' ? '快照型' : '时序型'}
              </div>
            </div>
          </div>

          <div>
            <label className="text-[10px] text-muted mb-1 block">显示名称</label>
            <input
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              className="w-full h-8 px-3 rounded-btn bg-base border border-border text-xs text-foreground placeholder:text-muted/40 focus:outline-none focus:border-accent/50"
            />
          </div>

          <div>
            <label className="text-[10px] text-muted mb-1 block">描述</label>
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="可选，简要说明数据的用途"
              className="w-full h-8 px-3 rounded-btn bg-base border border-border text-xs text-foreground placeholder:text-muted/40 focus:outline-none focus:border-accent/50"
            />
          </div>

          <div>
            <div className="flex items-center justify-between mb-1.5">
              <label className="text-[10px] text-muted">字段定义</label>
              <div className="flex items-center gap-2">
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
                  className="text-[10px] text-accent/80 hover:text-accent inline-flex items-center gap-0.5 disabled:opacity-40"
                >
                  {detecting ? <Loader2 className="h-3 w-3 animate-spin" /> : <Upload className="h-3 w-3" />}
                  从文件导入
                </button>
                <button onClick={addField} className="text-[10px] text-accent hover:text-accent/80">+ 添加字段</button>
              </div>
            </div>
            {symbolMapping && (
              <div className="mb-2 rounded-md border border-amber-500/30 bg-amber-500/[0.06] px-3 py-2 space-y-1.5">
                <div className="text-[10px] text-amber-400">未找到 symbol 列，请选择哪一列作为标的代码：</div>
                <div className="flex flex-wrap gap-1.5">
                  {symbolMapping.candidates.map(col => (
                    <button
                      key={col}
                      onClick={() => handleSymbolMap(col)}
                      className="px-2 py-1 rounded-btn bg-accent/10 text-accent text-[10px] font-medium hover:bg-accent/20 transition-colors"
                    >
                      {col}
                    </button>
                  ))}
                  <button
                    onClick={() => setSymbolMapping(null)}
                    className="px-2 py-1 rounded-btn bg-elevated text-muted text-[10px] hover:text-secondary transition-colors"
                  >
                    取消
                  </button>
                </div>
              </div>
            )}
            <div className="space-y-2">
              {fields.map((f, i) => {
                const isBuiltin = f.name === 'symbol' || f.name === 'name'
                return (
                  <div key={i} className="flex items-center gap-2">
                    <input
                      value={f.label}
                      onChange={(e) => updateField(i, 'label', e.target.value)}
                      placeholder="显示名"
                      disabled={isBuiltin}
                      className={`w-20 h-7 px-2 rounded-btn border text-[11px] text-foreground placeholder:text-muted/40 focus:outline-none focus:border-accent/50 ${
                        isBuiltin
                          ? 'bg-elevated/50 border-border text-muted cursor-not-allowed'
                          : 'bg-base border-border'
                      }`}
                    />
                    <input
                      value={f.name}
                      onChange={(e) => updateField(i, 'name', e.target.value)}
                      placeholder="字段名 (英文)"
                      disabled={isBuiltin}
                      className={`flex-1 h-7 px-2 rounded-btn border text-[11px] font-mono placeholder:text-muted/40 focus:outline-none focus:border-accent/50 ${
                        isBuiltin
                          ? 'bg-elevated/50 border-border text-muted cursor-not-allowed'
                          : 'bg-base border-border'
                      }`}
                    />
                    <select
                      value={f.dtype}
                      onChange={(e) => updateField(i, 'dtype', e.target.value)}
                      disabled={isBuiltin}
                      className={`h-7 px-2 rounded-btn border border-border text-[11px] text-foreground ${
                        isBuiltin ? 'bg-elevated/50 text-muted cursor-not-allowed' : 'bg-base'
                      }`}
                    >
                      <option value="string">文本</option>
                      <option value="int">整数</option>
                      <option value="float">小数</option>
                      <option value="bool">布尔</option>
                    </select>
                    {!isBuiltin && fields.length > 3 && (
                      <button onClick={() => removeField(i)} className="p-1 text-muted hover:text-danger">
                        <X className="h-3 w-3" />
                      </button>
                    )}
                    {isBuiltin && <div className="w-[18px]" />}
                  </div>
                )
              })}
            </div>
          </div>

          {error && (
            <div className="text-xs text-danger bg-danger/5 rounded-btn px-3 py-1.5">{error}</div>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 px-5 py-3 border-t border-border">
          <button onClick={onClose} className="px-4 py-1.5 rounded-btn bg-elevated text-secondary text-xs hover:bg-elevated/80 transition-colors">
            取消
          </button>
          <button
            onClick={() => update.mutate()}
            disabled={!valid || update.isPending}
            className="px-4 py-1.5 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent disabled:opacity-40 transition-colors"
          >
            {update.isPending ? '保存中…' : '保存'}
          </button>
        </div>
      </motion.div>
    </div>
  )
}
