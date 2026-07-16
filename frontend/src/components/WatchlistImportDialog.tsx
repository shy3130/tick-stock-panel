import { useCallback, useEffect, useRef, useState } from 'react'
import { ImagePlus, Loader2, Upload, X } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { api, type WatchlistImportCandidate } from '@/lib/api'
import { useWatchlistBatchAdd } from '@/lib/useSharedMutations'

interface Props {
  open: boolean
  onClose: () => void
}

export function WatchlistImportDialog({ open, onClose }: Props) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [provider, setProvider] = useState<string>('')
  const [candidates, setCandidates] = useState<WatchlistImportCandidate[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const batchAdd = useWatchlistBatchAdd()

  const reset = useCallback(() => {
    setBusy(false)
    setCandidates([])
    setSelected(new Set())
    setProvider('')
    if (previewUrl) URL.revokeObjectURL(previewUrl)
    setPreviewUrl(null)
    if (inputRef.current) inputRef.current.value = ''
  }, [previewUrl])

  useEffect(() => {
    if (!open) reset()
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  const runRecognize = async (file: File) => {
    if (!file.type.startsWith('image/') && !/\.(jpe?g|png|webp|bmp|gif)$/i.test(file.name)) {
      toast('请选择图片文件', 'error')
      return
    }
    if (previewUrl) URL.revokeObjectURL(previewUrl)
    setPreviewUrl(URL.createObjectURL(file))
    setBusy(true)
    setCandidates([])
    setSelected(new Set())
    try {
      const res = await api.watchlistImportImage(file)
      setProvider(res.provider)
      setCandidates(res.candidates)
      const defaults = new Set(
        res.candidates
          .filter(c => c.matched && c.symbol && !c.already_in_watchlist)
          .map(c => c.symbol!),
      )
      setSelected(defaults)
      if (res.candidates.length === 0) {
        toast('未识别到股票代码，请换一张更清晰的自选列表截图', 'error')
      } else if (res.matched_count === 0) {
        toast('识别到代码但未能匹配证券主数据', 'error')
      }
    } catch {
      /* toast already in request() */
    } finally {
      setBusy(false)
    }
  }

  const onPick = (file: File | undefined | null) => {
    if (file) void runRecognize(file)
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
  const selectable = matched.filter(c => !c.already_in_watchlist)
  const allSelected = selectable.length > 0 && selectable.every(c => selected.has(c.symbol!))

  const toggleAll = () => {
    if (allSelected) setSelected(new Set())
    else setSelected(new Set(selectable.map(c => c.symbol!)))
  }

  const confirmAdd = async () => {
    const symbols = [...selected]
    if (symbols.length === 0) {
      toast('请至少选择一只股票', 'error')
      return
    }
    try {
      await batchAdd.mutateAsync(symbols)
      toast(`已添加 ${symbols.length} 只自选`, 'success')
      onClose()
    } catch {
      /* toast in request */
    }
  }

  if (!open) return null

  return (
    <Modal
      onClose={onClose}
      labelledBy="watchlist-import-title"
      panelClassName="w-[92vw] max-w-lg max-h-[85vh] flex flex-col bg-surface border border-border rounded-card shadow-xl"
    >
      <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
        <div>
          <h2 id="watchlist-import-title" className="text-sm font-semibold text-foreground">
            从截图导入自选
          </h2>
          <p className="text-[11px] text-muted mt-0.5">
            上传券商自选列表截图，识别代码后确认添加
            {provider ? ` · ${provider}` : ''}
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

      <div className="px-4 py-3 overflow-y-auto flex-1 space-y-3">
        <input
          ref={inputRef}
          type="file"
          accept="image/jpeg,image/png,image/webp,image/bmp,image/gif,.jpg,.jpeg,.png"
          className="hidden"
          onChange={e => onPick(e.target.files?.[0])}
        />

        <button
          type="button"
          disabled={busy}
          onClick={() => inputRef.current?.click()}
          onDragOver={e => { e.preventDefault(); e.stopPropagation() }}
          onDrop={e => {
            e.preventDefault()
            onPick(e.dataTransfer.files?.[0])
          }}
          className="w-full flex flex-col items-center justify-center gap-2 rounded-btn border border-dashed border-border bg-elevated/40 hover:bg-elevated/70 px-4 py-6 text-secondary transition-colors disabled:opacity-50"
        >
          {busy ? (
            <Loader2 className="h-6 w-6 animate-spin text-accent" />
          ) : (
            <ImagePlus className="h-6 w-6 text-accent" />
          )}
          <span className="text-xs">
            {busy ? '识别中…' : '点击选择或拖拽截图到此处'}
          </span>
        </button>

        {previewUrl && (
          <div className="rounded-btn overflow-hidden border border-border bg-black/40 max-h-40">
            <img src={previewUrl} alt="预览" className="w-full h-full object-contain max-h-40" />
          </div>
        )}

        {candidates.length > 0 && (
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-xs text-secondary">
                识别 {candidates.length} 个代码 · 匹配 {matched.length} · 已选 {selected.size}
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
                const disabled = !c.matched || !c.symbol
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
                          <span className="text-[10px] text-muted">已在自选</span>
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
          disabled={selected.size === 0 || batchAdd.isPending || busy}
          onClick={() => void confirmAdd()}
          className="h-8 px-3 rounded-btn text-xs inline-flex items-center gap-1.5 bg-accent text-white hover:bg-accent/90 disabled:opacity-40"
        >
          {batchAdd.isPending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Upload className="h-3.5 w-3.5" />
          )}
          添加所选 ({selected.size})
        </button>
      </div>
    </Modal>
  )
}
