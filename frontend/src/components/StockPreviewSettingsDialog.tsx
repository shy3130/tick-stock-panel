import { useId, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ChevronRight, X } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { api, type Preferences } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

export function StockPreviewSettingsDialog({ dailyVisibleBars, onClose, onCustomizeInfoBar }: {
  dailyVisibleBars: number
  onClose: () => void
  onCustomizeInfoBar: () => void
}) {
  const fieldId = useId()
  const inputRef = useRef<HTMLInputElement>(null)
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState(String(dailyVisibleBars))
  const bars = Number(draft)
  const valid = Number.isSafeInteger(bars) && bars > 0
  const save = useMutation({
    mutationFn: api.updateStockPreviewDailyBars,
    onSuccess: async (updated) => {
      await queryClient.cancelQueries({ queryKey: QK.preferences, exact: true })
      queryClient.setQueryData<Preferences>(QK.preferences, current =>
        current ? { ...current, ...updated } : current)
      void queryClient.invalidateQueries({ queryKey: QK.preferences, exact: true })
      onClose()
    },
  })
  const close = () => { if (!save.isPending) onClose() }

  return (
    <Modal ariaLabel="默认配置" onClose={close} initialFocusRef={inputRef}
      overlayClassName="fixed inset-0 z-[60] flex items-center justify-center bg-black/40"
      panelClassName="w-[92vw] max-w-xs rounded-card border border-border bg-surface shadow-xl">
      <header className="flex items-center justify-between border-b border-border/60 px-4 py-3">
        <h2 className="text-sm font-semibold text-foreground">默认配置</h2>
        <button type="button" aria-label="关闭默认配置" onClick={close} disabled={save.isPending}
          className="rounded-btn p-1 text-secondary hover:bg-elevated hover:text-foreground disabled:opacity-50">
          <X className="h-4 w-4" />
        </button>
      </header>
      <form className="space-y-4 p-4" onSubmit={event => {
        event.preventDefault()
        if (valid && !save.isPending) save.mutate(bars)
      }}>
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
          <label htmlFor={fieldId} className="text-secondary">日 K 首屏根数</label>
          <div className="flex items-center gap-2">
            <input ref={inputRef} id={fieldId} type="number" min={1} step={1} required value={draft}
              aria-invalid={!valid} disabled={save.isPending}
              onChange={event => {
                setDraft(event.target.value)
                if (save.isError) save.reset()
              }}
              className="h-8 w-20 rounded-input border border-border bg-elevated px-2 font-mono text-sm text-foreground focus:border-accent focus:outline-none" />
            <span className="text-muted">根</span>
          </div>
        </div>
        {!valid && <p role="alert" className="text-xs text-danger">请输入有效的正整数。</p>}
        {save.isError && <p role="alert" className="text-xs text-danger">{save.error.message || '保存失败，请重试。'}</p>}
        <div className="flex justify-end gap-2 text-xs">
          <button type="button" onClick={close} disabled={save.isPending}
            className="rounded-btn px-3 py-2 text-secondary hover:bg-elevated disabled:opacity-50">取消</button>
          <button type="submit" disabled={!valid || save.isPending}
            className="rounded-btn bg-accent px-3 py-2 font-medium text-white disabled:opacity-50">
            {save.isPending ? '保存中…' : '保存'}
          </button>
        </div>
      </form>
      <button
        type="button"
        onClick={onCustomizeInfoBar}
        disabled={save.isPending}
        aria-haspopup="dialog"
        className="flex w-full items-center justify-between border-t border-border/60 px-4 py-3 text-left text-xs text-secondary hover:bg-elevated hover:text-foreground disabled:opacity-50"
      >
        自定义信息条 <ChevronRight className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
      </button>
    </Modal>
  )
}
