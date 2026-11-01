import { useMemo, useRef, useState, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ArrowRight, Check, CheckCircle2, Clock3, Download, FileCode2, FolderOpen, Info, Loader2, Package, Plus, RefreshCw, Server, ShieldAlert, Upload } from 'lucide-react'
import { api, type ImportCenterPreview, type ManagedExtensionPackage, type PluginDataSourceItem, type ResourceKind, type RuntimePagePack } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { resourceClosure } from '@/lib/resourceTransfer'
import { inspectPageBlob, inspectPageModule, refreshResourceQueries } from '@/lib/importCenter'
import { runtimeBundleUrl } from '@/extensions/runtime'
import { getFrontendExtensionLoadErrors } from '@/extensions/registry'
import { ResourceTransferDialog } from '@/components/ResourceTransferDialog'
import { PackageActions } from '@/custom/source-packages/extension'
import { PluginKeyConfig } from './DataSources'
import { INVALIDATE_KEYS, SettingsBackupPanel } from './Backup'

const BUTTON = 'inline-flex min-h-11 items-center justify-center gap-1.5 rounded-btn border border-border bg-surface px-3 py-2 text-xs font-medium text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-45 sm:min-h-9'
const PRIMARY = cn(BUTTON, 'border-blue-600 bg-blue-600 text-white hover:bg-blue-700 hover:text-white')
const INPUT = 'min-h-11 w-full min-w-0 rounded-input border border-border bg-base px-3 py-2 text-base text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent sm:min-h-9 sm:text-sm'
const KIND = { strategy: '策略', factor: '因子', signal: '信号', settings: '调参', code: '辅助模块' }
const TYPE = { resources: '策略与定义资源', backup: '配置备份', source: '数据源插件', extension: '后端扩展', page: '页面插件', project: '项目源码页面' }
type Review = ImportCenterPreview | { kind: 'project'; preview: { namespace: string } }
type Result = { title: string; message: string; details: string[]; target?: string; path?: string }

function Notice({ children, warning = false }: { children: ReactNode; warning?: boolean }) {
  return <div className={cn('flex items-start gap-2 rounded-btn px-3 py-3 text-xs leading-6', warning ? 'bg-warning/10 text-amber-800 dark:text-amber-300' : 'bg-accent/10 text-blue-800 dark:text-blue-300')}>
    <Info className="mt-1 h-3.5 w-3.5 shrink-0" aria-hidden="true" /><div className="min-w-0 break-words">{children}</div>
  </div>
}

function Status({ children }: { children: string }) {
  return <span className={cn('inline-flex max-w-full rounded px-2 py-1 text-[11px] leading-5',
    /失败|损坏/.test(children) ? 'bg-danger/10 text-danger' : /待|重启/.test(children) ? 'bg-warning/10 text-amber-800 dark:text-amber-300' : /已启用|已就绪/.test(children) ? 'bg-emerald-500/10 text-emerald-800 dark:text-emerald-300' : 'bg-elevated text-secondary')}>{children}</span>
}

