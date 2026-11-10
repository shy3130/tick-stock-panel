import { useEffect, useMemo, useState } from 'react'
import { Reorder, useDragControls } from 'framer-motion'
import { Archive, ArchiveRestore, ArrowDown, ArrowUp, FolderPlus, GripVertical, Layers, Loader2, Plus, Search, Trash2, X } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { api, type StrategyDetail, type StrategyPoolGroup } from '@/lib/api'
import { cn } from '@/lib/cn'
import { storage } from '@/lib/storage'
import type { StrategyPool } from '@/lib/useStrategyPool'
import './StrategyPoolDialog.css'

interface Props {
  pool: StrategyPool
  /** 新加入池的策略 ID (勾选/批量加入时回调, 页面决定是否立即扫描) */
  onAdded?: (ids: string[]) => void
  onClose: () => void
}

const SOURCE_LABEL: Record<string, string> = { custom: '自定义', ai: 'AI', composite: '叠加' }
const SOURCE_CLS: Record<string, string> = { custom: 'border-amber-400/25 text-amber-500', ai: 'border-purple-500/25 text-purple-400', composite: 'border-teal-500/25 text-teal-500' }
const BUTTON = 'inline-flex h-8 shrink-0 items-center justify-center gap-1.5 rounded-btn border border-border px-2.5 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40'
const SELECT_CLS = 'h-7 rounded border border-border bg-base px-1 text-[10px] text-secondary focus:border-accent focus:outline-none disabled:opacity-40'
type SourceTab = 'all' | 'custom' | 'ai' | 'composite'
const TABS: { id: SourceTab; label: string }[] = [{ id: 'all', label: '全部' }, { id: 'custom', label: '自定义' }, { id: 'ai', label: 'AI' }, { id: 'composite', label: '叠加' }]

/** 策略池排序 (视图偏好, 本地持久化); default = 后端目录顺序 */
type LibrarySort = 'default' | 'added_desc' | 'added_asc' | 'name_asc' | 'name_desc'
const SORT_OPTIONS: { id: LibrarySort; label: string }[] = [
  { id: 'default', label: '默认排序' },
  { id: 'added_desc', label: '加入时间 新→旧' },
  { id: 'added_asc', label: '加入时间 旧→新' },
  { id: 'name_asc', label: '名称 A→Z' },
  { id: 'name_desc', label: '名称 Z→A' },
]

function loadLibrarySort(): LibrarySort {
  const saved = storage.screenerPoolSort.get('default')
  return SORT_OPTIONS.some(option => option.id === saved) ? saved as LibrarySort : 'default'
}

/** ISO 秒级时间 → 「MM-DD HH:mm」短显示 */
function fmtTime(iso: string | null | undefined): string {
  if (!iso) return ''
  return iso.length >= 16 ? `${iso.slice(5, 10)} ${iso.slice(11, 16)}` : iso
}

// ── 已选策略行 (纯顺序管理: 拖拽/上下移/移出; 分组与归档在左侧库管理) ──

function SelectedRow({ id, label, strategy, addedAt, groupName, index, count, disabled, resolved, onMove, onRemove }: {
  id: string; label: string; strategy?: StrategyDetail; addedAt: string; groupName?: string
  index: number; count: number; disabled: boolean; resolved: boolean
  onMove: (from: number, offset: number) => void; onRemove: (id: string) => void
}) {
  const controls = useDragControls()
  const valid = strategy && !strategy.research_only
  return <Reorder.Item value={id} dragListener={false} dragControls={controls} data-strategy-id={id}
    className={cn('flex items-center gap-1.5 rounded-btn px-2 py-2', valid || !resolved ? 'hover:bg-elevated/60' : 'bg-danger/5')}
    whileDrag={{ zIndex: 1, boxShadow: '0 4px 12px rgba(0,0,0,.2)' }}>
    <span onPointerDown={event => { if (!disabled) controls.start(event) }} title="拖动排序，也可使用右侧上下移动按钮" className="inline-flex h-8 w-5 shrink-0 touch-none select-none cursor-grab items-center justify-center text-muted active:cursor-grabbing"><GripVertical className="h-4 w-4" /></span>
    <span className="w-4 shrink-0 text-center text-[10px] tabular-nums text-muted">{index + 1}</span>
    <div className="min-w-0 flex-1">
      <div className="flex items-center gap-1.5">
        <p className="truncate text-xs font-medium text-foreground" title={label}>{label}</p>
        {groupName && <span className="max-w-20 shrink-0 truncate rounded border border-border px-1 text-[9px] text-muted" title={`分组：${groupName}`}>{groupName}</span>}
      </div>
      <p className="mt-0.5 truncate text-[10px] leading-4 text-muted" title={`${id}${addedAt ? ` · 加入于 ${addedAt}` : ''}`}>{id}</p>
      {!valid && resolved && <p className="mt-1 text-[10px] text-danger">失效策略 · 已删除或尚未发布</p>}
    </div>
    <div className="flex shrink-0 items-center gap-0.5">
      <button type="button" aria-label={`上移：${label}`} title="上移" disabled={disabled || index === 0} onClick={() => onMove(index, -1)} className="inline-flex h-7 w-7 items-center justify-center rounded text-muted hover:bg-elevated hover:text-accent disabled:opacity-25"><ArrowUp className="h-3.5 w-3.5" /></button>
      <button type="button" aria-label={`下移：${label}`} title="下移" disabled={disabled || index === count - 1} onClick={() => onMove(index, 1)} className="inline-flex h-7 w-7 items-center justify-center rounded text-muted hover:bg-elevated hover:text-accent disabled:opacity-25"><ArrowDown className="h-3.5 w-3.5" /></button>
      <button type="button" aria-label={`移出策略池：${label}`} title="移出策略池，不删除策略" disabled={disabled} onClick={() => onRemove(id)} className="inline-flex h-7 w-7 items-center justify-center rounded text-muted hover:bg-danger/10 hover:text-danger disabled:opacity-25"><X className="h-3.5 w-3.5" /></button>
    </div>
  </Reorder.Item>
}

