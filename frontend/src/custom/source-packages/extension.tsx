import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Package, X, Upload, Loader2 } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { api, type SourcePackagePreview } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import type { FrontendExtension, FrontendSlotContextMap } from '@/extensions/types'

const button = 'inline-flex items-center justify-center gap-1.5 rounded-btn border border-border px-3 py-1.5 text-xs hover:bg-elevated disabled:opacity-50'

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
      <p className="text-secondary break-words">{preview.manifest.description}</p>
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
      <button className={`${button} bg-accent/10 text-accent`} disabled={busy || !preview || preview.errors.length > 0 || !trusted} onClick={() => void install()}>确认安装</button>
    </div>
  </Modal>
}

export function PackageActions({ plugin, refresh }: FrontendSlotContextMap['settings.data-sources.detail-actions']) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [confirm, setConfirm] = useState<string | null>(null)
  const [routing, setRouting] = useState(false)
  if (plugin.origin !== 'external') return null
  async function run(action: string) {
    setBusy(true); setError('')
    try { const result = await api.manageSourcePackage(plugin.name, action); toast(result.message, 'success'); refresh(); setConfirm(null) }
    catch (e) { setError(e instanceof Error ? e.message : '操作失败') }
    finally { setBusy(false) }
  }
  return <div className="mt-4 rounded-card bg-elevated/40 p-3 text-xs space-y-3">
    <p>外部安装 · 版本 {plugin.version} · {plugin.status}</p>
    {plugin.update_error && <p className="text-danger">{plugin.update_error}</p>}
    {plugin.pending_version && <p className="text-warning">版本 {plugin.pending_version} 已暂存，重启后生效。</p>}
    {plugin.pending_remove ? <p className="text-warning">重启后移除插件文件；已保存的 Key 保留。</p> : <div className="flex flex-wrap gap-2">
      {plugin.available && <button className={`${button} text-accent`} onClick={() => setRouting(true)}>选择启用能力</button>}
      <button disabled={busy} className={button} onClick={() => void run(plugin.enabled === false ? 'enable' : 'disable')}>{plugin.enabled === false ? '启用插件' : '停用插件'}</button>
      {plugin.previous_version && <button disabled={busy} className={button} onClick={() => setConfirm('rollback')}>回退到 {plugin.previous_version}</button>}
      {plugin.pending_version && <button disabled={busy} className={button} onClick={() => void run('cancel-update')}>取消待生效更新</button>}
      <button disabled={busy} className={`${button} text-danger`} onClick={() => setConfirm('remove')}>卸载插件</button>
      <span className="self-center text-muted">更新版本：在“导入文件”上传新包</span>
    </div>}
    {error && <p role="alert" className="text-danger">{error}</p>}
    {routing && <RoutingDialog plugin={plugin} onClose={() => setRouting(false)} onSaved={() => { refresh(); setRouting(false) }} />}
    {confirm && <Modal onClose={() => { if (!busy) setConfirm(null) }} ariaLabel="确认插件操作" closeOnBackdrop={!busy}>
      <div className="p-5 space-y-4 text-sm text-foreground">
        <p>{confirm === 'remove' ? '卸载前需要先切换正在使用它的能力。插件文件重启后删除，Key 保留，可在 Key 配置中单独清除。' : '重启后切换到上一版本，已保存的 Key 和能力路由保留。'}</p>
        {error && <p role="alert" className="text-danger">{error}</p>}
        <div className="flex justify-end gap-2"><button className={button} disabled={busy} onClick={() => setConfirm(null)}>取消</button><button className={button} disabled={busy} onClick={() => void run(confirm)}>确认</button></div>
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
