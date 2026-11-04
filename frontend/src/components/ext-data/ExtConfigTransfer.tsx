import { useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, Download, FileArchive, Loader2, Upload, X } from 'lucide-react'
import { api, type ExtDataConfigPreview } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'

const BUTTON = 'inline-flex min-h-9 items-center justify-center gap-1.5 rounded-btn border border-border bg-surface px-3 text-sm text-secondary transition-colors hover:text-foreground hover:bg-elevated focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50'

export function ExtConfigExportButton({ id, label }: { id: string; label: string }) {
  const [busy, setBusy] = useState(false)
  const exportConfig = async () => {
    if (busy) return
    setBusy(true)
    try {
      const blob = await api.extDataConfigExport(id)
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `tsp-ext-data-${id}.zip`
      document.body.append(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      toast('配置已导出，不含 API Key 和历史数据', 'success')
    } catch (error) {
      toast(error instanceof Error ? error.message : '配置导出失败，请重试', 'error')
    } finally { setBusy(false) }
  }
  return <button type="button" onClick={exportConfig} disabled={busy}
    aria-label={`导出${label}配置`} title="导出单表配置（不含 Key 和数据）"
    className="inline-flex h-7 shrink-0 items-center gap-1 rounded-lg px-2.5 text-[10px] text-muted transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50">
    {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Download className="h-3 w-3" />}
    {busy ? '导出中…' : '导出配置'}
  </button>
}

export function ExtConfigImportDialog({ onClose }: { onClose: () => void }) {
  const client = useQueryClient()
  const input = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<ExtDataConfigPreview | null>(null)
  const [id, setId] = useState('')
  const [busy, setBusy] = useState<'inspect' | 'import' | null>(null)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)
  const validId = /^[a-zA-Z0-9_]{1,64}$/.test(id)
  const close = () => { if (!busy) onClose() }

  const inspect = async (next: File) => {
    if (busy) return
    setFile(next); setPreview(null); setDone(false); setError('')
    if (next.size > 4 * 1024 * 1024) {
      setError('配置包超过 4 MiB，请选择单个扩展数据配置 ZIP。')
      return
    }
    setBusy('inspect')
    try {
      const nextPreview = await api.extDataConfigInspect(next)
      setPreview(nextPreview)
      setId(nextPreview.conflict ? `${nextPreview.id.slice(0, 59)}_copy` : nextPreview.id)
    } catch (err) { setError(err instanceof Error ? err.message : '配置包解析失败，请重新选择') }
    finally { setBusy(null) }
  }

  const importConfig = async () => {
    if (!file || !preview || !validId || busy) return
    setBusy('import'); setError('')
    try {
      await api.extDataConfigImport(file, id)
      void Promise.all([
        client.invalidateQueries({ queryKey: QK.extData }),
        client.invalidateQueries({ queryKey: QK.extDataSchemaAll }),
        client.invalidateQueries({ queryKey: QK.extSchemaAll }),
        client.invalidateQueries({ queryKey: QK.dataStatus }),
      ])
      setDone(true)
    } catch (err) { setError(err instanceof Error ? err.message : '配置导入失败，请重试') }
    finally { setBusy(null) }
  }

  return <Modal onClose={close} labelledBy="ext-config-import-title" closeOnBackdrop={!busy}
    panelClassName="flex max-h-[85vh] w-[92vw] max-w-lg flex-col overflow-hidden rounded-card border border-border bg-surface shadow-xl">
    <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
      <div>
        <h2 id="ext-config-import-title" className="text-base font-semibold text-foreground">导入扩展数据配置</h2>
        <p className="mt-1 text-xs leading-5 text-secondary">一次导入一张表的配置，API Key 由接收方自行填写。</p>
      </div>
      <button type="button" aria-label="关闭" onClick={close} disabled={!!busy}
        className="shrink-0 rounded-md p-1 text-secondary hover:bg-elevated focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50"><X className="h-4 w-4" /></button>
    </div>
    <div className="space-y-4 overflow-y-auto px-5 py-4">
      {done ? <div role="status" className="space-y-3 text-sm leading-6 text-secondary">
        <div className="flex items-center gap-2 font-medium text-foreground"><CheckCircle2 className="h-5 w-5 text-emerald-600 dark:text-emerald-400" />导入成功</div>
        <p>「{preview?.label}」已添加到数据卡片，标识为 <code className="break-all">{id}</code>。</p>
        {preview?.has_pull ? <p>{preview.requires_key ? '打开该卡片的设置，在“拉取”中填写自己的 API Key 并测试。' : '打开该卡片的设置，测试拉取接口。'}定时拉取默认关闭，确认后再启用。</p>
          : <p>可在该卡片中上传数据或配置拉取接口。</p>}
      </div> : <>
        <input ref={input} type="file" accept=".zip,application/zip" className="hidden" aria-label="选择扩展配置包"
          onChange={event => { const next = event.target.files?.[0]; event.target.value = ''; if (next) void inspect(next) }} />
        <button type="button" onClick={() => input.current?.click()} disabled={!!busy}
          className={`${BUTTON} w-full justify-start gap-3 py-3`}>
          {busy === 'inspect' ? <Loader2 className="h-5 w-5 shrink-0 animate-spin" /> : <FileArchive className="h-5 w-5 shrink-0" />}
          <span className="min-w-0 text-left"><span className="block truncate">{file?.name ?? '选择单表配置 ZIP'}</span><span className="mt-0.5 block text-xs text-secondary">不包含历史数据；最大 4 MiB</span></span>
          <Upload className="ml-auto h-4 w-4 shrink-0" />
        </button>
        {preview && <div className="space-y-3">
          <div className="flex flex-wrap items-baseline justify-between gap-2"><h3 className="break-words text-sm font-medium text-foreground">{preview.label}</h3><span className="text-xs text-secondary">{preview.mode === 'timeseries' ? '时序表' : '快照表'} · {preview.field_count} 个字段</span></div>
          <p className="break-words text-xs leading-5 text-secondary">{preview.fields.slice(0, 12).map(field => field.label || field.name).join('、')}{preview.fields.length > 12 ? '…' : ''}</p>
          {preview.pull_url && <p className="space-y-1 text-xs leading-5 text-secondary"><span className="block">拉取接口</span><code className="block break-all text-foreground">{preview.pull_method ?? 'GET'} {preview.pull_url}</code></p>}
          <label className="block space-y-1.5 text-sm text-foreground">导入标识
            <input name="config_id" value={id} onChange={event => setId(event.target.value)} maxLength={64} disabled={!!busy}
              aria-invalid={!validId} aria-describedby="ext-config-id-hint"
              className="h-9 w-full rounded-btn border border-border bg-base px-3 text-sm text-foreground outline-none focus:border-accent focus:ring-1 focus:ring-accent" />
          </label>
          <p id="ext-config-id-hint" className="text-xs leading-5 text-secondary">{preview.conflict ? '已有同名配置，已建议新标识。' : ''}仅限英文字母、数字和下划线；不会覆盖现有配置。</p>
          <p className="text-xs leading-5 text-secondary">{preview.requires_key ? '导入后需填写自己的 API Key。' : '此配置未声明 API Key 鉴权。'}{preview.has_pull ? '定时拉取先保持关闭。' : ''}</p>
          {preview.warnings.length > 0 && <ul className="space-y-1 text-xs leading-5 text-amber-800 dark:text-amber-300">{preview.warnings.map(note => <li key={note}>{note}</li>)}</ul>}
        </div>}
        {error && <p role="alert" className="break-words text-sm leading-5 text-danger">{error}</p>}
      </>}
    </div>
    <div className="flex justify-end gap-2 border-t border-border px-5 py-3">
      <button type="button" className={BUTTON} onClick={close} disabled={!!busy}>{done ? '完成' : '取消'}</button>
      {!done && <button type="button" className={cn(BUTTON, 'border-blue-600 bg-blue-600 text-white hover:bg-blue-700 hover:text-white')}
        disabled={!preview || !validId || !!busy} onClick={() => void importConfig()}>
        {busy === 'import' && <Loader2 className="h-4 w-4 animate-spin" />}{busy === 'import' ? '导入中…' : '导入配置'}
      </button>}
    </div>
  </Modal>
}
