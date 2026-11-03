import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowRight, Clock3, Package, X, Upload, Loader2 } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { api, type SourcePackagePreview } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import type { FrontendExtension, FrontendSlotContextMap } from '@/extensions/types'

const button = 'inline-flex min-h-11 items-center justify-center gap-1.5 rounded-btn border border-border px-3 py-2 text-xs font-medium text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50 sm:min-h-9'

export function PackageInstaller(_context: FrontendSlotContextMap['settings.data-sources.toolbar']) {
  return <Link to="/settings?tab=import&kind=source"
    className="inline-flex min-h-8 shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-btn border border-accent/20 bg-accent/10 px-3 text-xs font-medium text-blue-700 hover:bg-accent/15 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent dark:text-blue-400">
    <Package size={14} aria-hidden="true" />安装插件
  </Link>
}

function PackageManagementLink({ plugin }: FrontendSlotContextMap['settings.data-sources.detail-actions']) {
  if (plugin.origin !== 'external') return null
  return <Link to={'/settings?tab=import&view=installed&selected=' + encodeURIComponent('source:' + plugin.name)} className="mt-4 inline-flex min-h-9 items-center gap-2 rounded-btn border border-border px-3 py-2 text-xs text-accent hover:bg-elevated">
    <Package size={14} aria-hidden="true" />管理插件版本与能力
  </Link>
}

export function InstallDialog({ onClose, onInstalled }: { onClose: () => void; onInstalled: (name: string) => void }) {
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<SourcePackagePreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [trusted, setTrusted] = useState(false)
  const matrix = useQuery({ queryKey: QK.capabilityMatrix, queryFn: api.capabilityMatrix })
  const labels = new Map(matrix.data?.capabilities.map(c => [c.id, c.label]) ?? [])
  async function inspect(candidate: File | undefined) {
    if (!candidate || busy) return
    setFile(null); setPreview(null); setTrusted(false); setError('')
    if (candidate.size > 20 * 1024 * 1024) { setError('插件包不能超过 20 MiB'); return }
    setBusy(true)
    try { setPreview(await api.inspectSourcePackage(candidate)); setFile(candidate) }
    catch (e) { setError(e instanceof Error ? e.message : '无法读取插件包') }
    finally { setBusy(false) }
  }
  async function install() {
    if (!file || !preview || !trusted || busy) return
    setBusy(true); setError('')
    try {
      const result = await api.installSourcePackage(file, preview.digest)
      toast(result.message, 'success'); onInstalled(result.name)
    } catch (e) { setError(e instanceof Error ? e.message : '安装失败') }
    finally { setBusy(false) }
  }
  return <Modal onClose={() => { if (!busy) onClose() }} labelledBy="source-package-title" closeOnBackdrop={!busy}
    panelClassName="w-[94vw] max-w-xl max-h-[85vh] overflow-y-auto text-foreground bg-surface border border-border rounded-card shadow-xl p-5 space-y-4">
    <div className="flex items-center justify-between gap-3">
      <h2 id="source-package-title" className="text-[16px] leading-6 font-semibold text-foreground">安装数据源插件</h2>
      <button className={button} aria-label="关闭" disabled={busy} onClick={onClose}><X size={16} /></button>
    </div>
    <p className="text-sm text-secondary">安装后会在数据源列表中显示卡片。配置 API Key 后，再选择需要启用的能力。</p>
    <label className="flex flex-col items-center gap-3 rounded-card border border-dashed border-border p-6 text-sm text-secondary"
      onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); void inspect(e.dataTransfer.files[0]) }}>
      <Upload size={24} /><span>选择或拖入 ZIP 插件包 · 最大 20 MiB</span>
      <input aria-label="选择插件包" type="file" accept=".zip" disabled={busy} onChange={e => { void inspect(e.target.files?.[0]); e.target.value = '' }} className="max-w-full text-xs" />
    </label>
    {busy && <p role="status" className="flex gap-2 text-sm"><Loader2 size={16} className="animate-spin" />正在处理，请稍候…</p>}
    {file && <p className="text-xs text-muted break-all">{file.name}</p>}
    {preview && <div className="rounded-card border border-border p-4 space-y-2 text-sm">
      <div className="font-semibold break-words">{preview.manifest.display_name || preview.manifest.name} <span className="font-mono text-muted">{preview.manifest.version}</span></div>
      {preview.manifest.author && <p className="text-secondary">作者声明：{preview.manifest.author}</p>}
      {preview.manifest.description && <MarkdownRenderer content={preview.manifest.description} compact />}
      <p>支持能力：{preview.manifest.datasets.map(d => labels.get(d) || d).join('、')}</p>
      {preview.installed_version && <p className="text-warning">当前 {preview.installed_version} → {preview.manifest.version}，升级重启后生效。</p>}
      {preview.errors.map((item, i) => <p key={i} className="text-danger">{item}</p>)}
      {!preview.errors.length && <p className="text-bear">静态兼容检查通过</p>}
    </div>}
    {error && <p role="alert" className="text-sm text-danger break-words">{error}</p>}
    <label className="flex items-start gap-2 text-xs text-secondary"><input type="checkbox" checked={trusted} disabled={busy || !preview} onChange={e => setTrusted(e.target.checked)} />
      <span>我信任此插件的来源。插件会在本机执行 Python 代码，可访问主程序的数据和凭据。</span></label>
    <div className="flex justify-end gap-2">
      <button className={button} disabled={busy} onClick={onClose}>取消</button>
      <button className={cn(button, 'bg-accent/10 text-accent')} disabled={busy || !preview || preview.errors.length > 0 || !trusted} onClick={() => void install()}>确认安装</button>
    </div>
  </Modal>
}

