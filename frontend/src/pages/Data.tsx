import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Database,
  Play,
  Loader2,
  HardDrive,
  Clock,
  Calendar,
  CheckSquare,
  Trash2,
  Plus,
  Wifi,
  SlidersHorizontal,
  AlertTriangle,
  Info,
  WandSparkles,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { EndpointTestDialog } from '@/components/EndpointTestDialog'
import { api, type ExtDataConfig } from '@/lib/api'
import {
  useCapabilities,
  useSettings,
  usePreferences,
  useQuoteStatus,
  useQuoteInterval,
  useDataStatus,
} from '@/lib/useSharedQueries'
import { useToggleRealtimeQuotes, useUpdateQuoteInterval } from '@/lib/useSharedMutations'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { formatScheduleDatePart, formatScheduleTimePart, isToday } from '@/lib/format'

// 拆分出的子组件
import { StatCard, type FieldTab } from '@/components/data/StatCard'
import { ActiveJobCard } from '@/components/data/ActiveJobCard'
import { SectionTitle, HistoryRow } from '@/components/data/SectionTitle'
import { SettingsModal } from '@/components/data/SettingsModal'
import { ScheduleEditor } from '@/components/data/ScheduleEditor'
import { ExtendHistoryPanel } from '@/components/data/ExtendHistoryPanel'
import { RepairDailyPanel } from '@/components/data/RepairDailyPanel'
import { EnrichedRebuildPanel } from '@/components/data/EnrichedRebuildPanel'
import { MinuteSyncConfig } from '@/components/data/MinuteSyncConfig'
import { PipelineScopeConfig } from '@/components/data/PipelineScopeConfig'
import { PageSettingsModal, getCardVisibility, getCardOrder, type CardKey } from '@/components/data/PageSettingsModal'
import { QuoteConfigCard } from '@/components/data/QuoteConfigCard'
import { EnrichedSchemaModal } from '@/components/data/SchemaModal'
import { Skeleton } from '@/components/data/Skeleton'
import { ExtDataStatCard } from '@/components/ext-data/ExtDataStatCard'
import { CreateExtDialog } from '@/components/ext-data/CreateExtDialog'
import { EditExtDialog } from '@/components/ext-data/EditExtDialog'

