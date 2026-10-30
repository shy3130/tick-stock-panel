import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Modal } from '@/components/Modal'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, PackageOpen, ArrowLeft, Check, ChevronDown, Download, FileArchive, Link2, Loader2, Search, Upload, X } from 'lucide-react'
import { api, type ResourceKind, type ResourceTransferEntry, type ResourceTransferPreview, type ResourceTransferResult } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { downloadResourceBundle, resourceClosure, resourceKey, toggleResourceSelection } from '@/lib/resourceTransfer'

type Mode = 'import' | 'export'
const ZIP_NAMES = { strategy: 'tsp-strategies.zip', signal: 'tsp-signals.zip', factor: 'tsp-factors.zip' }
const KIND_LABEL: Record<ResourceTransferEntry['kind'], string> = { strategy: '策略', signal: '信号', factor: '因子', settings: '调参', code: '辅助模块' }
const BUTTON = 'inline-flex h-8 shrink-0 items-center justify-center gap-1.5 rounded-btn border border-border bg-surface px-3 text-xs font-medium text-secondary transition-colors hover:border-accent/40 hover:text-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-40'
const PRIMARY = cn(BUTTON, 'border-blue-600 bg-blue-600 text-white hover:bg-blue-700 hover:text-white')

export function ResourceTransferButton({ kind, className }: { kind: ResourceKind; className?: string }) {
  const [open, setOpen] = useState(false)
  const label = `${KIND_LABEL[kind]}导入与导出`
  return <>
    <button type="button" aria-label={label} title={label} onClick={() => setOpen(true)}
      className={cn('inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-btn border border-border bg-surface text-secondary transition-colors hover:border-accent/40 hover:text-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent', className)}>
      <PackageOpen className="h-3.5 w-3.5" />
    </button>
    {open && <ResourceTransferDialog kind={kind} onClose={() => setOpen(false)} />}
  </>
}

interface Props {
  kind: ResourceKind
  initialMode?: Mode
  onClose: () => void
}

