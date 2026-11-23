import { useState } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ListChecks, X } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { api, type WatchlistEntry, type WatchlistGroup } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

interface Props {
  source: string
  groups: WatchlistGroup[]
  entries: WatchlistEntry[]
}

export function WatchlistGroupTransfer(props: Props) {
  const [open, setOpen] = useState(false)
  return <>
    <button type="button" onClick={() => setOpen(true)} title="批量分组" aria-haspopup="dialog"
      className="ml-2 inline-flex shrink-0 items-center gap-1 rounded-btn px-2 text-xs text-secondary hover:bg-elevated hover:text-foreground">
      <ListChecks aria-hidden="true" className="h-4 w-4" />批量分组
    </button>
    {open && createPortal(<TransferDialog {...props} onClose={() => setOpen(false)} />, document.body)}
  </>
}

function TransferDialog({ source, groups, entries, onClose }: Props & { onClose: () => void }) {
  const qc = useQueryClient()
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const [targetId, setTargetId] = useState('')
  const members = entries.filter(entry => source === 'all' || (source === 'ungrouped'
    ? !entry.group_ids?.length : entry.group_ids?.includes(source)))
  const search = query.trim().toLowerCase()
  const visible = members.filter(e => e.symbol.toLowerCase().includes(search) || e.name?.toLowerCase().includes(search))
  const symbols = members.filter(e => selected.has(e.symbol)).map(e => e.symbol)
  const allVisible = visible.length > 0 && visible.every(e => selected.has(e.symbol))
  const someVisible = visible.some(e => selected.has(e.symbol))
  const targets = groups.filter(g => g.id !== source)
  const target = targets.find(g => g.id === targetId)
  const sourceName = source === 'all' ? '全部自选' : source === 'ungrouped' ? '未分组' : groups.find(g => g.id === source)?.name

  const transfer = useMutation({
    mutationFn: (input: { action: 'move' | 'copy'; target: WatchlistGroup; symbols: string[] }) =>
      api.watchlistGroupTransfer(source, input.target.id, input.symbols, input.action),
    onSuccess: (data, input) => {
      qc.setQueryData(QK.watchlist, { symbols: data.symbols })
      void qc.invalidateQueries({ queryKey: QK.watchlist })
      const skipped = input.symbols.length - data.changed
      const verb = input.action === 'move' ? '移动' : '复制'
      toast(data.changed === 0 ? `所选标的已在「${input.target.name}」中`
        : `已${verb} ${data.changed} 只到「${input.target.name}」${skipped ? `，${skipped} 只已在组内` : ''}`, 'success')
      onClose()
    },
    onError: () => {
      void qc.invalidateQueries({ queryKey: QK.watchlist })
      void qc.invalidateQueries({ queryKey: QK.watchlistGroups })
    },
  })
  const pending = transfer.isPending
  const canSubmit = !!target && symbols.length > 0 && !pending
  function toggle(items: WatchlistEntry[], checked: boolean) {
    setSelected(prev => {
      const next = new Set(prev)
      for (const { symbol } of items) {
        if (checked) next.add(symbol)
        else next.delete(symbol)
      }
      return next
    })
  }

  return <Modal ariaLabel="批量分组" onClose={() => { if (!pending) onClose() }}
    panelClassName="flex max-h-[90dvh] w-[92vw] max-w-md flex-col overflow-y-auto rounded-card border border-border bg-surface shadow-xl">
    <header className="flex items-start justify-between gap-2 border-b border-border px-4 py-3">
      <div className="min-w-0">
        <h2 className="text-[16px] font-semibold text-foreground">批量分组</h2>
        <p className="mt-1 break-words text-xs text-secondary">从「{sourceName}」选择标的</p>
      </div>
      <button type="button" aria-label="关闭" disabled={pending} onClick={onClose}
        className="grid h-9 w-9 shrink-0 place-items-center rounded-btn text-secondary hover:bg-elevated">
        <X className="h-4 w-4" />
      </button>
    </header>
    <div className="space-y-3 px-4 py-3">
      <input type="search" aria-label="搜索分组标的" placeholder="搜索名称或代码" value={query}
        disabled={pending} onChange={e => setQuery(e.target.value)}
        className="h-10 w-full rounded-btn border border-border bg-base px-3 text-sm text-foreground" />
      <div className="overflow-hidden rounded-btn border border-border">
        <div className="flex items-center justify-between gap-2 border-b border-border bg-elevated px-3 text-xs">
          <label className="flex min-h-10 cursor-pointer items-center gap-2 text-secondary">
            <input type="checkbox" className="accent-accent" checked={allVisible} disabled={!visible.length || pending}
              ref={el => { if (el) el.indeterminate = someVisible && !allVisible }}
              onChange={e => toggle(visible, e.target.checked)} />
            {search ? '全选搜索结果' : '全选'}
          </label>
          <span role="status" className="text-secondary">已选 {symbols.length} 只</span>
        </div>
        <div className="max-h-[min(32dvh,18rem)] overflow-y-auto overscroll-contain">
          {visible.map(entry => <label key={entry.symbol}
            className="flex min-h-11 cursor-pointer items-center gap-2 px-3 text-sm hover:bg-elevated">
            <input type="checkbox" className="accent-accent" checked={selected.has(entry.symbol)} disabled={pending}
              onChange={e => toggle([entry], e.target.checked)} />
            {entry.name && <span className="truncate text-foreground">{entry.name}</span>}
            <span className="ml-auto shrink-0 font-mono text-xs text-secondary">{entry.symbol}</span>
          </label>)}
          {!visible.length && <p className="px-3 py-4 text-center text-sm text-secondary">{members.length ? '没有匹配的标的' : '当前分组暂无标的'}</p>}
        </div>
      </div>
      <label className="block text-sm text-secondary">目标分组
        <select value={target?.id ?? ''} disabled={!targets.length || pending} onChange={e => setTargetId(e.target.value)}
          className="mt-1 block h-10 w-full rounded-btn border border-border bg-base px-2 text-foreground">
          <option value="">请选择目标分组</option>
          {targets.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}
        </select>
      </label>
      {!targets.length && <p className="text-xs text-secondary">请先在「管理自选分组」中新建目标分组。</p>}
      {transfer.isError && <p role="alert" className="text-sm text-danger">{transfer.error.message}</p>}
    </div>
    <footer className="flex flex-wrap justify-end gap-2 border-t border-border px-4 py-3">
      {source !== 'ungrouped' && <button type="button" disabled={!canSubmit} onClick={() => target && transfer.mutate({ action: 'copy', target, symbols })}
        className="min-h-10 rounded-btn border border-border bg-elevated px-4 text-sm text-foreground disabled:opacity-50">
        {pending && transfer.variables?.action === 'copy' ? '复制中…' : '复制到目标组'}
      </button>}
      {source !== 'all' && <button type="button" disabled={!canSubmit} onClick={() => target && transfer.mutate({ action: 'move', target, symbols })}
        className="min-h-10 rounded-btn bg-accent px-4 text-sm text-white hover:bg-accent/90 disabled:opacity-50">
        {pending && transfer.variables?.action === 'move' ? '移动中…' : '移动到目标组'}
      </button>}
    </footer>
  </Modal>
}
