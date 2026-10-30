import { useState, useMemo, useEffect, useCallback } from 'react'
import { Reorder, useDragControls } from 'framer-motion'
import { ArrowDown, ArrowUp, Check, GripVertical, Layers, Loader2, Search, X } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { api, type StrategyDetail } from '@/lib/api'
import { cn } from '@/lib/cn'

interface Props { pool: string[]; onConfirm: (newPool: string[]) => void; onClose: () => void }
const SOURCE_LABEL: Record<string, string> = { custom: '自定义', ai: 'AI', composite: '叠加' }
const SOURCE_CLS: Record<string, string> = { custom: 'border-amber-400/25 text-amber-500', ai: 'border-purple-500/25 text-purple-400', composite: 'border-teal-500/25 text-teal-500' }
const BUTTON = 'inline-flex h-8 shrink-0 items-center justify-center gap-1.5 rounded-btn border border-border px-2.5 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40'
type SourceTab = 'all' | 'custom' | 'ai' | 'composite'
const TABS: { id: SourceTab; label: string }[] = [{ id: 'all', label: '全部' }, { id: 'custom', label: '自定义' }, { id: 'ai', label: 'AI' }, { id: 'composite', label: '叠加' }]

function SelectedRow({ id, strategy, index, count, disabled, resolved, onMove, onRemove }: {
  id: string; strategy?: StrategyDetail; index: number; count: number; disabled: boolean; resolved: boolean
  onMove: (from: number, offset: number) => void; onRemove: (id: string) => void
}) {
  const controls = useDragControls()
  const valid = strategy && !strategy.research_only
  const label = strategy?.name ?? id
  return <Reorder.Item value={id} dragListener={false} dragControls={controls} data-strategy-id={id}
    className={cn('flex items-center gap-2 rounded-btn border bg-surface px-2 py-3', valid || !resolved ? 'border-border' : 'border-danger/30 bg-danger/5')}
    whileDrag={{ zIndex: 1, boxShadow: '0 4px 12px rgba(0,0,0,.2)' }}>
    <span onPointerDown={event => { if (!disabled) controls.start(event) }} title="拖动排序，也可使用右侧上下移动按钮" className="inline-flex h-8 w-5 shrink-0 touch-none cursor-grab items-center justify-center text-muted active:cursor-grabbing"><GripVertical className="h-4 w-4" /></span>
    <span className="w-4 shrink-0 text-center text-[10px] tabular-nums text-muted">{index + 1}</span>
    <div className="min-w-0 flex-1"><p className="truncate text-xs font-medium text-foreground" title={label}>{label}</p><p className="mt-1 truncate text-[10px] text-muted" title={id}>{id}</p>{!valid && resolved && <p className="mt-1 text-[10px] text-danger">失效策略 · 已删除或尚未发布</p>}</div>
    <div className="flex shrink-0 items-center gap-0.5">
      <button type="button" aria-label={`上移：${label}`} title="上移" disabled={disabled || index === 0} onClick={() => onMove(index, -1)} className="inline-flex h-8 w-8 items-center justify-center rounded text-muted hover:bg-elevated hover:text-accent disabled:opacity-25"><ArrowUp className="h-3.5 w-3.5" /></button>
      <button type="button" aria-label={`下移：${label}`} title="下移" disabled={disabled || index === count - 1} onClick={() => onMove(index, 1)} className="inline-flex h-8 w-8 items-center justify-center rounded text-muted hover:bg-elevated hover:text-accent disabled:opacity-25"><ArrowDown className="h-3.5 w-3.5" /></button>
      <button type="button" aria-label={`移出策略池：${label}`} title="移出策略池，不删除策略" disabled={disabled} onClick={() => onRemove(id)} className="inline-flex h-8 w-8 items-center justify-center rounded text-muted hover:bg-danger/10 hover:text-danger disabled:opacity-25"><X className="h-3.5 w-3.5" /></button>
    </div>
  </Reorder.Item>
}