/** 各页共用同一份传输逻辑, 主列表只展示当前页类型, 依赖统一预览。 */
export function ResourceTransferDialog({ kind, initialMode = 'import', onClose }: Props) {
  const qc = useQueryClient()
  const [mode, setMode] = useState<Mode>(initialMode)
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [overwrite, setOverwrite] = useState<Set<string>>(new Set())
  const [files, setFiles] = useState<File[]>([])
  const [preview, setPreview] = useState<ResourceTransferPreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<ResourceTransferResult | null>(null)
  const [exported, setExported] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const previewPanel = useRef<HTMLElement>(null)
  const catalog = useQuery({ queryKey: QK.resourceCatalog, queryFn: api.resourcesList, staleTime: 0, enabled: mode === 'export' })

  useEffect(() => {
    if (mode !== 'export' || !catalog.data || !catalog.isSuccess || catalog.isFetching || busy) return
    const known = new Set(catalog.data.items.map(resourceKey))
    if ([...selected].some(key => !known.has(key))) {
      setSelected(new Set([...selected].filter(key => known.has(key))))
      setPreview(null)
      setError('部分资源已不存在，已移除。请重新选择要导出的资源。')
    }
  }, [catalog.data, catalog.isSuccess, catalog.isFetching, mode, selected, busy])

  const entries = preview?.entries ?? []
  const effective = useMemo(() => resourceClosure(entries, selected), [entries, selected])
  const included = entries.filter(entry => effective.has(entry.key))
  const conflicts = mode === 'import' ? included.filter(entry => entry.action === 'conflict') : []
  const unresolved = conflicts.filter(entry => !overwrite.has(entry.key))
  const issues = included.flatMap(entry => entry.issues.map(issue => `${entry.label}: ${issue}`))
  const primaryCount = included.filter(entry => ['strategy', 'signal', 'factor'].includes(entry.kind)).length
  const dependencyCount = included.filter(entry => !selected.has(entry.key) && ['strategy', 'signal', 'factor'].includes(entry.kind)).length
  const mainEntries = included.filter(entry => ['strategy', 'signal', 'factor'].includes(entry.kind))
    .sort((a, b) => (mode === 'import' ? Number(b.action === 'conflict') - Number(a.action === 'conflict') : 0)
      || ['strategy', 'signal', 'factor'].indexOf(a.kind) - ['strategy', 'signal', 'factor'].indexOf(b.kind))
  const attachedEntries = included.filter(entry => entry.kind === 'settings' || entry.kind === 'code')
  const options = (mode === 'export'
    ? (catalog.data?.items ?? []).map(item => ({ ...item, key: resourceKey(item) }))
    : entries.filter(entry => entry.kind === kind))
    .sort((a, b) => ['strategy', 'signal', 'factor'].indexOf(a.kind) - ['strategy', 'signal', 'factor'].indexOf(b.kind))
  const visible = options.filter(item => item.kind === kind && `${item.label} ${item.id}`.toLowerCase().includes(search.trim().toLowerCase()))
  const refs = () => (catalog.data?.items ?? []).filter(item => selected.has(resourceKey(item))).map(({ kind, id }) => ({ kind, id }))

  const changeMode = (next: Mode) => {
    if (next === mode || busy) return
    setMode(next); setSelected(new Set()); setOverwrite(new Set()); setPreview(null)
    setFiles([]); setResult(null); setError(''); setSearch(''); setExported(false)
  }
  const select = (keys: string[]) => {
    setSelected(previous => toggleResourceSelection(previous, keys))
    setExported(false)
    if (mode === 'export') setPreview(null)
  }

  const inspectFiles = async (nextFiles: File[]) => {
    if (busy || !nextFiles.length) return
    setBusy(true); setError(''); setPreview(null); setResult(null); setOverwrite(new Set()); setSelected(new Set()); setFiles(nextFiles)
    try {
      const next = await api.resourcesInspect(nextFiles)
      const candidates = next.entries.filter(entry => entry.kind === kind).map(entry => entry.key)
      const roots = next.roots.filter(key => candidates.includes(key))
      setPreview(next); setSelected(new Set(roots.length ? roots : candidates)); setSearch('')
    } catch (err) { setError(err instanceof Error ? err.message : '资源文件解析失败，请重新选择') }
    finally { setBusy(false) }
  }

  const previewExport = async () => {
    setBusy(true); setError('')
    try {
      setPreview(await api.resourcesExportPreview(refs()))
      window.requestAnimationFrame(() => previewPanel.current?.scrollIntoView?.({ block: 'nearest' }))
    }
    catch (err) { setError(err instanceof Error ? err.message : '无法生成导出预览') }
    finally { setBusy(false) }
  }
  const download = async () => {
    setBusy(true); setError('')
    try {
      await downloadResourceBundle(refs(), ZIP_NAMES[kind], preview?.revision)
      setExported(true)
    } catch (err) { setError(err instanceof Error ? err.message : '导出失败，请重试'); setPreview(null) }
    finally { setBusy(false) }
  }
  const importSelected = async () => {
    if (!preview) return
    setBusy(true); setError('')
    try {
      const applied = await api.resourcesApply(files, [...selected], conflicts.filter(entry => overwrite.has(entry.key)).map(entry => entry.key), preview.revision)
      setResult(applied)
      const keys = [QK.resourceCatalog, QK.definitions, QK.customSignals, QK.customSignalsOptions, QK.factorColumns,
        QK.factorLibrary('all').slice(0, 1), QK.screenerStrategies().slice(0, 1), QK.strategyDetail('').slice(0, 1),
        QK.screenerCached().slice(0, 1), QK.screener, QK.monitorRuleOptions, QK.watchlist, QK.strategyLinkOptions().slice(0, 1)]
      await Promise.allSettled(keys.map(queryKey => qc.invalidateQueries({ queryKey })))
    } catch (err) { setError(err instanceof Error ? err.message : '导入失败，请重新预览') }
    finally { setBusy(false) }
  }

  const reset = () => {
    if (busy) return
    setSelected(new Set()); setOverwrite(new Set()); setFiles([]); setPreview(null)
    setResult(null); setExported(false); setError('')
  }
  const allVisibleSelected = visible.length > 0 && visible.every(item => selected.has(item.key))
  return createPortal(
    <Modal ariaLabel={`${KIND_LABEL[kind]}导入与导出`} onClose={() => { if (!busy) onClose() }} closeOnBackdrop={!busy}
      overlayClassName="fixed inset-0 z-[100] flex items-center justify-center bg-black/50 backdrop-blur-sm"
      panelClassName="flex h-[min(680px,90dvh)] w-[94vw] max-w-[1040px] flex-col overflow-hidden rounded-card border border-border bg-surface shadow-2xl">
      <header className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-3 sm:px-5">
        <div className="min-w-0">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-foreground"><PackageOpen className="h-4 w-4 text-accent" />{KIND_LABEL[kind]}导入与导出</h2>
          <p className="mt-1 text-[11px] leading-5 text-muted">{kind === 'strategy' ? 'ZIP 包含策略、关联因子、信号和已保存调参。' : `${KIND_LABEL[kind]}以 ZIP 包分享，所需的自定义依赖会自动携带。`}</p>
        </div>
        <button type="button" aria-label="关闭" disabled={busy} onClick={onClose} className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-btn text-muted hover:bg-elevated hover:text-foreground disabled:opacity-40"><X className="h-4 w-4" /></button>
      </header>
      <div className="flex shrink-0 gap-5 border-b border-border px-4 sm:px-5" role="tablist" aria-label="资源传输方式">
        {([['import', `导入${KIND_LABEL[kind]}`], ['export', '批量导出']] as const).map(([value, label]) => (
          <button key={value} type="button" role="tab" aria-selected={mode === value} disabled={busy} onClick={() => changeMode(value)}
            className={`border-b-2 py-3 text-xs font-medium transition-colors ${mode === value ? 'border-accent text-accent' : 'border-transparent text-secondary hover:text-foreground'}`}>{label}</button>
        ))}
      </div>

      {result ? (
        <div className="min-h-0 flex-1 overflow-y-auto p-6 sm:p-10" role="status">
          <Check className="mb-4 h-7 w-7 text-accent" />
          <h3 className="text-lg font-semibold text-foreground">资源已导入</h3>
          <p className="mt-2 text-sm text-secondary">新增或替换 {result.imported} 个文件，复用 {result.reused} 个已有文件。</p>
          <div className="mt-5 flex flex-wrap gap-5 text-sm text-foreground">{(['strategy', 'signal', 'factor'] as const).filter(kind => result.counts[kind]).map(kind => <span key={kind}>{KIND_LABEL[kind]} <strong className="ml-1 tabular-nums">{result.counts[kind]}</strong></span>)}</div>
          {Boolean(result.drafts?.length) && <p className="mt-4 text-xs leading-5 text-warning">{result.drafts!.length} 条策略保留为草稿。AI 草稿可在策略池的“AI”分组发布后使用。</p>}
          <p className="mt-5 text-xs leading-5 text-muted">当前页面的资源列表已刷新，可关闭窗口继续使用。{kind === 'strategy' && '导入不会改变策略池配置。'}</p>
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto md:flex md:flex-col md:overflow-hidden">
          <div className={mode === 'import' && !preview ? 'mx-auto flex min-h-full w-full max-w-4xl flex-col justify-center' : 'md:flex md:min-h-0 md:flex-1 md:flex-col'}>
          {mode === 'import' && (
            <div className={preview ? 'shrink-0 border-b border-border px-4 py-3 sm:px-5' : 'px-5 py-6 sm:px-10 sm:py-8'} onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); if (!busy) void inspectFiles(Array.from(event.dataTransfer.files)) }}>
              <input ref={fileInput} type="file" multiple accept=".zip,.py,.json" className="hidden" aria-label="选择资源文件" disabled={busy}
                onChange={event => { const next = Array.from(event.target.files ?? []); event.target.value = ''; void inspectFiles(next) }} />
              <button type="button" onClick={() => fileInput.current?.click()} disabled={busy}
                className={cn('flex w-full gap-3 rounded-card border border-dashed border-accent/40 bg-accent/5 px-4 transition-colors hover:bg-accent/10 disabled:opacity-60', preview ? 'items-center py-4 text-left' : 'min-h-[150px] flex-col items-center justify-center py-8 text-center')}>
                {busy && !preview ? <Loader2 className="h-7 w-7 shrink-0 animate-spin text-accent" /> : <FileArchive className={cn('shrink-0 text-accent', preview ? 'h-5 w-5' : 'h-8 w-8')} />}
                <span className={cn('min-w-0', preview && 'flex-1')}><span className="block text-xs font-medium text-foreground">{files.length ? `${files.length} 个文件 · 点击重新选择` : '选择文件，或将文件拖到这里'}</span><span className="mt-1 block break-words text-[11px] leading-5 text-muted">{files.length ? files.map(file => file.name).join('、') : '支持策略 .py、因子/信号 .json 或 ZIP，可多选并一起补齐依赖。普通 ZIP 无需清单文件。'}</span></span>
                {preview && <Upload className="h-4 w-4 shrink-0 text-accent" />}
              </button>
              {preview && <p className="mt-2 text-[11px] text-muted">文件已识别，尚未写入本机。选择需要的资源后再确认导入。</p>}
            </div>
          )}

          {(mode === 'export' || preview) && (
            <div className="grid min-h-[300px] md:min-h-0 md:flex-1 md:grid-rows-[minmax(0,1fr)] md:grid-cols-[minmax(0,0.95fr)_minmax(0,1.05fr)]">
              <section className="min-w-0 border-b border-border md:flex md:min-h-0 md:flex-col md:border-b-0 md:border-r">
                <div className="shrink-0 space-y-3 px-4 py-4 sm:px-5">
                  <div className="flex items-center justify-between gap-2"><h3 className="text-xs font-semibold">{`${mode === 'export' ? '选择要导出的' : '选择要导入的'}${KIND_LABEL[kind]}`} </h3><span className="text-[11px] text-muted">已选 {selected.size} 项</span></div>
                  {mode === 'export' && <p className="text-[11px] leading-5 text-muted">{kind === 'strategy' ? '支持多选策略，共享因子与信号只打包一份。' : kind === 'factor' ? '仅列出自定义与复合因子，内置因子无需打包。' : '选择要分享的信号，关联因子会自动打包。'}</p>}
                  <div className="relative"><Search className="pointer-events-none absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted" /><input aria-label="搜索资源" placeholder="搜索名称或 ID" value={search} disabled={busy} onChange={event => setSearch(event.target.value)} className="h-9 w-full rounded-input border border-border bg-base pl-8 pr-3 text-xs outline-none focus:border-accent" /></div>
                  <div className="flex items-center gap-3 text-[11px]"><button type="button" disabled={busy || !visible.length} onClick={() => select(visible.map(item => item.key))} className="text-accent disabled:text-muted">{allVisibleSelected ? '取消选择筛选结果' : '全选筛选结果'} ({visible.length})</button><button type="button" disabled={busy || !selected.size} onClick={() => { setSelected(new Set()); setExported(false); if (mode === 'export') setPreview(null) }} className="text-muted disabled:opacity-40">清空选择</button></div>
                </div>
                <div className="max-h-[200px] overflow-y-auto border-t border-border/60 px-2 pb-2 md:min-h-0 md:max-h-none md:flex-1">
                  {mode === 'export' && catalog.isFetching && <p className="p-5 text-center text-xs text-muted">资源列表加载中…</p>}
                  {mode === 'export' && catalog.isError && <div className="p-4 text-xs text-danger">资源列表加载失败。<button type="button" className="ml-2 text-accent" onClick={() => void catalog.refetch()}>重试</button></div>}
                  {!visible.length && (mode === 'import' || (!catalog.isFetching && !catalog.isError)) && <p className="p-5 text-center text-xs leading-5 text-muted">{search ? '没有匹配的资源，试试其他名称或 ID。' : mode === 'import' ? `文件中没有可导入的${KIND_LABEL[kind]}，请选择对应的资源文件。` : `暂无可导出的${KIND_LABEL[kind]}，可先在当前页创建。`}</p>}
                  {visible.map(item => {
                    const automatic = mode === 'import' && effective.has(item.key) && !selected.has(item.key)
                    return <label key={item.key} className={`flex cursor-pointer items-start gap-2.5 rounded-btn px-3 py-3 hover:bg-elevated/60 ${selected.has(item.key) || automatic ? 'bg-accent/5' : ''}`}>
                      <input type="checkbox" className="mt-0.5 shrink-0 accent-accent" aria-label={`选择${KIND_LABEL[item.kind]}：${item.label}`} checked={selected.has(item.key) || automatic} disabled={busy || automatic} onChange={() => select([item.key])} />
                      <span className="min-w-0 flex-1"><span className="flex flex-wrap items-center gap-2"><span className="break-words text-xs font-medium text-foreground">{item.label}</span><span className="text-[10px] text-muted">{KIND_LABEL[item.kind]}</span>{'draft' in item && Boolean(item.draft) && <span className="text-[10px] text-warning">草稿</span>}</span><span className="mt-1 block break-all text-[10px] text-muted">{item.id}</span>{automatic && <span className="mt-1 inline-flex items-center gap-1 text-[10px] text-accent"><Link2 className="h-3 w-3" />随所选资源自动携带</span>}</span>
                    </label>
                  })}
                </div>
              </section>

              <section ref={previewPanel} className="min-w-0 bg-base/30 px-4 py-4 sm:px-5 md:min-h-0 md:overflow-y-auto" aria-label="资源依赖预览">
                <div className="flex items-center justify-between gap-2"><h3 className="text-xs font-semibold">{mode === 'import' ? '导入预览' : '包内清单'}</h3>{preview && <span className="text-[11px] text-muted">自动携带 {dependencyCount} 项依赖</span>}</div>
                {!preview ? <div className="flex min-h-[220px] flex-col items-center justify-center gap-3 text-center"><Link2 className="h-6 w-6 text-muted/60" /><p className="max-w-64 text-xs leading-6 text-muted">选择资源后点击“预览导出”，查看包内定义和关联依赖。共享依赖只保留一份。</p></div> : (
                  <>
                    <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 border-b border-border pb-3 text-[11px] text-secondary">{(['strategy', 'signal', 'factor', 'settings'] as const).map(kind => { const count = included.filter(item => item.kind === kind).length; return count ? <span key={kind}>{KIND_LABEL[kind]} <strong className="ml-1 font-medium text-foreground">{count}</strong></span> : null })}{!included.length && <span>尚未选择资源</span>}</div>
                    <div className="mt-2 space-y-1">
                      {mainEntries.map(entry => <PreviewEntry key={entry.key} entry={entry} mode={mode} automatic={!selected.has(entry.key)} overwrite={overwrite.has(entry.key)} disabled={busy}
                        onOverwrite={() => setOverwrite(previous => { const next = new Set(previous); if (next.has(entry.key)) next.delete(entry.key); else next.add(entry.key); return next })} />)}
                      {attachedEntries.length > 0 && <details className="border-t border-border/60 px-2.5 pt-3 text-[11px] text-secondary" open={mode === 'import' && attachedEntries.some(entry => entry.action === 'conflict' || entry.issues.length > 0) ? true : undefined}><summary className="cursor-pointer">策略调参与辅助模块 ({attachedEntries.length})</summary><div className="mt-1">{attachedEntries.map(entry => <PreviewEntry key={entry.key} entry={entry} mode={mode} automatic overwrite={overwrite.has(entry.key)} disabled={busy} onOverwrite={() => setOverwrite(previous => { const next = new Set(previous); if (next.has(entry.key)) next.delete(entry.key); else next.add(entry.key); return next })} />)}</div></details>}
                    </div>
                    {mode === 'import' && conflicts.length > 0 && <p className="mt-3 text-[11px] leading-5 text-warning">{unresolved.length ? `还有 ${unresolved.length} 项内容不同，需要逐项确认替换，或取消引用它的资源。` : `已确认替换 ${conflicts.length} 项本机内容。`}</p>}
                    {issues.length > 0 && <div role="alert" className="mt-3 rounded-btn border border-danger/20 bg-danger/5 px-3 py-2 text-[11px] leading-5 text-danger"><span className="font-medium">所选资源尚不可用</span><ul className="mt-1 space-y-1">{[...new Set(issues)].map((issue, index) => <li key={index} className="break-words">{issue}</li>)}</ul></div>}
                    {mode === 'export' && <p className="mt-3 text-[11px] leading-5 text-muted">导出已保存的定义与调参。行情数据、计算结果和账号配置不包含在资源包中。</p>}
                  </>
                )}
              </section>
            </div>
          )}
          {mode === 'import' && !preview && !busy && <ol className="grid gap-5 px-5 pb-8 sm:grid-cols-3 sm:gap-6 sm:px-10" aria-label="导入步骤">
            {[
              ['识别文件', `从文件中识别${KIND_LABEL[kind]}及关联依赖。`],
              ['检查依赖与冲突', '相同内容自动复用，差异内容逐项确认。'],
              ['确认导入', '写入所选资源，关闭窗口继续使用。'],
            ].map(([title, description], index) => <li key={title} className="border-t border-border pt-3">
              <p className="text-xs font-medium text-secondary"><span className="mr-2 tabular-nums text-muted">{index + 1}</span>{title}</p>
              <p className="mt-1 text-[11px] leading-5 text-muted">{description}</p>
            </li>)}
          </ol>}
          {error && <div role="alert" className="mx-4 my-3 flex shrink-0 items-start gap-2 rounded-btn border border-danger/20 bg-danger/5 p-3 text-xs leading-5 text-danger"><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /><span className="min-w-0 flex-1 break-words">{error}</span>{mode === 'import' && files.length > 0 && <button type="button" className="shrink-0 underline" disabled={busy} onClick={() => void inspectFiles(files)}>重新预览</button>}</div>}
          {exported && <p role="status" className="mx-5 my-3 text-xs text-accent">资源包已生成，浏览器已开始下载。</p>}
          </div>
        </div>
      )}

      <footer className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-t border-border px-4 py-3 sm:px-5">
        <div className="text-[11px] leading-5 text-muted" aria-live="polite"><p>{busy ? '正在处理，请稍候…' : result ? '本次导入已完成' : preview ? `${primaryCount} 项资源${dependencyCount ? `，含 ${dependencyCount} 项自动关联依赖` : ''}` : mode === 'export' ? `已选择 ${selected.size} 项资源` : '预览确认后才会写入本机'}</p>{!result && (issues.length > 0 || unresolved.length > 0) ? <button type="button" className="text-warning underline underline-offset-2" onClick={() => previewPanel.current?.scrollIntoView?.({ block: 'start' })}>{issues.length ? `${issues.length} 项校验问题，查看原因` : `${unresolved.length} 项冲突待处理，查看冲突`}</button> : !result && preview ? <button type="button" className="text-accent underline underline-offset-2 md:hidden" onClick={() => previewPanel.current?.scrollIntoView?.({ block: 'start' })}>查看依赖清单</button> : null}</div>
        <div className="ml-auto flex items-center gap-2">
          {result ? <><button type="button" className={BUTTON} disabled={busy} onClick={reset}><ArrowLeft className="h-3.5 w-3.5" />继续导入</button><button type="button" className={PRIMARY} disabled={busy} onClick={onClose}>完成</button></> : <>
            <button type="button" className={BUTTON} disabled={busy} onClick={onClose}>取消</button>
            {mode === 'export' ? <button type="button" className={PRIMARY} disabled={busy || !catalog.isSuccess || catalog.isFetching || !selected.size || (preview !== null && issues.length > 0)} onClick={() => void (preview ? download() : previewExport())}>{busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}{preview ? '下载 ZIP 包' : '预览导出'}</button>
              : <button type="button" className={PRIMARY} disabled={busy || !preview || !primaryCount || issues.length > 0 || unresolved.length > 0} onClick={() => void importSelected()}>{busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Upload className="h-3.5 w-3.5" />}导入{primaryCount ? ` ${primaryCount} 项资源` : '所选资源'}</button>}
          </>}
        </div>
      </footer>
    </Modal>, document.body,
  )
}

