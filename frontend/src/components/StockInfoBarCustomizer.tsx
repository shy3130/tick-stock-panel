import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ListColumnCustomizer } from '@/components/ListColumnCustomizer'
import { api, type Preferences } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { INFO_GROUPS, serializeInfoFields, type StockInfoBarLayout } from '@/lib/stock-info-fields'
import { useStockInfoBarLayout } from '@/lib/useStockInfoBarLayout'

export function StockInfoBarCustomizer({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient()
  const saved = useStockInfoBarLayout()
  const [draft, setDraft] = useState<StockInfoBarLayout | null>(null)
  const layout = draft ?? saved
  const save = useMutation({
    mutationFn: (next: StockInfoBarLayout) => api.updateStockInfoBarLayout(next),
    onSuccess: async (updated) => {
      await queryClient.cancelQueries({ queryKey: QK.preferences, exact: true })
      queryClient.setQueryData<Preferences>(QK.preferences, current => current ? { ...current, ...updated } : current)
      void queryClient.invalidateQueries({ queryKey: QK.preferences, exact: true })
      onClose()
    },
  })

  const close = () => { if (!save.isPending) onClose() }
  const updateDraft = (next: StockInfoBarLayout) => { if (!save.isPending) setDraft(next) }

  useEffect(() => {
    const onEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.stopPropagation()
      if (!save.isPending) onClose()
    }
    document.addEventListener('keydown', onEscape, true)
    return () => document.removeEventListener('keydown', onEscape, true)
  }, [onClose, save.isPending])

  return (
    <ListColumnCustomizer
      open
      columns={layout.fields}
      groups={INFO_GROUPS}
      onChange={fields => updateDraft({ ...layout, fields })}
      onClose={close}
      title="信息条指标"
      builtinSectionLabel="可选指标"
      extColumnAlign="left"
      showStandaloneToggle
      disableBackdropBlur
      submitting={save.isPending}
      baselineStore={{
        get: () => layout.baseline,
        set: baseline => updateDraft({ ...layout, baseline: serializeInfoFields(baseline) }),
      }}
      footer={
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-border px-4 py-2.5 text-xs">
          {save.isError && <p role="alert" className="mr-auto text-danger">{save.error.message || '保存失败，请重试。'}</p>}
          <button type="button" onClick={close} disabled={save.isPending}
            className="rounded-btn px-3 py-2 text-secondary hover:bg-elevated disabled:opacity-50">取消</button>
          <button type="button" disabled={!draft || save.isPending}
            className="rounded-btn bg-accent px-3 py-2 font-medium text-white disabled:opacity-50"
            onClick={() => {
              if (draft) save.mutate({ ...draft, fields: serializeInfoFields(draft.fields) })
            }}>
            {save.isPending ? '保存中…' : '保存'}
          </button>
        </div>
      }
    />
  )
}