/** 档案库行: 恢复回库 (不回池) / 移出记录 */
function ArchivedRow({ strategy, addedAt, archivedAt, groupName, onRestore, onDiscard }: {
  strategy: StrategyDetail; addedAt: string; archivedAt: string; groupName?: string
  onRestore: (id: string) => void; onDiscard: (id: string) => void
}) {
  return <div className="flex items-center gap-3 rounded-btn px-3 py-2.5 hover:bg-elevated/60">
    <Archive className="h-3.5 w-3.5 shrink-0 text-muted/70" />
    <div className="min-w-0 flex-1">
      <div className="flex items-center gap-1.5">
        <p className="truncate text-xs text-secondary" title={strategy.name}>{strategy.name}</p>
        {groupName && <span className="max-w-24 shrink-0 truncate rounded border border-border px-1 text-[9px] text-muted" title={groupName}>{groupName}</span>}
      </div>
      <p className="mt-1 truncate text-[10px] text-muted">
        归档于 {archivedAt || '—'}{addedAt && ` · 加入于 ${addedAt}`}
      </p>
    </div>
    <div className="flex shrink-0 items-center gap-1">
      <button type="button" onClick={() => onRestore(strategy.id)} title="恢复到策略库 (需重新勾选入池)"
        className="inline-flex h-7 items-center gap-1 rounded border border-accent/30 px-2 text-[10px] text-accent transition-colors hover:bg-accent/10">
        <ArchiveRestore className="h-3 w-3" />恢复
      </button>
      <button type="button" aria-label={`移出档案库：${strategy.name}`} onClick={() => onDiscard(strategy.id)}
        title="移出档案库记录 (策略本身不受影响)"
        className="inline-flex h-7 w-7 items-center justify-center rounded text-muted hover:bg-danger/10 hover:text-danger">
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  </div>
}