export function PackageActions({ plugin, refresh }: FrontendSlotContextMap['settings.data-sources.detail-actions']) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [confirm, setConfirm] = useState<string | null>(null)
  const [routing, setRouting] = useState(false)
  const matrix = useQuery({
    queryKey: QK.capabilityMatrix,
    queryFn: api.capabilityMatrix,
    enabled: plugin.origin === 'external' && !plugin.pending_remove,
    staleTime: 0,
  })
  const references = (matrix.data?.capabilities ?? []).filter(cap => cap.field && cap.current.toLowerCase() === plugin.name)
  const missingDefault = references.some(cap => !cap.candidates.some(candidate => candidate.name === cap.default))
  if (plugin.origin !== 'external') return null
  async function run(action: string) {
    if (busy) return
    setBusy(true); setError('')
    try {
      const result = await api.manageSourcePackage(plugin.name, action, action === 'remove')
      toast(result.message, 'success')
      setConfirm(null)
      await refresh()
    }
    catch (e) { setError(e instanceof Error ? e.message : '操作失败') }
    finally { setBusy(false) }
  }
  return <div className="space-y-4 text-xs leading-6">
    {plugin.pending_remove ? <div className="flex items-start gap-3 rounded-btn border border-warning/20 bg-warning/5 p-4">
      <Clock3 className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden="true" />
      <div className="space-y-1">
        <p className="font-medium text-foreground">卸载已安排</p>
        <p className="text-secondary">插件已停止接收新请求，重启主程序后删除插件文件。</p>
        <p className="text-secondary">已保存的 API Key 保留，重新安装后可继续使用。</p>
      </div>
    </div> : <>
    {references.length > 0 && <p className="text-secondary">当前分配的能力：<span className="text-foreground">{references.map(cap => cap.label).join('、')}</span></p>}
    {plugin.enabled === false && <p className="text-secondary">插件已停用，启用后可继续配置并选择数据能力。</p>}
    {plugin.update_error && <p className="text-danger">{plugin.update_error}</p>}
    {plugin.pending_version && <p className="text-amber-800 dark:text-amber-300">版本 {plugin.pending_version} 已暂存，重启后生效。</p>}
    <div className="flex flex-wrap items-center gap-2">
      {plugin.available && <button disabled={busy} className={cn(button, 'border-accent/25 text-blue-700 dark:text-blue-400')} onClick={() => setRouting(true)}>选择启用能力</button>}
      <button disabled={busy} className={button} onClick={() => void run(plugin.enabled === false ? 'enable' : 'disable')}>{plugin.enabled === false ? '启用插件' : '停用插件'}</button>
      {plugin.previous_version && <button disabled={busy} className={button} onClick={() => setConfirm('rollback')}>回退到 {plugin.previous_version}</button>}
      {plugin.pending_version && <button disabled={busy} className={button} onClick={() => void run('cancel-update')}>取消待生效更新</button>}
      <button disabled={busy} className={cn(button, 'border-danger/25 text-red-700 hover:text-red-800 dark:text-red-300 dark:hover:text-red-200')} onClick={() => { setError(''); setConfirm('remove'); void matrix.refetch() }}>卸载插件</button>
    </div>
    <p className="text-secondary">更新版本可在<Link to="/settings?tab=import&kind=source" className="mx-1 text-accent underline underline-offset-4">导入文件</Link>上传同名插件包。</p>
    </>}
    {error && !confirm && <p role="alert" className="break-words text-danger">{error}</p>}
    {routing && <RoutingDialog plugin={plugin} onClose={() => setRouting(false)} onSaved={() => { refresh(); setRouting(false) }} />}
    {confirm && <Modal onClose={() => { if (!busy) setConfirm(null) }} labelledBy="source-package-confirm-title" closeOnBackdrop={!busy}
      panelClassName="w-[92vw] max-w-lg max-h-[85vh] overflow-y-auto rounded-card border border-border bg-surface shadow-xl">
      <div className="space-y-4 p-5 text-sm text-foreground sm:p-6">
        <h3 id="source-package-confirm-title" className="text-lg font-semibold">{confirm === 'remove' ? '卸载 ' + plugin.display_name : '回退 ' + plugin.display_name}</h3>
        {confirm === 'remove' ? <>
          <p className="text-sm leading-6 text-secondary">确认后停止使用此插件，插件文件在下次重启主程序时删除。已保存的 API Key 保留。</p>
          {matrix.isFetching && <p role="status" className="flex items-center gap-2 text-xs text-secondary"><Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />正在检查受影响的能力…</p>}
          {matrix.isError && <div role="alert" className="space-y-2 text-xs text-danger"><p>无法读取当前能力分配，请重试后再卸载。</p><button type="button" className={button} disabled={matrix.isFetching} onClick={() => void matrix.refetch()}>重新读取能力</button></div>}
          {matrix.isSuccess && !matrix.isFetching && (references.length ? <div className="space-y-3">
            <p className="text-xs leading-6 text-secondary">以下能力会一并恢复默认来源，无需提前逐项切换。</p>
            <dl className="divide-y divide-border border-y border-border">
              {references.map(cap => <div key={cap.id} className="flex items-center justify-between gap-3 py-2.5 text-xs">
                <dt className="min-w-0 break-words">{cap.label}</dt>
                <dd className="flex shrink-0 items-center gap-2 text-secondary"><ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />{[...cap.candidates, ...cap.pending].find(candidate => candidate.name === cap.default)?.display || (cap.default === 'tickflow' ? 'TickFlow' : cap.default)}</dd>
              </div>)}
            </dl>
            {missingDefault && <p className="text-xs leading-6 text-amber-800 dark:text-amber-300">部分默认来源当前不可用，相应能力将暂停取数。可在数据源设置中配置或选择其他来源。</p>}
          </div> : <p className="text-xs text-secondary">此插件未分配给任何数据能力，可直接卸载。</p>)}
        </> : <p className="text-sm leading-6 text-secondary">重启后切换到上一版本，已保存的 Key 和能力路由保留。</p>}
        {error && <p role="alert" className="break-words text-xs text-danger">{error}</p>}
        <div className="flex flex-wrap justify-end gap-2 border-t border-border pt-4">
          <button className={button} disabled={busy} onClick={() => setConfirm(null)}>取消</button>
          <button className={cn(button, confirm === 'remove' && 'border-danger/30 bg-danger/10 text-red-700 hover:text-red-800 dark:text-red-300 dark:hover:text-red-200')}
            disabled={busy || (confirm === 'remove' && (!matrix.isSuccess || matrix.isFetching))}
            onClick={() => void run(confirm)}>{busy ? '处理中…' : confirm === 'remove' ? '确认卸载' : '确认回退'}</button>
        </div>
      </div>
    </Modal>}
  </div>
}

