import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import { ArrowDown, ArrowUp, BookmarkPlus, ChevronsUpDown, Clock, Expand, Info, Layers3, ListFilter, ListPlus, Pickaxe, Play, Search, Sparkles, X, Zap } from 'lucide-react'
import { DatePicker } from '@/components/DatePicker'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import { WatchlistGroupMenu } from '@/components/WatchlistAddMenu'
import { api, type FactorBatchItem, type FactorBatchResult, type FactorColumn } from '@/lib/api'
import { fmtPct, priceColorClass } from '@/lib/format'
import { QK } from '@/lib/queryKeys'
import { FactorBacktest } from './FactorBacktest'
import { MiningWorkbench } from './MiningWorkbench'
import { AutoMiningDialog } from './AutoMiningDialog'
import { AddFactorSignalDialog } from './AddFactorSignalDialog'
import { factorBatchCandidate } from './researchCandidates'

const formatDate = (value: Date) => value.toISOString().slice(0, 10)
const monthsAgo = (months: number) => {
  const value = new Date()
  value.setMonth(value.getMonth() - months)
  return formatDate(value)
}
const TODAY = formatDate(new Date())
const INPUT_CLS = 'w-full rounded-input border border-border bg-surface px-2.5 py-1.5 text-xs focus:border-accent focus:outline-none'

type View = 'batch' | 'single' | 'mining'
type SortKey = 'ic' | 'ir' | 'return' | 't_nw' | 'win_rate' | 'drawdown'
type Verdict = 'valid' | 'edge' | 'invalid' | 'error'

// P0 判读为客户端经验规则, metrics_v2 (服务端显著性) 落地后切换为服务端 verdict
const VERDICT_META: Record<Verdict, { label: string; cls: string }> = {
  valid: { label: '有效', cls: 'bg-bull/10 text-bull' },
  edge: { label: '边缘', cls: 'bg-amber-400/10 text-amber-500' },
  invalid: { label: '无效', cls: 'bg-base text-muted' },
  error: { label: '失败', cls: 'bg-danger/10 text-danger' },
}

// 服务端判读优先 (metrics_v2: NW t ≥ 2 显著 且 BH q ≤ 0.1), 缺失时降级 P0 经验规则
const verdictOf = (item: FactorBatchItem): Verdict => {
  if (item.error) return 'error'
  const ic = Math.abs(item.ic_mean ?? 0)
  const ir = Math.abs(item.ir ?? 0)
  if (item.t_newey_west != null) {
    const significant = Math.abs(item.t_newey_west) >= 2
      && (item.q_value == null || item.q_value <= 0.1)
    const predictive = ic >= 0.02
    if (significant && predictive) return 'valid'
    if (significant || predictive) return 'edge'
    return 'invalid'
  }
  if (ic >= 0.02 && ir >= 0.3) return 'valid'
  if (ic >= 0.02 || ir >= 0.3) return 'edge'
  return 'invalid'
}

const hasServerVerdict = (results: FactorBatchItem[]): boolean =>
  results.some(item => !item.error && item.t_newey_west != null)

// 预设场景: 纯前端选择集, 数量按 columns 数据实时计算 (分组名缺失时该项自动为空并禁用)
type PresetDef = { id: string; label: string; hint: string; groups?: string[]; quick?: boolean }
const PRESETS: PresetDef[] = [
  { id: 'all', label: '全面体检', hint: '全部因子，耗时最长' },
  { id: 'quick', label: '快速体检', hint: '除财务组外每组各取 1 个代表因子' , quick: true },
  { id: 'trend', label: '趋势动量', hint: '动量组 + 趋势组', groups: ['动量', '趋势'] },
  { id: 'reversal', label: '超跌反转', hint: '超买超卖组 + 价格位置组', groups: ['超买超卖', '价格位置'] },
  { id: 'volume', label: '量价资金', hint: '量价组 + 流动性组', groups: ['量价', '流动性'] },
  { id: 'fundamental', label: '财务价值', hint: '财务组（需财务数据）', groups: ['财务'] },
]
const QUICK_PRESET = PRESETS.find(preset => preset.quick) ?? PRESETS[0]

const INTRO_STORAGE_KEY = 'factors-intro-collapsed'

/** 解析标的列表: 兼容中英文逗号/分号/顿号/空白分隔, 去重保序 (裸 split(',') 会把
 *  中文逗号输入静默变成整段无效代码)。 */
const parseSymbolList = (text: string): string[] =>
  Array.from(new Set(
    text.split(/[,，;；、\s]+/).map(value => value.trim()).filter(Boolean),
  ))

const EASE_SMOOTH: [number, number, number, number] = [0.16, 1, 0.3, 1]

/** 运行配置持久化: 「上次怎么跑的」跨会话保留 (资产/范围/区间/调仓参数)。 */
const RUN_CONFIG_KEY = 'factor-batch-config.v1'

interface RunConfig {
  symbols: string
  assetType: 'stock' | 'etf'
  start: string
  end: string
  nGroups: number
  rebalance: 'daily' | 'weekly' | 'monthly'
  fees: string
}

function loadRunConfig(): Partial<RunConfig> {
  try {
    const raw = localStorage.getItem(RUN_CONFIG_KEY)
    return raw ? JSON.parse(raw) as Partial<RunConfig> : {}
  } catch {
    return {}
  }
}

function saveRunConfig(config: RunConfig) {
  try {
    localStorage.setItem(RUN_CONFIG_KEY, JSON.stringify(config))
  } catch { /* 存储不可用时仅内存态 */ }
}

/** 会话内结果记忆: 切 tab / 刷新后立即恢复上次批量结果 (交互优先, 零等待),
 *  带运行时间提示; 重新筛选即覆盖。 */
const RESULTS_SESSION_KEY = 'factor-batch-results.v1'

