import { useRef, useState, useEffect } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import { Settings, Tag, Upload, Code, RefreshCw, CheckCircle2, Loader2, Pencil, ChevronDown, ChevronUp, AlertTriangle, Trash2 } from 'lucide-react'
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
  const [collapsedH, setCollapsedH] = useState(48)

  useEffect(() => {
    const el = fieldsRef.current
    if (!el) return
    const chip = el.firstElementChild as HTMLElement | null
    // 折叠高度按整行 chip 对齐 (行高 = chip 高 + gap-1), 只露完整行, 不拦腰截断
    const rowH = (chip ? chip.offsetHeight : 20) + 4
    const h = 2 * rowH - 4
    setCollapsedH(h)
    setFieldsOverflow(el.scrollHeight > h + 1)
  }, [config.fields])

  // 描述固定两行: 内嵌引擎有最小字号 (text-[10px] 实际行距 ≈17px 而非 16.25),
  // -webkit-line-clamp 与 px 硬编码都不可靠 — 实测前两行文本的包络高度定容器高
  const descRef = useRef<HTMLDivElement>(null)
  const [descMaxH, setDescMaxH] = useState<number | undefined>(undefined)
  useEffect(() => {
    const el = descRef.current
    if (!el) return
    const range = document.createRange()
    range.selectNodeContents(el)
    // 文本矩形按 top 聚类成行 (同行内中英文片段 top 差 < 8px)
    const lines: Array<{ top: number; bottom: number }> = []
    for (const r of [...range.getClientRects()].sort((a, b) => a.top - b.top)) {
      const last = lines[lines.length - 1]
      if (last && r.top - last.top < 8) {
        last.top = Math.min(last.top, r.top)
        last.bottom = Math.max(last.bottom, r.bottom)
      } else {
        lines.push({ top: r.top, bottom: r.bottom })
      }
    }
    if (lines.length < 3) {
      setDescMaxH(undefined)  // 本就不足三行, 不设限
      return
    }
    // 裁剪线定在第 3 行顶之上: 无论行距/行盒是否重叠, 第三行笔画一个像素都进不了盒内。
    // getBoundingClientRect 受页面缩放 (实测 ~1.1×) 影响, 须除回 CSS 像素再设置高度
    const r = el.getBoundingClientRect()
    const zoom = r.height / el.clientHeight || 1
    setDescMaxH(Math.max(24, Math.ceil((lines[2].top - r.top - 0.5) / zoom)))
  }, [config.description])

  const upload = useMutation({
    mutationFn: ({ id, file }: { id: string; file: File }) => api.extDataUpload(id, file),
    onMutate: () => { setUploading(true); setUploadResult(null) },
    onSuccess: (data) => {
      setUploadResult({ rows: data.rows, date: data.date })
      qc.invalidateQueries({ queryKey: QK.extData })
      qc.invalidateQueries({ queryKey: QK.dataStatus })
      setTimeout(() => setUploadResult(null), 3000)
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

  // 模式主色: 快照=蓝 / 时序=琥珀 (延续历史语义)。展示为三处层次而非整卡平涂:
  // 染色边框 + 顶部发光边线 + 自主色到卡底的纵向渐变 — 有色而不闹
  const accent = config.mode === 'snapshot' ? {
    border: 'border-blue-400/25 hover:border-blue-400/45',
    bg: 'bg-gradient-to-b from-blue-400/[0.07] via-surface to-surface',
    line: 'via-blue-400/60',
  } : {
    border: 'border-amber-400/25 hover:border-amber-400/45',
    bg: 'bg-gradient-to-b from-amber-400/[0.07] via-surface to-surface',
    line: 'via-amber-400/60',
  }

  return (
    <div className={`rounded-card border ${accent.border} ${accent.bg} relative flex flex-col transition-colors duration-300`}>
      <div className={`pointer-events-none absolute inset-x-3 top-0 h-px bg-gradient-to-r from-transparent ${accent.line} to-transparent`} />
      <div className="flex items-start justify-between gap-2 px-4 pt-4">
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-medium text-foreground truncate" title={config.label}>
            {config.label}
          </h3>
          <div className="flex items-center gap-1.5 mt-1.5">
            <span className={`whitespace-nowrap text-[10px] leading-none font-medium px-1.5 py-1 rounded-md ${
              config.mode === 'snapshot'
                ? 'bg-blue-500/10 text-blue-400'
                : 'bg-amber-500/10 text-amber-400'
            }`}>
              {config.mode === 'snapshot' ? '快照' : '时序'}
            </span>
            {config.mode === 'timeseries' && config.pull?.time_field && (
              <span
                className="whitespace-nowrap text-[10px] leading-none font-medium px-1.5 py-1 rounded-md bg-emerald-500/10 text-emerald-500 dark:text-emerald-400"
                title={`日内多盘 · 时间列 ${config.pull.time_field}`}
              >
                多盘
              </span>
            )}
          </div>
        </div>
        <button
          onClick={() => setSettingsOpen(v => !v)}
          className="shrink-0 p-1.5 rounded-md hover:bg-elevated/70 transition-colors text-muted hover:text-secondary"
        >
          <Settings className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* 描述固定两行: 行距由运行时实测 (见上方 descMaxH), 52px 为测量前的兜底 */}
      <div
        ref={descRef}
        style={descMaxH !== undefined ? { maxHeight: descMaxH } : undefined}
        className="px-4 pt-2 pb-1.5 text-[10px] text-muted leading-relaxed max-h-[52px] shrink-0 overflow-hidden break-words"
      >
        {config.description || `扩展数据 · ${config.mode === 'snapshot' ? '与标的 JOIN' : '与日K JOIN'}`}
      </div>

      <div className="flex-1 min-h-0 px-4 pb-2">
        <div
          className="relative overflow-hidden transition-[height] duration-300"
          style={fieldsExpanded || !fieldsOverflow ? undefined : { height: collapsedH }}
        >
          <div
            ref={fieldsRef}
            className="flex flex-wrap gap-1"
          >
            {config.fields
              .filter(f => f.name !== 'symbol' && f.name !== 'code')
              .map((f) => (
                <span
                  key={f.name}
                  className="text-[10px] leading-none py-1 px-1.5 rounded-md bg-elevated/70 border border-border/40 text-secondary"
                  title={f.name}
                >
                  {f.label || f.name}
                </span>
              ))
            }
          </div>
          {fieldsExpanded && fieldsOverflow && (
            <button
              onClick={() => setFieldsExpanded(false)}
              className="w-full flex items-center justify-center pt-1.5 text-muted hover:text-secondary transition-colors"
              title="收起字段"
            >
              <ChevronUp className="h-3 w-3" />
            </button>
          )}
        </div>
        {fieldsOverflow && !fieldsExpanded && (
          <button
            onClick={() => setFieldsExpanded(true)}
            className="mx-auto mt-1 flex items-center rounded-full border border-border/60 bg-elevated/60 px-2.5 py-0.5 text-muted hover:text-secondary hover:border-border transition-colors"
            title="展开全部字段"
          >
            <ChevronDown className="h-3 w-3" />
          </button>
        )}
      </div>

      <div className="mt-auto px-4 pb-4 pt-2.5 border-t border-border/60 space-y-1 text-[11px]">
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-muted shrink-0">标识</span>
          <span className="font-mono text-secondary/80 truncate" title={config.id}>{config.id}</span>
        </div>
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-muted shrink-0">最新</span>
          <span
            className="text-secondary/80 tabular-nums truncate"
            title={config.latest_sync_date ?? undefined}
          >
            {config.latest_sync_date ?? '—'}
          </span>
        </div>
        {config.plate && (
          <div className="flex items-baseline justify-between gap-3">
            <span className="text-muted shrink-0">盘次</span>
            <span
              className="font-mono text-secondary/80 tabular-nums whitespace-nowrap"
              title={`${config.plate.date} 多盘保留, @时刻可选盘`}
            >
              {config.plate.plates} 盘 · {config.plate.from.slice(0, 5)}–{config.plate.to.slice(0, 5)}
            </span>
          </div>
        )}
      </div>

      <AnimatePresence>
        {settingsOpen && (
          <SettingsModal title={config.label} onClose={() => setSettingsOpen(false)} width="max-w-xl">
            <div className="space-y-4">
              {/* 信息条: 一眼看清这张表是什么 */}
              <div className="flex items-center gap-2 flex-wrap rounded-lg border border-border/50 bg-elevated/30 px-3 py-2 text-[10px]">
                <span className={`px-1.5 py-px rounded font-medium border ${
                  config.mode === 'snapshot'
                    ? 'bg-blue-500/10 text-blue-400 border-blue-500/20'
                    : 'bg-amber-500/10 text-amber-500 border-amber-500/20'
                }`}>
                  {config.mode === 'snapshot' ? '快照' : '时序'}
                </span>
                <span className="font-mono text-secondary">{config.id}</span>
                <span className="text-muted/50">·</span>
                <span className="text-muted">{config.fields.filter(f => f.name !== 'symbol' && f.name !== 'code').length} 个字段</span>
                <span className="text-muted/50">·</span>
                <span className="text-muted">最新 <span className="text-secondary font-mono">{config.latest_sync_date ?? '—'}</span></span>
              </div>

              {/* 数据写入: 主操作区 */}
              <div>
                <div className="text-[11px] font-medium text-secondary mb-2">数据写入</div>
                <div className="grid grid-cols-3 gap-1 rounded-xl bg-elevated/50 p-1 border border-border/40">
                  <button
                    onClick={() => setIngestTab('pull')}
                    className={`inline-flex items-center justify-center gap-1.5 h-9 rounded-lg text-xs font-medium transition-all ${
                      ingestTab === 'pull' ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary hover:bg-elevated/40'
                    }`}
                  >
                    <RefreshCw className="h-3.5 w-3.5" />拉取
                  </button>
                  <button
                    onClick={() => setIngestTab('api')}
                    className={`inline-flex items-center justify-center gap-1.5 h-9 rounded-lg text-xs font-medium transition-all ${
                      ingestTab === 'api' ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary hover:bg-elevated/40'
                    }`}
                  >
                    <Code className="h-3.5 w-3.5" />推送
                  </button>
                  <button
                    onClick={() => setIngestTab('file')}
                    className={`inline-flex items-center justify-center gap-1.5 h-9 rounded-lg text-xs font-medium transition-all ${
                      ingestTab === 'file' ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary hover:bg-elevated/40'
                    }`}
                  >
                    <Upload className="h-3.5 w-3.5" />上传
                  </button>
                </div>
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
                    className={`relative cursor-pointer rounded-xl border-2 border-dashed transition-all py-8 flex flex-col items-center justify-center gap-2 ${
                      dragOver
                        ? 'border-accent bg-accent/10 scale-[1.01]'
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
                        <div className={`p-2.5 rounded-xl bg-elevated/60 ${dragOver ? 'text-accent' : 'text-muted/70'}`}>
                          <Upload className="h-5 w-5" />
                        </div>
                        <span className={`text-xs ${dragOver ? 'text-accent' : 'text-secondary'}`}>
                          拖拽文件到此处，或点击选择
                        </span>
                        <span className="text-[10px] text-muted/70">支持 CSV / Excel，需包含 symbol 列</span>
                      </>
                    )}
                  </div>
                </>
              )}

              {uploadResult && (
                <div className="text-[11px] text-green-400 bg-green-500/10 border border-green-500/25 rounded-lg px-3 py-2.5 text-center flex items-center justify-center gap-1.5 font-medium">
                  <CheckCircle2 className="h-3.5 w-3.5" />上传成功 · {uploadResult.rows} 行 · {uploadResult.date}
                </div>
              )}

              {/* 管理操作: 低频, 收到底部一行 */}
              <div className="flex items-center justify-between pt-2.5 border-t border-border/40">
                <div className="flex items-center gap-1">
                  {onEdit && (
                    <button
                      onClick={() => { setSettingsOpen(false); onEdit() }}
                      title="编辑名称、描述与字段定义"
                      className="inline-flex items-center gap-1 h-7 px-2.5 rounded-lg text-[10px] text-muted hover:text-foreground hover:bg-elevated focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
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
                    title="历史数据 symbol 列缺交易所后缀时批量补全 (600000 → 600000.SH)"
                    className="inline-flex items-center gap-1 h-7 px-2.5 rounded-lg text-[10px] text-muted hover:text-foreground hover:bg-elevated focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
                  >
                    <Tag className="h-3 w-3" />
                    修复代码格式
                  </button>
                </div>
                <button
                  onClick={() => setShowDelete(true)}
                  className="inline-flex items-center gap-1 h-7 px-2.5 rounded-lg text-[10px] text-danger/60 hover:text-danger hover:bg-danger/[0.06] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-danger/40 transition-colors"
                >
                  <Trash2 className="h-3 w-3" />
                  删除
                </button>
              </div>
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
                  className="px-3 py-1.5 rounded-btn bg-danger/90 text-white text-sm font-medium hover:bg-danger disabled:opacity-50 transition-colors"
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