function RoutingDialog({ plugin, onClose, onSaved }: { plugin: FrontendSlotContextMap['settings.data-sources.detail-actions']['plugin']; onClose: () => void; onSaved: () => void }) {
  const matrix = useQuery({ queryKey: QK.capabilityMatrix, queryFn: api.capabilityMatrix })
  const [selected, setSelected] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [tests, setTests] = useState<Record<string, string>>({})
  const caps = (matrix.data?.capabilities ?? []).filter(c => c.field && c.candidates.some(source => source.name === plugin.name))
  async function save() {
    setBusy(true); setError('')
    try {
      const changes = Object.fromEntries(caps.filter(c => selected.includes(c.id)).map(c => [c.field!, plugin.name]))
      await api.updateDataProviders(changes)
      toast('已更新所选能力的数据源', 'success'); onSaved()
    } catch (e) { setError(e instanceof Error ? e.message : '切换失败') }
    finally { setBusy(false) }
  }
  async function test(id: string) {
    setBusy(true)
    try {
      const result = await api.testDataSource(plugin.name, id, ['600000.SH'])
      setTests(previous => ({ ...previous, [id]: result.error || `试拉返回 ${result.rows} 行（不代表所有接口权限）` }))
    } catch (e) { setTests(previous => ({ ...previous, [id]: e instanceof Error ? e.message : '试拉失败' })) }
    finally { setBusy(false) }
  }
  return <Modal onClose={() => { if (!busy) onClose() }} ariaLabel="选择启用能力" closeOnBackdrop={!busy}>
    <div className="p-5 space-y-4 text-foreground max-h-[80vh] overflow-y-auto">
      <h3 className="text-[16px] leading-6 font-semibold text-foreground">选择启用能力</h3>
      <p className="text-xs text-secondary">仅修改勾选项，其他能力保留原数据源。试拉可能消耗供应商额度。</p>
      {matrix.isLoading && <p>正在读取能力…</p>}
      {matrix.error && <p role="alert">能力列表读取失败，请关闭后重试。</p>}
      {caps.map(cap => <div key={cap.id} className="rounded border border-border p-3 space-y-2 text-xs">
        <label className="flex items-center gap-2"><input type="checkbox" disabled={busy} checked={selected.includes(cap.id)} onChange={e => setSelected(prev => e.target.checked ? [...prev, cap.id] : prev.filter(id => id !== cap.id))} />
          {cap.label}：{cap.current_display} → {plugin.display_name}</label>
        {plugin.can_test ? <button className={button} disabled={busy} onClick={() => void test(cap.id)}>试拉{cap.label}</button> : <p className="text-muted">此插件未提供能力试拉；Key 验证不代表接口权限已确认。</p>}
        {tests[cap.id] && <p role="status">{tests[cap.id]}</p>}
      </div>)}
      {error && <p role="alert" className="text-danger text-xs">{error}</p>}
      <div className="flex justify-end gap-2"><button className={button} disabled={busy} onClick={onClose}>取消</button><button className={button} disabled={busy || !selected.length} onClick={() => void save()}>确认切换所选能力</button></div>
    </div>
  </Modal>
}

export default {
  id: 'official.source-packages', apiVersion: 1,
  slots: [
    { name: 'settings.data-sources.toolbar', id: 'install', component: PackageInstaller },
    { name: 'settings.data-sources.detail-actions', id: 'manage', component: PackageManagementLink },
  ],
} satisfies FrontendExtension
