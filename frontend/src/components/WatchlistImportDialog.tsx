import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { useQuery } from '@tanstack/react-query'
import { FileText, ImagePlus, Loader2, Tag, Upload, X, type LucideIcon } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { TagInput } from '@/components/TagInput'
import { toast } from '@/components/Toast'
import { api, type WatchlistImportCandidate } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { splitTags } from '@/lib/tags'
import { useWatchlistBatchAdd } from '@/lib/useSharedMutations'
import { getOcrInstallHint } from '@/lib/ocrInstallHint'

interface Props {
  open: boolean
  onClose: () => void
}

type ImportMode = 'image' | 'csv'

/** 一次最多排队识别的图片数，避免误选大量文件拖垮小内存机器。 */
const MAX_IMPORT_IMAGES = 10

function isImageFile(file: File): boolean {
  return file.type.startsWith('image/') || /\.(jpe?g|png|webp|bmp|gif)$/i.test(file.name)
}

function isCsvFile(file: File): boolean {
  return /\.(csv|txt)$/i.test(file.name)
}

/** 按 code 合并多图 OCR 结果：优先保留已匹配项，已在自选取并集。 */
export function mergeImportCandidates(
  lists: WatchlistImportCandidate[][],
): WatchlistImportCandidate[] {
  const byCode = new Map<string, WatchlistImportCandidate>()
  for (const list of lists) {
    for (const c of list) {
      const prev = byCode.get(c.code)
      if (!prev) {
        byCode.set(c.code, c)
        continue
      }
      if (c.matched && !prev.matched) {
        byCode.set(c.code, c)
        continue
      }
      if (c.matched && prev.matched) {
        byCode.set(c.code, {
          ...prev,
          symbol: prev.symbol || c.symbol,
          name: prev.name || c.name,
          already_in_watchlist: prev.already_in_watchlist || c.already_in_watchlist,
        })
      }
    }
  }
  return [...byCode.values()]
}

/** 默认勾选可添加的已匹配候选。 */
function defaultSelected(list: WatchlistImportCandidate[]): Set<string> {
  return new Set(
    list.filter(c => c.matched && c.symbol && !c.already_in_watchlist).map(c => c.symbol!),
  )
}

interface FileDropzoneProps {
  inputRef: RefObject<HTMLInputElement>
  accept: string
  multiple?: boolean
  icon: LucideIcon
  label: string
  busyLabel: string
  busy: boolean
  onPick: (list: FileList | File[] | null | undefined) => void
}

/** 隐藏 file input + 虚线拖拽上传按钮（截图 / CSV 两个页签共用）。 */
function FileDropzone({
  inputRef,
  accept,
  multiple,
  icon: Icon,
  label,
  busyLabel,
  busy,
  onPick,
}: FileDropzoneProps) {
  return (
    <>
      <input
        ref={inputRef}
        type="file"
        multiple={multiple}
        accept={accept}
        className="hidden"
        onChange={e => {
          onPick(e.target.files)
          e.target.value = ''
        }}
      />
      <button
        type="button"
        disabled={busy}
        onClick={() => inputRef.current?.click()}
        onDragOver={e => { e.preventDefault(); e.stopPropagation() }}
        onDrop={e => {
          e.preventDefault()
          onPick(e.dataTransfer.files)
        }}
        className="w-full flex flex-col items-center justify-center gap-2 rounded-btn border border-dashed border-border bg-elevated/40 hover:bg-elevated/70 px-4 py-6 text-secondary transition-colors disabled:opacity-50"
      >
        {busy ? (
          <Loader2 className="h-6 w-6 animate-spin text-accent" />
        ) : (
          <Icon className="h-6 w-6 text-accent" />
        )}
        <span className="text-xs">{busy ? busyLabel : label}</span>
      </button>
    </>
  )
}

