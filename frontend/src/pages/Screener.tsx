import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { motion } from 'framer-motion'
import { ScanSearch, Clock, TrendingUp, Star, Filter, Layers, Network, Sparkles, RefreshCw, Settings2, FlaskConical, BarChart3, AlertTriangle } from 'lucide-react'
import { api, genRuleId, type ScreenerStrategy, type ScreenerResult } from '@/lib/api'
import { useDataStatus, usePreferences } from '@/lib/useSharedQueries'
import { useWatchlistBatchAdd } from '@/lib/useSharedMutations'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { filterActiveScreenerRows, stageScreenerBacktestHandoff, type ScreenerBacktestTarget } from '@/lib/screenerBacktestHandoff'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { DatePicker } from '@/components/DatePicker'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { useStrategyPool } from '@/lib/useStrategyPool'
import { StrategyCard, CardSize, loadCardSize, cardWrapCls } from '@/components/screener/StrategyCard'
import { ScreenerTable } from '@/components/screener/ScreenerTable'
import { ScreenerFilter as ScreenerFilterType, defaultFilter, filterActive, countActiveFilters, applyFilter, FilterPanel } from '@/components/screener/ScreenerFilter'
import { StrategySettingsDialog } from '@/components/screener/StrategySettingsDialog'
import { StrategyPoolDialog } from '@/components/screener/StrategyPoolDialog'
import { StrategyBuilderDialog } from '@/components/screener/StrategyBuilderDialog'
import { ListColumnCustomizer } from '@/components/ListColumnCustomizer'
import { useTableSort } from '@/components/stock-table/useTableSort'
import { resolveCandleConfig } from '@/lib/list-columns'
import {
  SCREENER_BUILTIN_COLUMNS,
  SCREENER_COLUMN_GROUPS,
  buildExtColumnsParam,
  loadScreenerColumnConfig,
  saveScreenerColumnConfig,
  type ColumnConfig,
} from '@/lib/screener-columns'