export function Data() {
  const qc = useQueryClient()
  const [activeJobId, setActiveJobId] = useState<string | null>(null)
  const startTime = useRef<number | null>(null)
  const topRef = useRef<HTMLDivElement>(null)

  const caps = useCapabilities()
  const settings = useSettings()

  const status = useDataStatus({
    refetchInterval: (query) => {
      const data = query.state.data
      // 指标异步预热中 或 有活跃 job → 加快轮询到 2s
      if (activeJobId || data?.indicators_ready === false) return 2_000
      return 30_000
    },
  })

  const history = useQuery({
    queryKey: QK.pipelineJobs,
    queryFn: () => api.pipelineJobs(15),
    refetchInterval: activeJobId ? false : 60_000,
  })

  const job = useQuery({
    queryKey: QK.pipelineJob(activeJobId ?? ''),
    queryFn: () => api.pipelineJob(activeJobId!),
    enabled: !!activeJobId,
    refetchInterval: (q: any) => {
      const j = q.state.data
      return j && (j.status === 'succeeded' || j.status === 'failed') ? false : 1_000
    },
  })

  const startSync = useMutation({
    mutationFn: api.pipelineRun,
    onSuccess: ({ job_id }) => {
      setActiveJobId(job_id)
      startTime.current = Date.now()
    },
  })

  const [showClearConfirm, setShowClearConfirm] = useState(false)
  const clearData = useMutation({
    mutationFn: api.dataClear,
    onSuccess: () => {
      // 清库删除全部 parquet + alerts, 各页 (看板/自选/选股/指数/财务/个股/连板/监控) 缓存
      // 数据均已失效, 故广域失效所有 query 令其重取 —— 数据已清空, 全量刷新是正确行为而非误伤。
      qc.invalidateQueries()
      setShowClearConfirm(false)
    },
  })

  const updateSchedule = useMutation({
    mutationFn: ({ hour, minute }: { hour: number; minute: number }) =>
      api.updatePipelineSchedule(hour, minute),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.preferences })
      qc.invalidateQueries({ queryKey: QK.dataStatus })
      setShowScheduleEdit(false)
    },
  })

  const updateInstSchedule = useMutation({
    mutationFn: ({ hour, minute }: { hour: number; minute: number }) =>
      api.updateInstrumentsSchedule(hour, minute),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.preferences })
      qc.invalidateQueries({ queryKey: QK.dataStatus })
      setShowInstScheduleEdit(false)
    },
  })

  const [openSettings, setOpenSettings] = useState<string | null>(null)
  const [showScheduleEdit, setShowScheduleEdit] = useState(false)
  const [showInstScheduleEdit, setShowInstScheduleEdit] = useState(false)
  const [indexExtendValue, setIndexExtendValue] = useState(6)
  const [indexExtendUnit, setIndexExtendUnit] = useState<'month' | 'year'>('month')
  const [schemaTable, setSchemaTable] = useState<string | null>(null)
  const [showEndpointTest, setShowEndpointTest] = useState(false)
  const [showCreateExt, setShowCreateExt] = useState(false)
  const [showRepair, setShowRepair] = useState(false)
  const [editingExt, setEditingExt] = useState<ExtDataConfig | null>(null)
  const [indexBatchInput, setIndexBatchInput] = useState('100')

  const extConfigs = useQuery({
    queryKey: QK.extData,
    queryFn: api.extDataList,
  })

  const deleteExt = useMutation({
    mutationFn: (id: string) => api.extDataDelete(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.extData }),
  })

  const syncIndexDaily = useMutation({
    mutationFn: () => api.syncIndexDaily(indexSyncDays),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.dataStatus })
      qc.invalidateQueries({ queryKey: QK.indexList })
      qc.invalidateQueries({ queryKey: QK.indexQuotes })
      qc.invalidateQueries({ queryKey: ['index-daily'] })
    },
  })

  const prefs = usePreferences()

  // 数据源列表 + 当前数据源 (顶部"切换数据源"按钮展示)
  const dataSources = useQuery({
    queryKey: QK.dataSources,
    queryFn: api.dataSources,
    staleTime: 60_000,
  })
  const activeProvider = prefs.data?.daily_data_provider || 'tickflow'
  const activeDataSourceName = activeProvider === 'tickflow'
    ? 'TickFlow'
    : (dataSources.data?.custom?.find(s => s.name === activeProvider)?.display_name || activeProvider)

  // tierKey → 自定义数据集名映射 (用于数据画像 CapBadge 显示数据源名而非 TickFlow 档位)
  const TIERKEY_TO_DATASET: Record<string, string> = {
    daily: 'daily',
    adj_factor: 'adj_factor',
    etf: 'daily',        // ETF 复用日K能力
    minute: 'minute',
    financials: 'financial',
  }
  // 当前 custom 源支持的数据集集合
  const activeCustomDatasets = activeProvider !== 'tickflow'
    ? new Set(dataSources.data?.custom?.find(s => s.name === activeProvider)?.datasets || [])
    : new Set<string>()
  // 给定 tierKey, 返回 custom provider 显示名 (走 custom 时) 或 null (走 TickFlow)
  const getCustomProviderName = (tierKey: string): string | null => {
    if (activeProvider === 'tickflow') return null
    const ds = TIERKEY_TO_DATASET[tierKey]
    if (ds && activeCustomDatasets.has(ds)) return activeDataSourceName
    return null
  }

  const minuteAuto = prefs.data?.minute_sync_enabled ?? false
  const pipelineSched = prefs.data?.pipeline_schedule ?? { hour: 15, minute: 30 }
  const instrumentsSched = prefs.data?.instruments_schedule ?? { hour: 9, minute: 10 }
  const indexDailyBatchSize = prefs.data?.index_daily_batch_size ?? 100

  useEffect(() => {
    setIndexBatchInput(String(indexDailyBatchSize))
  }, [indexDailyBatchSize])

  const updateIndexBatchSize = useMutation({
    mutationFn: (size: number) => api.updateIndexDailyBatchSize(size),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.preferences }),
  })

  const [showIntervalEdit, setShowIntervalEdit] = useState(false)
  const handleToggleIntervalEdit = useCallback((fromEvent?: boolean) => {
    setShowIntervalEdit(v => {
      const next = !v
      if (!fromEvent) {
        window.dispatchEvent(new CustomEvent('quote-interval-editor-toggle', { detail: { source: 'data' } }))
      }
      return next
    })
  }, [])
  useEffect(() => {
    const handler = (e: Event) => {
      const ce = e as CustomEvent
      if (ce.detail?.source !== 'data') {
        setShowIntervalEdit(v => !v)
      }
    }
    window.addEventListener('quote-interval-editor-toggle', handler)
    return () => window.removeEventListener('quote-interval-editor-toggle', handler)
  }, [])
  const quoteInterval = useQuoteInterval()
  const updateInterval = useUpdateQuoteInterval()

  const realtimeEnabled = prefs.data?.realtime_quotes_enabled ?? false
  const quoteStatus = useQuoteStatus()
  const toggleQuote = useToggleRealtimeQuotes()

  const hasAdjCap = !!caps.data?.capabilities?.['adj_factor']
  const hasDailyBatchCap = !!caps.data?.capabilities?.['kline.daily.batch']
  const hasMinuteCap = !!caps.data?.capabilities?.['kline.minute.batch']
  const indexAuto = prefs.data?.pipeline_pull_index ?? true
  const etfAuto = prefs.data?.pipeline_pull_etf ?? false
  const pipelineSteps = [
    '日K',
    ...(hasAdjCap ? ['复权'] : []),
    '指标',
    ...(indexAuto ? ['指数'] : []),
    ...(etfAuto ? ['ETF'] : []),
    ...((hasMinuteCap && minuteAuto) ? ['分钟K'] : []),
  ]

  // 数据画像卡片显隐(由页面设置弹窗控制,存 localStorage)
  const [cardVisibleTick, setCardVisibleTick] = useState(0)
  useEffect(() => {
    const handler = () => setCardVisibleTick(t => t + 1)
    window.addEventListener('data-card-visible-change', handler)
    return () => window.removeEventListener('data-card-visible-change', handler)
  }, [])
  const cardVisible = getCardVisibility(caps.data?.capabilities)
  // 引用 cardVisibleTick 触发重渲染(避免 lint 警告)
  void cardVisibleTick

  useEffect(() => {
    if (job.data && (job.data.status === 'succeeded' || job.data.status === 'failed')) {
      qc.invalidateQueries({ queryKey: QK.dataStatus })
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
      const t = setTimeout(() => setActiveJobId(null), 5_000)
      return () => clearTimeout(t)
    }
  }, [job.data?.status])

  useEffect(() => {
    if (job.isError && /404/.test(String((job.error as any)?.message ?? ''))) {
      setActiveJobId(null)
    }
  }, [job.isError, job.error])

  useEffect(() => {
    if (!activeJobId && history.data?.active_id) {
      setActiveJobId(history.data.active_id)
    }
  }, [history.data?.active_id])

  const s = status.data
  const isLoading = status.isLoading
  const isRunning = job.data?.status === 'running' || job.data?.status === 'pending'
  const isStarting = startSync.isPending
  const hasData = !!(s?.instruments?.rows || s?.daily?.rows)
  // none 档(无 key / 无效 key) → 禁用立即同步 (同步依赖付费档的批量端点)
  const isNoKey = settings.data?.mode === 'none'
  const indexOverviewStats = s ? {
    rows: 0,
    earliest_date: s.index_daily?.earliest_date ?? s.index_enriched?.earliest_date ?? null,
    latest_date: s.index_daily?.latest_date ?? s.index_enriched?.latest_date ?? null,
    symbols_covered: s.index_daily?.symbols_covered ?? s.index_instruments?.rows ?? 0,
    trading_days: s.index_daily?.trading_days ?? s.index_enriched?.trading_days ?? 0,
  } : null
  // ETF 统计(后端已按 asset_type='etf' 从 index 存储中拆分)
  const etfOverviewStats = s ? {
    rows: 0,
    earliest_date: s.etf_daily?.earliest_date ?? s.etf_enriched?.earliest_date ?? null,
    latest_date: s.etf_daily?.latest_date ?? s.etf_enriched?.latest_date ?? null,
    symbols_covered: s.etf_daily?.symbols_covered ?? s.etf_instruments?.rows ?? 0,
    trading_days: s.etf_daily?.trading_days ?? s.etf_enriched?.trading_days ?? 0,
  } : null
  const indexOverviewLabel = s ? '日 · 维表 · 日K · 指标' : undefined
  const indexEarliestDate = s?.index_daily?.earliest_date ?? s?.index_enriched?.earliest_date ?? null
  const indexOffsetDays = indexExtendUnit === 'month' ? indexExtendValue * 30 : indexExtendValue * 365
  const indexTargetDate = (() => {
    const d = indexEarliestDate ? new Date(indexEarliestDate) : new Date()
    d.setDate(d.getDate() - indexOffsetDays)
    return d
  })()
  const indexTargetDateText = indexTargetDate.toISOString().slice(0, 10)
  const indexSyncDays = Math.min(
    5000,
    Math.max(30, Math.ceil((Date.now() - indexTargetDate.getTime()) / 86_400_000) + 1),
  )

  const STAGE_CARD: Record<string, string> = {
    sync_instruments: 'instruments',
    sync_daily: 'daily',
    extend_history: 'daily',
    sync_adj: 'adj_factor',
    compute_enriched: 'enriched',
    rebuild_enriched: 'enriched',
    sync_index: 'index_daily',
    sync_minute: 'minute',
    extend_minute: 'minute',
  }
  const activeCard = isRunning && job.data ? STAGE_CARD[job.data.stage] ?? null : null

  const skippedCards = new Set(
    (job.data?.result?.skipped_stages ?? [])
      .map(s => STAGE_CARD[s])
      .filter(Boolean) as string[]
  )

  const prevStageRef = useRef<string | null>(null)
  const [doneStages, setDoneStages] = useState<Set<string>>(new Set())
  useEffect(() => {
    if (!job.data?.stage) return
    const stage = job.data.stage
    if (stage === prevStageRef.current) return
    const prev = prevStageRef.current
    if (prev && STAGE_CARD[prev]) {
      setDoneStages((s) => new Set(s).add(STAGE_CARD[prev]))
    }
    prevStageRef.current = stage
    qc.invalidateQueries({ queryKey: QK.dataStatus })
  }, [job.data?.stage])

  useEffect(() => {
    if (!activeJobId) {
      setDoneStages(new Set())
      prevStageRef.current = null
    }
  }, [activeJobId])

  const handleJobClick = useCallback((id: string) => {
    setActiveJobId(id)
    requestAnimationFrame(() => {
      topRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })
  }, [])

  // 按卡片 key 渲染对应的 StatCard (顺序由 getCardOrder 控制, 显隐由 cardVisible 控制)
  const renderStatCard = (k: CardKey): React.ReactNode => {
    switch (k) {
      case 'instruments':
        return (
          <StatCard
            title="个股维表"
            hint="盘前同步 · 元数据快照"
            stats={s?.instruments}
            isInstrument
            loading={isLoading}
            active={activeCard === 'instruments'}
            done={doneStages.has('instruments')}
            skipped={skippedCards.has('instruments')}
            stagePct={activeCard === 'instruments' ? (job.data?.stage_pct ?? 0) : 0}
            tierKey="instruments"
            capLimits={caps.data?.capabilities}
            tierLabel={caps.data?.label}
            auto
            onShowFields={() => setSchemaTable('instruments')}
          />
        )
      case 'daily':
        return (
          <StatCard
            title="日 K"
            hint="增量同步 · 全市场"
            stats={s?.daily}
            loading={isLoading}
            active={activeCard === 'daily'}
            done={doneStages.has('daily')}
            skipped={skippedCards.has('daily')}
            stagePct={activeCard === 'daily' ? (job.data?.stage_pct ?? 0) : 0}
            tierKey="daily"
            capLimits={caps.data?.capabilities}
            tierLabel={caps.data?.label}
            customProvider={getCustomProviderName('daily')}
            auto
            onShowFields={() => setSchemaTable('daily')}
            onSettings={hasData ? () => setOpenSettings(v => v === 'daily' ? null : 'daily') : undefined}
            settingsOpen={openSettings === 'daily'}
          />
        )
      case 'adj_factor':
        return (
          <StatCard
            title="除权因子"
            hint="增量同步 · 全市场"
            stats={s?.adj_factor}
            loading={isLoading}
            active={activeCard === 'adj_factor'}
            done={doneStages.has('adj_factor')}
            skipped={skippedCards.has('adj_factor')}
            stagePct={activeCard === 'adj_factor' ? (job.data?.stage_pct ?? 0) : 0}
            tierKey="adj_factor"
            capLimits={caps.data?.capabilities}
            tierLabel={caps.data?.label}
            customProvider={getCustomProviderName('adj_factor')}
            auto
            onShowFields={() => setSchemaTable('adj_factor')}
          />
        )
      case 'enriched':
        return (
          <StatCard
            title="Enriched"
            hint="复权 OHLCV + 技术指标"
            stats={s?.enriched}
            loading={isLoading}
            active={activeCard === 'enriched'}
            done={doneStages.has('enriched')}
            skipped={skippedCards.has('enriched')}
            stagePct={activeCard === 'enriched' ? (job.data?.stage_pct ?? 0) : 0}
            tierKey="enriched"
            capLimits={caps.data?.capabilities}
            tierLabel={caps.data?.label}
            auto
            subLabel={status.data?.indicators_ready === false ? '字段 · 指标计算中…' : '字段 · 指标 · 信号'}
            localBadgeSuffix={`${prefs.data?.enriched_batch_size ?? 1000}只/批`}
            onShowFields={() => setSchemaTable('enriched')}
            onSettings={hasData ? () => setOpenSettings(v => v === 'enriched' ? null : 'enriched') : undefined}
            settingsOpen={openSettings === 'enriched'}
          />
        )
      case 'index':
        return (
          <StatCard
            title="指数"
            hint="CN_Index · 独立存储"
            stats={indexOverviewStats}
            loading={isLoading}
            active={activeCard === 'index_daily'}
            done={doneStages.has('index_daily')}
            skipped={skippedCards.has('index_daily')}
            stagePct={activeCard === 'index_daily' ? (job.data?.stage_pct ?? 0) : 0}
            tierKey="daily"
            capLimits={caps.data?.capabilities}
            tierLabel={caps.data?.label}
            auto={indexAuto}
            subLabel={indexOverviewLabel}
            fieldTabs={[
              { label: '维表', table: 'index_instruments' },
              { label: '日K', table: 'index_daily' },
              { label: '指标', table: 'index_enriched' },
            ] as FieldTab[]}
            onShowFields={(t) => setSchemaTable(t ?? 'index_daily')}
            onSettings={hasData ? () => setOpenSettings(v => v === 'index' ? null : 'index') : undefined}
            settingsOpen={openSettings === 'index'}
          />
        )
      case 'etf':
        return (
          <StatCard
            title="ETF"
            hint="场内基金 · 独立存储"
            stats={etfOverviewStats}
            loading={isLoading}
            tierKey="etf"
            capLimits={caps.data?.capabilities}
            tierLabel={caps.data?.label}
            customProvider={getCustomProviderName('etf')}
            auto={etfAuto}
            subLabel="维表 · 日K · 指标"
            fieldTabs={[
              { label: '维表', table: 'etf_instruments' },
              { label: '日K', table: 'etf_daily' },
              { label: '指标', table: 'etf_enriched' },
            ] as FieldTab[]}
            onShowFields={(t) => setSchemaTable(t ?? 'etf_daily')}
          />
        )
      case 'minute':
        return (
          <StatCard
            title="分钟 K"
            hint="全市场同步"
            stats={s?.minute}
            loading={isLoading}
            active={activeCard === 'minute'}
            done={doneStages.has('minute')}
            skipped={skippedCards.has('minute')}
            stagePct={activeCard === 'minute' ? (job.data?.stage_pct ?? 0) : 0}
            tierKey="minute"
            capLimits={caps.data?.capabilities}
            tierLabel={caps.data?.label}
            customProvider={getCustomProviderName('minute')}
            auto={minuteAuto}
            onShowFields={() => setSchemaTable('minute')}
            onSettings={hasData ? () => setOpenSettings(v => v === 'minute' ? null : 'minute') : undefined}
            settingsOpen={openSettings === 'minute'}
          />
        )
      case 'financials': {
        const historicalShareRows = s?.financials?.tables?.shares?.rows ?? 0
        return (
          <StatCard
            title="财务数据"
            hint="财报 / 指标 / 历史股本"
            stats={s?.financials ? { rows: s.financials.rows } : null}
            loading={isLoading}
            tierKey="financials"
            capLimits={caps.data?.capabilities}
            tierLabel={caps.data?.label}
            customProvider={getCustomProviderName('financials')}
            subLabel={`历史股本 · ${historicalShareRows.toLocaleString()} 条`}
            onSettings={hasData ? () => setOpenSettings(v => v === 'financials' ? null : 'financials') : undefined}
            settingsOpen={openSettings === 'financials'}
          />
        )
      }
      default:
        return null
    }
  }

  return (
    <>
      <div ref={topRef} />
      <PageHeader
        title="数据"
        subtitle="本地数据画像 · 同步状态 · 历史记录"
        right={
          <div className="flex items-center gap-3">
            {!hasData && !isLoading && (
              <span className="text-xs text-accent animate-pulse">首次使用请点击右侧按钮同步数据</span>
            )}
            <button
              onClick={() => startSync.mutate()}
              disabled={isStarting}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn bg-gradient-to-r from-accent/25 to-accent/10 border border-accent/30 text-accent text-xs font-medium hover:from-accent/35 hover:to-accent/20 disabled:opacity-40 transition-all duration-150"
            >
              {(isRunning || isStarting) ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Play className="h-3.5 w-3.5" />
              )}
              {isStarting ? '启动中…' : isRunning ? '同步中…' : '立即同步'}
            </button>
            <button
              onClick={() => setOpenSettings('pipeline-scope')}
              className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-secondary hover:text-accent hover:bg-accent/8 text-xs transition-colors duration-150"
            >
              <CheckSquare className="h-3.5 w-3.5" />
              数据范围
            </button>
            <button
              onClick={() => setShowRepair(true)}
              disabled={!hasData || isRunning}
              className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-secondary hover:text-accent hover:bg-accent/8 text-xs transition-colors duration-150 disabled:opacity-40 disabled:pointer-events-none"
            >
              <WandSparkles className="h-3.5 w-3.5" />
              修正数据
            </button>
            <div className="w-px h-4 bg-border" />
            <div className="flex items-center gap-1.5">
              <button
                onClick={() => setShowCreateExt(true)}
                className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-secondary hover:text-accent hover:bg-accent/8 text-xs transition-colors duration-150"
              >
                <Plus className="h-3.5 w-3.5" />
                扩展数据
              </button>
              <button
                onClick={() => setShowEndpointTest(true)}
                className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-secondary hover:text-accent hover:bg-accent/8 text-xs transition-colors duration-150"
              >
                <Wifi className="h-3.5 w-3.5" />
                测试端点
              </button>
              <button
                onClick={() => setOpenSettings('page-settings')}
                className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-secondary hover:text-accent hover:bg-accent/8 text-xs transition-colors duration-150"
              >
                <SlidersHorizontal className="h-3.5 w-3.5" />
                页面设置
              </button>
              <div className="w-px h-4 bg-border" />
              <Link
                to="/settings?tab=data-sources"
                className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-secondary hover:text-accent hover:bg-accent/8 text-xs transition-colors duration-150"
                title="切换数据源"
              >
                <Database className="h-3.5 w-3.5" />
                <span className="text-foreground/80 max-w-[120px] truncate">{activeDataSourceName}</span>
              </Link>
              <button
                onClick={() => setShowClearConfirm(true)}
                disabled={isRunning}
                className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-muted hover:text-danger hover:bg-danger/8 text-xs transition-colors duration-150 disabled:opacity-40 disabled:pointer-events-none"
              >
                <Trash2 className="h-3.5 w-3.5" />
                清除数据
              </button>
            </div>
          </div>
        }
      />

      <div className="px-8 py-6 space-y-6 max-w-6xl">
        {/* None 档提示 —— 非阻断: 无需 Key 也可获取历史日K, 仅实时行情等扩展能力受限 */}
        {isNoKey && (
          <div className="flex items-center gap-2 rounded-card border border-border bg-elevated/40 px-3 py-2 text-xs">
            <Info className="h-4 w-4 shrink-0 text-muted" />
            <span className="text-secondary leading-relaxed">
              当前为 None 档,将使用免费数据源获取历史日K(无需注册)。
              配置 API Key 可解锁实时行情监控等扩展能力,前往
              <Link to="/settings?tab=account" className="mx-0.5 font-medium text-accent hover:underline">
                配置
              </Link>
              。
            </span>
          </div>
        )}

        {/* 实时进度 */}
        <AnimatePresence>
          {job.data && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
              className="overflow-hidden"
            >
              <ActiveJobCard job={job.data} />
            </motion.div>
          )}
        </AnimatePresence>

        {/* 实时行情 + 存储 + 调度 */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <QuoteConfigCard
            enabled={realtimeEnabled}
            running={quoteStatus.data?.running ?? false}
            isTrading={quoteStatus.data?.is_trading_hours ?? false}
            lastFetchMs={quoteStatus.data?.last_fetch_ms ?? null}
            intervalS={quoteInterval.data?.interval ?? quoteStatus.data?.interval_s ?? 6}
            intervalMin={quoteInterval.data?.min_interval ?? 6}
            intervalMax={quoteInterval.data?.max_interval ?? 60}
            loading={quoteStatus.isLoading}
            onToggle={(v) => toggleQuote.mutate(v)}
            toggling={toggleQuote.isPending}
            showIntervalEdit={showIntervalEdit}
            onShowIntervalEdit={handleToggleIntervalEdit}
            onIntervalChange={(v) => updateInterval.mutate(v)}
          />

          {/* 自动调度 */}
          <div className="rounded-card border border-border bg-surface p-4">
            <div className="flex items-center gap-2 mb-3">
              <Calendar className="h-4 w-4 text-secondary" />
              <h3 className="text-sm font-medium text-foreground">自动调度</h3>
            </div>
            {isLoading ? (
              <div className="space-y-2">
                <div className="flex items-center justify-between"><Skeleton w="w-16" /><Skeleton w="w-28" /></div>
                <div className="flex items-center justify-between"><Skeleton w="w-16" /><Skeleton w="w-28" /></div>
                <div className="flex items-center justify-between"><Skeleton w="w-6" /><Skeleton w="w-20" /></div>
              </div>
            ) : (
              <div className="space-y-2">
                <div className="flex items-center gap-1.5 text-[10px] text-muted pb-2 border-b border-border/50">
                  <span className="text-accent/60 font-medium">盘前</span>
                  <span>个股维表</span>
                  <span className="text-border">→</span>
                  <span className="text-accent/60 font-medium">盘后</span>
                  {pipelineSteps.map((step, i) => (
                    <span key={step} className="contents">
                      {i > 0 && <span className="text-border">→</span>}
                      <span>{step}</span>
                    </span>
                  ))}
                </div>
                <div className="flex items-center justify-between text-[11px]">
                  <span className="text-muted">时区</span>
                  <span className="font-mono text-secondary">Asia/Shanghai</span>
                </div>
                <div className="flex items-center justify-between text-[11px]">
                  <div className="flex items-center gap-1">
                    <span className="text-muted">盘前 · 个股维表</span>
                    <span className="text-muted/50">·</span>
                    <span className="font-mono text-secondary">
                      {`${String(instrumentsSched.hour).padStart(2, '0')}:${String(instrumentsSched.minute).padStart(2, '0')}`}
                    </span>
                    <button
                      onClick={() => setShowInstScheduleEdit(v => !v)}
                      className={`p-0.5 rounded hover:bg-elevated transition-colors ${showInstScheduleEdit ? 'text-accent' : 'text-secondary'}`}
                    >
                      <Clock className="h-3 w-3" />
                    </button>
                  </div>
                  <div className="flex items-center gap-2 font-mono text-secondary">
                    {s?.last_instruments_run && (
                      <span className={`inline-flex flex-col items-center leading-tight ${isToday(s.last_instruments_run) ? 'text-bear' : 'text-secondary/70'}`}>
                        <span>✓ {formatScheduleDatePart(s.last_instruments_run)}</span>
                        <span>{formatScheduleTimePart(s.last_instruments_run)}</span>
                      </span>
                    )}
                    {s?.next_instruments_run && (
                      <span className="inline-flex flex-col items-center leading-tight text-foreground">
                        <span>→ {formatScheduleDatePart(s.next_instruments_run)}</span>
                        <span>{formatScheduleTimePart(s.next_instruments_run)}</span>
                      </span>
                    )}
                  </div>
                </div>
                <AnimatePresence>
                  {showInstScheduleEdit && (
                    <motion.div
                      initial={{ opacity: 0, height: 0 }}
                      animate={{ opacity: 1, height: 'auto' }}
                      exit={{ opacity: 0, height: 0 }}
                      transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
                      className="overflow-hidden"
                    >
                      <ScheduleEditor
                        value={instrumentsSched}
                        onSave={(h, m) => updateInstSchedule.mutate({ hour: h, minute: m })}
                        loading={updateInstSchedule.isPending}
                        hint="不晚于 09:15"
                      />
                    </motion.div>
                  )}
                </AnimatePresence>
                <div className="flex items-center justify-between text-[11px]">
                  <div className="flex items-center gap-1">
                    <span className="text-muted">盘后 · 全量管道</span>
                    <span className="text-muted/50">·</span>
                    <span className="font-mono text-secondary">
                      {`${String(pipelineSched.hour).padStart(2, '0')}:${String(pipelineSched.minute).padStart(2, '0')}`}
                    </span>
                    <button
                      onClick={() => setShowScheduleEdit(v => !v)}
                      className={`p-0.5 rounded hover:bg-elevated transition-colors ${showScheduleEdit ? 'text-accent' : 'text-secondary'}`}
                    >
                      <Clock className="h-3 w-3" />
                    </button>
                  </div>
                  <div className="flex items-center gap-2 font-mono text-secondary">
                    {s?.last_pipeline_run && (
                      <span className={`inline-flex flex-col items-center leading-tight ${isToday(s.last_pipeline_run) ? 'text-bear' : 'text-secondary/70'}`}>
                        <span>✓ {formatScheduleDatePart(s.last_pipeline_run)}</span>
                        <span>{formatScheduleTimePart(s.last_pipeline_run)}</span>
                      </span>
                    )}
                    {s?.next_pipeline_run && (
                      <span className="inline-flex flex-col items-center leading-tight text-foreground">
                        <span>→ {formatScheduleDatePart(s.next_pipeline_run)}</span>
                        <span>{formatScheduleTimePart(s.next_pipeline_run)}</span>
                      </span>
                    )}
                  </div>
                </div>
                <AnimatePresence>
                  {showScheduleEdit && (
                    <motion.div
                      initial={{ opacity: 0, height: 0 }}
                      animate={{ opacity: 1, height: 'auto' }}
                      exit={{ opacity: 0, height: 0 }}
                      transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
                      className="overflow-hidden"
                    >
                      <ScheduleEditor
                        value={pipelineSched}
                        onSave={(h, m) => updateSchedule.mutate({ hour: h, minute: m })}
                        loading={updateSchedule.isPending}
                        hint="不早于 15:00"
                      />
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>
            )}
          </div>

          {/* 存储 */}
          <div className="rounded-card border border-border bg-surface p-4">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <HardDrive className="h-4 w-4 text-secondary" />
                <h3 className="text-sm font-medium text-foreground">存储</h3>
              </div>
              {isLoading ? (
                <Skeleton w="w-12" />
              ) : (
                <span className="font-mono text-xs text-muted">{s ? `${s.storage.total_size_mb} MB` : '—'}</span>
              )}
            </div>
            <div className="space-y-2">
              {isLoading ? (
                Array.from({ length: 5 }).map((_, i) => (
                  <div key={i} className="flex items-center justify-between">
                    <Skeleton w="w-10" />
                    <div className="flex items-center gap-3">
                      <Skeleton w="w-14" />
                      <Skeleton w="w-16" />
                    </div>
                  </div>
                ))
              ) : [
                { label: '个股维表', files: s?.storage.instruments_files, size: s?.storage.instruments_size_mb },
                { label: '日 K',     files: s?.storage.daily_files,       size: s?.storage.daily_size_mb },
                { label: '除权因子', files: s?.storage.adj_factor_files,  size: s?.storage.adj_factor_size_mb },
                { label: 'Enriched', files: s?.storage.enriched_files,    size: s?.storage.enriched_size_mb },
                { label: '分钟 K',   files: s?.storage.minute_files,      size: s?.storage.minute_size_mb },
                { label: '财务数据', files: s?.storage.financials_files,   size: s?.storage.financials_size_mb },
              ].map((item) => (
                <div key={item.label} className="flex items-center justify-between text-[11px]">
                  <span className="text-muted">{item.label}</span>
                  <div className="flex items-center gap-3">
                    <span className="font-mono text-secondary">{item.files ?? 0} 文件</span>
                    <span className="font-mono text-muted w-16 text-right">{(item.size ?? 0).toFixed(1)} MB</span>
                  </div>
                </div>
              ))}
              {/* 扩展数据 */}
              {(extConfigs.data && (extConfigs.data.items?.length ?? 0) > 0) && (
                <div className="flex items-center justify-between text-[11px] border-t border-border/50 pt-2 mt-1">
                  <span className="text-muted">扩展数据</span>
                  <div className="flex items-center gap-3">
                    <span className="font-mono text-secondary">{s?.storage.ext_data_files ?? extConfigs.data.items.length} 文件</span>
                    <span className="font-mono text-muted w-16 text-right">
                      {s?.storage.ext_data_size_mb != null ? `${s.storage.ext_data_size_mb.toFixed(1)} MB` : '—'}
                    </span>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* 数据画像 */}
        <div>
          <SectionTitle icon={Database}>数据画像</SectionTitle>
          <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4 items-stretch">
            {getCardOrder().filter(k => cardVisible[k]).map((k: CardKey) => (
              <Fragment key={k}>{renderStatCard(k)}</Fragment>
            ))}
            {(extConfigs.data?.items ?? []).map((ext) => (
              <ExtDataStatCard
                key={ext.id}
                config={ext}
                onDelete={() => deleteExt.mutate(ext.id)}
                deleting={deleteExt.isPending}
                onEdit={() => setEditingExt(ext)}
              />
            ))}
          </div>
        </div>

        {/* 同步历史 */}
        <div>
          <SectionTitle icon={Clock}>同步历史</SectionTitle>
          <div className="mt-3 rounded-card border border-border overflow-hidden">
            {history.isLoading ? (
              <div className="px-5 py-6 space-y-3">
                {Array.from({ length: 3 }).map((_, i) => (
                  <div key={i} className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <Skeleton w="w-4" h="h-4" rounded="rounded-full" />
                      <div className="space-y-1.5">
                        <Skeleton w="w-20" />
                        <Skeleton w="w-28" h="h-3" />
                      </div>
                    </div>
                    <Skeleton w="w-32" />
                  </div>
                ))}
              </div>
            ) : history.data && history.data.jobs.length > 0 ? (
              <div className="divide-y divide-border">
                {history.data.jobs.map((j) => (
                  <HistoryRow key={j.id} job={j} onClick={() => handleJobClick(j.id)} />
                ))}
              </div>
            ) : (
              <div className="px-5 py-8 text-center text-sm text-muted">
                暂无同步记录 — 点右上角"立即同步"开始。
              </div>
            )}
          </div>
        </div>

        {startSync.isError && (
          <div className="rounded-btn border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger">
            启动失败:{String((startSync.error as any).message)}
          </div>
        )}
      </div>

      {/* 弹窗 */}
      <EnrichedSchemaModal
        table={schemaTable}
        onClose={() => setSchemaTable(null)}
      />

      {showEndpointTest && (
        <EndpointTestDialog
          hasKey={settings.data?.mode === 'api_key'}
          tierLabel={settings.data?.tier_label ?? ''}
          currentEndpoint={settings.data?.current_endpoint ?? ''}
          onClose={() => setShowEndpointTest(false)}
        />
      )}

      <AnimatePresence>
        {showCreateExt && (
          <CreateExtDialog onClose={() => setShowCreateExt(false)} />
        )}
        {editingExt && (
          <EditExtDialog config={editingExt} onClose={() => setEditingExt(null)} />
        )}
      </AnimatePresence>

      <AnimatePresence>
        {openSettings === 'daily' && (
          <SettingsModal title="日 K · 向前扩展历史" onClose={() => setOpenSettings(null)}>
            <ExtendHistoryPanel
              caps={caps.data}
              isRunning={!!activeJobId}
              earliestDate={s?.daily?.earliest_date ?? null}
              onStart={() => setOpenSettings(null)}
            />
          </SettingsModal>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {showRepair && (
          <SettingsModal title="日 K · 修正 / 补数据" onClose={() => setShowRepair(false)}>
            <RepairDailyPanel
              caps={caps.data}
              isRunning={!!activeJobId}
              latestDate={s?.daily?.latest_date ?? null}
              onStart={() => setShowRepair(false)}
            />
          </SettingsModal>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {openSettings === 'enriched' && (
          <SettingsModal title="Enriched · 计算设置" onClose={() => setOpenSettings(null)}>
            <EnrichedRebuildPanel
              isRunning={!!activeJobId}
              onStart={(jobId) => { setActiveJobId(jobId); setOpenSettings(null) }}
            />
          </SettingsModal>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {openSettings === 'financials' && (
          <SettingsModal title="财务数据 · 换手率重算" onClose={() => setOpenSettings(null)}>
            <EnrichedRebuildPanel
              isRunning={!!activeJobId}
              purpose="turnover"
              historicalShareRows={s?.financials?.tables?.shares?.rows ?? 0}
              onStart={(jobId) => { setActiveJobId(jobId); setOpenSettings(null) }}
            />
          </SettingsModal>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {openSettings === 'pipeline-scope' && (
          <SettingsModal title="盘后管道 · 拉取内容" onClose={() => setOpenSettings(null)}>
            <PipelineScopeConfig />
          </SettingsModal>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {openSettings === 'page-settings' && (
          <SettingsModal title="页面设置 · 数据画像卡片" onClose={() => setOpenSettings(null)}>
            <PageSettingsModal caps={caps.data?.capabilities} />
          </SettingsModal>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {openSettings === 'index' && (
          <SettingsModal title="指数 · 手动获取" onClose={() => setOpenSettings(null)}>
            <div className="space-y-4">
              <div className="rounded-card border border-border bg-base/30 p-4 space-y-3">
                <div>
                  <div className="text-sm font-medium text-foreground">指数日 K</div>
                  <div className="text-[11px] text-muted mt-1">获取数据时会先刷新 CN_Index 维表，再向前扩展指数历史；指数不需要复权。</div>
                </div>
                <div className="flex items-center gap-2">
                  <div className="flex items-center">
                    <button
                      onClick={() => setIndexExtendValue(v => Math.max(1, v - 1))}
                      disabled={!hasDailyBatchCap || !!activeJobId || syncIndexDaily.isPending}
                      className="h-6 w-6 flex items-center justify-center rounded-l-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
                    >−</button>
                    <div className="h-6 w-8 flex items-center justify-center border-y border-border text-[11px] font-mono tabular-nums text-foreground bg-base">
                      {indexExtendValue}
                    </div>
                    <button
                      onClick={() => setIndexExtendValue(v => Math.min(indexExtendUnit === 'year' ? 10 : 36, v + 1))}
                      disabled={!hasDailyBatchCap || !!activeJobId || syncIndexDaily.isPending}
                      className="h-6 w-6 flex items-center justify-center rounded-r-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
                    >+</button>
                  </div>

                  <div className="flex rounded-btn border border-border overflow-hidden">
                    {(['month', 'year'] as const).map(u => (
                      <button
                        key={u}
                        onClick={() => { setIndexExtendUnit(u); if (u === 'year' && indexExtendValue > 10) setIndexExtendValue(1); if (u === 'month' && indexExtendValue > 36) setIndexExtendValue(6) }}
                        disabled={!hasDailyBatchCap || !!activeJobId || syncIndexDaily.isPending}
                        className={`px-2 py-0.5 text-[10px] font-medium transition-colors disabled:opacity-40 ${
                          indexExtendUnit === u ? 'bg-accent/15 text-accent' : 'text-secondary hover:bg-elevated'
                        }`}
                      >{u === 'month' ? '月' : '年'}</button>
                    ))}
                  </div>
                </div>

                <div className="text-[10px] text-muted">
                  预计扩展至 <span className="font-mono text-secondary">{indexTargetDateText}</span>
                  {indexEarliestDate && <span> (当前最早: <span className="font-mono text-secondary">{indexEarliestDate}</span>)</span>}
                </div>

                <div className="rounded-btn border border-border bg-base/40 p-3 space-y-2">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-xs font-medium text-foreground">批次大小</div>
                      <div className="text-[10px] text-muted mt-0.5">每批同步并计算的指数数量，默认 100。</div>
                    </div>
                    <div className="flex items-center gap-2">
                      <input
                        type="number"
                        min={1}
                        max={10000}
                        value={indexBatchInput}
                        onChange={e => setIndexBatchInput(e.target.value)}
                        disabled={updateIndexBatchSize.isPending || !!activeJobId || syncIndexDaily.isPending}
                        className="w-20 px-2 py-1 rounded-btn bg-elevated border border-border text-xs font-mono text-foreground outline-none focus:border-accent disabled:opacity-40"
                      />
                      <button
                        onClick={() => {
                          const size = Math.max(1, Math.min(10000, Number(indexBatchInput) || 100))
                          setIndexBatchInput(String(size))
                          updateIndexBatchSize.mutate(size)
                        }}
                        disabled={updateIndexBatchSize.isPending || !!activeJobId || syncIndexDaily.isPending}
                        className="px-2.5 py-1 rounded-btn bg-elevated border border-border text-xs text-secondary hover:text-foreground disabled:opacity-40 transition-colors"
                      >
                        {updateIndexBatchSize.isPending ? '保存中…' : '保存'}
                      </button>
                    </div>
                  </div>
                  <div className="text-[10px] text-muted">
                    当前生效: <span className="font-mono text-secondary">{indexDailyBatchSize}</span>
                  </div>
                </div>
                <button
                  onClick={() => syncIndexDaily.mutate()}
                  disabled={!hasDailyBatchCap || !!activeJobId || syncIndexDaily.isPending}
                  className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent disabled:opacity-40 disabled:pointer-events-none transition-colors duration-150"
                >
                  {syncIndexDaily.isPending ? (
                    <>
                      <Loader2 className="h-3 w-3 animate-spin" />
                      获取中…
                    </>
                  ) : (
                    <>获取数据</>
                  )}
                </button>
                {!hasDailyBatchCap && (
                  <span className="text-[10px] text-warning/80 bg-warning/8 rounded px-1.5 py-px font-medium">
                    需 Starter+ / Pro 批量日 K 权限
                  </span>
                )}
              </div>
            </div>
          </SettingsModal>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {openSettings === 'minute' && (
          <SettingsModal title="分钟 K · 同步设置" onClose={() => setOpenSettings(null)}>
            <MinuteSyncConfig caps={caps.data} onJobStart={(jobId) => { setActiveJobId(jobId); setOpenSettings(null) }} />
          </SettingsModal>
        )}
      </AnimatePresence>

      {/* 清除数据二次确认弹窗 */}
      <AnimatePresence>
        {showClearConfirm && (
          <div className="fixed inset-0 z-50 flex items-center justify-center">
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="absolute inset-0 bg-black/60 backdrop-blur-sm"
              onClick={() => !clearData.isPending && setShowClearConfirm(false)}
            />
            <motion.div
              initial={{ opacity: 0, scale: 0.95, y: 12 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.97, y: 8 }}
              transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
              className="relative w-[90vw] max-w-[420px] rounded-card border border-border bg-base shadow-2xl p-6"
            >
              <div className="flex items-start gap-3">
                <div className="shrink-0 h-10 w-10 rounded-full bg-danger/12 flex items-center justify-center">
                  <AlertTriangle className="h-5 w-5 text-danger" />
                </div>
                <div className="min-w-0">
                  <h3 className="text-sm font-semibold text-foreground mb-1.5">确认清除本地数据？</h3>
                  <p className="text-xs text-secondary leading-relaxed">
                    此操作将<span className="text-danger font-medium">永久删除</span>所有已同步的本地数据，包括：
                  </p>
                  <ul className="mt-2 text-[11px] text-muted leading-relaxed space-y-0.5">
                    <li>· 个股维表、日 K、除权因子</li>
                    <li>· Enriched 指标数据、分钟 K</li>
                    <li>· 财务数据、指数、ETF</li>
                  </ul>
                  <p className="mt-2 text-[11px] text-danger/90">
                    操作不可恢复，需重新执行同步才能恢复数据。
                  </p>
                  <div className="mt-2 flex items-start gap-1.5 text-[11px] text-warning">
                    <Info className="h-3.5 w-3.5 shrink-0 mt-px text-warning" />
                    <span>此操作不会清除扩展数据，如需删除请在扩展数据设置中单独操作。</span>
                  </div>
                </div>
              </div>
              <div className="flex items-center justify-end gap-2 mt-5">
                <button
                  onClick={() => setShowClearConfirm(false)}
                  disabled={clearData.isPending}
                  className="px-3 py-1.5 rounded-btn bg-elevated text-secondary hover:bg-elevated/80 text-sm transition-colors disabled:opacity-50"
                >
                  取消
                </button>
                <button
                  onClick={() => clearData.mutate()}
                  disabled={clearData.isPending}
                  className="px-3 py-1.5 rounded-btn bg-danger/90 text-base text-sm font-medium hover:bg-danger disabled:opacity-50 transition-colors"
                >
                  {clearData.isPending ? '清除中…' : '清除数据'}
                </button>
              </div>
            </motion.div>
          </div>
        )}
      </AnimatePresence>
    </>
  )
}