export function WatchlistImportDialog({ open, onClose }: Props) {
  const inputRef = useRef<HTMLInputElement>(null)
  const csvInputRef = useRef<HTMLInputElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const genRef = useRef(0)
  const [mode, setMode] = useState<ImportMode>('image')
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null)
  const [provider, setProvider] = useState<string>('')
  const [candidates, setCandidates] = useState<WatchlistImportCandidate[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [previewUrls, setPreviewUrls] = useState<string[]>([])
  const [csvFileName, setCsvFileName] = useState('')
  const [ocrAvailable, setOcrAvailable] = useState<boolean | null>(null)
  const [installHint, setInstallHint] = useState('')
  const [batchTags, setBatchTags] = useState<string[]>([])
  const batchAdd = useWatchlistBatchAdd()

  const abortInFlight = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    genRef.current += 1
  }, [])

  const revokePreviews = useCallback((urls: string[]) => {
    for (const url of urls) URL.revokeObjectURL(url)
  }, [])

  const reset = useCallback(() => {
    abortInFlight()
    setMode('image')
    setBusy(false)
    setProgress(null)
    setCandidates([])
    setSelected(new Set())
    setProvider('')
    setCsvFileName('')
    setOcrAvailable(null)
    setInstallHint('')
    setBatchTags([])
    setPreviewUrls(prev => {
      revokePreviews(prev)
      return []
    })
    if (inputRef.current) inputRef.current.value = ''
    if (csvInputRef.current) csvInputRef.current.value = ''
  }, [abortInFlight, revokePreviews])

  // 复用宿主页 QK.watchlist 缓存作标签建议, 避免额外请求
  const watchlist = useQuery({ queryKey: QK.watchlist, queryFn: api.watchlistList })
  const existingTags = useMemo(() => {
    const tags = new Set<string>()
    for (const e of watchlist.data?.symbols ?? []) {
      for (const t of splitTags(e.tags)) tags.add(t)
    }
    return [...tags]
  }, [watchlist.data])

  useEffect(() => {
    if (!open) {
      reset()
      return
    }
    let cancelled = false
    void api.watchlistOcrStatus().then(
      res => {
        if (cancelled) return
        setOcrAvailable(res.available)
        if (!res.available) setInstallHint(getOcrInstallHint())
      },
      () => {
        if (cancelled) return
        setOcrAvailable(false)
        setInstallHint(getOcrInstallHint())
      },
    )
    return () => {
      cancelled = true
    }
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  const runRecognizeQueue = async (files: File[]) => {
    const images = files.filter(isImageFile)
    if (images.length === 0) {
      toast('请选择图片文件', 'error')
      return
    }
    if (images.length < files.length) {
      toast('已忽略非图片文件', 'error')
    }
    const queue = images.slice(0, MAX_IMPORT_IMAGES)
    if (images.length > MAX_IMPORT_IMAGES) {
      toast(`一次最多识别 ${MAX_IMPORT_IMAGES} 张，已取前 ${MAX_IMPORT_IMAGES} 张`, 'error')
    }

    abortInFlight()
    const controller = new AbortController()
    abortRef.current = controller
    const gen = genRef.current

    setPreviewUrls(prev => {
      revokePreviews(prev)
      return queue.map(f => URL.createObjectURL(f))
    })
    setBusy(true)
    setProgress({ done: 0, total: queue.length })
    setCandidates([])
    setSelected(new Set())
    setProvider('')

    const mergedLists: WatchlistImportCandidate[][] = []
    let lastProvider = ''
    let failed = 0
    let lastError = ''

    try {
      for (let i = 0; i < queue.length; i++) {
        if (gen !== genRef.current || controller.signal.aborted) return
        try {
          // quiet：避免每张失败各弹一条 toast，结束时统一提示
          const res = await api.watchlistImportImage(queue[i], controller.signal, true)
          if (gen !== genRef.current) return
          lastProvider = res.provider
          mergedLists.push(res.candidates)
        } catch (err) {
          if (gen !== genRef.current) return
          if (controller.signal.aborted) return
          failed += 1
          lastError = err instanceof Error ? err.message : ''
        }
        if (gen === genRef.current) {
          setProgress({ done: i + 1, total: queue.length })
        }
      }

      if (gen !== genRef.current) return

      const merged = mergeImportCandidates(mergedLists)
      setProvider(lastProvider)
      setCandidates(merged)
      setSelected(defaultSelected(merged))

      if (merged.length === 0) {
        toast(
          lastError
            || (failed > 0
              ? '识别失败或未识别到股票代码，请换更清晰的截图'
              : '未识别到股票代码，请换一张更清晰的自选列表截图'),
          'error',
        )
      } else if (merged.every(c => !c.matched)) {
        toast('识别到代码但未能匹配证券主数据', 'error')
      } else if (failed > 0) {
        toast(`有 ${failed} 张识别失败，已合并其余结果`, 'error')
      }
    } finally {
      if (gen === genRef.current) {
        setBusy(false)
        setProgress(null)
      }
    }
  }

  const runCsvImport = async (file: File) => {
    if (!isCsvFile(file)) {
      toast('请选择 CSV 或 TXT 文件', 'error')
      return
    }
    abortInFlight()
    const controller = new AbortController()
    abortRef.current = controller
    const gen = genRef.current

    setBusy(true)
    setCandidates([])
    setSelected(new Set())
    setCsvFileName(file.name)
    try {
      const res = await api.watchlistImportCsv(file, controller.signal, true)
      if (gen !== genRef.current) return
      setCandidates(res.candidates)
      setSelected(defaultSelected(res.candidates))
      if (res.candidates.every(c => !c.matched)) {
        toast('识别到代码但未能匹配证券主数据', 'error')
      }
    } catch (err) {
      if (gen !== genRef.current) return
      if (controller.signal.aborted) return
      toast(err instanceof Error ? err.message : 'CSV 解析失败', 'error')
    } finally {
      if (gen === genRef.current) setBusy(false)
    }
  }

  const onPick = (list: FileList | File[] | null | undefined) => {
    if (!list || list.length === 0) return
    void runRecognizeQueue(Array.from(list))
  }

  const onCsvPick = (list: FileList | File[] | null | undefined) => {
    if (!list || list.length === 0) return
    void runCsvImport(list[0])
  }

  const toggle = (symbol: string) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(symbol)) next.delete(symbol)
      else next.add(symbol)
      return next
    })
  }

  const matched = candidates.filter(c => c.matched && c.symbol)
  // 已有自选的重复标: 设置批量标签后自动并入合并标签, 无需勾选
  const existingDups = matched.filter(c => c.already_in_watchlist)
  const dupSymbols = batchTags.length > 0 ? existingDups.map(c => c.symbol!) : []
  const affectedCount = selected.size + dupSymbols.length
  const selectable = matched.filter(c => !c.already_in_watchlist)
  const allSelected = selectable.length > 0 && selectable.every(c => selected.has(c.symbol!))

  const toggleAll = () => {
    if (allSelected) setSelected(new Set())
    else setSelected(new Set(selectable.map(c => c.symbol!)))
  }

  const confirmAdd = async () => {
    const symbols = [...selected, ...dupSymbols]
    if (symbols.length === 0) {
      toast('请至少选择一只股票', 'error')
      return
    }
    try {
      const data = await batchAdd.mutateAsync({ symbols, tags: batchTags })
      const parts = [`已添加 ${data.added} 只自选`]
      if (dupSymbols.length > 0) parts.push(`${dupSymbols.length} 只已有自选自动合并标签`)
      toast(parts.join(' · '), 'success')
      onClose()
    } catch {
      /* toast in request */
    }
  }

  if (!open) return null

  const ocrBlocked = ocrAvailable === false
  const progressLabel =
    progress && progress.total > 1
      ? `识别中 ${progress.done}/${progress.total}…`
      : progress
        ? '识别中…'
        : null

  return (
    <Modal
      onClose={onClose}
      labelledBy="watchlist-import-title"
      panelClassName="w-[92vw] max-w-lg max-h-[85vh] flex flex-col bg-surface border border-border rounded-card shadow-xl"
    >
      <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
        <div>
          <h2 id="watchlist-import-title" className="text-sm font-semibold text-foreground">
            导入自选
          </h2>
          <p className="text-[11px] text-muted mt-0.5">
            {mode === 'image'
              ? (ocrBlocked
                ? 'OCR 引擎不可用'
                : '可多选截图，将逐张识别并合并结果后确认添加')
              : '支持含证券代码或名称的 CSV / TXT（UTF-8 或 GBK）'}
            {mode === 'image' && provider ? ` · ${provider}` : ''}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="h-8 w-8 inline-flex items-center justify-center rounded-btn text-secondary hover:bg-elevated"
          aria-label="关闭"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="px-4 pt-3 shrink-0">
        <div className="flex gap-1 rounded-lg bg-elevated/60 p-0.5">
          <button
            type="button"
            onClick={() => setMode('image')}
            className={`flex-1 inline-flex items-center justify-center gap-1.5 py-1.5 rounded-md text-[11px] font-medium transition-colors ${
              mode === 'image' ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary'
            }`}
          >
            <ImagePlus className="h-3.5 w-3.5" />
            截图识别
          </button>
          <button
            type="button"
            onClick={() => setMode('csv')}
            className={`flex-1 inline-flex items-center justify-center gap-1.5 py-1.5 rounded-md text-[11px] font-medium transition-colors ${
              mode === 'csv' ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary'
            }`}
          >
            <FileText className="h-3.5 w-3.5" />
            CSV 文件
          </button>
        </div>
      </div>

      <div className="px-4 py-3 overflow-y-auto flex-1 space-y-3">
        {mode === 'image' ? (
          ocrBlocked ? (
            <div className="rounded-btn border border-border bg-elevated/40 px-4 py-5 text-xs text-secondary leading-relaxed whitespace-pre-wrap">
              {installHint}
            </div>
          ) : (
            <>
              <FileDropzone
                inputRef={inputRef}
                accept="image/jpeg,image/png,image/webp,image/bmp,image/gif,.jpg,.jpeg,.png"
                multiple
                icon={ImagePlus}
                label={ocrAvailable === null ? '检查 OCR…' : '点击选择或拖拽截图（支持多选）'}
                busyLabel={progressLabel ?? '检查 OCR…'}
                busy={busy || ocrAvailable === null}
                onPick={onPick}
              />

              {previewUrls.length > 0 && (
                <div className="flex gap-2 overflow-x-auto pb-1">
                  {previewUrls.map((url, i) => (
                    <div
                      key={url}
                      className="shrink-0 w-20 h-20 rounded-btn overflow-hidden border border-border bg-black/40"
                    >
                      <img
                        src={url}
                        alt={`预览 ${i + 1}`}
                        className="w-full h-full object-contain"
                      />
                    </div>
                  ))}
                </div>
              )}
            </>
          )
        ) : (
          <>
            <FileDropzone
              inputRef={csvInputRef}
              accept=".csv,.txt,text/csv,text/plain"
              icon={FileText}
              label="点击选择或拖拽 CSV / TXT 文件"
              busyLabel="解析中…"
              busy={busy}
              onPick={onCsvPick}
            />

            {csvFileName && (
              <div className="flex items-center gap-2 rounded-btn border border-border bg-elevated/40 px-3 py-2 text-xs text-secondary">
                <FileText className="h-3.5 w-3.5 shrink-0 text-accent" />
                <span className="truncate flex-1">{csvFileName}</span>
              </div>
            )}
          </>
        )}

        {candidates.length > 0 && (
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-xs text-secondary">
                {mode === 'csv' ? '解析' : '识别'} {candidates.length} 个代码 · 匹配 {matched.length} · 已选 {selected.size}
                {dupSymbols.length > 0 ? ` · 自动并入 ${dupSymbols.length}` : ''}
              </span>
              {selectable.length > 0 && (
                <button
                  type="button"
                  onClick={toggleAll}
                  className="text-[11px] text-accent hover:underline"
                >
                  {allSelected ? '取消全选' : '全选可添加'}
                </button>
              )}
            </div>
            <ul className="divide-y divide-border/60 rounded-btn border border-border overflow-hidden">
              {candidates.map(c => {
                const key = c.symbol || c.code
                const disabled = !c.matched || !c.symbol || c.already_in_watchlist
                const checked = !!(c.symbol && selected.has(c.symbol))
                return (
                  <li key={key}>
                    <label
                      className={`flex items-center gap-3 px-3 py-2.5 text-sm ${
                        disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer hover:bg-elevated/50'
                      }`}
                    >
                      <input
                        type="checkbox"
                        disabled={disabled}
                        checked={checked}
                        onChange={() => c.symbol && toggle(c.symbol)}
                        className="rounded border-border"
                      />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-baseline gap-2">
                          <span className="font-medium text-foreground truncate">
                            {c.name || (c.matched ? c.symbol : '未匹配')}
                          </span>
                          <span className="text-[11px] text-muted tabular-nums shrink-0">
                            {c.code}
                            {c.symbol ? ` · ${c.symbol}` : ''}
                          </span>
                        </div>
                        {c.already_in_watchlist && (
                          <span className="text-[10px] text-muted">
                            已在自选{batchTags.length > 0 ? ' · 将自动合并标签' : ''}
                          </span>
                        )}
                        {!c.matched && (
                          <span className="text-[10px] text-warning/90">主数据未找到，已跳过</span>
                        )}
                      </div>
                    </label>
                  </li>
                )
              })}
            </ul>
          </div>
        )}
      </div>

      <div className="shrink-0 px-4 py-3 border-t border-border">
        <div className="flex items-center gap-1.5 text-[11px] text-secondary mb-2">
          <Tag className="h-3 w-3" />
          {affectedCount > 0 ? `为 ${affectedCount} 只批量打标签` : '批量打标签（本次所选）'}
        </div>
        <TagInput tags={batchTags} onChange={setBatchTags} allTags={existingTags} emptyLabel="未设置" compact />
      </div>

      <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-border shrink-0">
        <button
          type="button"
          onClick={onClose}
          className="h-8 px-3 rounded-btn text-xs text-secondary hover:bg-elevated"
        >
          取消
        </button>
        <button
          type="button"
          disabled={affectedCount === 0 || batchAdd.isPending || busy}
          onClick={() => void confirmAdd()}
          className="h-8 px-3 rounded-btn text-xs inline-flex items-center gap-1.5 bg-accent text-white hover:bg-accent/90 disabled:opacity-40"
        >
          {batchAdd.isPending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Upload className="h-3.5 w-3.5" />
          )}
          添加所选 ({affectedCount})
        </button>
      </div>
    </Modal>
  )
}
