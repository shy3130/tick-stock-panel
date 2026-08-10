import { useCallback, useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Check,
  GitMerge,
  Layers,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  Trash2,
} from 'lucide-react'
import { api, type StrategyDetail } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'

/** 局部 query key；保存后同时 invalidate QK.screenerStrategies 以同步回测策略列表 */
const COMPOSITE_STRATEGIES_KEY = ['composite-strategies'] as const
const COMPOSITE_DETAIL_KEY = (id: string) => ['composite-strategy-detail', id] as const

const MAX_CHILDREN = 8
const MIN_CHILDREN = 2

const INPUT_CLS = 'control w-full text-xs'

const SOURCE_LABEL: Record<string, string> = {
  builtin: '内置',
  custom: '自定义',
  ai: 'AI',
  composite: '组合',
}

const SOURCE_BADGE: Record<string, string> = {
  builtin: 'border-accent/25 bg-accent/10 text-accent',
  custom: 'border-warning/30 bg-warning/10 text-warning',
  ai: 'border-accent/20 bg-elevated text-secondary',
  composite: 'border-border bg-elevated text-secondary',
}

type ChildRow = { strategy_id: string; weight: number }
type MergeMode = 'union' | 'intersect'
type SaveMode = 'create' | 'update'

function isCompositeStrategy(s: StrategyDetail): boolean {
  return s.source === 'composite' || s.execution_backend === 'composite'
}


/**
 * 组合策略构建器 — 可独立挂入回测工作台 tab。
 *
 * 只读研究 + 显式保存：组合策略声明式合并子策略命中/排名，
 * 仍走现有回测链路，不生成交易建议、不下单。
 */