function PreviewEntry({ entry, mode, automatic, overwrite, disabled, onOverwrite }: {
  entry: ResourceTransferEntry; mode: Mode; automatic: boolean; overwrite: boolean; disabled: boolean; onOverwrite: () => void
}) {
  const labels = { create: '新增', reuse: '内容相同，复用', conflict: '内容不同' }
  return <div className={`rounded-btn px-2.5 py-2.5 ${mode === 'import' && entry.action === 'conflict' ? 'border border-warning/25 bg-warning/5' : ''}`}>
    <div className="flex items-start justify-between gap-2"><div className="min-w-0"><p className="break-words text-xs text-foreground">{entry.label}</p><p className="mt-1 text-[10px] text-muted">{KIND_LABEL[entry.kind]}{automatic ? ' · 自动携带' : ' · 手动选择'}</p></div>{mode === 'import' && <span className={`shrink-0 text-[10px] ${entry.action === 'conflict' ? 'text-warning' : entry.action === 'create' ? 'text-accent' : 'text-muted'}`}>{labels[entry.action]}</span>}</div>
    {mode === 'import' && entry.action === 'conflict' && <label className="mt-2 flex cursor-pointer items-center gap-2 text-[11px] text-warning"><input type="checkbox" aria-label={`确认替换${entry.label}`} checked={overwrite} disabled={disabled} onChange={onOverwrite} className="accent-accent" />用包内版本替换本机版本</label>}
    {mode === 'import' && entry.action === 'conflict' && entry.referenced_by && entry.referenced_by.length > 0 && <p className="mt-2 break-words text-[10px] leading-5 text-warning">本机引用方：{entry.referenced_by.join('、')}</p>}
    {mode === 'import' && entry.comparison && <details className="mt-2 text-[11px] text-secondary"><summary className="cursor-pointer">对比本机与包内内容</summary><div className="mt-2 space-y-2">{([['local', '本机版本'], ['incoming', '包内版本']] as const).map(([key, label]) => <div key={key}><p className="mb-1 text-[10px] text-muted">{label}</p><pre className="max-h-36 overflow-auto whitespace-pre-wrap break-all rounded-input border border-border bg-base p-2 text-[10px] leading-5">{entry.comparison![key]}</pre></div>)}</div>{entry.comparison.truncated && <p className="mt-1 text-[10px] text-muted">较长内容仅展示前 6000 个字符。</p>}</details>}
    {entry.local_dependencies.length > 0 && <p className="mt-1 break-words text-[10px] text-muted">使用本机已有依赖：{entry.local_dependencies.map(key => key.split(':').slice(1).join(':')).join('、')}</p>}
    {entry.dependencies.length > 0 && <details className="mt-1 text-[10px] text-muted"><summary className="inline-flex cursor-pointer list-none items-center gap-1"><ChevronDown className="h-3 w-3" />查看依赖 ({entry.dependencies.length})</summary><p className="mt-1 break-all leading-5">{entry.dependencies.map(key => key.split(':').slice(1).join(':')).join('、')}</p></details>}
  </div>
}
