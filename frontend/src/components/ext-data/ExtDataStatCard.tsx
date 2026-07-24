import { useRef, useState, useEffect } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import { Settings, Tag, Upload, Code, RefreshCw, CheckCircle2, Loader2, Pencil, ChevronDown, ChevronUp, AlertTriangle } from 'lucide-react'
import { api, type ExtDataConfig } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { SettingsModal } from '@/components/data/SettingsModal'
import { ExtDataPullPanel } from './ExtDataPullPanel'
import { ExtDataApiPanel } from './ExtDataApiPanel'

export function ExtDataStatCard({ config, onDelete, deleting, onEdit }: {
  config: ExtDataConfig
  onDelete: () => void
  deleting: boolean
  onEdit?: () => void
}) {
  const qc = useQueryClient()
  const fileRef = useRef<HTMLInputElement>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadResult, setUploadResult] = useState<{ rows: number; date: string } | null>(null)
  const [showDelete, setShowDelete] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const [ingestTab, setIngestTab] = useState<'file' | 'api' | 'pull'>('pull')
  const [copied, setCopied] = useState(false)
  const [fieldsExpanded, setFieldsExpanded] = useState(false)
  const fieldsRef = useRef<HTMLDivElement>(null)
  const [fieldsOverflow, setFieldsOverflow] = useState(false)

  useEffect(() => {
    const el = fieldsRef.current
    if (!el) return
    const prev = el.style.maxHeight
    el.style.maxHeight = 'none'
    const full = el.scrollHeight
    el.style.maxHeight = prev
    setFieldsOverflow(full > 68)
  }, [config.fields])

  const upload = useMutation({
    mutationFn: ({ id, file }: { id: string; file: File }) => api.extDataUpload(id, file),
    onMutate: () => { setUploading(true); setUploadResult(null) },
    onSuccess: (data) => {
      setUploadResult({ rows: data.rows, date: data.date })
      qc.invalidateQueries({ queryKey: QK.extData })
      qc.invalidateQueries({ queryKey: QK.dataStatus })
      setTimeout(() => {
        setUploadResult(null)
        setSettingsOpen(false)
      }, 1500)
    },
    onSettled: () => setUploading(false),
  })

  const doUpload = (file: File) => upload.mutate({ id: config.id, file })

  const handleFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) doUpload(file)
    e.target.value = ''
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    const file = e.dataTransfer.files[0]
    if (file) doUpload(file)
  }

  return (
    <div className={`rounded-card border flex flex-col transition-all duration-300 ${
      config.mode === 'snapshot'
        ? 'border-blue-500/30 bg-blue-500/[0.03]'
        : 'border-amber-500/30 bg-amber-500/[0.03]'
    }`}>
      <div className="flex items-center justify-between px-4 pt-4 pb-2">
        <h3 className="text-sm font-medium text-foreground">{config.label}</h3>
        <div className="flex items-center gap-1.5">
          <span className={`text-[10px] px-1.5 py-px rounded font-medium ${
            config.mode === 'snapshot'
              ? 'bg-blue-500/10 text-blue-400'
              : 'bg-amber-500/10 text-amber-400'
          }`}>
            {config.mode === 'snapshot' ? '快照' : '时序'}
          </span>
          <button
            onClick={() => setSettingsOpen(v => !v)}
            className="p-0.5 rounded hover:bg-elevated transition-colors text-secondary"
          >
            <Settings className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <div className="px-4 pb-1.5 text-[10px] text-muted/70 leading-relaxed line-clamp-2">
        {config.description || `扩展数据 · ${config.mode === 'snapshot' ? '与标的 JOIN' : '与日K JOIN'}`}
      </div>

      <div className="flex-1 min-h-[81px] px-4 pb-2">
        <div className={`relative overflow-hidden transition-all duration-300 ${fieldsExpanded ? '' : 'h-[75px]'}`}>
          <div
            ref={fieldsRef}
            className="flex flex-wrap gap-1"
          >
            {config.fields
              .filter(f => f.name !== 'symbol' && f.name !== 'code')
              .map((f) => (
                <span key={f.name} className="inline-flex items-center gap-1 text-[10px] bg-elevated rounded px-1.5 py-0.5">
                  <Tag className="h-2.5 w-2.5 text-muted" />
                  <span className="text-secondary">{f.label || f.name}</span>
                </span>
              ))
            }
          </div>
          {fieldsOverflow && !fieldsExpanded && (
            <div
              className="absolute bottom-0 inset-x-0 h-6 bg-gradient-to-t from-surface to-transparent cursor-pointer flex items-end justify-center"
              onClick={() => setFieldsExpanded(true)}
            >
              <ChevronDown className="h-3 w-3 text-muted" />
            </div>
          )}
          {fieldsExpanded && fieldsOverflow && (
            <button
              onClick={() => setFieldsExpanded(false)}
              className="w-full flex items-center justify-center pt-1 text-[10px] text-muted hover:text-secondary transition-colors"
            >
              <ChevronUp className="h-3 w-3" />
            </button>
          )}
        </div>
      </div>

      <div className="mt-auto px-4 pb-4 pt-2 border-t border-border/50">
        <div className="flex justify-between text-[11px]">
          <span className="text-muted">标识</span>
          <span className="font-mono text-secondary">{config.id}</span>
        </div>
        <div className="flex justify-between text-[11px] mt-1">
          <span className="text-muted">最新</span>
          <span className="text-secondary">{config.latest_sync_date ?? '—'}</span>
        </div>
      </div>

      <AnimatePresence>
        {settingsOpen && (
          <SettingsModal title={`${config.label} · 设置`} onClose={() => setSettingsOpen(false)}>
            <div className="space-y-3">
              {onEdit && (
                <button
                  onClick={() => { setSettingsOpen(false); onEdit() }}
                  className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-btn border border-border bg-elevated text-foreground text-xs font-medium hover:bg-border/30 transition-colors"
                >
                  <Pencil className="h-3 w-3" />
                  编辑配置
                </button>
              )}

              <button
                onClick={() => {
                  api.extDataFixSymbol(config.id).then((res) => {
                    setUploadResult({ rows: res.fixed_files, date: '格式修复完成' })
                    qc.invalidateQueries({ queryKey: QK.extData })
                    setTimeout(() => setUploadResult(null), 3000)
                  })
                }}
                className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-btn border border-border bg-elevated text-foreground text-xs font-medium hover:bg-border/30 transition-colors"
              >
                <Tag className="h-3 w-3" />
                修复代码格式
              </button>

              <div className="flex gap-1 rounded-lg bg-elevated/60 p-0.5">
                <button
                  onClick={() => setIngestTab('pull')}
                  className={`flex-1 inline-flex items-center justify-center gap-1 py-1.5 rounded-md text-[10px] font-medium transition-colors ${
                    ingestTab === 'pull' ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary'
                  }`}
                >
                  <RefreshCw className="h-3 w-3" />拉取
                </button>
                <button
                  onClick={() => setIngestTab('api')}
                  className={`flex-1 inline-flex items-center justify-center gap-1 py-1.5 rounded-md text-[10px] font-medium transition-colors ${
                    ingestTab === 'api' ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary'
                  }`}
                >
                  <Code className="h-3 w-3" />推送
                </button>
                <button
                  onClick={() => setIngestTab('file')}
                  className={`flex-1 inline-flex items-center justify-center gap-1 py-1.5 rounded-md text-[10px] font-medium transition-colors ${
                    ingestTab === 'file' ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary'
                  }`}
                >
                  <Upload className="h-3 w-3" />上传
                </button>
              </div>

              {ingestTab === 'pull' ? (
                <ExtDataPullPanel config={config} onSaved={() => qc.invalidateQueries({ queryKey: QK.extData })} />
              ) : ingestTab === 'api' ? (
                <ExtDataApiPanel config={config} copied={copied} setCopied={setCopied} />
              ) : (
                <>
                  <input
                    ref={fileRef}
                    type="file"
                    accept=".csv,.xlsx,.xls"
                    className="hidden"
                    onChange={handleFile}
                  />
                  <div
                    onClick={() => fileRef.current?.click()}
                    onDragOver={e => { e.preventDefault(); setDragOver(true) }}
                    onDragLeave={() => setDragOver(false)}
                    onDrop={handleDrop}
                    className={`relative cursor-pointer rounded-lg border-2 border-dashed transition-colors py-5 flex flex-col items-center justify-center gap-1.5 ${
                      dragOver
                        ? 'border-accent bg-accent/10'
                        : uploading
                          ? 'border-border/50 bg-elevated/30 pointer-events-none'
                          : 'border-border/40 hover:border-accent/50 hover:bg-accent/[0.03]'
                    }`}
                  >
                    {uploading ? (
                      <>
                        <Loader2 className="h-5 w-5 text-accent animate-spin" />
                        <span className="text-[11px] text-muted">上传中…</span>
                      </>
                    ) : (
                      <>
                        <Upload className={`h-5 w-5 ${dragOver ? 'text-accent' : 'text-muted'}`} />
                        <span className={`text-[11px] ${dragOver ? 'text-accent' : 'text-secondary'}`}>
                          拖拽文件到此处上传
                        </span>
                        <span className="text-[10px] text-muted">支持 CSV / Excel，需包含 symbol 列</span>
                      </>
                    )}
                  </div>
                </>
              )}

              {uploadResult && (
                <div className="text-[11px] text-green-400 bg-green-500/10 rounded-lg px-3 py-2 text-center flex items-center justify-center gap-1.5 font-medium">
                  <CheckCircle2 className="h-3.5 w-3.5" />上传成功 · {uploadResult.rows} 行 · {uploadResult.date}
                </div>
              )}
              <button
                onClick={() => setShowDelete(true)}
                className="w-full text-center text-[10px] text-danger/60 hover:text-danger transition-colors"
              >
                删除此扩展
              </button>
            </div>
          </SettingsModal>
        )}
      </AnimatePresence>

      {/* 删除二次确认弹窗 */}
      <AnimatePresence>
        {showDelete && (
          <div className="fixed inset-0 z-[60] flex items-center justify-center">
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="absolute inset-0 bg-black/60 backdrop-blur-sm"
              onClick={() => !deleting && setShowDelete(false)}
            />
            <motion.div
              initial={{ opacity: 0, scale: 0.95, y: 12 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.97, y: 8 }}
              transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
              className="relative w-[90vw] max-w-[380px] rounded-card border border-border bg-base shadow-2xl p-6"
            >
              <div className="flex items-start gap-3">
                <div className="shrink-0 h-10 w-10 rounded-full bg-danger/12 flex items-center justify-center">
                  <AlertTriangle className="h-5 w-5 text-danger" />
                </div>
                <div className="min-w-0">
                  <h3 className="text-sm font-semibold text-foreground mb-1.5">确认删除「{config.label}」？</h3>
                  <p className="text-xs text-secondary leading-relaxed">
                    将<span className="text-danger font-medium">永久删除</span>该扩展数据配置及其全部数据，包括字段、已上传/拉取的所有记录。
                  </p>
                  <p className="mt-2 text-[11px] text-danger/90">
                    操作不可恢复。
                  </p>
                </div>
              </div>
              <div className="flex items-center justify-end gap-2 mt-5">
                <button
                  onClick={() => setShowDelete(false)}
                  disabled={deleting}
                  className="px-3 py-1.5 rounded-btn bg-elevated text-secondary hover:bg-elevated/80 text-sm transition-colors disabled:opacity-50"
                >
                  取消
                </button>
                <button
                  onClick={() => { onDelete(); setShowDelete(false) }}
                  disabled={deleting}
                  className="px-3 py-1.5 rounded-btn bg-danger/90 text-base text-sm font-medium hover:bg-danger disabled:opacity-50 transition-colors"
                >
                  {deleting ? '删除中…' : '确认删除'}
                </button>
              </div>
            </motion.div>
          </div>
        )}
      </AnimatePresence>
    </div>
  )
}