function Stepper({ step }: { step: number }) {
  return <ol className="mb-6 flex flex-wrap items-center gap-x-6 gap-y-3 text-xs text-secondary" aria-label="导入进度">
    {['选择文件', '预览确认', '处理结果'].map((label, index) => <li key={label} aria-current={index === step ? 'step' : undefined} className={cn('flex items-center gap-2', index === step && 'text-accent')}>
      <span className={cn('flex h-6 w-6 items-center justify-center rounded-full text-[11px]', index < step ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300' : index === step ? 'bg-accent/10' : 'bg-elevated')}>{index < step ? <Check className="h-3.5 w-3.5" aria-hidden="true" /> : index + 1}</span>{label}
    </li>)}
  </ol>
}

const formatSize = (n: number) => n >= 1024 * 1024 ? (n / 1024 / 1024).toFixed(1) + ' MiB' : Math.max(1, Math.round(n / 1024)) + ' KiB'
const toggle = (previous: Set<string>, key: string) => {
  const next = new Set(previous)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  return next
}

export function SettingsImportCenterPanel() {
  const [params, setParams] = useSearchParams()
  const view = params.get('view') ?? 'import'
  const qc = useQueryClient()
  const fileInput = useRef<HTMLInputElement>(null)
  const busyRef = useRef(false)
  const [busy, setBusy] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [files, setFiles] = useState<File[]>([])
  const [review, setReview] = useState<Review | null>(null)
  const [result, setResult] = useState<Result | null>(null)
  const [error, setError] = useState('')
  const [trusted, setTrusted] = useState(false)
  const [selected, setSelected] = useState(new Set<string>())
  const [overwrite, setOverwrite] = useState(new Set<string>())
  const [categories, setCategories] = useState(new Set<string>())
  const [allowOverwrite, setAllowOverwrite] = useState(false)
  const [pageId, setPageId] = useState('')
  const [namespace, setNamespace] = useState('')
  const [exportKind, setExportKind] = useState<ResourceKind>(() => {
    const kind = params.get('kind')
    return kind === 'factor' || kind === 'signal' ? kind : 'strategy'
  })
  const [exporting, setExporting] = useState(false)

  const packages = useQuery({ queryKey: QK.extensionPackages, queryFn: api.extensionPackages, enabled: view === 'installed', staleTime: 0 })
  const sources = useQuery({ queryKey: QK.dataSources, queryFn: api.dataSources, enabled: view === 'installed' })
  const pages = useQuery({ queryKey: QK.extPages, queryFn: api.extPagesList, enabled: view === 'installed' || review?.kind === 'page' || review?.kind === 'project' })
  const namespaces = useQuery({ queryKey: QK.extPagesSourceNamespaces, queryFn: api.extPagesSourceNamespaces, enabled: view === 'import' && !review && !result })
  const matrix = useQuery({ queryKey: QK.capabilityMatrix, queryFn: api.capabilityMatrix, enabled: review?.kind === 'source' })
  const entries = review?.kind === 'resources' ? review.preview.entries : []
  const effective = useMemo(() => resourceClosure(entries, selected), [entries, selected])
  const included = entries.filter(entry => effective.has(entry.key))
  const conflicts = included.filter(entry => entry.action === 'conflict')
  const issues = included.flatMap(entry => entry.issues.map(issue => entry.label + '：' + issue))
  const unresolved = conflicts.some(entry => !overwrite.has(entry.key))
  const codeTrust = !!review && (['extension', 'source', 'page', 'project'].includes(review.kind) || included.some(entry => entry.kind === 'strategy' || entry.kind === 'code'))
  const packageErrors = review?.kind === 'extension' || review?.kind === 'source' ? review.preview.errors : []
  const isPage = review?.kind === 'page' || review?.kind === 'project'
  const invalidPageId = isPage && !/^[a-zA-Z0-9_]{1,64}$/.test(pageId)
  const pageConflict = isPage && (pages.data ?? []).some(page => page.id === pageId)
  const disabled = busy || !!packageErrors.length || !!issues.length || unresolved || (codeTrust && !trusted)
    || (review?.kind === 'resources' && !included.length) || (review?.kind === 'backup' && (!categories.size || !trusted))
    || !!invalidPageId || (isPage && (!pages.isSuccess || (pageConflict && !allowOverwrite)))

  function changeView(next: string, target?: string) {
    if (busyRef.current) return
    const nextParams = new URLSearchParams(params)
    nextParams.set('tab', 'import')
    if (next === 'import') nextParams.delete('view')
    else nextParams.set('view', next)
    if (target) nextParams.set('selected', target)
    else nextParams.delete('selected')
    setParams(nextParams, { replace: true })
    setError('')
  }
  function reset() {
    if (busyRef.current) return
    setReview(null); setResult(null); setFiles([]); setError(''); setTrusted(false)
    setSelected(new Set()); setOverwrite(new Set()); setCategories(new Set()); setAllowOverwrite(false)
    changeView('import')
  }
  async function inspect(candidateFiles: File[]) {
    if (busyRef.current || !candidateFiles.length) return
    busyRef.current = true; setBusy(true); setError(''); setReview(null); setResult(null); setTrusted(false)
    setSelected(new Set()); setOverwrite(new Set()); setCategories(new Set()); setAllowOverwrite(false)
    setFiles(candidateFiles)
    try {
      const next = await api.importCenterInspect(candidateFiles)
      setReview(next)
      if (next.kind === 'resources') {
        const kind = params.get('kind')
        const candidates = next.preview.entries.filter(entry => !kind || entry.kind === kind).map(entry => entry.key)
        const preferred = next.preview.roots.filter(key => candidates.includes(key))
        setSelected(new Set(preferred.length ? preferred : candidates.length ? candidates : next.preview.roots))
      }
      if (next.kind === 'backup') setCategories(new Set(next.preview.categories.filter(c => c.key !== 'secrets' && c.key !== 'auth').map(c => c.key)))
      if (next.kind === 'page') setPageId(next.preview.suggested_id)
    } catch (e) { setError(e instanceof Error ? e.message : '文件识别失败，请检查文件格式后重试') }
    finally { busyRef.current = false; setBusy(false) }
  }
  async function refreshInstalled() {
    await Promise.allSettled([QK.extensionPackages, QK.extPages, QK.dataSources, QK.capabilityMatrix].map(queryKey => qc.invalidateQueries({ queryKey })))
  }
  async function apply() {
    if (!review || disabled || busyRef.current) return
    busyRef.current = true; setBusy(true); setError('')
    try {
      let next: Result
      if (review.kind === 'extension') {
        const preview = review.preview
        if (preview.manifest.frontend) await inspectPageBlob(await api.extensionPackageFrontend(files[0], preview.digest), preview.manifest.id)
        const applied = await api.installExtensionPackage(files[0], preview.digest, preview.revision)
        next = { title: '扩展包已保存', message: applied.message, target: 'extension:' + applied.id, details: [
          preview.installed_version ? '当前进程继续使用 ' + preview.installed_version + '，重启主程序后切换到 ' + preview.manifest.version + '。' : '扩展尚未启用；后台接口和启动任务在重启主程序后生效。',
          ...(preview.manifest.frontend ? ['配套页面已通过加载契约检查；等待后台启动成功后，随同一版本启用。'] : []),
          '新版本后台启动失败时保留原版本；可在“已安装扩展”查看状态或回退。',
        ] }
      } else if (review.kind === 'source') {
        const applied = await api.installSourcePackage(files[0], review.preview.digest, review.preview.revision)
        next = { title: '数据源插件已处理', message: applied.message, target: 'source:' + applied.name, details: ['安装不会自动接管取数。请继续配置 API Key，再明确选择启用的数据能力。'] }
      } else if (review.kind === 'backup') {
        const applied = await api.configBackupApply(files[0], allowOverwrite, [...categories])
        const failed = applied.categories.flatMap(c => c.failed.map(f => c.label + ' · ' + f.file + '：' + f.error))
        next = { title: failed.length ? '配置恢复完成，部分条目失败' : '所选配置恢复完成', message: '成功恢复 ' + applied.categories.reduce((sum, c) => sum + c.imported, 0) + ' 项，跳过同名项 ' + applied.categories.reduce((sum, c) => sum + c.skipped_conflict, 0) + ' 项。', details: failed.length ? failed : ['只应用已勾选的类别；未选类别保持原样。', ...(categories.has('auth') ? ['访问凭证已恢复，可能需要使用备份中的密码重新登录。'] : [])] }
        await Promise.allSettled(INVALIDATE_KEYS.map(queryKey => qc.invalidateQueries({ queryKey })))
        await refreshResourceQueries(qc)
      } else if (review.kind === 'resources') {
        const applied = await api.resourcesApply(files, [...selected], conflicts.filter(entry => overwrite.has(entry.key)).map(entry => entry.key), review.preview.revision)
        next = { title: '资源已导入', message: '新增或替换 ' + applied.imported + ' 个文件，复用 ' + applied.reused + ' 个已有文件。', details: [
          ...(['strategy', 'signal', 'factor'] as const).filter(kind => applied.counts[kind]).map(kind => KIND[kind] + ' ' + applied.counts[kind] + ' 项'),
          ...(applied.drafts?.length ? [applied.drafts.length + ' 条策略保留为草稿，请在策略库中检查后发布。'] : []),
        ], path: included.some(e => e.kind === 'strategy') ? '/screener' : included.some(e => e.kind === 'factor') ? '/factors' : '/signals' }
        await refreshResourceQueries(qc)
      } else {
        if (review.kind === 'page' && !review.preview.source) await inspectPageBlob(files[0])
        const meta = review.kind === 'project'
          ? await api.importCenterProjectPage(review.preview.namespace, pageId, allowOverwrite)
          : await api.importCenterInstallPage(files[0], pageId, review.preview.digest, allowOverwrite)
        let summary = { label: '', path: '' }
        try {
          summary = await inspectPageModule(runtimeBundleUrl(meta.id) + '?trial=' + Date.now())
          await api.extPagesPutManifest(meta.id, { ...summary, error: '' })
        } catch (e) {
          const message = e instanceof Error ? e.message : '页面加载校验失败'
          await api.extPagesPutManifest(meta.id, { ...summary, error: message })
          throw new Error(message + '。页面已保持停用，请修正后重新导入。')
        }
        next = { title: '页面已通过加载校验', message: '刷新浏览器后登记页面和菜单入口。', details: ['页面只使用主程序已有接口。如需配套后台，请导入前后端完整扩展包。'], target: 'page:' + meta.id }
      }
      setResult(next)
      await refreshInstalled()
    } catch (e) { setError(e instanceof Error ? e.message : '处理失败，请检查后重试') }
    finally { busyRef.current = false; setBusy(false) }
  }

  const packageDoc = review?.kind === 'source' || review?.kind === 'extension' ? review.preview.manifest : null
  const installedVersion = review?.kind === 'source' || review?.kind === 'extension' ? review.preview.installed_version : null
  const pendingVersion = review?.kind === 'source' || review?.kind === 'extension' ? review.preview.pending_version : null
  const name = review?.kind === 'extension' ? review.preview.manifest.display_name || review.preview.manifest.id
    : review?.kind === 'source' ? review.preview.manifest.display_name || review.preview.manifest.name
      : review?.kind === 'project' ? review.preview.namespace : review?.kind === 'backup' ? '恢复配置备份' : review?.kind === 'resources' ? '策略、信号与因子' : '导入页面插件'
  const selectedItem = params.get('selected') ?? ''
  const liveErrors = getFrontendExtensionLoadErrors()

  return <div className="max-w-5xl space-y-6">
    <header className="flex flex-wrap items-start justify-between gap-4">
      <div><h2 className="text-xl font-semibold tracking-tight text-foreground">导入与备份</h2><p className="mt-2 text-sm leading-6 text-secondary">一个入口，导入资源、安装扩展、迁移配置。</p></div>
      <button type="button" className={BUTTON} disabled={busy} onClick={() => changeView('backup')}><Download className="h-4 w-4" aria-hidden="true" />创建配置备份</button>
    </header>
    <nav className="flex gap-6 border-b border-border" aria-label="导入与扩展">
      {[['import', '导入文件'], ['installed', '已安装扩展']].map(([key, label]) => <button key={key} type="button" aria-pressed={key === 'import' ? view !== 'installed' : view === key} disabled={busy} onClick={() => changeView(key)}
        className={cn('min-h-11 border-b-2 pb-3 text-sm font-medium disabled:opacity-50', (key === 'import' ? view !== 'installed' : view === key) ? 'border-accent text-accent' : 'border-transparent text-secondary hover:text-foreground')}>{label}</button>)}
    </nav>

    {view === 'backup' ? <><SettingsBackupPanel mode="export" /><button type="button" className={BUTTON} onClick={() => changeView('import')}><ArrowLeft className="h-3.5 w-3.5" />返回导入</button></>
      : view === 'installed' ? <InstalledView packages={packages.data ?? []} sources={(sources.data?.plugins ?? []).filter(p => p.origin === 'external')} pages={(pages.data ?? []).filter(p => !p.managed_by)}
        loading={packages.isLoading || sources.isLoading || pages.isLoading} error={packages.error || sources.error || pages.error}
        selected={selectedItem} select={target => changeView('installed', target)} install={reset} refresh={refreshInstalled} bootErrors={liveErrors} />
        : <>
          <Stepper step={result ? 2 : review ? 1 : 0} />
          {result ? <section className="max-w-2xl py-2" role="status">
            <CheckCircle2 className="mb-4 h-9 w-9 text-emerald-700 dark:text-emerald-300" aria-hidden="true" />
            <h3 className="text-lg font-semibold text-foreground">{result.title}</h3><p className="mt-3 text-sm leading-7 text-secondary">{result.message}</p>
            <ul className="my-6 space-y-3 border-y border-border py-5 text-sm leading-6 text-secondary">{result.details.map((detail, i) => <li key={i} className="break-words">{detail}</li>)}</ul>
            <div className="flex flex-wrap gap-3">{result.target && <button type="button" className={PRIMARY} onClick={() => changeView('installed', result.target)}>查看已安装扩展<ArrowRight className="h-3.5 w-3.5" /></button>}
              {result.path && <Link to={result.path} className={PRIMARY}>查看导入资源<ArrowRight className="h-3.5 w-3.5" /></Link>}
              <button type="button" className={BUTTON} onClick={reset}>继续导入</button></div>
          </section> : review ? <>
            <header className="space-y-2">
              <div className="flex flex-wrap items-center gap-3 text-xs text-secondary"><Package className="h-4 w-4" aria-hidden="true" /><span>{review.kind === 'extension' && review.preview.manifest.frontend ? '完整功能包' : TYPE[review.kind]}</span><span className="break-all">{review.kind === 'project' ? '项目源码 · ' + review.preview.namespace : files.map(f => f.name).join('、')}</span>{!!files.length && <span>{formatSize(files.reduce((sum, f) => sum + f.size, 0))}</span>}</div>
              <h3 className="text-lg font-semibold text-foreground">{name}</h3>
              {packageDoc && <p className="text-xs text-secondary">{installedVersion ? '本机 ' + installedVersion + ' → 目标 ' + packageDoc.version : '版本 ' + packageDoc.version}{packageDoc.author ? ' · 来源声明：' + packageDoc.author : ''}</p>}
              {packageDoc?.description && <p className="max-w-2xl text-sm leading-6 text-secondary">{packageDoc.description}</p>}
              {pendingVersion && packageDoc && <Notice warning>已有待生效版本 {pendingVersion}。{pendingVersion === packageDoc.version ? '本次是同一个暂存版本。' : '确认后会用 ' + packageDoc.version + ' 替换该暂存版本，当前运行版本保持不变。'}</Notice>}
            </header>
            <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1fr)_16rem]">
              <div className="min-w-0 space-y-7">
                {review.kind === 'resources' && <section>
                  <h4 className="mb-3 text-sm font-semibold text-foreground">选择资源与检查依赖</h4>
                  <div className="divide-y divide-border border-y border-border">{entries.filter(entry => ['strategy', 'signal', 'factor'].includes(entry.kind)).map(entry => <label key={entry.key} className="flex items-start gap-3 py-3 text-sm">
                    <input className="mt-1 accent-accent" type="checkbox" checked={selected.has(entry.key)} disabled={busy || (effective.has(entry.key) && !selected.has(entry.key))} onChange={() => setSelected(previous => toggle(previous, entry.key))} aria-label={'选择' + entry.label} />
                    <span className="min-w-0 flex-1 break-words"><span className="font-medium text-foreground">{entry.label}</span><span className="mt-1 block text-xs text-secondary">{KIND[entry.kind]} · {entry.id}{effective.has(entry.key) && !selected.has(entry.key) ? ' · 随所选资源自动携带' : ''}</span></span>
                    <span className="text-xs text-secondary">{entry.action === 'reuse' ? '复用' : entry.action === 'conflict' ? '内容不同' : '新增'}</span>
                  </label>)}</div>
                  {!!conflicts.length && <div className="mt-4 space-y-3"><Notice warning>所选资源或依赖与本机内容不同。确认替换，或取消引用它的主资源后继续。</Notice>{conflicts.map(entry => <div key={entry.key} className="space-y-2 text-xs leading-6 text-secondary">
                    <label className="flex items-start gap-2"><input className="mt-1 accent-accent" type="checkbox" disabled={busy} checked={overwrite.has(entry.key)} onChange={() => setOverwrite(previous => toggle(previous, entry.key))} aria-label={'确认替换' + entry.label} /><span>用包内版本替换“{entry.label}”</span></label>
                    {!!entry.referenced_by?.length && <p className="break-words">本机引用方：{entry.referenced_by.join('、')}</p>}
                    {entry.comparison && <details><summary className="cursor-pointer text-accent">对比本机与包内内容</summary><div className="mt-2 grid gap-3 sm:grid-cols-2">{(['local', 'incoming'] as const).map(key => <div key={key}><p>{key === 'local' ? '本机版本' : '包内版本'}</p><pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-input bg-elevated p-3">{entry.comparison![key]}</pre></div>)}</div></details>}
                  </div>)}</div>}
                  <p className="mt-4 text-xs leading-6 text-secondary">所选 {included.length} 项文件，包含关联定义、策略调参及辅助模块。相同内容自动复用。</p>
                </section>}
                {review.kind === 'backup' && <section>
                  <h4 className="mb-3 text-sm font-semibold text-foreground">选择恢复范围</h4>
                  <p className="mb-3 text-xs leading-6 text-secondary">备份时间 {review.preview.created_at || '未知'} · 来源版本 {review.preview.app_version || '未知'}</p>
                  <div className="divide-y divide-border border-y border-border">{review.preview.categories.map(category => <label key={category.key} className="flex items-start gap-3 py-3 text-sm">
                    <input className="mt-1 accent-accent" type="checkbox" disabled={busy} checked={categories.has(category.key)} onChange={() => setCategories(previous => toggle(previous, category.key))} aria-label={'恢复' + category.label} />
                    <span className="min-w-0 flex-1"><span className="text-foreground">{category.label}</span><span className="mt-1 block text-xs text-secondary">{category.count} 项{category.conflicts ? ' · ' + category.conflicts + ' 项同名' : ''}</span></span>
                    {['secrets', 'auth'].includes(category.key) && <span className="text-xs text-amber-800 dark:text-amber-300">敏感项</span>}
                  </label>)}</div>
                  <div className="mt-4"><Notice warning>密钥（API Key）和访问凭证需单独勾选。偏好与密钥按合并写入；恢复敏感项会覆盖本机同名值。</Notice></div>
                  {!!review.preview.conflict_files && <label className="mt-4 flex items-start gap-2 text-xs leading-6 text-secondary"><input type="checkbox" checked={allowOverwrite} disabled={busy} onChange={e => setAllowOverwrite(e.target.checked)} className="mt-1 accent-accent" />允许覆盖已存在的同名文件（默认跳过）</label>}
                  {!!review.preview.out_of_scope_files && <p className="mt-3 text-xs text-secondary">{review.preview.out_of_scope_files} 个不受支持的文件不会恢复。</p>}
                </section>}
                {review.kind === 'extension' && <section className="space-y-4">
                  <h4 className="text-sm font-semibold text-foreground">本次变更</h4>
                  <div className="flex items-start gap-3 border-b border-border pb-4"><Server className="mt-1 h-4 w-4 shrink-0 text-secondary" /><div><p className="text-sm font-medium text-foreground">后端服务与启动任务</p><p className="mt-1 text-xs leading-6 text-secondary">重启后注册扩展接口，执行包内声明的初始化任务。</p></div></div>
                  {review.preview.manifest.frontend && <div className="flex items-start gap-3 border-b border-border pb-4"><FileCode2 className="mt-1 h-4 w-4 shrink-0 text-secondary" /><div><p className="text-sm font-medium text-foreground">配套前端页面</p><p className="mt-1 text-xs leading-6 text-secondary">与后端按同一版本更新、启停和卸载。</p></div></div>}
                  <Notice>{review.preview.manifest.frontend ? '后台启动成功后，新版本页面才会对外提供。确认安装时会先加载页面代码检查其前端契约。' : '新版本仅在主程序重启时加载；安装和预览阶段不会执行包内 Python 代码。'}</Notice>
                  <details className="text-xs leading-6 text-secondary"><summary className="cursor-pointer">查看包内文件与技术信息</summary><p className="mt-2">接口命名空间：/api/extensions/{review.preview.manifest.id}/</p><ul className="mt-2 break-all font-mono">{review.preview.files.map(file => <li key={file}>{file}</li>)}</ul></details>
                </section>}
                {review.kind === 'source' && <section className="space-y-4"><h4 className="text-sm font-semibold text-foreground">本次变更</h4><p className="text-sm leading-7 text-secondary">安装数据源卡片。支持能力：{review.preview.manifest.datasets.map(id => matrix.data?.capabilities.find(c => c.id === id)?.label || id).join('、')}。</p><Notice>安装后继续配置 API Key 和启用能力。新版本更新在重启后切换，安装不会自动更改当前行情来源。</Notice></section>}
                {isPage && <section className="space-y-4"><h4 className="text-sm font-semibold text-foreground">页面与菜单入口</h4><label className="block space-y-2 text-xs text-secondary"><span>页面标识</span><input aria-label="页面标识" value={pageId} disabled={busy} onChange={e => { setPageId(e.target.value); setAllowOverwrite(false) }} className={INPUT} /></label>
                  {invalidPageId && <p role="alert" className="text-xs text-danger">标识需为 1–64 位字母、数字或下划线。</p>}
                  {pageConflict && <label className="flex items-start gap-2 text-xs leading-6 text-secondary"><input className="mt-1 accent-accent" type="checkbox" disabled={busy} checked={allowOverwrite} onChange={e => setAllowOverwrite(e.target.checked)} />确认替换该标识现有的页面插件</label>}
                  {pages.error && <p role="alert" className="text-xs text-danger">本机页面清单读取失败。<button className="ml-2 underline" onClick={() => void pages.refetch()}>重新读取</button></p>}
                  <Notice>这里只导入前端页面，加载校验通过后刷新生效。需要新的后台接口时，请使用包含 extension.yaml 的完整功能包。</Notice>
                </section>}
                {(review.kind === 'source' || review.kind === 'extension') && <section className="space-y-3"><h4 className="text-sm font-semibold text-foreground">兼容检查</h4>{packageErrors.length ? <div role="alert" className="space-y-2 text-sm text-danger">{packageErrors.map((message, i) => <p key={i}>{message}</p>)}</div> : <p className="flex items-center gap-2 text-sm text-emerald-800 dark:text-emerald-300"><CheckCircle2 className="h-4 w-4" />静态兼容检查通过</p>}<p className="text-xs leading-6 text-secondary">缺少依赖时停止安装，不会自动修改主程序运行环境。</p></section>}
                {!!issues.length && <div role="alert" className="space-y-2 text-xs leading-6 text-danger">{issues.map((issue, i) => <p key={i}>{issue}</p>)}</div>}
              </div>
              <aside className="min-w-0 rounded-card border border-border bg-surface p-4">
                <h4 className="text-sm font-semibold text-foreground">{review.kind === 'backup' ? '本次恢复' : '本次操作'}</h4>
                <dl className="my-4 space-y-3 text-xs leading-6"><div className="flex justify-between gap-3"><dt className="text-secondary">处理方式</dt><dd className="text-right text-foreground">{review.kind === 'backup' ? '按类别恢复' : review.kind === 'resources' ? '所选资源与依赖' : 'installed_version' in review.preview && review.preview.installed_version ? '更新已有扩展' : '新增导入'}</dd></div>
                  <div className="flex justify-between gap-3"><dt className="text-secondary">生效时间</dt><dd className="text-right text-foreground">{review.kind === 'extension' ? '重启主程序后' : isPage ? '刷新页面后' : review.kind === 'source' && review.preview.installed_version ? '重启主程序后' : '处理完成后'}</dd></div>
                  {review.kind === 'backup' && <div className="flex justify-between gap-3"><dt className="text-secondary">恢复范围</dt><dd>{categories.size} / {review.preview.categories.length} 类</dd></div>}
                  {packageDoc && <div className="flex justify-between gap-3"><dt className="text-secondary">目标版本</dt><dd>{packageDoc.version}</dd></div>}
                </dl>
                {(codeTrust || review.kind === 'backup') && <label className="flex items-start gap-2 text-xs leading-6 text-secondary"><input type="checkbox" className="mt-1 accent-accent" disabled={busy} aria-label={review.kind === 'backup' ? '信任备份来源' : '信任代码来源'} checked={trusted} onChange={e => setTrusted(e.target.checked)} /><span>{review.kind === 'backup' ? '我确认这是可信的备份，允许恢复所选配置及其中的代码。' : '我信任此代码来源，允许它在本机运行。代码可访问主程序的数据和凭据。'}</span></label>}
                <button type="button" className={cn(PRIMARY, 'mt-4 w-full whitespace-normal')} disabled={disabled} onClick={() => void apply()}>{busy && <Loader2 className="h-4 w-4 animate-spin" />}{busy ? '正在处理…' : review.kind === 'backup' ? '恢复所选配置' : review.kind === 'resources' ? '确认导入' : 'installed_version' in review.preview && review.preview.installed_version ? '确认更新' : '确认安装'}</button>
                <p className="mt-3 text-xs leading-6 text-secondary">确认前不会应用任何变更。{unresolved ? '请先处理同名冲突。' : ''}</p>
              </aside>
            </div>
            <button type="button" className={BUTTON} disabled={busy} onClick={reset}><ArrowLeft className="h-3.5 w-3.5" />重新选择文件</button>
          </> : <>
            <div className={cn('rounded-card border border-dashed bg-surface px-4 py-10 text-center transition-colors sm:py-12', dragging ? 'border-accent bg-accent/5' : 'border-border')}
              onDragOver={e => { e.preventDefault(); if (!busy) setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={e => { e.preventDefault(); setDragging(false); void inspect(Array.from(e.dataTransfer.files)) }}>
              <Upload className="mx-auto mb-4 h-7 w-7 text-secondary" aria-hidden="true" /><h3 className="text-lg font-semibold text-foreground">把文件放到这里</h3>
              <p className="mx-auto mt-3 max-w-xl text-sm leading-7 text-secondary">自动识别策略与定义、配置备份、数据源插件、页面插件，以及后端或前后端完整扩展包。</p>
              <button type="button" className={cn(PRIMARY, 'mt-5')} disabled={busy} onClick={() => fileInput.current?.click()}>{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <FolderOpen className="h-4 w-4" />}{busy ? '正在识别文件…' : '选择文件'}</button>
              <p className="mt-3 text-xs leading-6 text-secondary">支持 ZIP、PY、JSON、JS、MJS、TSX。资源文件可多选；扩展和备份逐包处理。</p>
            </div>
            {!!namespaces.data?.length && <details className="border-b border-border pb-5 text-xs text-secondary"><summary className="cursor-pointer">从本项目的源码页面导入</summary><p className="mt-3 leading-6">把现有源码页面编译为独立页面插件；不会把它的后端代码一起打包。</p><div className="mt-3 flex flex-wrap items-center gap-2"><select aria-label="项目源码页面" className={cn(INPUT, 'sm:w-64')} value={namespace} onChange={e => setNamespace(e.target.value)} disabled={busy}><option value="">选择源码页面</option>{namespaces.data.map(ns => <option key={ns} value={ns}>{ns}</option>)}</select><button type="button" className={BUTTON} disabled={busy || !namespace} onClick={() => { setReview({ kind: 'project', preview: { namespace } }); setPageId(namespace.replace(/[^a-zA-Z0-9_]/g, '_').slice(0, 64)); setTrusted(false); setAllowOverwrite(false) }}>预览项目页面</button></div></details>}
            <section className="flex flex-wrap items-end justify-between gap-4 border-b border-border pb-5">
              <div><h3 className="text-sm font-medium text-foreground">分享策略、信号与因子</h3><p className="mt-2 text-xs leading-6 text-secondary">批量导出定义和关联依赖；换机迁移请使用“创建配置备份”。</p></div>
              <div className="flex flex-wrap gap-2"><select aria-label="导出资源类型" className={cn(INPUT, 'w-auto')} value={exportKind} onChange={e => setExportKind(e.target.value as ResourceKind)}>{(['strategy', 'signal', 'factor'] as const).map(kind => <option key={kind} value={kind}>{KIND[kind]}</option>)}</select><button type="button" className={BUTTON} onClick={() => setExporting(true)}><Download className="h-3.5 w-3.5" />批量导出资源</button></div>
            </section>
            <Notice>导入前会展示变更范围、依赖和冲突。后端扩展安装到用户数据目录，升级主程序不会覆盖安装文件。</Notice>
          </>}
          <input ref={fileInput} type="file" className="hidden" aria-label="选择导入文件" multiple accept=".zip,.py,.json,.js,.mjs,.tsx" disabled={busy} onChange={e => { const next = Array.from(e.target.files ?? []); e.target.value = ''; void inspect(next) }} />
        </>}
    {error && <div role="alert" className="flex items-start gap-2 rounded-btn border border-danger/30 bg-danger/5 p-3 text-sm leading-6 text-danger"><ShieldAlert className="mt-1 h-4 w-4 shrink-0" /><span className="min-w-0 break-words">{error}</span>{!review && !!files.length && <button type="button" className="shrink-0 underline" disabled={busy} onClick={() => void inspect(files)}>重新预览</button>}</div>}
    {exporting && <ResourceTransferDialog kind={exportKind} initialMode="export" exportOnly onClose={() => setExporting(false)} />}
  </div>
}

function InstalledView({ packages, sources, pages, selected, select, install, refresh, loading, error, bootErrors }: {
  packages: ManagedExtensionPackage[]; sources: PluginDataSourceItem[]; pages: RuntimePagePack[]
  selected: string; select: (target: string) => void; install: () => void; refresh: () => Promise<void>
  loading: boolean; error: unknown; bootErrors: { source: string; error: string; extensionId?: string }[]
}) {
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState('')
  const [message, setMessage] = useState('')
  const [confirm, setConfirm] = useState('')
  const pack = packages.find(p => selected === 'extension:' + p.id)
  const source = sources.find(p => selected === 'source:' + p.name)
  const page = pages.find(p => selected === 'page:' + p.id)
  const items = [
    ...packages.map(p => ({ key: 'extension:' + p.id, title: p.name, subtitle: (p.frontend ? '前端 + 后端' : '后端扩展') + ' · ' + (p.version || '尚未生效') + (p.pending_version ? ' → ' + p.pending_version : ''), status: p.status, icon: Server })),
    ...sources.map(p => ({ key: 'source:' + p.name, title: p.display_name, subtitle: '数据源插件 · ' + (p.version || '—') + (p.pending_version ? ' → ' + p.pending_version : ''), status: p.pending_remove ? '待重启卸载' : p.update_error ? '更新失败' : p.pending_version ? '待重启更新' : p.status, icon: Package })),
    ...pages.map(p => ({ key: 'page:' + p.id, title: p.label || p.id, subtitle: '页面插件 · ' + (p.path || '尚未登记页面路径'), status: p.error ? '加载失败' : p.enabled ? '已启用' : '已停用', icon: FileCode2 })),
  ]
  async function run(action: string) {
    if (busy) return
    setBusy(true); setFailure(''); setMessage('')
    try {
      if (pack) setMessage((await api.manageExtensionPackage(pack.id, action, pack.revision)).message)
      if (page) {
        if (action === 'remove') await api.extPagesDelete(page.id)
        else await api.extPagesPutEnabled(page.id, action === 'enable')
        setMessage('变更已保存，刷新浏览器后生效')
      }
      setConfirm('')
      await refresh()
    } catch (e) { setFailure(e instanceof Error ? e.message : '操作失败，请重试'); await refresh() }
    finally { setBusy(false) }
  }
  const activeError = pack?.error || page?.error
  return <>
    <header className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="text-lg font-semibold text-foreground">已安装扩展</h3><p className="mt-2 text-xs leading-6 text-secondary">统一查看版本、启停状态与待生效变更。</p></div><div className="flex gap-2"><button className={BUTTON} disabled={busy || loading} onClick={() => void refresh()} aria-label="刷新扩展清单"><RefreshCw className="h-3.5 w-3.5" /></button><button className={BUTTON} disabled={busy} onClick={install}><Plus className="h-3.5 w-3.5" />安装扩展</button></div></header>
    {!!error && <div role="alert" className="text-sm text-danger">扩展清单读取失败，已保留可用列表。请点击刷新重试。</div>}
    {loading && <p role="status" className="flex gap-2 text-sm text-secondary"><Loader2 className="h-4 w-4 animate-spin" />正在读取扩展…</p>}
    <div className={cn('grid items-start gap-6', (pack || source || page) && 'xl:grid-cols-[minmax(0,1fr)_19rem]')}>
      <div className="min-w-0 divide-y divide-border border-y border-border">
        {!loading && !items.length && <p className="py-12 text-sm leading-7 text-secondary">尚未安装外部扩展。可从“导入文件”开始；源码内置页面仍在“页面管理”维护。</p>}
        {items.map(item => <button key={item.key} type="button" disabled={busy} aria-pressed={selected === item.key} onClick={() => { setFailure(''); setMessage(''); setConfirm(''); select(item.key) }} className={cn('flex w-full flex-wrap items-center gap-3 py-4 text-left hover:bg-elevated/50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50', selected === item.key && 'text-accent')}>
          <item.icon className="h-4 w-4 shrink-0 text-secondary" aria-hidden="true" /><span className="min-w-0 flex-1 basis-40"><span className="block break-words text-sm font-medium">{item.title}</span><span className="mt-1 block break-words text-xs leading-6 text-secondary">{item.subtitle}</span></span><Status>{item.status}</Status>
        </button>)}
      </div>
      {(pack || source || page) && <aside className="min-w-0 rounded-card border border-border bg-surface p-4">
        <h4 className="break-words text-sm font-semibold text-foreground">{pack?.name || source?.display_name || page?.label || page?.id}</h4>
        {pack && <><dl className="my-4 space-y-3 text-xs leading-6"><div className="flex justify-between gap-3"><dt className="text-secondary">当前版本</dt><dd>{pack.version || '尚未生效'}</dd></div>{pack.pending_version && <div className="flex justify-between gap-3"><dt className="text-secondary">待生效版本</dt><dd>{pack.pending_version}</dd></div>}<div className="flex justify-between gap-3"><dt className="text-secondary">当前进程</dt><dd>{pack.runtime_version ? '已加载 ' + pack.runtime_version : '未加载'}</dd></div></dl>
          <p className="text-xs leading-6 text-secondary">{pack.frontend ? '前端页面与后台服务一起更新、启停和卸载。' : '后端接口、初始化任务和通知格式化器在重启后生效。'}</p>
          {!pack.pending_remove && <div className="mt-4 flex flex-wrap gap-2"><button className={BUTTON} disabled={busy} onClick={() => void run(pack.enabled ? 'disable' : 'enable')}>{pack.enabled ? '停用扩展' : '启用扩展'}</button>{pack.previous_version && <button className={BUTTON} disabled={busy} onClick={() => setConfirm('rollback')}>{'回退到 ' + pack.previous_version}</button>}{pack.pending_version && pack.version && <button className={BUTTON} disabled={busy} onClick={() => void run('cancel-update')}>取消待生效更新</button>}<button className={cn(BUTTON, 'text-danger')} disabled={busy} onClick={() => setConfirm('remove')}>卸载扩展</button></div>}
          <p className="mt-4 flex items-start gap-2 text-xs leading-6 text-secondary"><Clock3 className="mt-1 h-3.5 w-3.5 shrink-0" />变更后请重启主程序；刷新浏览器不能替代后台重启。</p>
        </>}
        {source && <div className="mt-4 space-y-4">{source.api_key_env && <PluginKeyConfig key={source.name} plugin={source} />}<PackageActions key={source.name} plugin={source} refresh={() => { void refresh() }} /></div>}
        {page && <><p className="mt-3 break-all text-xs leading-6 text-secondary">页面标识：{page.id}</p><p className="mt-2 text-xs leading-6 text-secondary">启停和卸载在刷新浏览器后生效。更新时从“导入文件”上传新产物，并确认替换相同标识。</p><div className="mt-4 flex flex-wrap gap-2"><button className={BUTTON} disabled={busy || !!page.error} onClick={() => void run(page.enabled ? 'disable' : 'enable')}>{page.enabled ? '停用页面' : '启用页面'}</button><button className={cn(BUTTON, 'text-danger')} disabled={busy} onClick={() => setConfirm('remove')}>卸载页面</button></div></>}
        {activeError && <p className="mt-4 break-words text-xs leading-6 text-danger">{pack?.failed_version ? '版本 ' + pack.failed_version + ' 启动失败；' + (pack.runtime_version ? '当前仍运行 ' + pack.runtime_version + '。' : '扩展未启用。') : activeError}</p>}
        {confirm && <div className="mt-5 space-y-3 border-t border-border pt-4"><p className="text-xs leading-6 text-secondary">{confirm === 'remove' ? '确认卸载此扩展？扩展包及关联页面将移除，包目录外的数据保留。' : '确认回退：' + pack?.version + ' → ' + pack?.previous_version + '。重启后切换，原有启停状态保留。' + (pack?.pending_version ? '已暂存的 ' + pack.pending_version + ' 将被替换。' : '')}</p><div className="flex flex-wrap gap-2"><button className={BUTTON} disabled={busy} onClick={() => setConfirm('')}>取消</button><button className={cn(BUTTON, confirm === 'remove' && 'text-danger')} disabled={busy} onClick={() => void run(confirm)}>{busy ? '处理中…' : '确认'}</button></div></div>}
      </aside>}
    </div>
    {failure && <p role="alert" className="break-words text-sm text-danger">{failure}</p>}
    {message && <p role="status" className="text-sm leading-6 text-secondary">{message}</p>}
    {!!bootErrors.filter(e => e.source.startsWith('runtime:')).length && <Notice warning><p className="font-medium">本次启动有页面加载失败</p>{bootErrors.filter(e => e.source.startsWith('runtime:')).map((e, i) => <p key={i} className="break-words">{e.source}：{e.error}</p>)}<p>请停用或更新对应页面，避免与主程序或其他扩展的路由冲突。</p></Notice>}
  </>
}