/** 分组管理弹窗: 新建/重命名/排序/删除 (即时生效; 删除分组不删除组内策略)。 */
function GroupsManagerModal({ groups, active, onClose, onCreate, onRename, onReorder, onDelete }: {
  groups: StrategyPoolGroup[]; active: boolean; onClose: () => void
  onCreate: (name: string) => Promise<unknown>
  onRename: (groupId: string, name: string) => Promise<unknown>
  onReorder: (orderedIds: string[]) => Promise<unknown>
  onDelete: (groupId: string) => Promise<unknown>
}) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  const guard = async (op: () => Promise<unknown>) => {
    setBusy(true)
    try { await op() } catch (error) { toast(`分组操作失败：${error instanceof Error ? error.message : '请重试'}`, 'error') } finally { setBusy(false) }
  }
  const submitCreate = () => {
    const trimmed = name.trim()
    if (!trimmed) return
    void guard(async () => { await onCreate(trimmed); setName('') })
  }
  const submitRename = (groupId: string) => {
    const trimmed = renameDraft.trim()
    if (!trimmed) return
    void guard(async () => { await onRename(groupId, trimmed); setRenamingId(null) })
  }
  const move = (index: number, offset: number) => {
    const next = [...groups]
    const to = index + offset
    if (to < 0 || to >= next.length) return
    ;[next[index], next[to]] = [next[to], next[index]]
    void guard(() => onReorder(next.map(g => g.id)))
  }
  return <Modal ariaLabel="管理策略分组" active={active} onClose={onClose}
    panelClassName="flex max-h-[80%] w-[92%] max-w-md flex-col overflow-hidden rounded-card border border-border bg-surface shadow-2xl">
    <header className="flex items-center justify-between border-b border-border px-4 py-3">
      <h3 className="text-sm font-semibold text-foreground">管理分组</h3>
      <button type="button" aria-label="关闭" onClick={onClose} className="rounded p-1 text-muted hover:bg-elevated hover:text-foreground"><X className="h-4 w-4" /></button>
    </header>
    <div className="flex gap-2 border-b border-border px-4 py-3">
      <input value={name} onChange={event => setName(event.target.value)} maxLength={24} placeholder="新分组名称" aria-label="新分组名称"
        onKeyDown={event => { if (event.key === 'Enter') submitCreate() }}
        className="h-8 min-w-0 flex-1 rounded-input border border-border bg-base px-2.5 text-xs focus:border-accent focus:outline-none" />
      <button type="button" onClick={submitCreate} disabled={busy || !name.trim()}
        className="inline-flex h-8 shrink-0 items-center gap-1 rounded-btn border border-accent/40 px-2.5 text-xs text-accent hover:bg-accent/10 disabled:opacity-40">
        <Plus className="h-3.5 w-3.5" />新建
      </button>
    </div>
    <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto px-4 py-3">
      {groups.length === 0 && <p className="py-6 text-center text-xs text-muted">暂无分组。新建后可在策略池里把策略归入分组。</p>}
      {groups.map((group, index) => (
        <div key={group.id} className="flex items-center gap-2 rounded-btn border border-border bg-base/40 px-2.5 py-2">
          {renamingId === group.id ? (
            <>
              <input value={renameDraft} onChange={event => setRenameDraft(event.target.value)} maxLength={24} autoFocus aria-label="分组名称"
                onKeyDown={event => { if (event.key === 'Enter') submitRename(group.id); if (event.key === 'Escape') setRenamingId(null) }}
                className="h-7 min-w-0 flex-1 rounded border border-border bg-base px-2 text-xs focus:border-accent focus:outline-none" />
              <button type="button" onClick={() => submitRename(group.id)} disabled={busy} className="text-[11px] text-accent disabled:opacity-40">保存</button>
              <button type="button" onClick={() => setRenamingId(null)} className="text-[11px] text-muted">取消</button>
            </>
          ) : (
            <>
              <span className="min-w-0 flex-1 truncate text-xs text-foreground" title={group.name}>{group.name}</span>
              <button type="button" aria-label={`上移分组：${group.name}`} disabled={busy || index === 0} onClick={() => move(index, -1)} title="上移" className="inline-flex h-7 w-7 items-center justify-center rounded text-muted hover:bg-elevated hover:text-accent disabled:opacity-25"><ArrowUp className="h-3 w-3" /></button>
              <button type="button" aria-label={`下移分组：${group.name}`} disabled={busy || index === groups.length - 1} onClick={() => move(index, 1)} title="下移" className="inline-flex h-7 w-7 items-center justify-center rounded text-muted hover:bg-elevated hover:text-accent disabled:opacity-25"><ArrowDown className="h-3 w-3" /></button>
              <button type="button" onClick={() => { setRenamingId(group.id); setRenameDraft(group.name) }} title="重命名" className="rounded px-1.5 py-1 text-[11px] text-secondary hover:text-accent">改名</button>
              {confirmDeleteId === group.id ? (
                <button type="button" disabled={busy} onClick={() => { void guard(async () => { await onDelete(group.id); setConfirmDeleteId(null) }) }}
                  className="rounded border border-danger/40 bg-danger/10 px-1.5 py-1 text-[11px] text-danger disabled:opacity-40">确认删除</button>
              ) : (
                <button type="button" onClick={() => setConfirmDeleteId(group.id)} title="删除分组 (组内策略保留, 转为未分组)"
                  className="rounded p-1 text-muted hover:bg-danger/10 hover:text-danger"><Trash2 className="h-3 w-3" /></button>
              )}
            </>
          )}
        </div>
      ))}
    </div>
    <footer className="border-t border-border px-4 py-2.5">
      <p className="text-[11px] text-muted">删除分组不会移除组内策略，策略会转为「未分组」。</p>
    </footer>
  </Modal>
}