export function Screener() {
  const navigate = useNavigate()
  const [activeStrategy, setActiveStrategy] = useState<string | null>(null)
  const [result, setResult] = useState<ScreenerResult | null>(null)
  const [asOf, setAsOf] = useState<string>('')
  const [batchMsg, setBatchMsg] = useState<string>('')
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const [previewName, setPreviewName] = useState<string>('')
  const closePreview = useCallback(() => { setPreviewSymbol(null); setPreviewName('') }, [])
  const [settingsStrategyId, setSettingsStrategyId] = useState<string | null>(null)
  const [showPoolDialog, setShowPoolDialog] = useState(false)
  const [showBuilder, setShowBuilder] = useState(false)
  const [builderMode, setBuilderMode] = useState<'create' | 'modify'>('create')
  const { pool, addToPool, removeFromPool, reorderPool, prune } = useStrategyPool()
  const [cardSize, setCardSize] = useState<CardSize>(loadCardSize)
  // 日k蜡烛图显示开关（仅当 candle 列可见时才有意义；持久化）
  const [dailyKChartVisible, setDailyKChartVisible] = useState<boolean>(() => storage.screenerCandle.get(true))
  const toggleDailyKChart = useCallback(() => {
    setDailyKChartVisible(v => {
      const next = !v
      storage.screenerCandle.set(next)
      return next
    })
  }, [])
  const [showAll, setShowAll] = useState(false)
  const [showFilter, setShowFilter] = useState(false)
  const [filter, setFilter] = useState<ScreenerFilterType>(defaultFilter)
  const filterMap = useRef<Map<string, ScreenerFilterType>>(new Map())
  const runAllDateRef = useRef<string | null>(null)

  // 结果列配置 — 默认内置列，异步合并后端/localStorage 偏好
  const [columns, setColumns] = useState<ColumnConfig[]>([...SCREENER_BUILTIN_COLUMNS])
  const [customizerOpen, setCustomizerOpen] = useState(false)
  const columnsLoaded = useRef(false)

  useEffect(() => {
    if (columnsLoaded.current) return
    columnsLoaded.current = true
    loadScreenerColumnConfig().then(setColumns)
  }, [])

  const handleColumnsChange = useCallback((next: ColumnConfig[]) => {
    setColumns(next)
    saveScreenerColumnConfig(next)
  }, [])

  const extColumnsParam = useMemo(() => buildExtColumnsParam(columns), [columns])

  // 各策略命中数 (进入页面自动跑)
  const [hitCounts, setHitCounts] = useState<Record<string, number>>({})
  // 各策略失效数 (今日曾命中 - 当前命中)
  const [expiredCounts, setExpiredCounts] = useState<Record<string, number>>({})
  // 各策略显示上限 (null = 全部)
  const [strategyLimits, setStrategyLimits] = useState<Record<string, number | null>>({})

  // 筛选条件变化时同步到 map（供切换策略时读取最新值）
  useEffect(() => {
    if (activeStrategy) filterMap.current.set(activeStrategy, filter)
  }, [filter, activeStrategy])

  // 切换策略时恢复该策略之前保存的筛选
  const handleStrategySwitch = useCallback((strategyId: string) => {
    setFilter(filterMap.current.get(strategyId) ?? { ...defaultFilter })
  }, [])

  // 对原始结果应用过滤
  const filteredRows = result
    ? applyFilter(result.rows, filter)
    : []

  const { data: prefs } = usePreferences()
  const screenerAutoRun = prefs?.screener_auto_run ?? true

  const strategies = useQuery({
    queryKey: QK.screenerStrategies,
    queryFn: api.screenerStrategies,
  })

  // 策略结果缓存 — 文件读取，SSE invalidation 自动刷新
  const cachedQuery = useQuery({
    queryKey: QK.screenerCached(extColumnsParam),
    queryFn: () => api.screenerCached(extColumnsParam || undefined),
  })

  const dataStatus = useDataStatus({ staleTime: 0 })

  // 默认日期 = enriched 最新日期（始终跟随最新）
  useEffect(() => {
    const latest = dataStatus.data?.enriched?.latest_date
    if (latest) setAsOf(latest)
  }, [dataStatus.data?.enriched?.latest_date])

  // 策略 ID → 名称映射
  const strategyIdToName = useMemo(() => {
    const map: Record<string, string> = {}
    for (const p of strategies.data?.presets ?? []) {
      map[p.id] = p.name
    }
    return map
  }, [strategies.data])

  // 策略 ID → 完整对象映射（避免每张卡片 find 遍历）
  const strategyMap = useMemo(() => {
    const map = new Map<string, ScreenerStrategy>()
    for (const p of strategies.data?.presets ?? []) {
      map.set(p.id, p)
    }
    return map
  }, [strategies.data])

  const availableStrategyIds = useMemo(() => new Set((strategies.data?.presets ?? []).map(s => s.id)), [strategies.data])
  const visiblePool = useMemo(() => pool.filter(id => availableStrategyIds.has(id)), [pool, availableStrategyIds])

  // 策略列表加载后,自动清除池中失效的自定义策略(如本地开发残留的、
  // 当前后端已不存在的策略 ID),避免"策略池"对话框持续显示失效项。
  // availableStrategyIds 初始为空集合时跳过,防止首次渲染误清整个池。
  useEffect(() => {
    if (availableStrategyIds.size === 0) return
    prune(availableStrategyIds)
  }, [availableStrategyIds, prune])

  // 进入页面自动跑策略池中的策略，获取命中数
  const runAll = useMutation({
    mutationFn: ({ date, strategyIds }: { date?: string; strategyIds?: string[] } = {}) =>
      api.screenerRunAll(date, strategyIds ?? visiblePool, extColumnsParam || undefined),
    onSuccess: (data) => {
      if (data.as_of) setAsOf(data.as_of)
    },
  })

  const applyRunAllResult = useCallback((strategyId: string, date: string, data = runAll.data) => {
    const cached = data?.results?.[strategyId]
    if (!cached || cached.as_of !== date) return false

    setResult({
      as_of: cached.as_of,
      strategy: strategyId,
      rows: cached.rows,
      total: cached.total,
      elapsed_ms: 0,
    })
    setHitCounts(prev => ({ ...prev, [strategyId]: cached.total }))
    return true
  }, [runAll.data])

  // 缓存是否覆盖当前策略池
  const cacheCoversPool = useMemo(() => {
    if (!cachedQuery.data?.as_of || cachedQuery.data.as_of !== asOf) return false
    if (!cachedQuery.data.results) return false
    return visiblePool.length > 0 && visiblePool.every(id => id in cachedQuery.data!.results)
  }, [cachedQuery.data, asOf, visiblePool])

  // 统一数据源: 缓存优先，runAll fallback
  const effectiveResults = useMemo(() => {
    if (cacheCoversPool) return cachedQuery.data!.results
    return runAll.data?.results ?? null
  }, [cacheCoversPool, cachedQuery.data, runAll.data])

  // 从 effectiveResults 同步 hitCounts + expiredCounts
  useEffect(() => {
    if (!effectiveResults) return
    const counts: Record<string, number> = {}
    for (const [id, r] of Object.entries(effectiveResults)) {
      counts[id] = r.total
    }
    setHitCounts(counts)

    // 从缓存数据计算失效数 (ever_matched - current)
    const everMatched = cachedQuery.data?.today_ever_matched
    if (everMatched) {
      const expired: Record<string, number> = {}
      for (const [id, symbols] of Object.entries(everMatched) as [string, string[]][]) {
        const currentRows = effectiveResults[id]?.rows ?? []
        const currentSet = new Set(currentRows.map((r: any) => r.symbol))
        const expiredCount = symbols.filter((s: string) => !currentSet.has(s)).length
        if (expiredCount > 0) expired[id] = expiredCount
      }
      setExpiredCounts(expired)
    }

    // 如果有激活策略，同步当前 result（扩展列变化时也会刷新行数据）
    if (activeStrategy && effectiveResults[activeStrategy]) {
      const r = effectiveResults[activeStrategy]
      setResult(prev => {
        if (prev?.strategy === activeStrategy && prev.as_of === r.as_of && prev.rows === r.rows && prev.total === r.total) return prev
        return {
          as_of: r.as_of,
          strategy: activeStrategy,
          rows: r.rows,
          total: r.total,
          elapsed_ms: 0,
        }
      })
    }
  }, [effectiveResults, cachedQuery.data, activeStrategy])

  // symbol → 所属策略列表 (来自 effectiveResults)
  const symbolStrategyMap = useMemo(() => {
    const map = new Map<string, string[]>()
    if (!effectiveResults) return map
    for (const [sid, r] of Object.entries(effectiveResults)) {
      for (const row of r.rows) {
        const arr = map.get(row.symbol)
        if (arr) {
          arr.push(sid)
        } else {
          map.set(row.symbol, [sid])
        }
      }
    }
    return map
  }, [effectiveResults])

  // "全部" 模式: 合并所有策略的去重个股
  const allRows = useMemo(() => {
    if (!effectiveResults) return []
    const seen = new Set<string>()
    const merged: any[] = []
    for (const r of Object.values(effectiveResults)) {
      for (const row of r.rows) {
        if (!seen.has(row.symbol)) {
          seen.add(row.symbol)
          merged.push(row)
        }
      }
    }
    return merged
  }, [effectiveResults])

  // 计算失效行: 在 today_ever_rows 中但不在当前 results 中
  const expiredRowsMap = useMemo(() => {
    const map = new Map<string, any[]>() // strategyId → expired rows
    const everRows = cachedQuery.data?.today_ever_rows
    if (!everRows || !effectiveResults) return map

    for (const [sid, symMap] of Object.entries(everRows) as [string, Record<string, any>][]) {
      const currentRows = effectiveResults[sid]?.rows ?? []
      const currentSymbols = new Set(currentRows.map((r: any) => r.symbol))
      const expired = Object.entries(symMap)
        .filter(([sym]) => !currentSymbols.has(sym))
        .map(([, row]) => ({ ...row, _expired: true }))
      if (expired.length > 0) map.set(sid, expired)
    }
    return map
  }, [cachedQuery.data, effectiveResults])

  // 表头排序（受控）：用户点击列则按该列；未点时下方按评分默认降序
  const { sort, toggle, sortRows } = useTableSort()

  // 当前显示的行数据 (全部模式 或 单策略模式) + 失效行
  const displayRows = useMemo(() => {
    let rows = showAll
      ? applyFilter(allRows, filter)
      : filteredRows
    // 排序：用户点了表头则按该列，否则默认评分降序
    rows = sort
      ? sortRows(rows, columns)
      : [...rows].sort((a, b) => (b.score ?? -Infinity) - (a.score ?? -Infinity))
    const limit = !showAll && activeStrategy
      ? strategyLimits[activeStrategy] ?? null
      : null
    const mainRows = limit != null ? rows.slice(0, limit) : rows

    // 追加当前策略的失效行 (灰色)
    if (!showAll && activeStrategy) {
      const expired = expiredRowsMap.get(activeStrategy) ?? []
      if (expired.length > 0) {
        return [...mainRows, ...expired]
      }
    }
    return mainRows
  }, [showAll, allRows, filteredRows, filter, activeStrategy, strategyLimits, expiredRowsMap, sort, sortRows, columns])

  // 批量加自选 / 送回测只用未失效的行：_expired 是"今日曾命中、当前已失效"的灰色行
  const activeDisplayRows = useMemo(() => filterActiveScreenerRows(displayRows), [displayRows])

  // 送回测的 as_of：单策略模式取当前结果日期；全部模式取实际数据源日期（缓存或 runAll）
  const handoffAsOf = showAll
    ? (cachedQuery.data?.as_of ?? runAll.data?.as_of ?? null)
    : (result?.as_of ?? null)

  // 日k列是否启用 → 决定是否加载批量 kline 数据
  const candleColumn = useMemo(() =>
    columns.find(c => c.source.type === 'builtin' && c.source.key === 'candle' && c.visible),
    [columns],
  )
  const candleColumnEnabled = !!candleColumn
  // 日k天数（来自列配置，已钳制边界）
  const candleDays = useMemo(() => resolveCandleConfig(candleColumn?.candleConfig).days, [candleColumn])
  // 真正请求/渲染蜡烛图：列可见 且 眼睛开关开启
  const dailyKVisible = candleColumnEnabled && dailyKChartVisible

  // 批量日k数据 (仅当蜡烛图可见时加载，省请求)
  const resultSymbolsKey = useMemo(() => displayRows.map((r: any) => r.symbol).join(','), [displayRows])
  const klineBatch = useQuery({
    queryKey: QK.screenerKlineBatch(`${resultSymbolsKey}|${candleDays}`),
    queryFn: () => api.klineDailyBatch(displayRows.map((r: any) => r.symbol), candleDays),
    enabled: dailyKVisible && displayRows.length > 0,
    staleTime: 5 * 60_000,
  })
  const klineData = dailyKVisible ? (klineBatch.data?.data ?? {}) : {}

  // asOf 确定后 + 策略列表就绪 + 策略池非空 → 自动跑一次 (受系统设置开关控制)
  // 缓存命中时秒加载; 未命中时, 仅当 screener_auto_run 开启才自动触发 runAll
  // 必须等 cachedQuery.isFetched（非 isLoading）: 否则缓存未读到时 cacheCoversPool 恒为
  // false, 会与缓存加载竞态触发多余的 runAll
  useEffect(() => {
    if (!cachedQuery.isFetched) return
    if (!asOf || !strategies.data?.presets?.length || runAll.isPending || visiblePool.length === 0) return
    const runKey = `${asOf}|${visiblePool.join(',')}|${extColumnsParam}`
    if (runAllDateRef.current === runKey) return
    // 缓存已覆盖当前策略池 → 秒加载, 不触发 runAll
    if (cacheCoversPool) {
      runAllDateRef.current = runKey
      return
    }
    // 未覆盖: 受系统开关控制
    if (!screenerAutoRun) return
    runAllDateRef.current = runKey
    runAll.mutate({ date: asOf }, {
      onSuccess: (data) => {
        if (activeStrategy) applyRunAllResult(activeStrategy, asOf, data)
      },
    })
  }, [cachedQuery.isFetched, asOf, strategies.data, visiblePool, extColumnsParam, cacheCoversPool, screenerAutoRun, activeStrategy, applyRunAllResult])

  const qc = useQueryClient()

  const run = useMutation({
    mutationFn: ({ id, date }: { id: string; date: string }) =>
      api.screenerRunPreset(id, undefined, date || undefined, extColumnsParam || undefined),
    onSuccess: (data, vars) => {
      setResult(data)
      // 同步更新卡片上的命中数
      setHitCounts(prev => ({ ...prev, [vars.id]: data.total }))
      // 单策略重跑后, 后端 _update_cache_strategy 已更新该策略的缓存条目;
      // 这里 invalidate screenerCached 让前端缓存同步, 避免点卡片时 handleRun
      // 仍读到旧的 effectiveResults (改参数后刷新会回退到旧个数的根因)
      qc.invalidateQueries({ queryKey: ['screener-cached'] })
    },
  })

  const handleRun = (s: ScreenerStrategy) => {
    handleStrategySwitch(s.id)
    setActiveStrategy(s.id)
    setShowAll(false)
    // 优先从 effectiveResults (缓存 + runAll) 取数据
    const r = effectiveResults?.[s.id]
    if (r && r.as_of === asOf) {
      setResult({
        as_of: r.as_of,
        strategy: s.id,
        rows: r.rows,
        total: r.total,
        elapsed_ms: 0,
      })
      setHitCounts(prev => ({ ...prev, [s.id]: r.total }))
      return
    }
    // Fall back to runAll data or single run
    if (!applyRunAllResult(s.id, asOf)) {
      run.mutate({ id: s.id, date: asOf })
    }
  }

  // 日期变化时，重新跑全部策略命中数 + 当前激活策略
  const handleDateChange = (newDate: string) => {
    setAsOf(newDate)
    runAllDateRef.current = `${newDate}|${visiblePool.join(',')}|${extColumnsParam}`
    runAll.mutate({ date: newDate }, {
      onSuccess: (data) => {
        if (activeStrategy) applyRunAllResult(activeStrategy, newDate, data)
      },
    })
    if (activeStrategy) {
      setResult(null)
    }
  }

  const minDate = dataStatus.data?.enriched?.earliest_date ?? ''
  const maxDate = dataStatus.data?.enriched?.latest_date ?? ''

  const batchAdd = useWatchlistBatchAdd()

  // 自选股列表 (用于判断是否在自选中)
  const watchlist = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
  })
  const watchlistSet = useMemo(() => {
    const symbols = watchlist.data?.symbols ?? []
    return new Set(symbols.map((s: any) => s.symbol))
  }, [watchlist.data])

  // 单只股票加入/移出自选
  const toggleWatchlist = useMutation({
    mutationFn: ({ symbol, inList }: { symbol: string; inList: boolean }) =>
      inList ? api.watchlistRemove(symbol) : api.watchlistAdd(symbol),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.watchlist })
      qc.invalidateQueries({ queryKey: QK.watchlistEnriched() })
    },
  })

  // 重新运行策略：重载策略文件 + 重跑全部策略，刷新符合条件的个股
  const reloadStrategies = useMutation({
    mutationFn: api.strategyReload,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.screenerStrategies })
      if (asOf) runAll.mutate({ date: asOf })
    },
  })

  // 策略监控: 查询规则, 建立 strategyId → ruleId 映射 (只看 type=strategy 且 enabled)
  const monitorRules = useQuery({ queryKey: QK.monitorRules, queryFn: api.monitorRulesList })
  const strategyMonitorMap = useMemo(() => {
    const m = new Map<string, string>()
    for (const r of monitorRules.data?.rules ?? []) {
      if (r.type === 'strategy' && r.enabled && r.strategy_id) {
        m.set(r.strategy_id, r.id)
      }
    }
    return m
  }, [monitorRules.data])

  const toggleStrategyMonitor = (strategyId: string, strategyName: string) => {
    const existingRuleId = strategyMonitorMap.get(strategyId)
    if (existingRuleId) {
      // 已监控 → 删除规则
      api.monitorRuleDelete(existingRuleId).then(() =>
        qc.invalidateQueries({ queryKey: QK.monitorRules }),
      )
    } else {
      // 未监控 → 直接创建 type=strategy 规则
      api.monitorRuleSave({
        id: genRuleId(),
        name: `策略监控 · ${strategyName}`,
        enabled: true,
        type: 'strategy',
        scope: 'all',
        symbols: [],
        sector: null,
        strategy_id: strategyId,
        direction: 'entry',
        conditions: [],
        logic: 'or',
        cooldown_seconds: 3600,
        severity: 'info',
        message: '',
      }).then(() => qc.invalidateQueries({ queryKey: QK.monitorRules }))
    }
  }

  // 失效行 (_expired) 不进自选、不进回测
  const handleBatchAdd = () => {
    if (!activeDisplayRows.length) return
    const symbols = activeDisplayRows.map(r => r.symbol)
    batchAdd.mutate(symbols, {
      onSuccess: (data) => {
        setBatchMsg(`已添加 ${data.added} 只到自选`)
        setTimeout(() => setBatchMsg(''), 3000)
      },
      onError: () => {
        setBatchMsg('添加失败')
        setTimeout(() => setBatchMsg(''), 3000)
      },
    })
  }

  const sendToBacktest = useCallback((target: ScreenerBacktestTarget) => {
    const symbols = activeDisplayRows.map(r => r.symbol)
    if (symbols.length === 0) {
      // S3 review: 全部行失效 (或无命中) 时不写交接 — 空池 + strategyId 会
      // 让回测页静默切到「全市场 + 该策略」, 用户丢掉原股票池。
      setBatchMsg('当前结果没有可带入回测的标的代码。')
      setTimeout(() => setBatchMsg(''), 3000)
      return
    }
    if (stageScreenerBacktestHandoff({
      target,
      symbols,
      asOf: handoffAsOf,
      // 仅单策略视图下携带当前策略，让回测页直接选中它；全部模式是合并池，不指定
      strategyId: target === 'strategy' && !showAll && activeStrategy ? activeStrategy : null,
    }) === 0) {
      setBatchMsg('当前结果没有可带入回测的标的代码。')
      setTimeout(() => setBatchMsg(''), 3000)
      return
    }
    navigate('/backtest')
  }, [activeDisplayRows, handoffAsOf, showAll, activeStrategy, navigate])

  return (
    <div className="workspace-page">
      <PageHeader
        title="策略选股"
        subtitle="选择策略、运行扫描，再把命中结果送入自选或回测"
      />

      <div className="workspace-content space-y-3">
        <section className="panel" aria-labelledby="screener-command-title">
          <div className="flex min-w-0 flex-col gap-3 p-3 lg:flex-row lg:items-center lg:justify-between">
            <div className="min-w-0 space-y-2">
              <div>
                <h2 id="screener-command-title" className="text-sm font-semibold text-foreground">
                  扫描控制
                </h2>
                <p className="mt-0.5 text-[11px] text-muted">
                  数据基于本地 enriched 表，与监控和回测使用同一套策略口径
                </p>
              </div>
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                {asOf && (
                  <DatePicker
                    value={asOf}
                    onChange={handleDateChange}
                    min={minDate}
                    max={maxDate}
                  />
                )}
                <button
                  type="button"
                  onClick={() => reloadStrategies.mutate()}
                  disabled={reloadStrategies.isPending}
                  title="重新加载策略并运行全部策略，刷新当前符合条件的个股"
                  className="btn-primary min-h-9 disabled:cursor-wait disabled:opacity-50"
                >
                  <RefreshCw
                    className={`h-3.5 w-3.5 ${reloadStrategies.isPending ? 'animate-spin' : ''}`}
                    aria-hidden="true"
                  />
                  {reloadStrategies.isPending ? '运行中' : '运行全部策略'}
                </button>
                <button
                  type="button"
                  onClick={() => setShowAll(v => { if (!v) setActiveStrategy(null); return !v })}
                  aria-pressed={showAll}
                  className={`btn-secondary min-h-9 ${showAll ? '!border-accent/50 !bg-accent/10 !text-accent' : ''}`}
                >
                  <Network className="h-3.5 w-3.5" aria-hidden="true" />
                  全部结果
                </button>
              </div>
            </div>

            <div className="flex min-w-0 flex-col gap-2 lg:items-end">
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => setShowPoolDialog(true)}
                  className="btn-secondary min-h-9"
                >
                  <Layers className="h-3.5 w-3.5" aria-hidden="true" />
                  策略池
                  <span className="ml-0.5 flex h-4 min-w-[28px] items-center justify-center rounded-full bg-accent/15 text-[10px] font-bold text-accent num">
                    {visiblePool.length}/{strategies.data?.presets?.length ?? 0}
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => { setBuilderMode('create'); setShowBuilder(true) }}
                  className="btn-secondary min-h-9"
                >
                  <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
                  创建策略 · AI
                </button>
              </div>
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <span className="text-[10px] font-medium text-muted">策略卡片</span>
                <div className="flex min-h-9 items-center overflow-hidden rounded-btn border border-border" role="group" aria-label="策略卡片密度">
                  {(['hidden', 'mini', 'normal', 'large'] as const).map(sz => (
                    <button
                      key={sz}
                      type="button"
                      aria-pressed={cardSize === sz}
                      onClick={() => { setCardSize(sz); storage.screenerCardSize.set(sz) }}
                      className={`h-8 px-2.5 text-[10px] font-medium transition-colors ${
                        cardSize === sz
                          ? 'bg-accent/10 text-accent'
                          : 'text-muted hover:bg-elevated hover:text-secondary'
                      }`}
                    >
                      {sz === 'hidden' ? '隐藏' : sz === 'mini' ? '紧凑' : sz === 'normal' ? '标准' : '详细'}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </section>
        {cachedQuery.data?.discarded_as_of && (
          <div className="flex items-center gap-2 rounded-btn border border-warning/30 bg-warning/5 px-3 py-2 text-xs text-warning">
            <Clock className="h-3.5 w-3.5 shrink-0" />
            <span>
              已隔离未获数据源确认的 {cachedQuery.data.discarded_as_of} 选股缓存；
              当前 canonical 日期为 {cachedQuery.data.canonical_as_of ?? '未知'}，请按当前日期重新运行。
            </span>
          </div>
        )}
        {cardSize !== 'hidden' && (
          <section className="panel" aria-labelledby="strategy-pool-title">
            <div className="panel-header flex-col !items-start sm:flex-row sm:!items-center">
              <div className="min-w-0">
                <h2 id="strategy-pool-title" className="section-title">选择与运行策略</h2>
                <p className="mt-0.5 text-[11px] text-muted">
                  点击策略卡片运行单项扫描；设置与监控入口保留在卡片内
                </p>
              </div>
              <span className="shrink-0 text-[11px] text-muted num">{visiblePool.length} 个策略</span>
            </div>
            <div className="panel-body">
              {strategies.isLoading && (
                <div className="text-sm text-muted" role="status">正在加载策略…</div>
              )}
              {!strategies.isLoading && visiblePool.length === 0 && (
                <div className="rounded-btn border border-dashed border-border py-5 text-center">
                  <p className="text-sm text-secondary">策略池为空</p>
                  <p className="mt-1 text-[11px] text-muted">使用上方「策略池」添加已有策略，或创建新策略</p>
                </div>
              )}
              <div className={cardWrapCls(cardSize)}>
                {visiblePool.map(id => {
                  const s = strategyMap.get(id)
                  if (!s) return null
                  return (
                    <StrategyCard
                      key={s.id}
                      name={s.name}
                      description={s.description}
                      source={s.source}
                      active={activeStrategy === s.id}
                      count={hitCounts[id]}
                      expiredCount={expiredCounts[id]}
                      loading={runAll.isPending && hitCounts[id] == null}
                      cardSize={cardSize}
                      onRun={() => handleRun(s)}
                      disabled={run.isPending && activeStrategy === s.id}
                      onSettings={() => setSettingsStrategyId(s.id)}
                      monitored={strategyMonitorMap.has(s.id)}
                      onToggleMonitor={() => toggleStrategyMonitor(s.id, s.name)}
                    />
                  )
                })}
              </div>
            </div>
          </section>
        )}

        {runAll.isError && (
          <div className="flex items-center gap-2 rounded-btn border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger" role="alert">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span>策略批量运行失败：{runAll.error instanceof Error ? runAll.error.message : String(runAll.error)}</span>
          </div>
        )}
        {runAll.data?.failed?.length ? (
          <div className="flex items-start gap-2 rounded-btn border border-warning/30 bg-warning/5 px-3 py-2 text-xs text-warning" role="status">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span>
              {runAll.data.failed.length} 个策略运行失败：{runAll.data.failed.map(item => item.strategy_id).join('、')}
              ，其余策略结果正常显示。
            </span>
          </div>
        ) : null}

        <section className="panel" aria-labelledby="screener-results-title">
          <div className="flex min-w-0 flex-col gap-3 border-b border-border px-3 py-2.5 xl:flex-row xl:items-center xl:justify-between">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <TrendingUp className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" />
                <h2 id="screener-results-title" className="section-title">选股结果</h2>
                <span className="rounded-btn bg-accent/10 px-2 py-0.5 text-xs font-semibold text-accent num">
                  {(showAll ? allRows.length > 0 : !!result) ? displayRows.length : '—'}
                </span>
                {(showAll ? allRows.length > 0 : !!result) && filterActive(filter) && displayRows.length !== (showAll ? allRows.length : result!.total) && (
                  <span className="text-[11px] text-muted">
                    原始 {showAll ? allRows.length : result!.total} 只
                  </span>
                )}
                {runAll.isPending && (
                  <span className="text-[11px] text-muted animate-pulse" role="status">扫描中…</span>
                )}
              </div>
              <p className="mt-1 text-[11px] text-muted">
                {showAll
                  ? `汇总 ${visiblePool.length} 个策略`
                  : activeStrategy
                    ? `${strategyIdToName[activeStrategy] ?? activeStrategy} · 当前策略`
                    : '先选择策略运行，或切换到全部结果'}
                {!showAll && visiblePool.length > 0 && (
                  <> · 策略池累计命中 {visiblePool.reduce((sum, id) => sum + (hitCounts[id] ?? 0), 0)} 只</>
                )}
              </p>
            </div>

            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <div className="flex min-h-9 flex-wrap items-center gap-1 rounded-btn border border-border bg-elevated p-0.5" role="group" aria-label="结果列表工具">
                {(showAll ? allRows.length > 0 : !!result) && displayRows.length > 0 && (
                  <button
                    type="button"
                    onClick={() => setShowFilter(v => !v)}
                    aria-pressed={showFilter}
                    className={`btn-ghost !h-8 min-h-8 ${filterActive(filter) ? '!bg-accent/10 !text-accent' : ''}`}
                  >
                    <Filter className="h-3 w-3" aria-hidden="true" />
                    筛选
                    {filterActive(filter) && (
                      <span className="flex h-4 w-4 items-center justify-center rounded-full bg-accent text-[10px] font-bold text-white">
                        {countActiveFilters(filter)}
                      </span>
                    )}
                  </button>
                )}
                {filterActive(filter) && (
                  <button
                    type="button"
                    onClick={() => {
                      setFilter(defaultFilter)
                      if (activeStrategy) filterMap.current.delete(activeStrategy)
                    }}
                    className="btn-ghost !h-8 min-h-8 text-muted hover:text-danger"
                  >
                    重置
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => setCustomizerOpen(true)}
                  aria-label="配置结果列表列"
                  title="列表配置"
                  className={`btn-ghost !h-8 min-h-8 !px-2 ${customizerOpen ? '!bg-accent/10 !text-accent' : ''}`}
                >
                  <Settings2 className="h-3.5 w-3.5" aria-hidden="true" />
                </button>
              </div>

              {(showAll ? allRows.length > 0 : !!result) && displayRows.length > 0 && (
                <div className="flex min-h-9 flex-wrap items-center gap-1 rounded-btn border border-border bg-elevated p-0.5" role="group" aria-label="结果后续操作">
                  <button
                    type="button"
                    onClick={handleBatchAdd}
                    disabled={batchAdd.isPending}
                    className="btn-ghost !h-8 min-h-8 !text-accent disabled:opacity-50"
                  >
                    <Star className="h-3 w-3" aria-hidden="true" />
                    {batchAdd.isPending ? '添加中…' : '加自选'}
                  </button>
                  <button
                    type="button"
                    onClick={() => sendToBacktest('strategy')}
                    className="btn-ghost !h-8 min-h-8"
                    title="以当前选股结果作为股票池进入策略回测"
                  >
                    <FlaskConical className="h-3.5 w-3.5" aria-hidden="true" />
                    策略回测
                  </button>
                  <button
                    type="button"
                    onClick={() => sendToBacktest('factor')}
                    className="btn-ghost !h-8 min-h-8"
                    title="以当前选股结果作为股票池进入因子回测"
                  >
                    <BarChart3 className="h-3.5 w-3.5" aria-hidden="true" />
                    因子回测
                  </button>
                </div>
              )}
              {batchMsg && <span className="text-xs text-accent animate-pulse" role="status">{batchMsg}</span>}
              {!showAll && result && result.elapsed_ms > 0 && (
                <div className="flex items-center gap-1.5 text-xs text-muted">
                  <Clock className="h-3 w-3" aria-hidden="true" />
                  <span className="num">{result.elapsed_ms.toFixed(1)} ms</span>
                </div>
              )}
            </div>
          </div>
          <div className="panel-body space-y-3">
          {run.isError && (
            <div className="text-sm text-danger bg-danger/10 border border-danger/30 rounded-btn px-3 py-2">
              {String((run.error as Error).message)}
            </div>
          )}

          {(showAll ? allRows.length > 0 : !!result) && (
            <motion.div
              key={showAll ? `all-${asOf}` : `${result!.as_of}-${result!.strategy}`}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
              className="space-y-3"
            >
              {displayRows.length === 0 ? (
                <EmptyState
                  icon={ScanSearch}
                  title="今日无命中"
                  hint="可能数据未跑盘后管道,或策略条件过于严苛。试试 POST /api/pipeline/run。"
                />
              ) : (
                <>
                  {showFilter && (
                    <FilterPanel
                      value={filter}
                      onChange={setFilter}
                      onClose={() => setShowFilter(false)}
                      onReset={() => {
                        setFilter(defaultFilter)
                        if (activeStrategy) filterMap.current.delete(activeStrategy)
                      }}
                    />
                  )}

                  <div className="min-w-0 overflow-x-auto">
                    <ScreenerTable
                      rows={displayRows}
                      columns={columns}
                      strategyIdToName={strategyIdToName}
                      symbolStrategyMap={symbolStrategyMap}
                      activeStrategy={activeStrategy}
                      watchlistSet={watchlistSet}
                      onPreview={(symbol, name) => { setPreviewSymbol(symbol); setPreviewName(name) }}
                      onToggleWatchlist={(symbol, inList) => toggleWatchlist.mutate({ symbol, inList })}
                      watchlistPending={toggleWatchlist.isPending}
                      klineData={klineData}
                      dailyKChartVisible={dailyKChartVisible}
                      onToggleDailyKChart={toggleDailyKChart}
                      sort={sort}
                      onSortToggle={toggle}
                    />
                  </div>
                </>
              )}
            </motion.div>
          )}

          {!showAll && !result && !run.isPending && (
            <div className="flex flex-col items-center justify-center py-12 gap-3">
              <div className="w-12 h-12 rounded-btn bg-elevated border border-border flex items-center justify-center">
                <ScanSearch className="h-6 w-6 text-muted" />
              </div>
              <div className="flex flex-col items-center gap-1.5">
                <span className="text-sm text-secondary">可先在上方扫描控制里切换日期，再点击策略卡片查看选股结果</span>
                <span className="text-[11px] text-muted">若提示 enriched 表无数据，请先运行盘后管道</span>
              </div>
            </div>
          )}
          </div>
        </section>
      </div>

      <ListColumnCustomizer
        columns={columns}
        groups={SCREENER_COLUMN_GROUPS}
        onChange={handleColumnsChange}
        open={customizerOpen}
        onClose={() => setCustomizerOpen(false)}
        title="自定义策略结果列"
        builtinSectionLabel="策略内置列"
        extColumnAlign="center"
      />

      <StockPreviewDialog
        symbol={previewSymbol}
        name={previewName}
        onClose={closePreview}
      />

      <StrategySettingsDialog
        strategyId={settingsStrategyId}
        onClose={() => setSettingsStrategyId(null)}
        onSaved={(limit) => {
          if (settingsStrategyId) {
            setStrategyLimits(prev => ({ ...prev, [settingsStrategyId]: limit }))
            run.mutate({ id: settingsStrategyId, date: asOf })
          }
        }}
        onAiModify={async () => {
          if (!settingsStrategyId) return
          try {
            const [src, detail] = await Promise.all([
              api.strategyGetSource(settingsStrategyId),
              api.strategyGet(settingsStrategyId),
            ])
            storage.strategyModify.set({
              name: detail.name ?? '',
              description: detail.description ?? '',
              direction: 'long',
              rules: storage.strategyRules.get({})[settingsStrategyId] ?? '',
              code: src.code, step: 2, strategyId: settingsStrategyId,
            })
            setSettingsStrategyId(null)
            setBuilderMode('modify')
            setShowBuilder(true)
          } catch {}
        }}
        onDeleted={() => {
          if (settingsStrategyId) {
            removeFromPool(settingsStrategyId)
            const rules = storage.strategyRules.get({})
            delete rules[settingsStrategyId]; storage.strategyRules.set(rules)
            setStrategyLimits(prev => { const next = {...prev}; delete next[settingsStrategyId]; return next })
            qc.invalidateQueries({ queryKey: QK.screenerStrategies })
          }
        }}
      />

      {showPoolDialog && (
        <StrategyPoolDialog
          pool={pool}
          onConfirm={(newPool) => {
            reorderPool(newPool)
            if (asOf) {
              runAllDateRef.current = ''
              runAll.mutate({ date: asOf, strategyIds: newPool })
            }
          }}
          onClose={() => setShowPoolDialog(false)}
        />
      )}
      <StrategyBuilderDialog
        open={showBuilder}
        onClose={() => setShowBuilder(false)}
        mode={builderMode}
        onSavedId={async id => {
          const data = await qc.fetchQuery({ queryKey: QK.screenerStrategies, queryFn: api.screenerStrategies })
          if (!data.presets.some(s => s.id === id)) {
            throw new Error(`策略 ${id} 已保存但未加载，请检查策略代码`)
          }
          addToPool(id)
        }}
      />
    </div>
  )
}