function loadRestoredResults(): { ts: number; payload: FactorBatchResult } | null {
  try {
    const raw = sessionStorage.getItem(RESULTS_SESSION_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as { ts: number; payload: FactorBatchResult }
    return parsed?.payload?.results && !parsed.payload.error ? parsed : null
  } catch {
    return null
  }
}

// 表头排序: IC/IR/t 值/多空收益按 |值| 排 (正负都是信号), 胜率/回撤按原值排
const SORT_GETTERS: Record<SortKey, (item: FactorBatchItem) => number | null | undefined> = {
  ic: item => item.ic_mean,
  ir: item => item.ir,
  return: item => item.long_short_return,
  t_nw: item => item.t_newey_west,
  win_rate: item => item.ic_win_rate,
  drawdown: item => item.long_short_max_drawdown,
}
const ABS_SORT_KEYS: ReadonlySet<SortKey> = new Set(['ic', 'ir', 'return', 't_nw'])

function SortableTh({ label, sortKeyName, sortKey, sortAsc, onSort, className, title }: {
  label: string
  sortKeyName: SortKey
  sortKey: SortKey
  sortAsc: boolean
  onSort: (key: SortKey) => void
  className?: string
  title?: string
}) {
  const active = sortKey === sortKeyName
  const Icon = !active ? ChevronsUpDown : sortAsc ? ArrowUp : ArrowDown
  return (
    <th
      className={`${className ?? ''} font-medium`}
      title={title}
      aria-sort={active ? (sortAsc ? 'ascending' : 'descending') : 'none'}
    >
      <button
        type="button"
        onClick={() => onSort(sortKeyName)}
        className={`inline-flex items-center gap-0.5 rounded-btn transition-colors hover:text-foreground ${active ? 'text-foreground' : ''}`}
        title={title ? `${title}${active ? '' : '（点击排序）'}` : '点击排序'}
      >
        {label}
        <Icon className={`h-3 w-3 ${active ? 'text-accent' : 'text-muted/50'}`} />
      </button>
    </th>
  )
}

function BatchDiscovery({
  onFullscreen,
  onDetail,
  onOrder,
  focusFactor,
}: {
  onFullscreen: (factorName: string, label: string) => void
  onDetail: (factorName: string, label: string) => void
  /** 当前排序下的结果因子序列 — 供父层实现 ↑↓ 键盘流式浏览 */
  onOrder?: (order: { name: string; label: string }[]) => void
  focusFactor?: string
}) {
  const queryClient = useQueryClient()
  const initialized = useRef(false)
  const savedConfig = useRef(loadRunConfig()).current
  const [selected, setSelected] = useState<string[]>([])
  const [symbols, setSymbols] = useState(savedConfig.symbols ?? '')
  const [assetType, setAssetType] = useState<'stock' | 'etf'>(savedConfig.assetType ?? 'stock')
  const [start, setStart] = useState(savedConfig.start ?? monthsAgo(3))
  const [end, setEnd] = useState(savedConfig.end ?? TODAY)
  const [nGroups, setNGroups] = useState(savedConfig.nGroups ?? 5)
  const [rebalance, setRebalance] = useState<'daily' | 'weekly' | 'monthly'>(savedConfig.rebalance ?? 'daily')
  const [fees, setFees] = useState(savedConfig.fees ?? '2')
  const [sortKey, setSortKey] = useState<SortKey>('ic')
  const [sortAsc, setSortAsc] = useState(false)
  const [factorQuery, setFactorQuery] = useState('')
  const deferredFactorQuery = useDeferredValue(factorQuery)
  const [activePreset, setActivePreset] = useState<string | null>(null)
  const [signalFrom, setSignalFrom] = useState<FactorBatchItem | null>(null)

  const columns = useQuery({
    queryKey: QK.factorColumns,
    queryFn: api.factorColumns,
  })
  const watchlist = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
    staleTime: 30_000,
  })
  const watchlistEntries = watchlist.data?.symbols ?? []
  const watchlistCounts = useMemo(() => {
    // 多组并存: 一股计入每个所属分组
    const counts: Record<string, number> = { ungrouped: 0 }
    for (const entry of watchlistEntries) {
      const gids = entry.group_ids ?? []
      if (gids.length === 0) counts.ungrouped += 1
      else for (const gid of gids) counts[gid] = (counts[gid] ?? 0) + 1
    }
    return counts
  }, [watchlistEntries])
  const allColumns = useMemo(() => columns.data?.columns ?? [], [columns.data])
  const columnsByGroup = useMemo(() => {
    const groups: Record<string, FactorColumn[]> = {}
    for (const column of allColumns) (groups[column.group] ??= []).push(column)
    return groups
  }, [allColumns])
  const presetIds = (preset: PresetDef): string[] => {
    if (preset.id === 'all') return allColumns.map(item => item.id)
    if (preset.quick) {
      // 每组取列表中位代表 (上游按窗口升序排列时即窗口中位数, 如动量组取 20日)
      return Object.entries(columnsByGroup)
        .filter(([group]) => group !== '财务')
        .map(([, items]) => items[Math.floor((items.length - 1) / 2)])
        .filter(item => item != null)
        .map(item => item.id)
    }
    return (preset.groups ?? []).flatMap(group => columnsByGroup[group] ?? []).map(item => item.id)
  }

  useEffect(() => {
    if (initialized.current || !columns.data?.columns.length) return
    initialized.current = true
    // 因子库「去检验」联动: focus 参数命中则只选该因子
    if (focusFactor && columns.data.columns.some(column => column.id === focusFactor)) {
      setSelected([focusFactor])
      setActivePreset(null)
      return
    }
    // 默认快速体检 (每组 1 个代表因子): 全市场×全因子×3 个月对首次用户太重
    setSelected(presetIds(QUICK_PRESET))
    setActivePreset('quick')
  }, [columns.data, focusFactor])

  const factorGroups = useMemo(() => {
    // 搜索只影响展示, 不改变已选集合; 匹配 id/中文label/公式描述
    // useDeferredValue: 输入即时回显, 过滤计算让位渲染不掉帧
    const query = deferredFactorQuery.trim().toLowerCase()
    const groups: Record<string, FactorColumn[]> = {}
    for (const column of columns.data?.columns ?? []) {
      if (query && !`${column.id} ${column.label} ${column.desc}`.toLowerCase().includes(query)) continue
      ;(groups[column.group] ??= []).push(column)
    }
    return groups
  }, [columns.data, deferredFactorQuery])

  // 可取消的批量运行: 调用方持有 AbortController, 界面「取消」即时中止;
  // 因子清单由调用方传入 (空态「立即快速体检」在 setState 生效前就要发起)
  const runAbortRef = useRef<AbortController | null>(null)
  const run = useMutation({
    mutationFn: ({ signal, factorNames }: { signal: AbortSignal; factorNames: string[] }) => api.factorBatch({
      factor_names: factorNames,
      symbols: symbols ? parseSymbolList(symbols) : null,
      asset_type: assetType,
      start: start || null,
      end: end || null,
      n_groups: nGroups,
      rebalance,
      fees_pct: Number(fees) / 10000,
    }, signal),
  })
  const startRun = (factorNames: string[] = selected) => {
    if (!factorNames.length) return
    const ctl = new AbortController()
    runAbortRef.current = ctl
    run.mutate({ signal: ctl.signal, factorNames })
  }
  const cancelRun = () => runAbortRef.current?.abort()
  const runCancelled = run.isError && run.error instanceof DOMException && run.error.name === 'AbortError'

  // 运行配置持久化: 任意参数变化即存, 重进页面恢复
  useEffect(() => {
    saveRunConfig({ symbols, assetType, start, end, nGroups, rebalance, fees })
  }, [symbols, assetType, start, end, nGroups, rebalance, fees])

  // 会话内结果记忆: 新结果落 sessionStorage; 无新结果时用上次结果渲染 (标注时间)
  const [restored] = useState(loadRestoredResults)
  const results = run.data ?? restored?.payload ?? null
  const resultsRef = useRef(results)
  resultsRef.current = results
  useEffect(() => {
    if (!run.data) return
    try {
      sessionStorage.setItem(RESULTS_SESSION_KEY, JSON.stringify({ ts: Date.now(), payload: run.data }))
    } catch { /* 超额静默, 下次运行重写 */ }
  }, [run.data])
  const save = useMutation({
    mutationFn: (item: FactorBatchItem) => {
      const current = resultsRef.current
      if (!current) throw new Error('暂无批量结果')
      return api.researchCandidateCreate(factorBatchCandidate(current, item))
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.researchCandidates })
      toast('已保存到候选方案（右上角「候选方案」查看）', 'success')
    },
    onError: error => toast(`保存失败 · ${String((error as Error).message || error)}`, 'error'),
  })

  const sortedResults = useMemo(() => {
    const getter = SORT_GETTERS[sortKey]
    const useAbs = ABS_SORT_KEYS.has(sortKey)
    const value = (item: FactorBatchItem) => {
      const raw = getter(item)
      if (raw == null || !Number.isFinite(raw)) return sortAsc ? Number.POSITIVE_INFINITY : Number.NEGATIVE_INFINITY
      return useAbs ? Math.abs(raw) : raw
    }
    // verdict 随排序一次算好 (每行渲染直接读, 不再重复调用 verdictOf)
    return [...(results?.results ?? [])]
      .sort((left, right) => (sortAsc ? value(left) - value(right) : value(right) - value(left)))
      .map(item => ({ item, verdict: verdictOf(item) }))
  }, [results, sortKey, sortAsc])

  // 向父层同步当前排序序列 (↑↓ 键盘浏览的消费方)
  useEffect(() => {
    onOrder?.(sortedResults.map(({ item }) => ({ name: item.factor_name, label: item.label })))
  }, [sortedResults, onOrder])

  const applySort = (key: SortKey) => {
    if (key === sortKey) {
      setSortAsc(current => !current)
    } else {
      setSortKey(key)
      setSortAsc(false)
    }
  }

  // 结论句: 有效数按经验规则统计; 最强按 |IC| 取 (与默认排序口径一致)
  const computableResults = useMemo(() => (results?.results ?? []).filter(item => !item.error), [results])
  const verdictCounts = useMemo(() => {
    const counts = { valid: 0, edge: 0, invalid: 0, error: 0 }
    for (const item of results?.results ?? []) counts[verdictOf(item)] += 1
    return counts
  }, [results])
  const validCount = verdictCounts.valid
  const bestFactor = useMemo(() => {
    let best: FactorBatchItem | null = null
    for (const item of computableResults) {
      if (item.ic_mean == null) continue
      if (best == null || Math.abs(item.ic_mean) > Math.abs(best.ic_mean ?? 0)) best = item
    }
    return best
  }, [computableResults])

  const toggleFactor = (factorName: string) => {
    setActivePreset(null)
    setSelected(current => current.includes(factorName)
      ? current.filter(name => name !== factorName)
      : [...current, factorName])
  }
  const toggleGroup = (items: FactorColumn[]) => {
    setActivePreset(null)
    const ids = items.map(item => item.id)
    const allSelected = ids.every(id => selected.includes(id))
    setSelected(current => allSelected
      ? current.filter(id => !ids.includes(id))
      : [...current, ...ids.filter(id => !current.includes(id))]
    )
  }
  const importFromWatchlist = (groupId: string | null) => {
    // 'all'=全部自选; null=未分组; 其他=指定分组 (多组归属时该股计入每个所属分组)
    const entries = groupId === 'all'
      ? watchlistEntries
      : groupId == null
        ? watchlistEntries.filter(entry => !(entry.group_ids?.length))
        : watchlistEntries.filter(entry => !!entry.group_ids?.includes(groupId))
    setSymbols(Array.from(new Set([...parseSymbolList(symbols), ...entries.map(entry => entry.symbol)])).join(','))
  }
  const allSelected = allColumns.length > 0 && allColumns.every(item => selected.includes(item.id))
  const applyPreset = (preset: PresetDef) => {
    setSelected(presetIds(preset))
    setActivePreset(preset.id)
  }

  return (
    <div className="grid h-full min-h-0 grid-cols-1 overflow-hidden rounded-card border border-border bg-surface/80 xl:grid-cols-[19rem_minmax(0,1fr)]">
      <section className="flex min-h-0 flex-col border-b border-border bg-base/25 xl:border-b-0 xl:border-r">
        {/* 顶部固定区: 计数 + 预设 + 搜索 */}
        <div className="shrink-0 space-y-2 border-b border-border/60 px-3 py-2.5">
          <div className="flex items-center justify-between gap-2">
            <div className="text-xs font-semibold text-foreground">选择因子</div>
            <div className="flex items-center gap-2">
              <span className="font-mono text-[10px] text-muted">{selected.length}/{allColumns.length}</span>
              <button
                type="button"
                onClick={() => { setActivePreset(null); setSelected(allSelected ? [] : allColumns.map(item => item.id)) }}
                className="rounded-btn px-1.5 py-0.5 text-[10px] text-accent transition-colors hover:bg-accent/10"
              >
                {allSelected ? '清空' : '全选'}
              </button>
            </div>
          </div>
          <div className="flex flex-wrap gap-1">
            {PRESETS.map(preset => {
              const ids = presetIds(preset)
              const active = activePreset === preset.id
              return (
                <button
                  key={preset.id}
                  type="button"
                  onClick={() => applyPreset(preset)}
                  disabled={ids.length === 0}
                  title={preset.hint}
                  className={`inline-flex items-center gap-1 rounded-btn border px-2 py-1 text-[10px] font-medium transition-colors disabled:pointer-events-none disabled:opacity-40 ${active
                    ? 'border-accent/60 bg-accent/10 text-accent'
                    : 'border-border bg-surface text-secondary hover:border-accent/40 hover:text-accent'
                  }`}
                >
                  {preset.label}
                  <span className="font-mono opacity-70">{ids.length}</span>
                  {active && <span aria-hidden>✓</span>}
                </button>
              )
            })}
          </div>
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3 w-3 -translate-y-1/2 text-muted" />
            <input
              type="text"
              value={factorQuery}
              onChange={event => setFactorQuery(event.target.value)}
              placeholder="搜索因子 (名称/公式)"
              className={`${INPUT_CLS} pl-7`}
            />
          </div>
        </div>

        {/* 中部弹性区: 分组因子列表 (单列行, 选中高亮) */}
        <div className="max-h-[45vh] min-h-0 flex-1 space-y-2.5 overflow-y-auto px-3 py-2.5 xl:max-h-none">
          {Object.entries(factorGroups).map(([group, items]) => {
            const groupSelected = items.filter(item => selected.includes(item.id)).length
            return (
              <div key={group}>
                <div className="mb-1 flex items-center gap-1.5 px-1">
                  <span className="text-[11px] font-semibold text-secondary">{group}</span>
                  <span className="font-mono text-[9px] text-muted">{groupSelected}/{items.length}</span>
                  <button
                    type="button"
                    onClick={() => toggleGroup(items)}
                    className="ml-auto text-[10px] text-muted transition-colors hover:text-accent"
                  >
                    {groupSelected === items.length && items.length > 0 ? '取消' : '全选'}
                  </button>
                </div>
                <div className="space-y-0.5">
                  {items.map(item => {
                    const checked = selected.includes(item.id)
                    return (
                      <label
                        key={item.id}
                        className={`flex cursor-pointer items-center gap-2 rounded-btn px-1.5 py-1 text-xs transition-colors ${checked
                          ? 'bg-accent/[0.08] text-foreground'
                          : 'text-secondary hover:bg-elevated/50 hover:text-foreground'
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleFactor(item.id)}
                          className="h-3.5 w-3.5 shrink-0 accent-accent"
                        />
                        <span className="min-w-0 truncate" title={`${item.label} (${item.id}) · ${item.desc}`}>{item.label}</span>
                      </label>
                    )
                  })}
                </div>
              </div>
            )
          })}
          {Object.keys(factorGroups).length === 0 && (
            <div className="py-4 text-center text-[10px] text-muted">
              {columns.isLoading ? '因子加载中…' : '无匹配因子'}
            </div>
          )}
        </div>

        {/* 底部固定区: 范围与区间 (常用) + 回测参数 (折叠) */}
        <div className="shrink-0 space-y-2 border-t border-border/60 px-3 py-2.5">
          <div>
            <div className="mb-1 flex items-center justify-between">
              <span className="text-[10px] font-medium text-muted">资产与范围</span>
              <div className="inline-flex overflow-hidden rounded-btn border border-border bg-surface">
                {(['stock', 'etf'] as const).map(value => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => { setAssetType(value); setSymbols('') }}
                    className={`px-2.5 py-1 text-[11px] font-medium transition-colors ${assetType === value
                      ? 'bg-accent/10 text-accent'
                      : 'text-muted hover:text-foreground'
                    }`}
                  >
                    {value === 'stock' ? '股票' : 'ETF'}
                  </button>
                ))}
              </div>
            </div>
            <div className="flex items-center gap-1.5">
              <input
                type="text"
                value={symbols}
                onChange={event => setSymbols(event.target.value)}
                placeholder="留空使用全市场"
                className={`${INPUT_CLS} min-w-0 flex-1 font-mono`}
              />
              <WatchlistGroupMenu
                onSelect={importFromWatchlist}
                disabled={watchlist.isLoading || watchlistEntries.length === 0}
                includeAll
                counts={watchlistCounts}
                total={watchlistEntries.length}
                disableEmpty
                menuLabel="选择自选分组"
                align="right"
                triggerClassName="inline-flex h-8 shrink-0 items-center gap-1 whitespace-nowrap rounded-input border border-border bg-surface px-2 text-[11px] text-secondary transition-colors hover:border-accent/50 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
                title="从自选分组加入筛选范围"
                ariaLabel="从自选加入筛选范围"
              >
                <ListPlus className="h-3 w-3" />
                {watchlist.isLoading ? '加载中' : watchlistEntries.length === 0 ? '自选为空' : '自选'}
              </WatchlistGroupMenu>
            </div>
            {symbols.trim() && (
              <div className="mt-1 text-[10px] text-muted" title="按逗号/分号/空格分隔, 自动去重; 留空使用全市场">
                已识别 {parseSymbolList(symbols).length} 只标的 · 留空使用全市场
              </div>
            )}
          </div>

          <div>
            <span className="mb-1 block text-[10px] font-medium text-muted">区间</span>
            <div className="grid grid-cols-[1fr_1fr] gap-1.5">
              <DatePicker value={start} onChange={setStart} max={end || undefined} className="w-full" buttonClassName="w-full justify-start" align="left" />
              <DatePicker value={end} onChange={setEnd} min={start || undefined} className="w-full" buttonClassName="w-full justify-start" />
            </div>
            <div className="mt-1.5 flex gap-1">
              {[3, 6, 12].map(months => (
                <button
                  key={months}
                  type="button"
                  onClick={() => { setStart(monthsAgo(months)); setEnd(TODAY) }}
                  className={`flex-1 rounded-btn px-2 py-1 text-[10px] transition-colors ${start === monthsAgo(months) && end === TODAY
                    ? 'bg-accent/10 font-medium text-accent'
                    : 'text-muted hover:bg-elevated hover:text-secondary'
                  }`}
                >
                  {months === 12 ? '1年' : `${months}个月`}
                </button>
              ))}
            </div>
          </div>

          <details className="rounded-btn border border-border bg-surface/60">
            <summary className="cursor-pointer select-none px-2.5 py-1.5 text-[11px] text-secondary transition-colors hover:text-foreground">
              回测参数 · {rebalance === 'daily' ? '日度' : rebalance === 'weekly' ? '周度' : '月度'} / {nGroups}组 / 佣金 {fees}‱
            </summary>
            <div className="grid grid-cols-3 gap-1.5 border-t border-border/60 px-2.5 py-2">
              <label className="block">
                <span className="mb-1 block text-[10px] text-muted">调仓</span>
                <select value={rebalance} onChange={event => setRebalance(event.target.value as typeof rebalance)} className={INPUT_CLS}>
                  <option value="daily">日度</option>
                  <option value="weekly">周度</option>
                  <option value="monthly">月度</option>
                </select>
              </label>
              <label className="block">
                <span className="mb-1 block text-[10px] text-muted">分组</span>
                <select value={nGroups} onChange={event => setNGroups(Number(event.target.value))} className={INPUT_CLS}>
                  <option value={3}>3组</option>
                  <option value={5}>5组</option>
                  <option value={10}>10组</option>
                </select>
              </label>
              <label className="block">
                <span className="mb-1 block text-[10px] text-muted">佣金/万</span>
                <input type="number" value={fees} onChange={event => setFees(event.target.value)} className={INPUT_CLS} />
              </label>
            </div>
          </details>
        </div>

        {/* 运行条: 常驻底部, 不随列表滚动消失 */}
        <div className="shrink-0 space-y-1.5 border-t border-border bg-surface/70 px-3 py-2.5">
          <button
            type="button"
            onClick={() => startRun()}
            disabled={run.isPending || selected.length === 0}
            title={selected.length >= 16 ? '因子较多, 全市场运行可能需要较长时间; 可先用「快速体检」' : undefined}
            className="inline-flex w-full items-center justify-center gap-1.5 rounded-btn bg-accent px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Play className="h-3.5 w-3.5" />
            {run.isPending ? '筛选中…' : `筛选 ${selected.length} 个因子`}
          </button>
          {run.isPending && (
            <button
              type="button"
              onClick={cancelRun}
              className="w-full rounded-btn border border-border px-3 py-1.5 text-[11px] text-secondary transition-colors hover:border-danger/40 hover:text-danger"
            >
              取消本次筛选
            </button>
          )}
        </div>
      </section>

      <section className="min-w-0 bg-base/15 xl:overflow-y-auto">
        {run.isPending && (
          <div className="m-3 flex items-center gap-3 rounded-btn border border-accent/30 bg-accent/5 px-3 py-2.5 text-xs text-secondary">
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-accent/25 border-t-accent" />
            正在加载共享数据面板并评估 {selected.length} 个因子
            <button
              type="button"
              onClick={cancelRun}
              className="ml-auto shrink-0 rounded-btn border border-border px-2 py-1 text-[10px] text-secondary transition-colors hover:border-danger/40 hover:text-danger"
            >
              取消
            </button>
          </div>
        )}
        {runCancelled && (
          <div className="m-3 rounded-btn border border-border bg-elevated/40 px-3 py-2 text-xs text-muted">
            已取消本次筛选, 配置与已选因子保留, 可调整后重新运行。
          </div>
        )}
        {run.isError && !runCancelled && (
          <div className="m-3 rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
            {String((run.error as Error).message)}
          </div>
        )}
        {run.data?.error && (
          <div className="m-3 rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">{run.data.error}</div>
        )}
        {!results && !run.isPending && (
          <div className="m-3 rounded-card border border-border bg-surface/60 p-6 text-center">
            <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-full bg-accent/10 text-accent">
              <Search className="h-5 w-5" />
            </div>
            <div className="mt-3 text-sm font-medium text-foreground">三步找到有效因子</div>
            <div className="mx-auto mt-1.5 max-w-md text-xs leading-relaxed text-muted">
              左侧选因子（或用预设）→ 点「筛选」批量检验 → 点击结果行看单因子详情。
              IC 衡量历史上因子值与后续涨跌的关联强度（绝对值越大关联越强），IR 是稳定度。
            </div>
            <button
              type="button"
              onClick={() => {
                const ids = presetIds(QUICK_PRESET)
                setSelected(ids)
                setActivePreset('quick')
                startRun(ids)
              }}
              disabled={allColumns.length === 0}
              className="mt-3 inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-2 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Play className="h-3.5 w-3.5" />
              立即跑「快速体检」
            </button>
            <div className="mt-1.5 text-[10px] text-muted">每组取 1 个代表因子, 通常十几秒出结果</div>
          </div>
        )}
        {results && !results.error && (
          <div>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border px-4 py-3">
              <div className="min-w-0">
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                  <span className="text-sm font-medium text-foreground">筛选结果</span>
                  <span className="text-[10px] text-muted">
                    {results.results.length} 因子 · {results.n_symbols} 标的 · {results.n_dates} 交易日 · <Clock className="inline h-3 w-3" />{results.elapsed_ms.toFixed(0)}ms
                  </span>
                  {!run.data && restored && (
                    <span
                      className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated/40 px-1.5 py-0.5 text-[10px] text-muted"
                      title="会话内记忆的上次运行结果; 修改左侧配置后点「筛选」重新计算"
                    >
                      <Clock className="h-3 w-3" />
                      上次运行 {new Date(restored.ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}
                    </span>
                  )}
                </div>
                {computableResults.length > 0 && (
                  <div className="mt-1 text-[11px] text-secondary">
                    <span className="font-medium text-bull">{validCount} 个有效</span>
                    <span className="text-muted">
                      {hasServerVerdict(computableResults)
                        ? '（NW 显著性检验：|t|≥2 且 BH q≤0.1，|IC|≥0.02）'
                        : '（经验规则：|IC|≥0.02 且 |IR|≥0.3）'}
                    </span>
                    {bestFactor && bestFactor.ic_mean != null && (
                      <> · 最强 <span className="font-medium text-foreground">{bestFactor.label}</span>（IC {fmtPct(bestFactor.ic_mean)}）</>
                    )}
                  </div>
                )}
              </div>
              {results.results.length > 0 && (
                <div className="ml-auto shrink-0 text-right" title="全部结果的结论分布 (有效/边缘/无效/失败)">
                  <div className="ml-auto flex h-1.5 w-40 overflow-hidden rounded-full bg-base">
                    {([
                      ['bg-bull', verdictCounts.valid],
                      ['bg-amber-400', verdictCounts.edge],
                      ['bg-border', verdictCounts.invalid],
                      ['bg-danger/70', verdictCounts.error],
                    ] as const).map(([cls, count]) => count > 0 && (
                      <span key={cls} className={cls} style={{ width: `${(count / results.results.length) * 100}%` }} />
                    ))}
                  </div>
                  <div className="mt-1 text-[10px] text-muted">
                    有效 {verdictCounts.valid} · 边缘 {verdictCounts.edge} · 无效 {verdictCounts.invalid}
                    {verdictCounts.error > 0 && ` · 失败 ${verdictCounts.error}`}
                  </div>
                  <div className="mt-0.5 text-[10px] text-muted/60">点击行深看 · ↑↓ 切换 · 表头排序</div>
                </div>
              )}
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[820px] text-xs">
                <thead className="sticky top-0 bg-elevated text-left text-[11px] text-secondary">
                  <tr>
                    <th className="w-12 px-3 py-2.5 text-center font-medium" title="按当前排序键排序的名次，默认按 |IC|。">排名</th>
                    <th className="px-3 py-2.5 font-medium">因子</th>
                    <SortableTh label="IC 均值" sortKeyName="ic" sortKey={sortKey} sortAsc={sortAsc} onSort={applySort} className="px-3 py-2.5 text-right" title="每天用因子给股票打分、与次日真实涨跌算相关性（Rank IC）的均值。|IC|≥0.02 且稳定即视为历史有效；负值同样有效（反向使用）。" />
                    <SortableTh label="稳定度 IR" sortKeyName="ir" sortKey={sortKey} sortAsc={sortAsc} onSort={applySort} className="px-3 py-2.5 text-right" title="IC 均值 ÷ IC 波动。≥0.3 值得关注，≥0.5 相当稳定。" />
                    <SortableTh label="t 值(NW)" sortKeyName="t_nw" sortKey={sortKey} sortAsc={sortAsc} onSort={applySort} className="px-3 py-2.5 text-right" title="Newey-West HAC 稳健 t 值（滞后 1）：|t|≥2 视为统计显著；悬停查看多重检验校正后的 q 值（≤0.1 通过）。样本不足显示 —。" />
                    <SortableTh label="方向一致占比" sortKeyName="win_rate" sortKey={sortKey} sortAsc={sortAsc} onSort={applySort} className="px-3 py-2.5 text-right" title="IC 与自身方向一致的天数占比。50% 是抛硬币，55%+ 不错。" />
                    <SortableTh label="多空收益" sortKeyName="return" sortKey={sortKey} sortAsc={sortAsc} onSort={applySort} className="px-3 py-2.5 text-right" title="每期买因子最高组、（模拟）卖最低组的累计收益差。A 股做空受限，此列为理论口径。" />
                    <SortableTh label="最大回撤" sortKeyName="drawdown" sortKey={sortKey} sortAsc={sortAsc} onSort={applySort} className="px-3 py-2.5 text-right" title="上述多空组合最痛的一段亏损幅度，衡量拿得住不住。" />
                    <th
                      className="w-16 px-3 py-2.5 text-center font-medium"
                      title={hasServerVerdict(computableResults)
                        ? 'NW 显著性检验：|t|≥2 且 BH q≤0.1 且 |IC|≥0.02 为有效；其一达标为边缘'
                        : '经验规则：|IC|≥0.02 且 |IR|≥0.3 为有效；其一达标为边缘'}
                    >
                      结论
                    </th>
                    <th className="w-24 px-3 py-2.5 text-right font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {sortedResults.map(({ item, verdict }, index) => (
                    <tr
                      key={item.factor_name}
                      onClick={() => !item.error && onDetail(item.factor_name, item.label)}
                      title={item.error ? undefined : '点击查看单因子详情 (IC 曲线 / 分层净值 / 分组表现)'}
                      className={`border-t border-border/70 transition-colors hover:bg-elevated/40 ${item.error ? '' : 'cursor-pointer'}`}
                    >
                      <td className="px-3 py-3 text-center font-mono text-muted">{index + 1}</td>
                      <td className="px-3 py-3">
                        <div className="font-medium text-foreground">
                          {item.label}
                          {!item.error && item.ic_mean != null && (
                            item.ic_mean >= 0
                              ? <span className="ml-1 text-bull" title="样本内方向：值大看多（IC 为正）。历史方向不代表未来。">↑</span>
                              : <span className="ml-1 text-bear" title="样本内方向：值小看多（IC 为负，反向使用）。历史方向不代表未来。">↓</span>
                          )}
                        </div>
                        <div className="mt-0.5 text-[10px] text-muted">{item.group} · {item.factor_name}</div>
                        {item.error && <div className="mt-1 text-[10px] text-danger">{item.error}</div>}
                      </td>
                      <td className="px-3 py-3 text-right font-mono">
                        <div className="flex items-center justify-end gap-1.5">
                          <span className={priceColorClass(item.ic_mean)}>{item.ic_mean == null ? '—' : fmtPct(item.ic_mean)}</span>
                          {item.ic_mean != null && (
                            <span className="inline-flex h-1 w-10 shrink-0 overflow-hidden rounded-full bg-base" title={`|IC|=${Math.abs(item.ic_mean).toFixed(4)} 的可视化`}>
                              <span
                                className={`h-full ${item.ic_mean >= 0 ? 'bg-bull/70' : 'bg-bear/70'}`}
                                style={{ width: `${Math.min(Math.abs(item.ic_mean) / 0.08, 1) * 100}%` }}
                              />
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="px-3 py-3 text-right font-mono text-foreground">{item.ir == null ? '—' : item.ir.toFixed(2)}</td>
                      <td
                        className={`px-3 py-3 text-right font-mono ${item.t_newey_west != null && Math.abs(item.t_newey_west) >= 2 ? 'text-foreground' : 'text-muted'}`}
                        title={item.t_newey_west != null
                          ? `NW t=${item.t_newey_west.toFixed(2)}${item.q_value != null ? `，BH q=${item.q_value.toFixed(3)}` : ''}（${item.q_value != null && item.q_value <= 0.1 ? '通过' : '未通过'}多重检验校正）`
                          : '样本不足，无法计算'}
                      >
                        {item.t_newey_west == null ? '—' : item.t_newey_west.toFixed(2)}
                      </td>
                      <td className="px-3 py-3 text-right font-mono text-secondary">{item.ic_win_rate == null ? '—' : fmtPct(item.ic_win_rate)}</td>
                      <td className={`px-3 py-3 text-right font-mono ${priceColorClass(item.long_short_return)}`}>{item.long_short_return == null ? '—' : fmtPct(item.long_short_return)}</td>
                      <td className="px-3 py-3 text-right font-mono text-bear">{item.long_short_max_drawdown == null ? '—' : fmtPct(item.long_short_max_drawdown)}</td>
                      <td className="px-3 py-3 text-center">
                        <span className={`inline-flex rounded-btn px-1.5 py-0.5 text-[10px] font-medium ${VERDICT_META[verdict].cls}`}>
                          {VERDICT_META[verdict].label}
                        </span>
                      </td>
                      <td className="px-3 py-3">
                        <div className="flex justify-end gap-1">
                          <button
                            type="button"
                            onClick={() => save.mutate(item)}
                            disabled={!!item.error || save.isPending}
                            className="flex h-7 w-7 items-center justify-center rounded-btn text-muted transition-colors hover:bg-accent/10 hover:text-accent disabled:opacity-40"
                            title="保存候选"
                            aria-label={`保存 ${item.label} 为候选`}
                          >
                            <BookmarkPlus className="h-3.5 w-3.5" />
                          </button>
                          <button
                            type="button"
                            onClick={() => setSignalFrom(item)}
                            disabled={!!item.error}
                            className="flex h-7 w-7 items-center justify-center rounded-btn text-muted transition-colors hover:bg-amber-400/10 hover:text-amber-400 disabled:opacity-40"
                            title="加入信号条件（方向按 IC 预填，阈值给建议值）"
                            aria-label={`把 ${item.label} 加入信号条件`}
                          >
                            <Zap className="h-3.5 w-3.5" />
                          </button>
                          <button
                            type="button"
                            onClick={event => { event.stopPropagation(); onFullscreen(item.factor_name, item.label) }}
                            disabled={!!item.error}
                            className="flex h-7 w-7 items-center justify-center rounded-btn text-muted transition-colors hover:bg-elevated hover:text-foreground disabled:opacity-40"
                            title="全屏单因子检验（完整配置与图表）"
                            aria-label={`全屏检验 ${item.label}`}
                          >
                            <Expand className="h-3.5 w-3.5" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </section>

      {signalFrom && <AddFactorSignalDialog item={signalFrom} onClose={() => setSignalFrom(null)} />}
    </div>
  )
}

/** 单因子详情面板: 批量结果行的上下文内深看。

 *  宽屏为右侧分栏 (批量结果保持可见, 看详情-回列表零成本), 窄屏退化为
 *  覆盖层; 「全屏」按钮进入完整检验视图做深度操作。内嵌的 FactorBacktest
 *  以 autoRun 挂载即出结果, 打开即看 IC 曲线与分层净值。 */
function FactorDetailPanel({ name, label, onClose, onFullscreen }: {
  name: string
  label: string
  onClose: () => void
  onFullscreen: () => void
}) {
  // label 由调用方带给出 (结果行); 因子库「去检验」直达时只有 id, 经共享列缓存解析
  const columns = useQuery({
    queryKey: QK.factorColumns,
    queryFn: api.factorColumns,
    staleTime: 300_000,
  })
  const resolvedLabel = label || columns.data?.columns.find(column => column.id === name)?.label || name
  const known = !columns.data || columns.data.columns.some(column => column.id === name)
  // ↑↓ 快速连切时防抖 250ms 才发起试算, 避免每次按键都触发一次重计算请求
  const [runName, setRunName] = useState(name)
  useEffect(() => {
    if (name === runName) return
    const timer = setTimeout(() => setRunName(name), 250)
    return () => clearTimeout(timer)
  }, [name, runName])
  return (
    <motion.aside
      initial={{ x: 24, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      exit={{ x: 24, opacity: 0 }}
      transition={{ duration: 0.22, ease: EASE_SMOOTH }}
      aria-label={`单因子详情: ${resolvedLabel}`}
      className={cn(
        'flex min-h-0 flex-col overflow-hidden rounded-card border border-border bg-surface',
        'lg:w-[42rem] lg:shrink-0',
        'max-lg:fixed max-lg:inset-y-2 max-lg:right-2 max-lg:z-40 max-lg:w-[min(60rem,calc(100vw-1rem))] max-lg:shadow-2xl',
      )}
    >
      <div className="flex h-11 shrink-0 items-center gap-2 border-b border-border px-3">
        <Layers3 className="h-4 w-4 shrink-0 text-accent" />
        <span className="min-w-0 shrink-0 truncate text-sm font-medium text-foreground">{resolvedLabel}</span>
        <span className="min-w-0 truncate font-mono text-[10px] text-muted">{name}</span>
        {name !== runName && (
          <span className="shrink-0 animate-pulse text-[10px] text-accent">切换中…</span>
        )}
        <span className="ml-auto hidden shrink-0 text-[10px] text-muted md:inline" title="按当前排序切换, 首尾循环">
          ↑↓ 切换 · Esc 关闭
        </span>
        <div className="flex shrink-0 items-center gap-0.5">
          <button
            type="button"
            onClick={onFullscreen}
            title="全屏单因子检验（完整配置与图表）"
            aria-label="全屏单因子检验"
            className="flex h-7 w-7 items-center justify-center rounded-btn text-secondary transition-colors hover:bg-elevated hover:text-foreground"
          >
            <Expand className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={onClose}
            title="关闭详情 (Esc)"
            aria-label="关闭详情"
            className="flex h-7 w-7 items-center justify-center rounded-btn text-secondary transition-colors hover:bg-elevated hover:text-foreground"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {known ? (
          <div className="h-full min-h-[36rem]">
            <FactorBacktest key={runName} initialFactorName={runName} autoRun />
          </div>
        ) : (
          <div className="p-6 text-center text-xs text-muted">
            未找到因子 <code className="font-mono text-secondary">{name}</code>, 可能已被删除或重命名。
          </div>
        )}
      </div>
    </motion.aside>
  )
}

export function FactorDiscovery({ focusFactor, initialView = 'batch' }: { focusFactor?: string; initialView?: View } = {}) {
  const [view, setView] = useState<View>(initialView)
  const [detailFactor, setDetailFactor] = useState('momentum_20d')
  const [detail, setDetail] = useState<{ name: string; label: string } | null>(null)
  /** 当前排序下的结果序列 (BatchDiscovery 上报), ↑↓ 键盘流式浏览的消费源 */
  const [order, setOrder] = useState<{ name: string; label: string }[]>([])
  const handleOrder = useCallback((list: { name: string; label: string }[]) => setOrder(list), [])
  const [autoOpen, setAutoOpen] = useState(false)
  const [introCollapsed, setIntroCollapsed] = useState(() => {
    try {
      return localStorage.getItem(INTRO_STORAGE_KEY) === '1'
    } catch {
      return false
    }
  })
  const collapseIntro = () => {
    setIntroCollapsed(true)
    try {
      localStorage.setItem(INTRO_STORAGE_KEY, '1')
    } catch { /* 隐私模式等场景忽略 */ }
  }
  // 键盘: Esc 关闭详情; ↑↓ 按当前排序流式切换因子 (输入控件内不拦截)
  useEffect(() => {
    if (!detail) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setDetail(null)
        return
      }
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
      const target = event.target as HTMLElement | null
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable)) return
      const index = order.findIndex(entry => entry.name === detail.name)
      if (index < 0 || order.length === 0) return
      event.preventDefault()
      const next = event.key === 'ArrowDown'
        ? (index + 1) % order.length
        : (index - 1 + order.length) % order.length
      setDetail(order[next])
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [detail, order])
  const openDetail = (factorName: string, label: string) => setDetail({ name: factorName, label })
  // 因子库「去检验」直达: focus 参数命中即打开详情面板 (autoRun 即出结果),
  // 批量侧同时只选它 — 省掉「跳过来再自己点筛选」的手动一步。
  const focusHandled = useRef(false)
  useEffect(() => {
    if (!focusFactor || focusHandled.current) return
    focusHandled.current = true
    setDetail({ name: focusFactor, label: '' })
  }, [focusFactor])
  const openFullscreen = (factorName: string, _label: string) => {
    setDetail(null)
    setDetailFactor(factorName)
    setView('single')
  }
  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      {!introCollapsed && (
        <div className="flex shrink-0 items-start gap-2 rounded-card border border-border bg-surface/60 px-3 py-2 text-[11px] leading-relaxed text-secondary">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" />
          <div className="min-w-0 flex-1">
            <span className="font-medium text-foreground">三步看懂本页：</span>
            ① 因子 = 给股票打分排序的特征（如 20日涨幅、换手率变化）；② 本页度量过去哪些特征与次日涨跌存在稳定关联；③ IC = 关联强度（绝对值越大越强，负值反向用），IR = 稳定度（越大越稳）。所有结果均为历史统计，不构成投资建议，不代表未来表现。
          </div>
          <button
            type="button"
            onClick={collapseIntro}
            className="shrink-0 rounded-btn px-2 py-0.5 text-[10px] text-muted transition-colors hover:bg-elevated hover:text-foreground"
          >
            知道了
          </button>
        </div>
      )}
      <div className="flex shrink-0 items-center border-b border-border/70 px-1 pb-2">
        {view === 'single' && (
          <button
            type="button"
            onClick={() => setView('batch')}
            className="mr-2 inline-flex items-center gap-1 rounded-btn px-2 py-1 text-[11px] text-muted transition-colors hover:bg-elevated hover:text-foreground"
            title="返回批量结果（筛选配置与结果保留）"
          >
            ← 返回批量
          </button>
        )}
        <div className="inline-flex rounded-btn border border-border bg-surface/80 p-0.5">
          {([
            ['batch', '批量筛选', ListFilter],
            ['mining', '挖掘工作台', Pickaxe],
          ] as const).map(([value, label, Icon]) => (
            <button
              key={value}
              type="button"
              onClick={() => setView(value)}
              className={`inline-flex items-center gap-1.5 rounded-[5px] px-3 py-1.5 text-xs font-medium transition-colors ${view === value || (value === 'batch' && view === 'single')
                ? 'bg-accent text-white shadow-sm'
                : 'text-secondary hover:bg-elevated hover:text-foreground'
              }`}
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
            </button>
          ))}
        </div>
        {view !== 'mining' && (
          <button
            type="button"
            onClick={() => setAutoOpen(true)}
            className="ml-auto mr-1 inline-flex items-center gap-1.5 rounded-btn border border-accent/40 bg-accent/5 px-2.5 py-1.5 text-xs font-medium text-accent transition-colors hover:bg-accent/10"
            title="不知道选什么因子？让系统全量筛选达标因子并自动搜索组合（嵌套样本外验证）"
          >
            <Sparkles className="h-3.5 w-3.5" />
            自动挖掘
          </button>
        )}
      </div>
      <div className="min-h-0 flex-1">
        {/* 批量视图保持挂载: 切到挖掘/全屏再返回时, 选择集与结果不丢;
            详情面板与结果并排, 点击行深看不打断浏览 */}
        <div className={view === 'batch' ? 'h-full' : 'hidden'}>
          <div className="flex h-full min-h-0 gap-3">
            <div className="min-w-0 flex-1">
              <BatchDiscovery onFullscreen={openFullscreen} onDetail={openDetail} onOrder={handleOrder} focusFactor={focusFactor} />
            </div>
            <AnimatePresence>
              {detail && (
                <FactorDetailPanel
                  key={detail.name}
                  name={detail.name}
                  label={detail.label}
                  onClose={() => setDetail(null)}
                  onFullscreen={() => openFullscreen(detail.name, detail.label)}
                />
              )}
            </AnimatePresence>
          </div>
        </div>
        {view === 'single' && (
          <div className="h-full">
            <FactorBacktest key={detailFactor} initialFactorName={detailFactor} autoRun />
          </div>
        )}
        {view === 'mining' && (
          <div className="h-full min-h-0 xl:overflow-y-auto">
            <MiningWorkbench />
          </div>
        )}
      </div>

      {autoOpen && <AutoMiningDialog onClose={() => setAutoOpen(false)} />}
    </div>
  )
}