export function CompositeStrategyBuilder() {
  const queryClient = useQueryClient()

  const [mode, setMode] = useState<SaveMode>('create')
  const [strategyId, setStrategyId] = useState(() => `composite_${Date.now().toString(36)}`)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [children, setChildren] = useState<ChildRow[]>([])
  const [mergeMode, setMergeMode] = useState<MergeMode>('union')
  const [minConfirm, setMinConfirm] = useState(0)
  const [search, setSearch] = useState('')
  const [formError, setFormError] = useState<string | null>(null)
  const [editPick, setEditPick] = useState('')

  const listQuery = useQuery({
    queryKey: COMPOSITE_STRATEGIES_KEY,
    queryFn: api.strategyList,
    staleTime: 30_000,
  })

  const allStrategies = listQuery.data?.strategies ?? []

  const nonComposite = useMemo(
    () => allStrategies.filter(s => !isCompositeStrategy(s)),
    [allStrategies],
  )

  const compositeList = useMemo(
    () => allStrategies.filter(isCompositeStrategy),
    [allStrategies],
  )

  const strategyMap = useMemo(() => {
    const m = new Map<string, StrategyDetail>()
    allStrategies.forEach(s => m.set(s.id, s))
    return m
  }, [allStrategies])

  const selectedIds = useMemo(
    () => new Set(children.map(c => c.strategy_id)),
    [children],
  )

  const candidates = useMemo(() => {
    const kw = search.trim().toLowerCase()
    return nonComposite.filter(s => {
      if (selectedIds.has(s.id)) return false
      if (!kw) return true
      return (
        s.id.toLowerCase().includes(kw)
        || s.name.toLowerCase().includes(kw)
        || (s.description ?? '').toLowerCase().includes(kw)
      )
    })
  }, [nonComposite, search, selectedIds])

  const totalWeight = useMemo(
    () => children.reduce((sum, c) => sum + (Number.isFinite(c.weight) ? c.weight : 0), 0),
    [children],
  )

  const detailQuery = useQuery({
    queryKey: COMPOSITE_DETAIL_KEY(editPick),
    queryFn: () => api.strategyGet(editPick),
    enabled: mode === 'update' && !!editPick,
  })

  // 载入待编辑组合策略
  useEffect(() => {
    if (mode !== 'update') return
    const detail = detailQuery.data
    if (!detail || detail.id !== editPick) return
    if (!isCompositeStrategy(detail)) {
      setFormError('所选策略不是组合策略，无法以组合模式编辑')
      return
    }
    setStrategyId(detail.id)
    setName(detail.name ?? '')
    setDescription(detail.description ?? '')
    const mm = (detail.params_defaults?.merge_mode as MergeMode | undefined) ?? 'union'
    setMergeMode(mm === 'intersect' ? 'intersect' : 'union')
    setMinConfirm(Number(detail.params_defaults?.min_confirm ?? 0) || 0)
    const rows = (detail.composite_children ?? []).map(c => ({
      strategy_id: c.id,
      weight: Number.isFinite(c.weight) ? c.weight : 1,
    }))
    setChildren(rows)
    setFormError(null)
  }, [mode, editPick, detailQuery.data])

  const resetCreate = useCallback(() => {
    setMode('create')
    setEditPick('')
    setStrategyId(`composite_${Date.now().toString(36)}`)
    setName('')
    setDescription('')
    setChildren([])
    setMergeMode('union')
    setMinConfirm(0)
    setSearch('')
    setFormError(null)
  }, [])

  const addChild = useCallback((s: StrategyDetail) => {
    if (isCompositeStrategy(s)) {
      setFormError('禁止选择组合策略作为子策略，避免递归嵌套')
      return
    }
    setChildren(curr => {
      if (curr.some(c => c.strategy_id === s.id)) {
        setFormError('子策略不可重复')
        return curr
      }
      if (curr.length >= MAX_CHILDREN) {
        setFormError(`最多 ${MAX_CHILDREN} 个子策略`)
        return curr
      }
      setFormError(null)
      return [...curr, { strategy_id: s.id, weight: 1 }]
    })
  }, [])

  const removeChild = useCallback((id: string) => {
    setChildren(curr => curr.filter(c => c.strategy_id !== id))
  }, [])

  const updateWeight = useCallback((id: string, raw: number) => {
    const weight = Number.isFinite(raw) ? raw : 0
    setChildren(curr =>
      curr.map(c => (c.strategy_id === id ? { ...c, weight } : c)),
    )
  }, [])

  const normalizeWeights = useCallback(() => {
    if (totalWeight <= 0) return
    setChildren(curr =>
      curr.map(c => ({
        ...c,
        weight: Math.round((Math.max(0, c.weight) / totalWeight) * 1000) / 1000,
      })),
    )
  }, [totalWeight])

  const validate = useCallback((): string | null => {
    const sid = strategyId.trim()
    if (!sid) return '请填写策略 ID'
    if (!sid.startsWith('composite_')) return '策略 ID 必须以 composite_ 开头'
    if (!/^[A-Za-z0-9_]+$/.test(sid)) return '策略 ID 只能包含字母、数字和下划线'
    if (!name.trim()) return '请填写策略名称'
    if (children.length < MIN_CHILDREN) return `至少选择 ${MIN_CHILDREN} 个非组合子策略`
    if (children.length > MAX_CHILDREN) return `最多 ${MAX_CHILDREN} 个子策略`

    const ids = children.map(c => c.strategy_id)
    if (new Set(ids).size !== ids.length) return '子策略不可重复'

    for (const c of children) {
      const meta = strategyMap.get(c.strategy_id)
      if (!meta) return `子策略 ${c.strategy_id} 不存在或已失效`
      if (isCompositeStrategy(meta)) return `子策略 ${c.strategy_id} 是组合策略，禁止嵌套`
      if (!Number.isFinite(c.weight) || c.weight < 0) return `子策略 ${c.strategy_id} 权重无效`
    }
    if (totalWeight <= 0) return '权重总和必须大于 0'
    if (children.some(c => c.weight <= 0)) return '每个子策略权重须大于 0'

    if (mergeMode === 'intersect') {
      if (!Number.isFinite(minConfirm) || minConfirm < 0 || minConfirm > children.length) {
        return '最少确认数须在 0 到子策略数量之间（0 表示全部命中）'
      }
    }

    if (mode === 'create' && strategyMap.has(sid)) {
      return `策略 ${sid} 已存在，请切换为更新模式或更换 ID`
    }
    if (mode === 'update') {
      const existing = strategyMap.get(sid)
      if (!existing) return `策略 ${sid} 不存在，请先创建`
      if (!isCompositeStrategy(existing)) return '目标策略不是组合策略，无法以组合模式覆盖'
    }

    return null
  }, [
    children,
    mergeMode,
    minConfirm,
    mode,
    name,
    strategyId,
    strategyMap,
    totalWeight,
  ])

  const save = useMutation({
    mutationFn: () =>
      api.strategySaveComposite({
        strategy_id: strategyId.trim(),
        name: name.trim(),
        description: description.trim() || undefined,
        children: children.map(c => ({
          strategy_id: c.strategy_id,
          weight: c.weight,
        })),
        merge_mode: mergeMode,
        min_confirm: mergeMode === 'intersect' ? minConfirm : 0,
        mode,
      }),
    onSuccess: async result => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: COMPOSITE_STRATEGIES_KEY }),
        queryClient.invalidateQueries({ queryKey: QK.screenerStrategies }),
        queryClient.invalidateQueries({ queryKey: COMPOSITE_DETAIL_KEY(result.strategy_id) }),
      ])
      toast(
        mode === 'update'
          ? `已更新组合策略 ${result.strategy_id}`
          : `已创建组合策略 ${result.strategy_id}`,
        'success',
      )
      setFormError(null)
      if (mode === 'create') {
        setMode('update')
        setEditPick(result.strategy_id)
      }
    },
    onError: (err: unknown) => {
      const msg = err instanceof Error ? err.message : '保存组合策略失败'
      setFormError(msg)
      toast(msg, 'error')
    },
  })

  const submit = useCallback(() => {
    const err = validate()
    if (err) {
      setFormError(err)
      return
    }
    setFormError(null)
    save.mutate()
  }, [save, validate])

  const effectiveMinHint = useMemo(() => {
    if (mergeMode !== 'intersect') return null
    const n = children.length
    if (n === 0) return '先选择子策略'
    if (minConfirm > 0) return `需至少 ${Math.max(minConfirm, 1)} 个子策略同时命中`
    return `需全部 ${n} 个子策略同时命中`
  }, [children.length, mergeMode, minConfirm])

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col gap-3 lg:flex-row">
      {/* 左：说明 + 已有组合 */}
      <aside className="flex w-full shrink-0 flex-col gap-3 lg:w-72 xl:w-80 min-w-0">
        <section className="panel">
          <div className="panel-header">
            <div>
              <div className="section-kicker">Composite</div>
              <h2 className="section-title flex items-center gap-1.5">
                <Layers className="h-3.5 w-3.5 text-accent" />
                组合策略构建器
              </h2>
            </div>
          </div>
          <div className="panel-body space-y-3">
          <p className="text-[11px] leading-relaxed text-muted">
            将多个非组合子策略按权重合并为一条可回测策略。仅研究用途，不产生下单指令。
          </p>

          <div className="space-y-2 rounded-btn border border-border bg-elevated/40 p-2.5 text-[11px] leading-relaxed text-secondary">
            <div>
              <span className="font-medium text-foreground">union（并集）</span>
              <span className="text-muted"> — 任一子策略命中即入选；分数按命中子策略的排名归一后加权融合。</span>
            </div>
            <div>
              <span className="font-medium text-foreground">intersect（交集）</span>
              <span className="text-muted"> — 需达到最少确认数的子策略同时命中才入选；0 表示必须全部命中。</span>
            </div>
            <div>
              <span className="font-medium text-foreground">min_confirm</span>
              <span className="text-muted"> — 仅 intersect 生效：effective_min = max(min_confirm, 1)（min_confirm &gt; 0 时），否则等于子策略数。</span>
            </div>
            <div className="border-t border-border/60 pt-2 text-muted">
              保存后可在「策略回测」中直接选择该组合策略运行；合并逻辑与选股一致，不改交易撮合，也不自动荐股/下单。
            </div>
          </div>
          </div>
        </section>

        <section className="panel flex min-h-0 flex-1 flex-col">
          <div className="panel-header">
            <div>
              <div className="section-kicker">Library</div>
              <h3 className="section-title">已有组合策略</h3>
            </div>
            <button
              type="button"
              onClick={() => listQuery.refetch()}
              disabled={listQuery.isFetching}
              className="btn-ghost !h-7 !px-2 text-[11px]"
            >
              <RefreshCw className={cn('h-3 w-3', listQuery.isFetching && 'animate-spin')} />
              刷新
            </button>
          </div>
          <div className="panel-body flex min-h-0 flex-1 flex-col !pt-2">

          <button
            type="button"
            onClick={resetCreate}
            className={cn(
              'mb-2 inline-flex h-8 w-full items-center justify-center gap-1.5 rounded-btn border text-xs font-medium transition-colors',
              mode === 'create'
                ? 'border-accent/40 bg-accent/10 text-accent'
                : 'border-border bg-elevated text-secondary hover:bg-surface hover:text-foreground',
            )}
          >
            <Plus className="h-3.5 w-3.5" />
            新建组合
          </button>

          <div className="min-h-0 flex-1 space-y-1 overflow-y-auto">
            {listQuery.isLoading && (
              <div className="flex items-center justify-center gap-2 py-8 text-xs text-muted">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                加载策略列表…
              </div>
            )}
            {!listQuery.isLoading && compositeList.length === 0 && (
              <div className="rounded-btn border border-dashed border-border px-3 py-6 text-center text-[11px] text-muted">
                尚无组合策略。从右侧挑选至少两个子策略后保存。
              </div>
            )}
            {compositeList.map(s => {
              const active = mode === 'update' && editPick === s.id
              return (
                <button
                  key={s.id}
                  type="button"
                  onClick={() => {
                    setMode('update')
                    setEditPick(s.id)
                    setFormError(null)
                  }}
                  className={cn(
                    'w-full rounded-btn border px-2.5 py-2 text-left transition-colors',
                    active
                      ? 'border-accent/40 bg-accent/10'
                      : 'border-border bg-base hover:bg-elevated',
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-xs font-medium text-foreground">{s.name}</span>
                    <span className={cn('shrink-0 rounded border px-1 py-px text-[10px]', SOURCE_BADGE.composite)}>
                      组合
                    </span>
                  </div>
                  <div className="mt-0.5 truncate font-mono text-[10px] text-muted">{s.id}</div>
                  {s.description && (
                    <div className="mt-1 line-clamp-2 text-[10px] leading-relaxed text-secondary">
                      {s.description}
                    </div>
                  )}
                </button>
              )
            })}
          </div>
          </div>
        </section>
      </aside>

      {/* 右：表单 */}
      <main className="panel min-h-0 min-w-0 flex-1 overflow-y-auto">
        <header className="panel-header flex-wrap">
          <div className="flex items-center gap-2 min-w-0">
            <GitMerge className="h-4 w-4 text-accent shrink-0" />
            <div className="min-w-0">
              <div className="section-kicker">{mode === 'update' ? 'Update' : 'Create'}</div>
              <h3 className="section-title">
                {mode === 'update' ? '更新组合策略' : '创建组合策略'}
              </h3>
              <p className="text-[11px] text-muted">
                至少 {MIN_CHILDREN} 个子策略 · 禁止嵌套组合 · 权重 &gt; 0
              </p>
            </div>
          </div>
          <div className="inline-flex rounded-btn border border-border bg-elevated p-0.5">
            {(['create', 'update'] as const).map(m => (
              <button
                key={m}
                type="button"
                onClick={() => {
                  if (m === 'create') resetCreate()
                  else {
                    setMode('update')
                    if (!editPick && compositeList[0]) setEditPick(compositeList[0].id)
                  }
                }}
                className={cn(
                  'rounded-[5px] px-2.5 py-1 text-[11px] font-medium transition-colors',
                  mode === m
                    ? 'bg-accent text-white shadow-sm'
                    : 'text-secondary hover:text-foreground',
                )}
              >
                {m === 'create' ? '创建' : '更新'}
              </button>
            ))}
          </div>
        </header>

        <div className="panel-body space-y-4">
          {mode === 'update' && detailQuery.isLoading && editPick && (
            <div className="flex items-center gap-2 text-xs text-muted">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              读取 {editPick}…
            </div>
          )}
          {mode === 'update' && detailQuery.isError && (
            <div className="rounded-btn border border-danger/25 bg-danger/10 px-3 py-2 text-xs text-danger">
              无法读取组合策略配置
            </div>
          )}

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="block text-xs text-secondary">
              策略 ID
              <input
                value={strategyId}
                onChange={e => setStrategyId(e.target.value)}
                readOnly={mode === 'update'}
                spellCheck={false}
                className={cn(
                  INPUT_CLS,
                  'mt-1 font-mono',
                  mode === 'update' && 'cursor-not-allowed bg-elevated text-muted',
                )}
                placeholder="composite_my_blend"
              />
              <span className="mt-1 block text-[10px] text-muted">
                必须以 composite_ 开头，仅字母数字下划线
              </span>
            </label>
            <label className="block text-xs text-secondary">
              策略名称
              <input
                value={name}
                onChange={e => setName(e.target.value)}
                className={cn(INPUT_CLS, 'mt-1')}
                placeholder="动量 + 质量 双确认"
              />
            </label>
          </div>

          <label className="block text-xs text-secondary">
            说明（可选）
            <input
              value={description}
              onChange={e => setDescription(e.target.value)}
              className={cn(INPUT_CLS, 'mt-1')}
              placeholder="研究用组合：不生成交易建议"
            />
          </label>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="block text-xs text-secondary">
              合并模式 merge_mode
              <select
                value={mergeMode}
                onChange={e => setMergeMode(e.target.value as MergeMode)}
                className={cn(INPUT_CLS, 'mt-1')}
              >
                <option value="union">union · 并集（任一命中）</option>
                <option value="intersect">intersect · 交集（多策略确认）</option>
              </select>
            </label>
            <label className="block text-xs text-secondary">
              最少确认数 min_confirm
              <input
                type="number"
                min={0}
                max={Math.max(children.length, 0)}
                value={minConfirm}
                disabled={mergeMode !== 'intersect'}
                onChange={e => setMinConfirm(Math.max(0, Number(e.target.value) || 0))}
                className={cn(
                  INPUT_CLS,
                  'mt-1',
                  mergeMode !== 'intersect' && 'cursor-not-allowed opacity-50',
                )}
              />
              <span className="mt-1 block text-[10px] text-muted">
                {mergeMode === 'intersect'
                  ? (effectiveMinHint ?? '0 = 要求全部子策略命中')
                  : '仅 intersect 模式生效'}
              </span>
            </label>
          </div>

          {/* 已选子策略 */}
          <section>
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h4 className="text-xs font-medium text-secondary">
                子策略（{children.length}/{MAX_CHILDREN}，至少 {MIN_CHILDREN}）
              </h4>
              <div className="flex items-center gap-2 text-[11px] text-muted">
                <span>权重合计 {totalWeight.toFixed(3)}</span>
                {totalWeight > 0 && Math.abs(totalWeight - 1) > 0.001 && (
                  <button
                    type="button"
                    onClick={normalizeWeights}
                    className="text-accent hover:underline"
                  >
                    归一到 1
                  </button>
                )}
              </div>
            </div>

            <div className="space-y-1.5 rounded-btn border border-border bg-elevated/30 p-2">
              {children.length === 0 && (
                <div className="py-6 text-center text-xs text-muted">
                  从下方列表添加至少 {MIN_CHILDREN} 个非组合子策略
                </div>
              )}
              {children.map(child => {
                const meta = strategyMap.get(child.strategy_id)
                const src = meta?.source ?? 'custom'
                return (
                  <div
                    key={child.strategy_id}
                    className="flex flex-wrap items-center gap-2 rounded-btn border border-border/80 bg-surface px-2.5 py-2"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="truncate text-xs font-medium text-foreground">
                          {meta?.name ?? child.strategy_id}
                        </span>
                        <span
                          className={cn(
                            'rounded border px-1 py-px text-[10px]',
                            SOURCE_BADGE[src] ?? SOURCE_BADGE.custom,
                          )}
                        >
                          {SOURCE_LABEL[src] ?? src}
                        </span>
                      </div>
                      <div className="mt-0.5 font-mono text-[10px] text-muted">{child.strategy_id}</div>
                    </div>
                    <label className="flex items-center gap-1.5 text-[11px] text-secondary">
                      权重
                      <input
                        type="number"
                        min={0}
                        step={0.1}
                        value={child.weight}
                        onChange={e => updateWeight(child.strategy_id, Number(e.target.value))}
                        className="w-20 rounded-input border border-border bg-base px-2 py-1 text-xs focus:border-accent focus:outline-none"
                      />
                      <span className="w-12 text-right font-mono text-muted">
                        {totalWeight <= 0
                          ? '—'
                          : `${((Math.max(0, child.weight) / totalWeight) * 100).toFixed(1)}%`}
                      </span>
                    </label>
                    <button
                      type="button"
                      onClick={() => removeChild(child.strategy_id)}
                      className="rounded-btn p-1.5 text-muted hover:bg-danger/10 hover:text-danger"
                      aria-label={`移除 ${child.strategy_id}`}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                )
              })}
            </div>
          </section>

          {/* 候选池 */}
          <section>
            <h4 className="mb-2 text-xs font-medium text-secondary">可选非组合子策略</h4>
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
              <input
                value={search}
                onChange={e => setSearch(e.target.value)}
                placeholder="搜索名称或 ID"
                className={cn(INPUT_CLS, 'pl-8')}
              />
            </div>
            <div className="mt-2 max-h-56 space-y-1 overflow-y-auto rounded-btn border border-border bg-elevated/30 p-1.5">
              {listQuery.isLoading && (
                <div className="flex items-center justify-center gap-2 py-6 text-xs text-muted">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  加载中
                </div>
              )}
              {!listQuery.isLoading && candidates.length === 0 && (
                <div className="py-6 text-center text-xs text-muted">
                  {nonComposite.length === 0
                    ? '暂无可用子策略'
                    : selectedIds.size >= nonComposite.length
                      ? '可选策略已全部加入'
                      : '无匹配结果'}
                </div>
              )}
              {candidates.map(s => (
                <button
                  key={s.id}
                  type="button"
                  disabled={children.length >= MAX_CHILDREN}
                  onClick={() => addChild(s)}
                  className="flex w-full items-center gap-2 rounded-btn px-2.5 py-2 text-left transition-colors hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <Plus className="h-3.5 w-3.5 shrink-0 text-accent" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="truncate text-xs font-medium text-foreground">{s.name}</span>
                      <span
                        className={cn(
                          'shrink-0 rounded border px-1 py-px text-[10px]',
                          SOURCE_BADGE[s.source] ?? SOURCE_BADGE.custom,
                        )}
                      >
                        {SOURCE_LABEL[s.source] ?? s.source}
                      </span>
                    </div>
                    <div className="truncate font-mono text-[10px] text-muted">{s.id}</div>
                  </div>
                </button>
              ))}
            </div>
          </section>
        </div>

        {formError && (
          <div
            role="alert"
            className="border-t border-danger/25 bg-danger/10 px-4 py-2 text-xs text-danger"
          >
            {formError}
          </div>
        )}

        <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-4 py-3">
          <p className="text-[11px] text-muted">
            保存后 invalidate 策略列表；可在策略回测中运行，不会触发下单。
          </p>
          <div className="flex items-center gap-2">
            {mode === 'update' && (
              <button
                type="button"
                onClick={resetCreate}
                disabled={save.isPending}
                className="btn-secondary"
              >
                转为新建
              </button>
            )}
            <button
              type="button"
              onClick={submit}
              disabled={save.isPending || listQuery.isLoading}
              className="btn-primary"
            >
              {save.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Check className="h-3.5 w-3.5" />
              )}
              {mode === 'update' ? '保存更新' : '创建组合'}
            </button>
          </div>
        </footer>
      </main>
    </div>
  )
}