export function StrategyPoolDialog({ pool, onConfirm, onClose }: Props) {
  const [originalPool] = useState<string[]>(() => [...new Set(pool)])
  const [draftPool, setDraftPool] = useState<string[]>(() => [...originalPool])
  const [clearedPool, setClearedPool] = useState<string[] | null>(null)
  const [allStrategies, setAllStrategies] = useState<StrategyDetail[]>([])
  const [loading, setLoading] = useState(true)
  const [loaded, setLoaded] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [message, setMessage] = useState('')
  const [publishError, setPublishError] = useState('')
  const [publishingId, setPublishingId] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState<SourceTab>('all')
  const [search, setSearch] = useState('')
  const [timeframe, setTimeframe] = useState('all')
  const [mobilePane, setMobilePane] = useState<'library' | 'selected'>('library')
  const loadStrategies = useCallback(async () => {
    setLoading(true); setLoadError('')
    try {
      const data = await api.strategyList(undefined, 'all', true)
      setAllStrategies(data.strategies); setLoaded(true)
      return true
    } catch (error) { setLoadError(error instanceof Error ? error.message : '请检查连接后重试'); return false }
    finally { setLoading(false) }
  }, [])
  useEffect(() => { void loadStrategies() }, [loadStrategies])
  useEffect(() => { if (draftPool.length > 0) setClearedPool(null) }, [draftPool.length])
  const stratMap = useMemo(() => new Map(allStrategies.map(strategy => [strategy.id, strategy])), [allStrategies])
  const selectedIds = useMemo(() => new Set(draftPool), [draftPool])
  const ready = loaded && !loading && !loadError
  const disabled = !ready || publishingId !== null
  const invalid = ready ? draftPool.filter(id => !stratMap.has(id) || stratMap.get(id)!.research_only) : []
  const changed = JSON.stringify(draftPool) !== JSON.stringify(originalPool)
  const publicStrategies = allStrategies.filter(strategy => !strategy.research_only)
  const catalogCount = allStrategies.filter(strategy => !strategy.research_only || strategy.source === 'ai').length
  const query = search.trim().toLowerCase()
  const matches = (strategy: StrategyDetail) => (activeTab === 'all' || strategy.source === activeTab)
    && (timeframe === 'all' || (strategy.timeframes?.length ? strategy.timeframes : ['1d']).includes(timeframe))
    && `${strategy.name} ${strategy.id} ${strategy.description}`.toLowerCase().includes(query)
  const visible = publicStrategies.filter(matches)
  const drafts = allStrategies.filter(strategy => strategy.research_only && strategy.source === 'ai' && matches(strategy))
  const available = visible.filter(strategy => !selectedIds.has(strategy.id))
  const toggle = (id: string) => setDraftPool(previous => previous.includes(id) ? previous.filter(item => item !== id) : [...previous, id])
  const remove = (id: string) => setDraftPool(previous => previous.filter(item => item !== id))
  const move = (from: number, offset: number) => setDraftPool(previous => {
    const next = [...previous], to = from + offset
    if (to < 0 || to >= next.length) return previous
    ;[next[from], next[to]] = [next[to], next[from]]
    return next
  })
  const publish = async (strategy: StrategyDetail) => {
    setPublishingId(strategy.id); setPublishError(''); setMessage('')
    try {
      await api.strategyPublish(strategy.id)
      if (await loadStrategies()) setMessage(`已发布「${strategy.name}」，可勾选加入策略池。`)
    } catch (error) { setPublishError(error instanceof Error ? error.message : '发布失败，请重试') }
    finally { setPublishingId(null) }
  }

  return <Modal ariaLabel="策略池" onClose={() => { if (!publishingId) onClose() }} closeOnBackdrop={!publishingId}
    panelClassName="flex h-[min(720px,90dvh)] w-[94vw] max-w-[1080px] flex-col overflow-hidden rounded-card border border-border bg-surface shadow-2xl">
    <header className="flex shrink-0 items-start justify-between gap-3 border-b border-border px-4 py-3 sm:px-5">
      <div><h2 className="flex items-center gap-2 text-sm font-semibold text-foreground"><Layers className="h-4 w-4 text-accent" />策略池</h2><p className="mt-1 text-[11px] leading-5 text-muted">勾选参与扫描的策略，再调整显示顺序。移出策略池不会删除策略。</p></div>
      <button type="button" aria-label="关闭" disabled={!!publishingId} onClick={onClose} className="rounded-btn p-1.5 text-muted hover:bg-elevated hover:text-foreground disabled:opacity-40"><X className="h-4 w-4" /></button>
    </header>
    {loadError && <div role="alert" className="flex shrink-0 items-center gap-3 border-b border-danger/20 bg-danger/5 px-4 py-3 text-xs text-danger"><span className="min-w-0 flex-1 break-words">策略列表加载失败：{loadError}</span><button type="button" onClick={() => void loadStrategies()} className="shrink-0 underline">重试</button></div>}
    {publishError && <p role="alert" className="shrink-0 border-b border-danger/20 bg-danger/5 px-4 py-2 text-xs text-danger">发布失败：{publishError}</p>}
    {message && <p role="status" className="shrink-0 border-b border-border bg-accent/5 px-4 py-2 text-xs text-accent">{message}</p>}
    {invalid.length > 0 && <div role="alert" className="flex shrink-0 flex-wrap items-center gap-2 border-b border-warning/25 bg-warning/5 px-4 py-2 text-[11px] text-warning"><span className="flex-1">已选中 {invalid.length} 项失效策略，移除后才能保存。</span><button type="button" onClick={() => setDraftPool(previous => previous.filter(id => !invalid.includes(id)))} className="underline underline-offset-2">移除失效项</button></div>}
    <div className="flex shrink-0 border-b border-border px-4 md:hidden" aria-label="策略池视图">{([['library', '策略库'], ['selected', `已选 ${draftPool.length}`]] as const).map(([pane, label]) => <button type="button" key={pane} aria-pressed={mobilePane === pane} onClick={() => setMobilePane(pane)} className={cn('border-b-2 px-3 py-2.5 text-xs font-medium', mobilePane === pane ? 'border-accent text-accent' : 'border-transparent text-secondary')}>{label}</button>)}</div>
    <div className="grid min-h-0 flex-1 md:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
      <section aria-label="策略库" className={cn('min-h-0 min-w-0 flex-col md:flex md:border-r md:border-border', mobilePane === 'library' ? 'flex' : 'hidden')}>
        <div className="shrink-0 space-y-3 border-b border-border px-4 py-3">
          <div className="flex items-center justify-between gap-2"><h3 className="text-xs font-semibold text-foreground">策略库 <span className="ml-1 font-normal tabular-nums text-muted">{catalogCount}</span></h3><button type="button" disabled={disabled || !available.length} onClick={() => setDraftPool(previous => [...new Set([...previous, ...available.map(strategy => strategy.id)])])} className="text-[11px] text-accent disabled:opacity-40">加入筛选结果 ({available.length})</button></div>
          <div className="flex gap-2"><div className="relative min-w-0 flex-1"><Search className="pointer-events-none absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted" /><input aria-label="搜索策略" placeholder="搜索名称、ID 或描述" value={search} onChange={event => setSearch(event.target.value)} className="h-9 w-full rounded-input border border-border bg-base pl-8 pr-3 text-xs focus:border-accent focus:outline-none" /></div><select aria-label="策略周期" value={timeframe} onChange={event => setTimeframe(event.target.value)} className="h-9 rounded-input border border-border bg-base px-2 text-xs text-secondary focus:border-accent focus:outline-none"><option value="all">全部周期</option><option value="1d">日线</option><option value="1m">分钟</option></select></div>
          <div className="flex flex-wrap gap-1" aria-label="策略来源">{TABS.map(tab => {
            const count = allStrategies.filter(strategy => (!strategy.research_only || strategy.source === 'ai') && (tab.id === 'all' || strategy.source === tab.id)).length
            return <button type="button" key={tab.id} aria-pressed={activeTab === tab.id} onClick={() => setActiveTab(tab.id)} className={cn('rounded-btn px-2.5 py-1.5 text-[11px] transition-colors', activeTab === tab.id ? 'bg-accent/10 font-medium text-accent' : 'text-secondary hover:bg-elevated')}>{tab.label}<span className="ml-1.5 tabular-nums text-muted">{count}</span></button>
          })}</div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
          {loading && <p role="status" className="flex items-center justify-center gap-2 py-8 text-xs text-muted"><Loader2 className="h-4 w-4 animate-spin" />策略列表加载中…</p>}
          {!loading && !loadError && !visible.length && !drafts.length && <p className="px-3 py-10 text-center text-xs leading-6 text-muted">{publicStrategies.length || search || activeTab !== 'all' || timeframe !== 'all' ? '没有符合条件的策略，试试其他名称或筛选条件。' : '暂无策略，可先创建或导入策略。'}</p>}
          <div className="space-y-1">{visible.map(strategy => {
            const checked = selectedIds.has(strategy.id)
            return <label key={strategy.id} className={cn('flex cursor-pointer items-start gap-3 rounded-btn border px-3 py-3 transition-colors', checked ? 'border-accent/25 bg-accent/5' : 'border-transparent hover:bg-elevated/60')}>
              <input type="checkbox" aria-label={`选择策略：${strategy.name}`} checked={checked} disabled={disabled} onChange={() => toggle(strategy.id)} className="mt-1 shrink-0 accent-accent" />
              <div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-x-2 gap-y-1"><span className="min-w-0 break-words text-xs font-medium text-foreground">{strategy.name}</span><span className={cn('rounded border px-1 py-px text-[10px] leading-tight', SOURCE_CLS[strategy.source] ?? SOURCE_CLS.custom)}>{SOURCE_LABEL[strategy.source] ?? strategy.source}</span><span className="text-[10px] text-muted">{strategy.timeframes?.includes('1m') ? '分钟' : '日线'}</span>{checked && <span className="inline-flex items-center gap-1 text-[10px] text-accent"><Check className="h-3 w-3" />已加入</span>}</div><p className="mt-1 break-all text-[10px] text-muted">{strategy.id}</p>{strategy.description && <p className="mt-1 line-clamp-2 text-[11px] leading-5 text-secondary" title={strategy.description}>{strategy.description}</p>}</div>
            </label>
          })}</div>
          {drafts.length > 0 && <div className="mt-4 border-t border-border px-2 pt-3"><h4 className="text-[11px] font-medium text-secondary">AI 草稿 · 发布后可加入</h4><p className="mt-1 text-[10px] leading-5 text-muted">发布会立即生效；加入策略池需另行保存。</p><div className="mt-2 space-y-1">{drafts.map(strategy => <div key={strategy.id} className="flex items-center gap-3 rounded-btn border border-purple-500/15 bg-purple-500/5 px-3 py-3"><div className="min-w-0 flex-1"><p className="break-words text-xs text-foreground">{strategy.name}</p><p className="mt-1 break-all text-[10px] text-muted">{strategy.id}</p></div><button type="button" disabled={disabled} onClick={() => void publish(strategy)} className="inline-flex shrink-0 items-center gap-1 rounded-btn border border-purple-500/25 px-2 py-1 text-[11px] text-purple-400 disabled:opacity-40">{publishingId === strategy.id && <Loader2 className="h-3 w-3 animate-spin" />}发布</button></div>)}</div></div>}
        </div>
      </section>
      <section aria-label="已选策略" className={cn('min-h-0 min-w-0 flex-col bg-base/30 md:flex', mobilePane === 'selected' ? 'flex' : 'hidden')}>
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-3"><div><h3 className="text-xs font-semibold text-foreground">已选策略 <span className="ml-1 tabular-nums text-accent">{draftPool.length}</span></h3><p className="mt-1 text-[10px] leading-5 text-muted">拖动排序，或使用上下移动按钮。</p></div>{clearedPool && !draftPool.length
            ? <button type="button" disabled={disabled} onClick={() => { setDraftPool(clearedPool); setClearedPool(null) }} className="shrink-0 text-[11px] text-accent disabled:opacity-40">撤销清空</button>
            : <button type="button" disabled={disabled || !draftPool.length} onClick={() => { setClearedPool([...draftPool]); setDraftPool([]) }} className="shrink-0 text-[11px] text-muted hover:text-danger disabled:opacity-40">清空所选</button>}</div>
        <div className="min-h-0 flex-1 overflow-y-auto p-3">{!draftPool.length ? <p className="px-2 py-10 text-center text-xs leading-6 text-muted">尚未选择策略，在策略库勾选后会出现在这里。</p> : <Reorder.Group axis="y" values={draftPool} onReorder={next => { if (!disabled) setDraftPool(next) }} className="space-y-2">{draftPool.map((id, index) => <SelectedRow key={id} id={id} strategy={stratMap.get(id)} index={index} count={draftPool.length} disabled={disabled} resolved={!!ready} onMove={move} onRemove={remove} />)}</Reorder.Group>}</div>
      </section>
    </div>
    <footer className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-t border-border px-4 py-3 sm:px-5">
      <p className="text-[11px] text-muted" aria-live="polite">已选 <strong className="font-medium tabular-nums text-foreground">{draftPool.length}</strong> 项{changed ? ' · 有未保存的调整' : ' · 未做调整'}</p>
      <div className="ml-auto flex gap-2"><button type="button" title="还原为打开窗口时的策略池" disabled={!changed || !!publishingId} onClick={() => { setDraftPool([...originalPool]); setClearedPool(null) }} className={BUTTON}>还原</button><button type="button" disabled={!!publishingId} onClick={onClose} className={BUTTON}>取消</button><button type="button" disabled={disabled || !changed || invalid.length > 0} onClick={() => { onConfirm(draftPool); onClose() }} className={cn(BUTTON, 'border-blue-600 bg-blue-600 font-medium text-white hover:bg-blue-700 hover:text-white')}>保存策略池</button></div>
    </footer>
  </Modal>
}