export function StrategyPoolDialog({ pool, onAdded, onClose }: Props) {
  const { pool: poolIds, groups, groupNames, metaMap, archivedIds } = pool
  const [allStrategies, setAllStrategies] = useState<StrategyDetail[]>([])
  const [loading, setLoading] = useState(true)
  const [loaded, setLoaded] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [message, setMessage] = useState('')
  const [publishError, setPublishError] = useState('')
  const [publishingId, setPublishingId] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState<SourceTab>('all')
  const [groupFilter, setGroupFilter] = useState<string>('all')
  const [sort, setSort] = useState<LibrarySort>(loadLibrarySort)
  const [search, setSearch] = useState('')
  const [timeframe, setTimeframe] = useState('all')
  // 左侧视图: 策略库 / 档案库 (桌面与移动共用; 移动端另有 library 切换)
  const [leftPane, setLeftPane] = useState<'library' | 'archive'>('library')
  const [mobilePane, setMobilePane] = useState<'library' | 'selected' | 'archive'>('library')
  const [showGroups, setShowGroups] = useState(false)
  const [clearedPool, setClearedPool] = useState<string[] | null>(null)

  const changeSort = (mode: LibrarySort) => {
    setSort(mode)
    storage.screenerPoolSort.set(mode)
  }

  const loadStrategies = async () => {
    setLoading(true); setLoadError('')
    try {
      const data = await api.strategyList(undefined, 'all', true)
      setAllStrategies(data.strategies); setLoaded(true)
      return true
    } catch (error) { setLoadError(error instanceof Error ? error.message : '请检查连接后重试'); return false }
    finally { setLoading(false) }
  }
  useEffect(() => { void loadStrategies() }, [])

  const stratMap = useMemo(() => new Map(allStrategies.map(strategy => [strategy.id, strategy])), [allStrategies])
  const poolSet = useMemo(() => new Set(poolIds), [poolIds])
  const archivedSet = useMemo(() => new Set(archivedIds), [archivedIds])
  const ready = loaded && !loading && !loadError
  const disabled = !ready || publishingId !== null

  // 策略库: 只列未归档的公开策略 (归档的在左侧档案库视图)
  const publicStrategies = useMemo(
    () => allStrategies.filter(strategy => !strategy.research_only && !archivedSet.has(strategy.id)),
    [allStrategies, archivedSet],
  )
  const query = search.trim().toLowerCase()
  const matches = (strategy: StrategyDetail) => (activeTab === 'all' || strategy.source === activeTab)
    && (timeframe === 'all' || (strategy.timeframes?.length ? strategy.timeframes : ['1d']).includes(timeframe))
    && `${strategy.name} ${strategy.id} ${strategy.description}`.toLowerCase().includes(query)
  const matchesGroup = (strategy: StrategyDetail, filter: string) => filter === 'all'
    || (filter === 'none' ? !metaMap.get(strategy.id)?.group : metaMap.get(strategy.id)?.group === filter)
  // 列表与分组批量操作共用筛选结果, 分组之间的数量不受当前选中分组限制。
  const matchingStrategies = useMemo(() => publicStrategies.filter(matches), [publicStrategies, activeTab, timeframe, query])
  const visible = useMemo(() => {
    const list = matchingStrategies.filter(strategy => matchesGroup(strategy, groupFilter))
    if (sort === 'default') return list
    const nameOf = (s: StrategyDetail) => s.name
    const addedOf = (s: StrategyDetail) => metaMap.get(s.id)?.added_at ?? ''
    const sorted = [...list]
    if (sort === 'added_desc') sorted.sort((a, b) => addedOf(b).localeCompare(addedOf(a)))
    else if (sort === 'added_asc') sorted.sort((a, b) => addedOf(a).localeCompare(addedOf(b)))
    else if (sort === 'name_asc') sorted.sort((a, b) => nameOf(a).localeCompare(nameOf(b), 'zh-Hans-CN'))
    else sorted.sort((a, b) => nameOf(b).localeCompare(nameOf(a), 'zh-Hans-CN'))
    return sorted
  }, [matchingStrategies, groupFilter, sort, metaMap])
  const drafts = allStrategies.filter(strategy => strategy.research_only && strategy.source === 'ai'
    && !archivedSet.has(strategy.id) && matches(strategy) && matchesGroup(strategy, groupFilter))

  // 分组数量、勾选状态与批量操作均按当前来源/周期/搜索范围计算。
  const groupMemberIds = (filter: string): string[] => matchingStrategies
    .filter(strategy => matchesGroup(strategy, filter))
    .map(strategy => strategy.id)
  const groupChipState = (ids: string[]) => {
    const inPool = ids.filter(id => poolSet.has(id))
    return { count: ids.length, checked: ids.length > 0 && inPool.length === ids.length, indeterminate: inPool.length > 0 && inPool.length < ids.length }
  }
  /** 全选/反选: 有未入池的 → 补齐全部; 已全在池中 → 整组移出 */
  const toggleGroupPool = (ids: string[]) => {
    const fresh = ids.filter(id => !poolSet.has(id))
    if (fresh.length > 0) {
      pool.reorderPool([...poolIds, ...fresh])
      notifyAdded(fresh)
    } else {
      pool.reorderPool(poolIds.filter(id => !ids.includes(id)))
    }
  }

  // 档案库: 有策略详情的归档条目 (记录在但策略已删除的不再展示, 记录由 prune/删除级联清理)
  const archivedRows = useMemo(
    () => archivedIds
      .map(id => ({ id, strategy: stratMap.get(id) }))
      .filter((row): row is { id: string; strategy: StrategyDetail } =>
        !!row.strategy && !row.strategy.research_only),
    [archivedIds, stratMap],
  )

  // ── 实时操作 (无草稿/保存): 勾选即入池, 归档/分组即时生效 ──
  const notifyAdded = (ids: string[]) => { if (ids.length > 0) onAdded?.(ids) }
  const toggle = (id: string) => {
    if (poolSet.has(id)) { pool.removeFromPool(id); return }
    pool.addToPool(id)
    notifyAdded([id])
  }
  const move = (from: number, offset: number) => {
    const next = [...poolIds], to = from + offset
    if (to < 0 || to >= next.length) return
    ;[next[from], next[to]] = [next[to], next[from]]
    pool.reorderPool(next)
  }
  const clearSelected = () => {
    setClearedPool([...poolIds])
    pool.reorderPool([])
  }
  const undoClear = () => {
    if (clearedPool) pool.reorderPool(clearedPool.filter(id => !archivedSet.has(id)))
    setClearedPool(null)
  }

  const publish = async (strategy: StrategyDetail) => {
    setPublishingId(strategy.id); setPublishError(''); setMessage('')
    try {
      await api.strategyPublish(strategy.id)
      if (await loadStrategies()) setMessage(`已发布「${strategy.name}」，可勾选加入策略池。`)
    } catch (error) { setPublishError(error instanceof Error ? error.message : '发布失败，请重试') }
    finally { setPublishingId(null) }
  }

  const labelOf = (id: string) => stratMap.get(id)?.name ?? id
  const leftTabBar = (pane: 'library' | 'archive') => (
    <div className="strategy-library-desktop-tabs shrink-0 items-stretch border-b border-border px-1" role="tablist" aria-label="策略池视图">
      {([['library', `策略库 ${publicStrategies.length}`], ['archive', `档案库 ${archivedRows.length}`]] as const).map(([paneId, label]) => (
        <button type="button" key={paneId} role="tab" aria-selected={pane === paneId}
          onClick={() => { setLeftPane(paneId); setMobilePane(paneId) }}
          className={cn('border-b-2 px-3 py-2 text-xs font-medium transition-colors', pane === paneId ? 'border-accent text-accent' : 'border-transparent text-secondary hover:text-foreground')}>
          {label}
        </button>
      ))}
    </div>
  )

  return <>
    <Modal ariaLabel="策略库" active={!showGroups} onClose={() => { if (!publishingId) onClose() }} closeOnBackdrop={!publishingId}
      panelClassName="strategy-library-dialog flex h-[92%] w-[96%] max-w-[1440px] max-h-[900px] flex-col overflow-hidden rounded-card border border-border bg-surface shadow-2xl">
      <header className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-2.5 sm:px-5">
        <div className="flex min-w-0 flex-wrap items-baseline gap-x-4 gap-y-1"><h2 className="flex shrink-0 items-center gap-2 text-sm font-semibold text-foreground"><Layers className="h-4 w-4 self-center text-accent" />策略库</h2><p className="text-[11px] leading-5 text-muted">勾选参与扫描，拖动调整顺序；分组与归档即时生效。</p></div>
        <button type="button" aria-label="关闭" disabled={!!publishingId} onClick={onClose} className="rounded-btn p-1.5 text-muted hover:bg-elevated hover:text-foreground disabled:opacity-40"><X className="h-4 w-4" /></button>
      </header>
      {loadError && <div role="alert" className="flex shrink-0 items-center gap-3 border-b border-danger/20 bg-danger/5 px-4 py-3 text-xs text-danger"><span className="min-w-0 flex-1 break-words">策略列表加载失败：{loadError}</span><button type="button" onClick={() => void loadStrategies()} className="shrink-0 underline">重试</button></div>}
      {publishError && <p role="alert" className="shrink-0 border-b border-danger/20 bg-danger/5 px-4 py-2 text-xs text-danger">发布失败：{publishError}</p>}
      {message && <p role="status" className="shrink-0 border-b border-border bg-accent/5 px-4 py-2 text-xs text-accent">{message}</p>}
      <div className="strategy-library-mobile-tabs flex shrink-0 border-b border-border px-3" aria-label="策略池视图">
        {([['library', `策略库 ${publicStrategies.length}`], ['selected', `已选 ${poolIds.length}`], ['archive', `档案库 ${archivedRows.length}`]] as const).map(([pane, label]) => (
          <button type="button" key={pane} aria-pressed={mobilePane === pane}
            onClick={() => { setMobilePane(pane); if (pane !== 'selected') setLeftPane(pane) }}
            className={cn('flex-1 border-b-2 px-3 py-2.5 text-xs font-medium', mobilePane === pane ? 'border-accent text-accent' : 'border-transparent text-secondary')}>{label}</button>
        ))}
      </div>
      <div className="strategy-library-panels grid min-h-0 flex-1 overflow-hidden">
        {/* 左: 策略库 / 档案库 (分组与归档都在这里) */}
        <section aria-label="策略库与档案库" className={cn('strategy-library-pane strategy-library-catalog min-h-0 min-w-0 flex-col overflow-hidden', mobilePane === 'library' || mobilePane === 'archive' ? 'flex' : 'hidden')}>
          {leftTabBar(leftPane)}
          <div className={cn('min-h-0 flex-1 flex-col overflow-hidden', leftPane === 'library' ? 'flex' : 'hidden')} aria-label="策略池">
            <div aria-label="策略筛选" className="shrink-0 space-y-2 border-b border-border bg-surface px-3 py-2.5">
              <div className="strategy-library-filter-fields grid grid-cols-2 gap-2">
                <div className="strategy-library-search relative col-span-2 min-w-0"><Search className="pointer-events-none absolute left-2 top-2 h-3.5 w-3.5 text-muted" /><input aria-label="搜索策略" placeholder="搜索名称、ID 或描述" value={search} onChange={event => setSearch(event.target.value)} className="h-8 w-full rounded-input border border-border bg-base pl-7 pr-3 text-xs focus:border-accent focus:outline-none" /></div>
                <select aria-label="策略周期" value={timeframe} onChange={event => setTimeframe(event.target.value)} className="h-8 rounded-input border border-border bg-base px-2 text-xs text-secondary focus:border-accent focus:outline-none"><option value="all">全部周期</option><option value="1d">日线</option><option value="1m">分钟</option></select>
                <select aria-label="策略池排序" value={sort} onChange={event => changeSort(event.target.value as LibrarySort)} className="h-8 rounded-input border border-border bg-base px-2 text-xs text-secondary focus:border-accent focus:outline-none">
                  {SORT_OPTIONS.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}
                </select>
              </div>
              {/* 来源分类: 切换时重置下级分组筛选 */}
              <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1" aria-label="策略来源分类">
                <span className="mr-0.5 text-[10px] text-muted/80">来源</span>
                {TABS.map(tab => {
                const count = allStrategies.filter(strategy => (!strategy.research_only || strategy.source === 'ai') && !archivedSet.has(strategy.id) && (tab.id === 'all' || strategy.source === tab.id)).length
                return <button type="button" key={tab.id} aria-pressed={activeTab === tab.id} onClick={() => { setActiveTab(tab.id); setGroupFilter('all') }} className={cn('rounded-btn px-2 py-0.5 text-[11px] transition-colors', activeTab === tab.id ? 'bg-accent/10 font-medium text-accent' : 'text-secondary hover:bg-elevated')}>{tab.label}<span className="ml-1 tabular-nums text-muted">{count}</span></button>
              })}</div>
              {/* 分组横向铺开: 点名称筛选该分组, 勾选框整组入池/出池 (全选/反选) */}
              <div className="flex items-center gap-2" aria-label="策略分组">
                <span className="shrink-0 text-[10px] text-muted/80">分组</span>
                <div role="region" aria-label="分组列表" tabIndex={0} className="flex min-w-0 flex-1 items-center gap-1.5 overflow-x-auto overscroll-x-contain py-0.5 focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent/50">
                {([['all', '全部'], ['none', '未分组'], ...groups.map(group => [group.id, group.name] as const)] as const).map(([key, label]) => {
                  const ids = groupMemberIds(key)
                  const state = groupChipState(ids)
                  return (
                    <div key={key} role="group" aria-label={`分组：${label}`} title="点击筛选该分组；勾选框按当前来源、周期及搜索结果入池 / 出池（仅已发布策略）"
                      onClick={() => setGroupFilter(key)}
                      className={cn('flex shrink-0 cursor-pointer select-none items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] transition-colors',
                        groupFilter === key ? 'border-accent/50 bg-accent/10 text-accent' : 'border-border bg-base/60 text-secondary hover:border-accent/30 hover:text-foreground')}>
                      <input type="checkbox" aria-label={`${label}全选反选`} checked={state.checked} disabled={disabled || state.count === 0}
                        title={state.count ? '全选或移出当前筛选范围内的组内策略' : '当前筛选下没有可选策略'}
                        ref={el => { if (el) el.indeterminate = state.indeterminate }}
                        onClick={event => event.stopPropagation()}
                        onChange={() => toggleGroupPool(ids)}
                        className="h-3 w-3 cursor-pointer accent-accent disabled:cursor-not-allowed" />
                      <span className="font-medium">{label}</span>
                      <span className="tabular-nums text-muted">{state.count}</span>
                    </div>
                  )
                })}
                </div>
                <button type="button" onClick={() => setShowGroups(true)}
                  className="inline-flex h-7 shrink-0 items-center gap-1 rounded border border-border px-2 text-[10px] text-muted transition-colors hover:border-accent/40 hover:text-accent">
                  <FolderPlus className="h-3 w-3" />管理分组
                </button>
              </div>
            </div>
            <div role="region" aria-label="策略列表" tabIndex={0} className="scrollbar-gutter-stable min-h-0 flex-1 overflow-y-auto overscroll-contain px-2 py-1 focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent/50">
              {loading && <p role="status" className="flex items-center justify-center gap-2 py-8 text-xs text-muted"><Loader2 className="h-4 w-4 animate-spin" />策略列表加载中…</p>}
              {!loading && !loadError && !visible.length && !drafts.length && <p className="px-3 py-10 text-center text-xs leading-6 text-muted">{publicStrategies.length || search || activeTab !== 'all' || timeframe !== 'all' || groupFilter !== 'all' ? '没有符合条件的策略，试试其他筛选条件。' : '暂无策略，可先创建或导入策略。'}</p>}
              <div className="divide-y divide-border/60">{visible.map(strategy => {
                const checked = poolSet.has(strategy.id)
                const meta = metaMap.get(strategy.id)
                return <div key={strategy.id} className={cn('strategy-library-strategy-row flex flex-col gap-2 rounded-btn px-2.5 py-2 transition-colors', checked ? 'bg-accent/5' : 'hover:bg-elevated/60')}>
                  <label className="flex min-w-0 flex-1 cursor-pointer items-start gap-2.5">
                    <input type="checkbox" aria-label={`选择策略：${strategy.name}`} checked={checked} disabled={disabled} onChange={() => toggle(strategy.id)} className="mt-1 shrink-0 accent-accent" />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-start gap-2">
                        <span className="strategy-library-strategy-name min-w-0 break-words text-xs font-medium text-foreground" title={strategy.name}>{strategy.name}</span>
                        <span className={cn('shrink-0 rounded border px-1 py-px text-[10px] leading-tight', SOURCE_CLS[strategy.source] ?? SOURCE_CLS.custom)}>{SOURCE_LABEL[strategy.source] ?? strategy.source}</span>
                        <span className="shrink-0 text-[10px] text-muted">{strategy.timeframes?.includes('1m') ? '分钟' : '日线'}</span>
                      </div>
                      <p className="mt-0.5 truncate text-[10px] leading-4 text-muted" title={`${strategy.id}${meta?.added_at ? ` · 加入于 ${fmtTime(meta.added_at)}` : ''}`}>{strategy.id}{meta?.added_at && <span className="ml-2">加入于 {fmtTime(meta.added_at)}</span>}</p>
                      {strategy.description && <p className="mt-0.5 truncate text-[11px] leading-4 text-secondary" title={strategy.description}>{strategy.description}</p>}
                    </div>
                  </label>
                  <div className="strategy-library-row-actions ml-6 flex shrink-0 items-center justify-end gap-1.5">
                    <select aria-label={`分组：${strategy.name}`} value={meta?.group ?? ''}
                      disabled={disabled || groups.length === 0}
                      onChange={event => pool.setStrategyGroup(strategy.id, event.target.value || null)}
                      title={groups.length === 0 ? '暂无分组，点击「管理分组」新建' : '所属分组'}
                      className={cn(SELECT_CLS, 'w-24 max-w-28')}>
                      <option value="">未分组</option>
                      {groups.map(group => <option key={group.id} value={group.id}>{group.name}</option>)}
                    </select>
                    <button type="button" aria-label={`归档：${strategy.name}`} disabled={disabled}
                      onClick={() => pool.archiveStrategy(strategy.id)}
                      title="归档到档案库 (可恢复, 不删除策略)"
                      className="inline-flex h-7 w-7 items-center justify-center rounded text-muted hover:bg-warning/10 hover:text-warning disabled:opacity-25">
                      <Archive className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
              })}</div>
              {drafts.length > 0 && <div className="mt-4 border-t border-border px-2 pt-3"><h4 className="text-[11px] font-medium text-secondary">AI 草稿 · 发布后可加入</h4><p className="mt-1 text-[10px] leading-5 text-muted">发布会立即生效；发布后勾选即可入池。</p><div className="mt-2 space-y-1">{drafts.map(strategy => <div key={strategy.id} className="flex items-center gap-3 rounded-btn border border-purple-500/15 bg-purple-500/5 px-3 py-3"><div className="min-w-0 flex-1"><p className="break-words text-xs text-foreground">{strategy.name}</p><p className="mt-1 break-all text-[10px] text-muted">{strategy.id}</p></div><button type="button" disabled={disabled} onClick={() => void publish(strategy)} className="inline-flex shrink-0 items-center gap-1 rounded-btn border border-purple-500/25 px-2 py-1 text-[11px] text-purple-400 disabled:opacity-40">{publishingId === strategy.id && <Loader2 className="h-3 w-3 animate-spin" />}发布</button></div>)}</div></div>}
            </div>
          </div>
          <div className={cn('scrollbar-gutter-stable min-h-0 flex-1 overflow-y-auto overscroll-contain p-2', leftPane === 'archive' ? 'block' : 'hidden')} aria-label="档案库">
            {archivedRows.length === 0
              ? <p className="px-2 py-10 text-center text-xs leading-6 text-muted">档案库为空。策略行的归档按钮可把不用的策略收进这里，可随时恢复。</p>
              : <div className="divide-y divide-border/60">
                {archivedRows.map(({ id, strategy }) => {
                  const meta = metaMap.get(id)
                  return <ArchivedRow
                    key={id}
                    strategy={strategy}
                    addedAt={fmtTime(meta?.added_at)}
                    archivedAt={fmtTime(meta?.archived_at)}
                    groupName={meta?.group ? groupNames.get(meta.group) : undefined}
                    onRestore={pool.unarchiveStrategy}
                    onDiscard={pool.discardRecord}
                  />
                })}
              </div>}
          </div>
        </section>
        {/* 右: 已选策略 (纯顺序) */}
        <section aria-label="已选策略" className={cn('strategy-library-pane min-h-0 min-w-0 flex-col overflow-hidden bg-base/30', mobilePane === 'selected' ? 'flex' : 'hidden')}>
          <div className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-3 py-2.5">
            <div>
              <h3 className="text-xs font-semibold text-foreground">已选策略 <span className="ml-1 tabular-nums text-accent">{poolIds.length}</span></h3>
              <p className="mt-0.5 text-[10px] leading-4 text-muted">拖动手柄排序，或用箭头微调。</p>
            </div>
            {clearedPool && poolIds.length === 0
              ? <button type="button" disabled={disabled} onClick={undoClear} className="shrink-0 text-[11px] text-accent disabled:opacity-40">撤销清空</button>
              : <button type="button" disabled={disabled || !poolIds.length} onClick={clearSelected} className="shrink-0 text-[11px] text-muted hover:text-danger disabled:opacity-40">清空所选</button>}
          </div>
          <Reorder.Group as="ol" axis="y" layoutScroll aria-label="已选策略列表" tabIndex={0}
            values={poolIds} onReorder={next => { if (!disabled) pool.reorderPool(next) }}
            className="scrollbar-gutter-stable min-h-0 flex-1 divide-y divide-border/60 overflow-y-auto overscroll-contain px-2 py-1 focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent/50">
            {!poolIds.length
              ? <li className="px-2 py-10 text-center text-xs leading-6 text-muted">尚未选择策略，在策略库勾选后会出现在这里。</li>
              : poolIds.map((id, index) => {
                  const meta = metaMap.get(id)
                  return <SelectedRow key={id} id={id} label={labelOf(id)} strategy={stratMap.get(id)}
                    addedAt={fmtTime(meta?.added_at)}
                    groupName={meta?.group ? groupNames.get(meta.group) : undefined}
                    index={index} count={poolIds.length} disabled={disabled} resolved={!!ready}
                    onMove={move} onRemove={pool.removeFromPool} />
                })}
          </Reorder.Group>
        </section>
      </div>
      <footer className="flex shrink-0 items-center justify-between gap-3 border-t border-border px-4 py-2 sm:px-5">
        <p className="text-[11px] text-muted" aria-live="polite">已选 <strong className="font-medium tabular-nums text-foreground">{poolIds.length}</strong> 项 · 档案 <strong className="font-medium tabular-nums text-foreground">{archivedRows.length}</strong> 项 · 调整即时生效</p>
        <button type="button" disabled={!!publishingId} onClick={onClose} className={cn(BUTTON, 'border-blue-600 bg-blue-600 font-medium text-white hover:bg-blue-700 hover:text-white')}>完成</button>
      </footer>
    </Modal>
    {showGroups && <GroupsManagerModal groups={groups} active onClose={() => setShowGroups(false)}
      onCreate={pool.createGroup} onRename={pool.renameGroup} onReorder={pool.reorderGroups} onDelete={pool.deleteGroup} />}
  </>
}
