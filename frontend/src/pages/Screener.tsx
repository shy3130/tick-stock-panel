import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { ScanSearch, Clock, TrendingUp, Star, Filter, Layers, Network, Sparkles, RefreshCw, Settings2, Store, RotateCcw, X } from 'lucide-react'
import { api, genRuleId, type ScreenerStrategy, type ScreenerResult } from '@/lib/api'
import { DEFAULT_STRATEGY_NOTIFY_EVENTS } from '@/lib/strategyMonitorEvents'
import { toast } from '@/components/Toast'
import { useDataStatus, usePreferences, useCapabilities, useQuoteStatus } from '@/lib/useSharedQueries'
import { useWatchlistBatchAdd } from '@/lib/useSharedMutations'
import { isExpertOrAbove } from '@/lib/capability-labels'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
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
import { StrategyStoreDialog } from '@/components/screener/StrategyStoreDialog'
import { CompositeStrategyDialog } from '@/components/screener/CompositeStrategyDialog'
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
  const [assetType, setAssetType] = useState<'stock' | 'etf'>('stock')
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
  const [showStore, setShowStore] = useState(false)
  const [showComposite, setShowComposite] = useState(false)
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
  // 分时图显示开关（仅当 intraday 列可见时才有意义；持久化）
  const [intradayChartVisible, setIntradayChartVisible] = useState<boolean>(() => storage.screenerIntraday.get(true))
  const toggleIntradayChart = useCallback(() => {
    setIntradayChartVisible(v => {
      const next = !v
      storage.screenerIntraday.set(next)
      return next
    })
  }, [])
  // 截断提示可关闭 (仅本次会话, 不持久化)
  const [intradayCapDismissed, setIntradayCapDismissed] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const [showFilter, setShowFilter] = useState(false)
  const [filter, setFilter] = useState<ScreenerFilterType>(defaultFilter)
  const filterMap = useRef<Map<string, ScreenerFilterType>>(new Map())
  const runAllDateRef = useRef<string | null>(null)
  const qc = useQueryClient()

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
    queryKey: QK.screenerStrategies('all'),
    queryFn: () => api.screenerStrategies(),
  })

  // 卡片首屏只读取轻量摘要；明细在点击策略或“全部”时按需加载。
  const summaryQuery = useQuery({
    queryKey: QK.screenerCachedSummary,
    queryFn: api.screenerCachedSummary,
    enabled: assetType === 'stock',
  })

  const fullCachedQuery = useQuery({
    queryKey: QK.screenerCached(asOf, extColumnsParam),
    queryFn: () => api.screenerCached(extColumnsParam || undefined),
    enabled: assetType === 'stock' && showAll,
  })

  const singleCachedQuery = useQuery({
    queryKey: QK.screenerCachedResult(activeStrategy ?? '', asOf, extColumnsParam),
    queryFn: () => api.screenerCachedResult(activeStrategy!, extColumnsParam || undefined),
    enabled: assetType === 'stock'
      && !showAll
      && !!activeStrategy
      && summaryQuery.data?.results[activeStrategy]?.as_of === asOf,
  })

  const dataStatus = useDataStatus({ staleTime: 0 })

  // 默认日期 = enriched 最新日期（始终跟随最新）
  useEffect(() => {
    const latest = dataStatus.data?.enriched?.latest_date
    if (latest) setAsOf(latest)
  }, [dataStatus.data?.enriched?.latest_date])

  const strategyPresets = useMemo(
    () => (strategies.data?.presets ?? []).filter(s => s.asset_types.includes(assetType)),
    [strategies.data, assetType],
  )

  // 策略 ID → 名称映射
  const strategyIdToName = useMemo(() => {
    const map: Record<string, string> = {}
    for (const p of strategyPresets) {
      map[p.id] = p.name
    }
    return map
  }, [strategyPresets])

  // 策略 ID → 完整对象映射（避免每张卡片 find 遍历）
  const strategyMap = useMemo(() => {
    const map = new Map<string, ScreenerStrategy>()
    for (const p of strategyPresets) {
      map.set(p.id, p)
    }
    return map
  }, [strategyPresets])

  const allStrategyIds = useMemo(
    () => new Set((strategies.data?.presets ?? []).map(s => s.id)),
    [strategies.data],
  )
  const availableStrategyIds = useMemo(
    () => new Set(strategyPresets.map(s => s.id)),
    [strategyPresets],
  )
  const visiblePool = useMemo(() => pool.filter(id => availableStrategyIds.has(id)), [pool, availableStrategyIds])

  // 策略列表加载后,自动清除池中失效的自定义策略(如本地开发残留的、
  // 当前后端已不存在的策略 ID),避免"策略池"对话框持续显示失效项。
  // 关键: 仅当本次拉取成功且返回非空列表时才 prune。
  // 拉取中/失败/返回空(如引擎 reload 瞬时把某策略跳过)时一律不碰池,
  // 否则会把用户池里仍有效的 ID 永久清空并写入 localStorage,导致卡片全没。
  useEffect(() => {
    if (strategies.isError) return        // 拉取失败: 不 prune
    if (!strategies.isSuccess) return     // 加载中: 不 prune
    if (allStrategyIds.size === 0) return  // 空列表: 不 prune
    prune(allStrategyIds)
  }, [allStrategyIds, prune, strategies.isError, strategies.isSuccess])

  // 策略文件加载失败时提示用户(避免"策略静默消失"被误判为正常)
  const loadErrors = strategies.data?.load_errors ?? []
  useEffect(() => {
    for (const e of loadErrors) {
      toast(`策略「${e.file}」加载失败：${e.error}`, 'error')
    }
  }, [loadErrors])

  // 进入页面自动跑策略池中的策略，获取命中数
  const runAll = useMutation({
    mutationFn: ({ date, strategyIds }: { date?: string; strategyIds?: string[] } = {}) =>
      api.screenerRunAll(
        date,
        strategyIds ?? visiblePool,
        assetType,
      ),
    onSuccess: (data) => {
      if (data.as_of) setAsOf(data.as_of)
      const counts: Record<string, number> = {}
      for (const [id, item] of Object.entries(data.results)) {
        counts[id] = item.total
      }
      setHitCounts(prev => ({ ...prev, ...counts }))
      qc.invalidateQueries({ queryKey: ['screener-cached'] })
    },
  })

  const missingStrategyIds = useMemo(
    () => visiblePool.filter(id => summaryQuery.data?.results[id]?.as_of !== asOf),
    [visiblePool, summaryQuery.data, asOf],
  )
  const cacheCoversPool = visiblePool.length > 0 && missingStrategyIds.length === 0

  // 防止 reload / auto-run / StrictMode 叠出并发 run_all（后端 Numba 会崩溃）
  // 用 ref 同步门闩，避免同一渲染周期内 isPending 尚未更新导致重复触发
  const runAllPendingRef = useRef(false)
  const requestRunAll = useCallback((
    vars: { date?: string; strategyIds?: string[] } = {},
    options?: Parameters<typeof runAll.mutate>[1],
  ) => {
    if (runAllPendingRef.current || runAll.isPending) return
    runAllPendingRef.current = true
    runAll.mutate(vars, {
      ...options,
      onSettled: (...args) => {
        runAllPendingRef.current = false
        options?.onSettled?.(...args)
      },
    })
  }, [runAll])

  // 摘要只同步当前日期的卡片数量，避免旧日期缓存短暂显示成当前结果。
  useEffect(() => {
    if (!summaryQuery.data || !asOf) return
    const counts: Record<string, number> = {}
    const expired: Record<string, number> = {}
    for (const [id, r] of Object.entries(summaryQuery.data.results)) {
      if (r.as_of !== asOf) continue
      counts[id] = r.total
      const everCount = summaryQuery.data.today_ever_counts[id] ?? r.total
      const expiredCount = Math.max(everCount - r.total, 0)
      if (expiredCount > 0) expired[id] = expiredCount
    }
    setHitCounts(counts)
    setExpiredCounts(expired)
  }, [summaryQuery.data, asOf])

  // 当前单策略缓存更新后同步明细；参数保存的强制重算结果仍由 run 直接覆盖。
  useEffect(() => {
    const cached = singleCachedQuery.data?.result
    if (!cached || showAll || cached.strategy !== activeStrategy || cached.as_of !== asOf) return
    setResult(cached)
    if (activeStrategy) {
      setHitCounts(prev => ({ ...prev, [activeStrategy]: cached.total }))
    }
  }, [singleCachedQuery.data, showAll, activeStrategy, asOf])

  const effectiveResults = useMemo(() => {
    if (fullCachedQuery.data?.as_of !== asOf) return null
    const entries = Object.entries(fullCachedQuery.data.results)
      .filter(([, item]) => item.as_of === asOf)
    return Object.fromEntries(entries)
  }, [fullCachedQuery.data, asOf])

  // symbol → 所属策略列表。单策略接口同时返回轻量归属映射，保留策略列原有展示。
  const symbolStrategyMap = useMemo(() => {
    const map = new Map<string, string[]>()
    if (showAll) {
      for (const [sid, r] of Object.entries(effectiveResults ?? {})) {
        for (const row of r.rows) {
          const arr = map.get(row.symbol)
          if (arr) arr.push(sid)
          else map.set(row.symbol, [sid])
        }
      }
      return map
    }
    for (const [symbol, ids] of Object.entries(singleCachedQuery.data?.strategy_ids_by_symbol ?? {})) {
      map.set(symbol, ids)
    }
    if (activeStrategy && result) {
      for (const row of result.rows) {
        if (!map.has(row.symbol)) map.set(row.symbol, [activeStrategy])
      }
    }
    return map
  }, [showAll, effectiveResults, singleCachedQuery.data, activeStrategy, result])

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

  // 计算当前策略的失效行: 今日曾命中但当前已不命中。
  const expiredRows = useMemo(() => {
    const everRows = singleCachedQuery.data?.today_ever_rows
    if (!everRows || !result || result.as_of !== asOf) return []
    const currentSymbols = new Set(result.rows.map((row: any) => row.symbol))
    return Object.entries(everRows)
      .filter(([symbol]) => !currentSymbols.has(symbol))
      .map(([, row]) => ({ ...row, _expired: true }))
  }, [singleCachedQuery.data, result, asOf])

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
      if (expiredRows.length > 0) {
        return [...mainRows, ...expiredRows]
      }
    }
    return mainRows
  }, [showAll, allRows, filteredRows, filter, activeStrategy, strategyLimits, expiredRows, sort, sortRows, columns])

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
  const dailyKSymbols = useMemo(
    () => [...new Set(displayRows.map((r: any) => r.symbol as string))].sort(),
    [displayRows],
  )
  const resultSymbolsKey = dailyKSymbols.join(',')
  const klineBatch = useQuery({
    queryKey: QK.screenerKlineBatch(`${resultSymbolsKey}|${candleDays}`),
    queryFn: () => api.klineDailyBatch(dailyKSymbols, candleDays),
    enabled: dailyKVisible && dailyKSymbols.length > 0,
    staleTime: 5 * 60_000,
    placeholderData: previousData => previousData,
  })
  const klineData = dailyKVisible ? (klineBatch.data?.data ?? {}) : {}

  // 分时列是否启用 → 决定是否加载批量分时数据 (需 kline.minute.batch 能力)
  const intradayColumn = useMemo(() =>
    columns.find(c => c.source.type === 'builtin' && c.source.key === 'intraday' && c.visible),
    [columns],
  )
  // 分时图需 Pro+ (kline.minute.batch), 低档用户开了列也不拉数据
  const caps = useCapabilities()
  const hasMinuteBatch = !!caps.data?.capabilities?.['kline.minute.batch']
  const intradayVisible = !!intradayColumn && hasMinuteBatch && intradayChartVisible

  // 分时数据加载策略 (与自选页一致, 简洁优先):
  //  - 全量加载当前列表 symbol, 但按套餐 batch 上限截断 (Pro=100 / Expert=200),
  //    超出时只取前 batch 只并提示用户, 避免一次性发太多请求打爆 rpm 配额
  //  - 刷新: minute_intraday_refresh 偏好开启时按用户设定间隔轮询; 否则仅首次加载,
  //    用户可点表头刷新按钮手动更新
  const minuteBatchCap = caps.data?.capabilities?.['kline.minute.batch']?.batch ?? 100
  const quoteStatus = useQuoteStatus()
  const realtimeRunning = quoteStatus.data?.running ?? false
  const intradayRefreshEnabled = prefs?.minute_intraday_refresh ?? false
  const intradayRefreshInterval = prefs?.minute_intraday_refresh_interval ?? 6

  const allIntradaySymbols = useMemo(
    () => displayRows.map((r: any) => r.symbol),
    [displayRows],
  )
  const intradayTruncated = intradayVisible && allIntradaySymbols.length > minuteBatchCap
  // 是否已是最高档 (Expert+): 最高档时截断提示不再建议"升级套餐"
  const isMaxTier = isExpertOrAbove(caps.data?.label ?? '')
  // 截断到 batch 上限 (Pro=100 / Expert=200), 一次请求 = 一次 TickFlow 调用
  const intradaySymbols = useMemo(
    () => intradayTruncated ? allIntradaySymbols.slice(0, minuteBatchCap) : allIntradaySymbols,
    [allIntradaySymbols, intradayTruncated, minuteBatchCap],
  )
  const intradayRequestSymbols = useMemo(
    () => [...new Set(intradaySymbols)].sort(),
    [intradaySymbols],
  )
  const intradaySymbolsKey = intradayRequestSymbols.join(',')

  const minuteBatch = useQuery({
    queryKey: QK.minuteBatch(intradaySymbolsKey),
    queryFn: () => api.klineMinuteBatch(intradayRequestSymbols),
    enabled: intradayVisible && intradayRequestSymbols.length > 0,
    staleTime: 10_000,
    placeholderData: previousData => previousData,
    // 仅当开启分时刷新偏好 且 盘中实时行情运行时 才轮询 (省 rpm)
    refetchInterval: (intradayRefreshEnabled && realtimeRunning) ? intradayRefreshInterval * 1000 : false,
  })
  const minuteData = intradayVisible ? (minuteBatch.data?.data ?? {}) : {}

  // asOf 确定后 + 策略列表就绪 + 策略池非空 → 自动跑一次 (受系统设置开关控制)
  // 缓存命中时秒加载; 未命中时, 仅当 screener_auto_run 开启才自动触发 runAll
  useEffect(() => {
    // ETF 模式无股票盘后缓存/ runAll, 单策略走实时单跑, 不触发 runAll
    if (assetType !== 'stock') return
    if (!asOf || strategyPresets.length === 0 || !summaryQuery.isSuccess || runAll.isPending || visiblePool.length === 0) return
    const runKey = `${asOf}|${visiblePool.join(',')}`
    if (runAllDateRef.current === runKey) return
    // 缓存已覆盖当前策略池 → 秒加载, 不触发 runAll
    if (cacheCoversPool) {
      runAllDateRef.current = runKey
      return
    }
    // 未覆盖: 受系统开关控制
    if (!screenerAutoRun) return
    runAllDateRef.current = runKey
    requestRunAll({ date: asOf, strategyIds: missingStrategyIds })
  }, [asOf, strategyPresets.length, summaryQuery.isSuccess, visiblePool, cacheCoversPool, missingStrategyIds, screenerAutoRun, assetType, runAll.isPending, requestRunAll])

  const run = useMutation({
    mutationFn: ({ id, date }: { id: string; date: string }) =>
      api.screenerRunPreset(id, undefined, date || undefined, extColumnsParam || undefined, assetType),
    onSuccess: (data, vars) => {
      setResult(data)
      // 同步更新卡片上的命中数
      setHitCounts(prev => ({ ...prev, [vars.id]: data.total }))
      // 单策略重跑后刷新摘要和当前按需明细，避免参数保存后回退到旧缓存。
      qc.invalidateQueries({ queryKey: ['screener-cached'] })
    },
  })

  const handleRun = (s: ScreenerStrategy) => {
    handleStrategySwitch(s.id)
    setActiveStrategy(s.id)
    setShowAll(false)
    if (result?.strategy !== s.id || result.as_of !== asOf) setResult(null)
    // ETF 模式: 无股票盘后缓存, 始终实时单跑。
    // 传空日期让后端用 ETF 自己的最新交易日 (asOf 跟随的是股票 enriched, 两者可能不同日)。
    if (assetType !== 'stock') {
      run.mutate({ id: s.id, date: '' })
      return
    }
    // 摘要命中时由 singleCachedQuery 按需加载明细；缺失时才单独计算。
    if (summaryQuery.data?.results[s.id]?.as_of === asOf || runAll.isPending) return
    run.mutate({ id: s.id, date: asOf })
  }

  // 日期变化交给统一 effect 计算一次，避免这里与 effect 重复请求。
  const handleDateChange = (newDate: string) => {
    setAsOf(newDate)
    runAllDateRef.current = null
    setResult(null)
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
      qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })
    },
  })

  // 重新运行策略：重载策略文件 + 重跑全部策略，刷新符合条件的个股
  const reloadStrategies = useMutation({
    mutationFn: api.strategyReload,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['screener-strategies'] })
      if (asOf) requestRunAll({ date: asOf })
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
        notify_events: [...DEFAULT_STRATEGY_NOTIFY_EVENTS],
        conditions: [],
        logic: 'or',
        cooldown_seconds: 3600,
        severity: 'info',
        message: '',
      }).then(() => qc.invalidateQueries({ queryKey: QK.monitorRules }))
    }
  }

  const handleBatchAdd = () => {
    if (!displayRows.length) return
    const symbols = displayRows.map((r: any) => r.symbol)
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


  return (
    <>
      <PageHeader
        title="策略"
        subtitle="基于本地 enriched 表 · 毫秒级 SQL"
        right={
          <div className="flex items-center gap-2">
            {/* 资产类型切换: 股票 / ETF */}
            <div className="flex items-center h-7 rounded-btn border border-border overflow-hidden">
              {(['stock', 'etf'] as const).map(t => (
                <button
                  key={t}
                  onClick={() => { setAssetType(t); setActiveStrategy(null); setResult(null); setShowAll(false) }}
                  className={`h-full px-2.5 text-xs font-medium transition-colors cursor-pointer
                    ${assetType === t
                      ? 'bg-accent/10 text-accent'
                      : 'text-muted hover:text-secondary hover:bg-elevated'
                    }`}
                >
                  {t === 'stock' ? '股票' : 'ETF'}
                </button>
              ))}
            </div>
            {/* 重新运行策略：重载策略文件并重跑全部策略，更新命中个股 */}
            <button
              onClick={() => reloadStrategies.mutate()}
              disabled={reloadStrategies.isPending}
              title="重新加载策略并运行全部策略，刷新当前符合条件的个股"
              className="inline-flex items-center gap-1.5 h-7 px-2.5 rounded-btn
                border border-border bg-surface text-xs font-medium text-muted
                hover:text-accent hover:border-accent/50 transition-colors cursor-pointer
                disabled:opacity-50 disabled:cursor-wait"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${reloadStrategies.isPending ? 'animate-spin' : ''}`} />
              重载
            </button>
            {asOf && (
              <DatePicker
                value={asOf}
                onChange={handleDateChange}
                min={minDate}
                max={maxDate}
              />
            )}
            {/* 全部切换 */}
            <button
              onClick={() => setShowAll(v => { if (!v) setActiveStrategy(null); return !v })}
              title="显示全部策略个股"
              className={`inline-flex items-center justify-center h-7 w-7 rounded-btn border transition-colors cursor-pointer
                ${showAll
                  ? 'border-accent/50 bg-accent/10 text-accent'
                  : 'border-border bg-surface text-muted hover:text-secondary hover:border-accent/40'
                }`}
            >
              <Network className="h-3.5 w-3.5" />
            </button>
            {/* 卡片尺寸切换 */}
            <div className="flex items-center h-7 rounded-btn border border-border overflow-hidden">
              {(['hidden', 'mini', 'normal', 'large'] as const).map(sz => (
                <button
                  key={sz}
                  onClick={() => { setCardSize(sz); storage.screenerCardSize.set(sz) }}
                  className={`h-full px-2 text-[10px] font-medium transition-colors cursor-pointer
                    ${cardSize === sz
                      ? 'bg-accent/10 text-accent'
                      : 'text-muted hover:text-secondary hover:bg-elevated'
                    }`}
                >
                  {sz === 'hidden' ? '隐藏' : sz === 'mini' ? '紧凑' : sz === 'normal' ? '标准' : '详细'}
                </button>
              ))}
            </div>
            {/* 策略池按钮 */}
            <button
              onClick={() => setShowPoolDialog(true)}
              className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn
                border border-border bg-surface text-xs font-medium text-secondary
                hover:text-accent hover:border-accent/50 transition-colors cursor-pointer"
            >
              <Layers className="h-3.5 w-3.5" />
              策略池
              <span className="ml-0.5 min-w-[28px] h-4 flex items-center justify-center rounded-full bg-accent/15 text-accent text-[10px] font-bold">
                {visiblePool.length}/{strategyPresets.length}
              </span>
            </button>
            {/* 创建叠加策略 */}
            <button
              onClick={() => setShowComposite(true)}
              className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn
                text-xs font-medium text-teal-400 border border-teal-500/20 bg-teal-500/5
                hover:bg-teal-500/15 transition-colors cursor-pointer"
            >
              <Layers className="h-3.5 w-3.5" />
              叠加策略
            </button>
            {/* 创建策略 */}
            <button
              onClick={() => { setBuilderMode('create'); setShowBuilder(true) }}
              className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn
                text-xs font-medium text-amber-400 border border-amber-400/20 bg-amber-400/5
                hover:bg-amber-400/15 transition-colors cursor-pointer"
            >
              <Sparkles className="h-3.5 w-3.5" />
              创建策略 · AI
            </button>
            {/* 获取策略（占位，敬请期待） */}
            <button
              onClick={() => setShowStore(true)}
              className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn
                border border-border bg-surface text-xs font-medium text-secondary
                hover:text-accent hover:border-accent/50 transition-colors cursor-pointer"
            >
              <Store className="h-3.5 w-3.5" />
              获取策略
            </button>
          </div>
        }
      />

      <div className="px-8 py-4 space-y-3">
        {/* 策略卡片 */}
        {cardSize !== 'hidden' && (
        <section>
          {strategies.isLoading && <div className="text-sm text-muted">加载中…</div>}
          {!strategies.isLoading && visiblePool.length === 0 && (
            <div className="text-sm text-muted py-4 text-center border border-dashed border-border rounded-btn">
              策略池为空，点击右上角「策略池」按钮添加策略
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
                  loading={runAll.isPending}
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
        </section>
        )}

        {/* 结果 */}
        <section>
          {run.isError && (
            <div className="text-sm text-danger bg-danger/10 border border-danger/30 rounded-btn px-3 py-2">
              {String((run.error as any).message)}
            </div>
          )}

          {(showAll ? allRows.length > 0 : !!result) && (
            <motion.div
              key={showAll ? `all-${asOf}` : `${result!.as_of}-${result!.strategy}`}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
              className="space-y-3"
            >
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-medium text-foreground flex items-center gap-2">
                  {!showAll && activeStrategy && (
                    <span className="text-secondary">{strategyIdToName[activeStrategy] ?? ''}</span>
                  )}
                  <TrendingUp className="h-4 w-4 text-accent" />
                  {showAll ? '全部' : ''}命中 <span className="text-accent num">{displayRows.length}</span> 只
                  {filterActive(filter) && displayRows.length !== (showAll ? allRows.length : result!.total) && (
                    <span className="text-muted text-xs">/ {showAll ? allRows.length : result!.total}</span>
                  )}
                  <span className="text-[11px] text-muted font-normal">
                    · {visiblePool.length} 策略
                    {!showAll && visiblePool.length > 0 && (
                      <> · 共 {visiblePool.reduce((sum, id) => sum + (hitCounts[id] ?? 0), 0)} 只</>
                    )}
                  </span>
                  {runAll.isPending && (
                    <span className="text-[11px] text-muted animate-pulse">扫描中…</span>
                  )}
                </h2>
                <div className="flex items-center gap-3">
                  {(showAll ? allRows.length > 0 : !!result?.rows.length) && (
                    <div className="inline-flex items-stretch h-7 rounded-btn border border-border bg-surface overflow-hidden">
                      <button
                        onClick={() => setShowFilter(v => !v)}
                        className={`inline-flex items-center gap-1.5 px-2.5 text-xs font-medium transition-colors duration-150 cursor-pointer
                          ${filterActive(filter)
                            ? 'bg-accent/15 text-accent'
                            : showFilter
                              ? 'bg-accent/8 text-accent'
                              : 'text-secondary hover:bg-elevated hover:text-foreground'
                          }`}
                      >
                        <Filter className="h-3 w-3" />
                        筛选
                        {filterActive(filter) && (
                          <span className="bg-accent text-base rounded-full min-w-4 h-4 px-1 flex items-center justify-center text-[10px] font-bold leading-none">
                            {countActiveFilters(filter)}
                          </span>
                        )}
                      </button>
                      {filterActive(filter) && (
                        <>
                          <span className="w-px self-stretch my-1 bg-border" />
                          <button
                            onClick={() => {
                              setFilter(defaultFilter)
                              if (activeStrategy) filterMap.current.delete(activeStrategy)
                            }}
                            title="清空筛选条件"
                            className="inline-flex items-center gap-1 px-2 text-muted
                              hover:bg-danger/10 hover:text-danger transition-colors duration-150 cursor-pointer"
                          >
                            <RotateCcw className="h-3 w-3" />
                          </button>
                        </>
                      )}
                    </div>
                  )}
                  {displayRows.length > 0 && (
                    <button
                      onClick={handleBatchAdd}
                      disabled={batchAdd.isPending}
                      className="inline-flex items-center gap-1.5 h-7 px-2.5 rounded-btn
                        border border-accent/40 bg-accent/10 text-accent text-xs font-medium
                        hover:bg-accent/20 disabled:opacity-50 transition-colors duration-150 cursor-pointer"
                    >
                      <Star className="h-3 w-3" />
                      {batchAdd.isPending ? '添加中…' : '批量加自选'}
                    </button>
                  )}
                  <button
                    onClick={() => setCustomizerOpen(true)}
                    title="列表配置"
                    className={`inline-flex items-center justify-center h-7 w-7 rounded-btn border text-xs font-medium transition-colors cursor-pointer
                      ${customizerOpen
                        ? 'border-accent/50 bg-accent/10 text-accent'
                        : 'border-border bg-surface text-secondary hover:text-accent hover:border-accent/50'
                      }`}
                  >
                    <Settings2 className="h-3 w-3" />
                  </button>
                  {batchMsg && (
                    <span className="text-xs text-accent animate-pulse">{batchMsg}</span>
                  )}
                  {!showAll && result && result.elapsed_ms > 0 && (
                    <div className="flex items-center gap-2 text-xs text-muted">
                      <Clock className="h-3 w-3" />
                      <span className="num">{result.elapsed_ms.toFixed(1)} ms</span>
                    </div>
                  )}
                  {/* 分时截断提示: 超套餐上限时在工具栏内联显示, 可关闭 */}
                  {intradayTruncated && !intradayCapDismissed && (
                    <span className="inline-flex items-center gap-1 text-xs text-warning/90">
                      分时仅前 {minuteBatchCap}/{allIntradaySymbols.length}
                      {!isMaxTier && ', 可升级'}
                      <button
                        type="button"
                        onClick={() => setIntradayCapDismissed(true)}
                        className="text-warning/50 hover:text-warning transition-colors"
                        title="关闭提示"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </span>
                  )}
                </div>
              </div>

              {/* 筛选面板: 只要原始结果有数据就显示 (哪怕筛完后为空, 用户才能改条件) */}
              {showFilter && (showAll ? allRows.length > 0 : !!result?.rows.length) && (
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

              {displayRows.length === 0 ? (
                <EmptyState
                  icon={ScanSearch}
                  title={filterActive(filter) ? '筛选后无命中' : '今日无命中'}
                  hint={filterActive(filter)
                    ? '当前筛选条件过严, 试试放宽或重置筛选。'
                    : '可能数据未跑盘后管道,或策略条件过于严苛。试试 POST /api/pipeline/run。'}
                />
              ) : (
                <>
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
                    minuteData={minuteData}
                    intradayChartVisible={intradayChartVisible}
                    onToggleIntradayChart={toggleIntradayChart}
                    intradayAutoRefresh={intradayRefreshEnabled && realtimeRunning}
                    onRefreshIntraday={() => minuteBatch.refetch()}
                    intradayRefreshing={minuteBatch.isFetching}
                    sort={sort}
                    onSortToggle={toggle}
                  />
                </>
              )}
            </motion.div>
          )}

          {!showAll && !result && !run.isPending && (
            <div className="flex flex-col items-center justify-center py-16 gap-4">
              <div className="w-16 h-16 rounded-2xl bg-accent/5 border border-border flex items-center justify-center">
                <ScanSearch className="h-7 w-7 text-accent/40" />
              </div>
              <div className="flex flex-col items-center gap-1.5">
                <span className="text-sm text-secondary">点击策略卡片查看选股结果</span>
                <span className="text-[11px] text-muted">若提示 enriched 表无数据，请先运行盘后管道</span>
              </div>
            </div>
          )}
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
              code: src.code, step: 2, strategyId: settingsStrategyId, source: src.source as any,
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
            qc.invalidateQueries({ queryKey: ['screener-strategies'] })
          }
        }}
      />

      {showPoolDialog && (
        <StrategyPoolDialog
          pool={pool}
          onConfirm={(newPool) => {
            reorderPool(newPool)
          }}
          onClose={() => setShowPoolDialog(false)}
        />
      )}
      <StrategyBuilderDialog
        open={showBuilder}
        onClose={() => setShowBuilder(false)}
        mode={builderMode}
        existingStrategyIds={allStrategyIds}
        onSavedId={async id => {
          const data = await qc.fetchQuery({ queryKey: QK.screenerStrategies('all'), queryFn: () => api.screenerStrategies(), staleTime: 0 })
          if (!data.presets.some(s => s.id === id)) {
            throw new Error(`策略 ${id} 已保存但未加载，请检查策略代码`)
          }
          addToPool(id)
        }}
      />

      <CompositeStrategyDialog
        open={showComposite}
        onClose={() => setShowComposite(false)}
        onSavedId={async id => {
          await qc.fetchQuery({ queryKey: QK.screenerStrategies('all'), queryFn: () => api.screenerStrategies(), staleTime: 0 })
          addToPool(id)
        }}
      />

      <StrategyStoreDialog
        open={showStore}
        onClose={() => setShowStore(false)}
      />
    </>
  )
}
