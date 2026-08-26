import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Database,
  FlaskConical,
  Gauge,
  Link2,
  LoaderCircle,
  Play,
  RefreshCw,
  Rocket,
  Save,
  Settings2,
  Square,
} from 'lucide-react'
import { EmptyState } from '@/components/EmptyState'
import { toast } from '@/components/Toast'
import {
  api,
  type FactorColumn,
  type MiningBudgetProfile,
  type MiningCandidateGate,
  type MiningCandidateRow,
  type MiningRequestV1,
  type MiningResult,
  type MiningRun,
  type MiningScheduleConfig,
} from '@/lib/api'
import {
  attachMiningRun,
  cancelMining,
  startMining,
  tryReconnectMining,
  useMiningTask,
} from '@/lib/miningTask'
import { QK } from '@/lib/queryKeys'
import { FactorCorrelationHeatmap } from './charts/FactorCorrelationHeatmap'
import { MiningOosChart } from './charts/MiningOosChart'
import { RegimeComparisonChart } from './charts/RegimeComparisonChart'

const DRAFT_KEY = 'mining_workbench_draft_v1'
const TODAY = new Date().toISOString().slice(0, 10)
const INPUT = 'h-8 w-full rounded-input border border-border bg-surface px-2 text-xs text-foreground outline-none transition-colors focus:border-accent'
const LABEL = 'mb-1 block text-[10px] font-medium text-secondary'
const SUCCESS = new Set(['succeeded', 'succeeded_with_budget_exhausted'])
const ACTIVE = new Set(['queued', 'running', 'cancelling'])
const PROFILE_LABELS: Record<MiningBudgetProfile, string> = {
  exploratory: '探索档',
  balanced: '均衡档',
  strict: '严格档',
}

interface MiningDraft {
  assetType: 'stock' | 'etf'
  start: string
  end: string
  profile: MiningBudgetProfile
  factorNames: string[]
  strategyIds: string[]
  commissionBps: string
  stampTaxBps: string
  slippageBps: string
  correlationThreshold: string
  maxCombinationFactors: string
  beamWidth: string
  maxFinalists: string
  force: boolean
}

function yearsAgo(years: number) {
  const value = new Date()
  value.setFullYear(value.getFullYear() - years)
  return value.toISOString().slice(0, 10)
}

const DEFAULT_DRAFT: MiningDraft = {
  assetType: 'stock',
  start: yearsAgo(4),
  end: TODAY,
  profile: 'exploratory',
  factorNames: [],
  strategyIds: [],
  commissionBps: '2',
  stampTaxBps: '5',
  slippageBps: '5',
  correlationThreshold: '0.75',
  maxCombinationFactors: '4',
  beamWidth: '12',
  maxFinalists: '8',
  force: false,
}

function loadDraft(): MiningDraft {
  try {
    const value = JSON.parse(localStorage.getItem(DRAFT_KEY) || '')
    if (!value || typeof value !== 'object') return DEFAULT_DRAFT
    return { ...DEFAULT_DRAFT, ...value }
  } catch {
    return DEFAULT_DRAFT
  }
}

function parseBoundedNumber(value: string, label: string, min: number, max: number, integer = false) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed) || parsed < min || parsed > max || (integer && !Number.isInteger(parsed))) {
    toast(`${label}需为 ${min}–${max}${integer ? ' 的整数' : ''}`, 'error')
    return null
  }
  return parsed
}

function formatNumber(value: number | null | undefined, digits = 2) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—'
}

function formatPct(value: number | null | undefined, digits = 2) {
  return typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(digits)}%` : '—'
}

function formatBytes(value: number | null | undefined) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB`
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB`
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${value} B`
}

function statusLabel(status?: string) {
  return ({
    queued: '排队中',
    running: '挖掘中',
    cancelling: '正在取消',
    succeeded: '已完成',
    succeeded_with_budget_exhausted: '已完成（预算耗尽）',
    failed: '失败',
    cancelled: '已取消',
    interrupted: '启动中断',
    skipped_prerequisite: '前置条件不足',
  } as Record<string, string>)[status || ''] || '未运行'
}

function confidenceLabel(value?: string) {
  return value === 'high' ? '高' : value === 'standard' ? '标准' : '低'
}

function definitionLabel(candidate: MiningCandidateRow) {
  if (candidate.kind === 'existing_strategy') return candidate.strategy_id || '已有策略'
  return candidate.factor_names?.join(' + ') || '因子组合'
}

function gateTitle(gate?: MiningCandidateGate | null) {
  if (!gate || !gate.reasons.length) return undefined
  return gate.reasons.join('\n')
}

function RequestSummaryLine({ result }: { result: MiningResult }) {
  const request = result.request_summary
  if (!request) return null
  const items: [string, string][] = [
    ['资产', request.asset_type === 'etf' ? 'ETF' : '股票'],
    ['档位', PROFILE_LABELS[request.budget_profile as MiningBudgetProfile] || request.budget_profile],
    ['区间', `${request.start || '—'} → ${request.end || '—'}`],
    ['因子', String(request.factor_count)],
    ['对照策略', String(request.strategy_count)],
    ['成本', `佣金 ${formatBps(request.commission_pct)} / 印花 ${formatBps(request.stamp_tax_pct)} / 滑点 ${typeof request.slippage_bps === 'number' && Number.isFinite(request.slippage_bps) ? `${request.slippage_bps.toFixed(1)}bp` : '—'}`],
    ['相关阈值', request.correlation_threshold == null ? '—' : request.correlation_threshold.toFixed(2)],
  ]
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 border-b border-border px-3 py-1.5 text-[9px] text-muted">
      {items.map(([label, value]) => (
        <span key={label} className="min-w-0">
          <span className="text-muted">{label}</span>{' '}
          <span className="font-mono text-secondary">{value}</span>
        </span>
      ))}
    </div>
  )
}

function formatBps(ratio: number | null | undefined, digits = 1) {
  return typeof ratio === 'number' && Number.isFinite(ratio) ? `${(ratio * 10000).toFixed(digits)}bp` : '—'
}

function foldKindLabel(kind?: string | null) {
  if (kind === 'cross') return '跨折'
  if (kind === 'benchmark') return '对照'
  return undefined
}

function SummaryStrip({ result }: { result: MiningResult }) {
  const items = [
    ['因子', `${result.summary.selected_factor_count}/${result.summary.factor_count}`],
    ['候选', String(result.summary.candidate_count)],
    ['有效折', String(result.summary.valid_fold_count)],
    ['跳过折', String(result.summary.skipped_fold_count)],
    ['置信度', confidenceLabel(result.summary.confidence)],
    ['耗时', result.summary.elapsed_ms == null ? '—' : `${(result.summary.elapsed_ms / 1000).toFixed(1)}s`],
    ['峰值内存', formatBytes(result.summary.peak_rss_bytes)],
  ]
  return (
    <div className="grid grid-cols-2 border-b border-border bg-base/30 sm:grid-cols-4 xl:grid-cols-7">
      {items.map(([label, value]) => (
        <div key={label} className="min-w-0 border-b border-r border-border/60 px-3 py-2 last:border-r-0 sm:border-b-0">
          <div className="text-[9px] text-muted">{label}</div>
          <div className="mt-0.5 truncate font-mono text-xs font-semibold text-foreground" title={value}>{value}</div>
        </div>
      ))}
    </div>
  )
}

function RunStatus({ run, progress, error, reconnecting }: { run: MiningRun | null; progress?: { label?: string; phase?: string; percent?: number } | null; error?: string | null; reconnecting?: boolean }) {
  const active = !!run && ACTIVE.has(run.status)
  const success = !!run && SUCCESS.has(run.status)
  const Icon = reconnecting ? RefreshCw : active ? LoaderCircle : success ? CheckCircle2 : error ? AlertTriangle : Clock3
  return (
    <div className={`flex min-w-0 items-center gap-2 border-b border-border px-3 py-2 ${error ? 'bg-danger/5' : 'bg-base/20'}`}>
      <Icon className={`h-3.5 w-3.5 shrink-0 ${reconnecting ? 'animate-spin text-warning' : active ? 'animate-spin text-accent' : success ? 'text-success' : error ? 'text-danger' : 'text-muted'}`} />
      <div className="min-w-0 flex-1">
        <div className="truncate text-[11px] font-medium text-foreground">
          {reconnecting ? '连接恢复中' : run ? statusLabel(run.status) : '研究任务'}
          {progress?.label ? ` · ${progress.label}` : ''}
        </div>
        {(error || progress?.phase) && (
          <div className={`mt-0.5 text-[9px] leading-4 ${error ? 'whitespace-pre-wrap break-words text-danger' : 'truncate text-muted'}`} title={error || progress?.phase}>
            {error || progress?.phase}
          </div>
        )}
      </div>
      {typeof progress?.percent === 'number' && (
        <span className="shrink-0 font-mono text-[10px] text-secondary">{Math.round(progress.percent)}%</span>
      )}
      {run?.run_id && <span className="hidden max-w-40 truncate font-mono text-[9px] text-muted sm:block">{run.run_id}</span>}
    </div>
  )
}

function FactorTable({ result }: { result: MiningResult }) {
  return (
    <div className="min-w-[760px]">
      <div className="grid grid-cols-[minmax(160px,1.5fr)_58px_repeat(7,minmax(72px,1fr))_minmax(130px,1.3fr)] border-b border-border bg-base/50 px-2 py-1.5 text-[9px] font-medium text-muted">
        <span>因子</span><span>方向</span><span>评分</span><span>IC</span><span>IR</span><span>覆盖</span><span>换手</span><span>价差</span><span>Sharpe</span><span>状态</span>
      </div>
      {result.factors.map(row => (
        <div key={row.factor_name} className="grid grid-cols-[minmax(160px,1.5fr)_58px_repeat(7,minmax(72px,1fr))_minmax(130px,1.3fr)] border-b border-border/50 px-2 py-1.5 text-[10px] text-secondary last:border-b-0 hover:bg-elevated/40">
          <span className="truncate font-medium text-foreground" title={row.factor_name}>{row.label || row.factor_name}</span>
          <span>{row.direction === 1 ? '正向' : '反向'}</span>
          <span className="font-mono">{formatNumber(row.score, 3)}</span>
          <span className="font-mono">{formatNumber(row.ic_mean, 4)}</span>
          <span className="font-mono">{formatNumber(row.ir, 2)}</span>
          <span className="font-mono">{formatPct(row.coverage, 1)}</span>
          <span className="font-mono">{formatPct(row.turnover, 1)}</span>
          <span className="font-mono">{formatPct(row.spread_return, 2)}</span>
          <span className="font-mono">{formatNumber(row.spread_sharpe, 2)}</span>
          <span className={row.selected ? 'text-success' : 'truncate text-muted'} title={row.excluded_reason || undefined}>
            {row.selected ? '入选' : row.excluded_reason || '未入选'}
          </span>
        </div>
      ))}
    </div>
  )
}

export function MiningWorkbench() {
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const initializedFactors = useRef(false)
  const [draft, setDraft] = useState<MiningDraft>(loadDraft)
  const [scheduleDraft, setScheduleDraft] = useState<MiningScheduleConfig | null>(null)
  const [correlationScope, setCorrelationScope] = useState<'all' | 'selected'>('selected')
  const task = useMiningTask()
  const runFromUrl = searchParams.get('run') || ''
  const selectedCandidate = searchParams.get('candidate') || ''

  const factorQuery = useQuery({ queryKey: QK.factorColumns, queryFn: api.factorColumns })
  const strategyQuery = useQuery({
    queryKey: QK.strategyLinkOptions(draft.assetType),
    queryFn: () => api.strategyList(draft.assetType),
  })
  const runsQuery = useQuery({
    queryKey: QK.miningRuns,
    queryFn: api.miningRuns,
    refetchInterval: task.isPending ? 5000 : false,
  })
  const validDateRange = !draft.start || !draft.end || draft.start <= draft.end
  const availabilityQuery = useQuery({
    queryKey: QK.miningAvailability(
      draft.assetType,
      draft.profile,
      draft.start,
      draft.end,
    ),
    queryFn: () => api.miningAvailability({
      assetType: draft.assetType,
      budgetProfile: draft.profile,
      start: draft.start || undefined,
      end: draft.end || undefined,
    }),
    enabled: validDateRange,
    staleTime: 30_000,
  })
  const regimeLatestQuery = useQuery({ queryKey: QK.regimeLatest, queryFn: api.regimeLatest, staleTime: 60_000 })
  const configQuery = useQuery({ queryKey: QK.miningConfig, queryFn: api.miningConfig })

  useEffect(() => {
    localStorage.setItem(DRAFT_KEY, JSON.stringify(draft))
  }, [draft])

  useEffect(() => {
    if (initializedFactors.current || !factorQuery.data?.columns.length) return
    initializedFactors.current = true
    if (!draft.factorNames.length) {
      setDraft(current => ({ ...current, factorNames: factorQuery.data!.columns.slice(0, 48).map(item => item.id) }))
    }
  }, [factorQuery.data, draft.factorNames.length])

  useEffect(() => {
    if (configQuery.data && !scheduleDraft) setScheduleDraft(configQuery.data)
  }, [configQuery.data, scheduleDraft])

  useEffect(() => {
    if (runFromUrl) {
      if (task.isPending && !task.runId) return
      if (task.runId !== runFromUrl) void attachMiningRun(runFromUrl)
      return
    }
    if (task.runId) {
      const params = new URLSearchParams(searchParams)
      params.set('run', task.runId)
      setSearchParams(params, { replace: true })
      return
    }
    tryReconnectMining()
  }, [runFromUrl, searchParams, setSearchParams, task.runId])

  const currentResult = task.runId
    && (!runFromUrl || runFromUrl === task.runId)
    && task.run?.run_id === task.runId
    && SUCCESS.has(task.run.status)
    && task.result?.run_id === task.runId
    ? task.result
    : null
  const showingPrevious = !!(
    task.runId
    && (!runFromUrl || runFromUrl === task.runId)
    && task.previousResult
    && task.previousResult.run_id !== task.runId
    && (
      task.isPending
      || !!task.error
      || (!!task.run && !SUCCESS.has(task.run.status))
    )
  )
  const result = currentResult ?? (showingPrevious ? task.previousResult : null)
  const candidates = result?.candidates ?? []
  const activeCandidate = candidates.find(item => item.signature === selectedCandidate) ?? candidates[0] ?? null
  const candidateFolds = activeCandidate?.folds?.length ? activeCandidate.folds : result?.folds ?? []
  const correlation = useMemo(() => {
    if (!result || correlationScope === 'all') return result?.correlation ?? null
    const selected = new Set(result.factors.filter(item => item.selected).map(item => item.factor_name))
    const indexes = result.correlation.labels
      .map((label, index) => selected.has(label) ? index : -1)
      .filter(index => index >= 0)
    if (!indexes.length) return result.correlation
    return {
      ...result.correlation,
      labels: indexes.map(index => result.correlation.labels[index]),
      matrix: indexes.map(row => indexes.map(column => result.correlation.matrix[row]?.[column] ?? null)),
      pair_counts: indexes.map(row => indexes.map(column => result.correlation.pair_counts?.[row]?.[column] ?? null)),
    }
  }, [result, correlationScope])

  const setSelectedCandidate = (signature: string) => {
    const params = new URLSearchParams(searchParams)
    if (signature) params.set('candidate', signature)
    else params.delete('candidate')
    setSearchParams(params, { replace: true })
  }

  useEffect(() => {
    if (!selectedCandidate && candidates[0]) setSelectedCandidate(candidates[0].signature)
    if (selectedCandidate && candidates.length && !candidates.some(item => item.signature === selectedCandidate)) {
      setSelectedCandidate(candidates[0]?.signature || '')
    }
  }, [candidates, selectedCandidate])

  const factorGroups = useMemo(() => {
    const groups: Record<string, FactorColumn[]> = {}
    for (const item of factorQuery.data?.columns ?? []) (groups[item.group] ??= []).push(item)
    return groups
  }, [factorQuery.data])
  const strategies = strategyQuery.data?.strategies.filter(item => item.execution_backend === 'matrix_native') ?? []

  useEffect(() => {
    if (!strategyQuery.data) return
    const compatibleIds = new Set(strategies.map(item => item.id))
    setDraft(current => {
      const strategyIds = current.strategyIds.filter(id => compatibleIds.has(id))
      return strategyIds.length === current.strategyIds.length ? current : { ...current, strategyIds }
    })
  }, [strategyQuery.data])

  const updateDraft = <K extends keyof MiningDraft>(key: K, value: MiningDraft[K]) => {
    setDraft(current => ({ ...current, [key]: value }))
  }
  const changeAssetType = (assetType: MiningDraft['assetType']) => {
    setDraft(current => ({
      ...current,
      assetType,
      strategyIds: [],
    }))
  }
  const toggleFactor = (id: string) => {
    setDraft(current => ({
      ...current,
      factorNames: current.factorNames.includes(id)
        ? current.factorNames.filter(value => value !== id)
        : current.factorNames.length < 48 ? [...current.factorNames, id] : current.factorNames,
    }))
  }
  const toggleStrategy = (id: string) => {
    setDraft(current => ({
      ...current,
      strategyIds: current.strategyIds.includes(id)
        ? current.strategyIds.filter(value => value !== id)
        : current.strategyIds.length < 8 ? [...current.strategyIds, id] : current.strategyIds,
    }))
  }

  const runMining = () => {
    if (!draft.factorNames.length) {
      toast('至少选择一个因子', 'error')
      return
    }
    if (draft.start && draft.end && draft.start > draft.end) {
      toast('开始日期不能晚于结束日期', 'error')
      return
    }
    if (availabilityQuery.isPending || availabilityQuery.isFetching) {
      toast('正在核验有效交易日，请稍候', 'error')
      return
    }
    if (availabilityQuery.isError || !availabilityQuery.data) {
      toast('无法核验有效交易日，请检查数据状态后重试', 'error')
      return
    }
    if (!availabilityQuery.data.eligible) {
      toast(
        `${PROFILE_LABELS[draft.profile]}至少需要 ${availabilityQuery.data.required_bars} 个交易日，当前范围仅 ${availabilityQuery.data.trading_bars} 个`,
        'error',
      )
      return
    }
    if (regimeLatestQuery.isPending || regimeLatestQuery.isFetching) {
      toast('正在核验市场环境数据，请稍候', 'error')
      return
    }
    if (regimeLatestQuery.isError || !regimeLatestQuery.data) {
      toast('无法核验市场环境数据，请检查数据状态后重试', 'error')
      return
    }
    if (!regimeLatestQuery.data.row) {
      toast('尚未计算市场环境数据：挖掘含市场环境分组评估，请先在数据页完成市场环境计算', 'error')
      return
    }
    const commissionBps = parseBoundedNumber(draft.commissionBps, '佣金', 0, 500)
    const stampTaxBps = parseBoundedNumber(draft.stampTaxBps, '印花税', 0, 500)
    const slippageBps = parseBoundedNumber(draft.slippageBps, '滑点', 0, 1000)
    const correlationThreshold = parseBoundedNumber(draft.correlationThreshold, '相关阈值', Number.EPSILON, 1)
    const maxCombinationFactors = parseBoundedNumber(draft.maxCombinationFactors, '组合上限', 1, 4, true)
    const beamWidth = parseBoundedNumber(draft.beamWidth, 'Beam', 1, 12, true)
    const maxFinalists = parseBoundedNumber(draft.maxFinalists, 'Finalists', 1, 8, true)
    if ([commissionBps, stampTaxBps, slippageBps, correlationThreshold, maxCombinationFactors, beamWidth, maxFinalists].some(value => value == null)) return
    const payload: MiningRequestV1 = {
      factor_names: draft.factorNames,
      strategy_ids: draft.strategyIds,
      asset_type: draft.assetType,
      start: draft.start || null,
      end: draft.end || null,
      budget_profile: draft.profile,
      commission_pct: commissionBps! / 10000,
      stamp_tax_pct: stampTaxBps! / 10000,
      slippage_bps: slippageBps!,
      correlation_threshold: correlationThreshold!,
      max_combination_factors: maxCombinationFactors!,
      beam_width: beamWidth!,
      max_finalists: maxFinalists!,
      force: draft.force,
    }
    const params = new URLSearchParams(searchParams)
    params.delete('run')
    params.delete('candidate')
    setSearchParams(params, { replace: true })
    void startMining(payload).then(() => queryClient.invalidateQueries({ queryKey: QK.miningRuns }))
  }

  const promote = useMutation({
    mutationFn: (candidate: MiningCandidateRow) => {
      if (!currentResult) throw new Error('只能保存当前已成功运行的候选')
      return api.miningPromote(currentResult.run_id, candidate.signature)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.researchCandidates })
      toast('候选已保存，状态为待验证', 'success')
    },
    onError: error => toast(`保存失败 · ${String((error as Error).message || error)}`, 'error'),
  })
  const publish = useMutation({
    mutationFn: (candidate: MiningCandidateRow) => {
      if (!currentResult) throw new Error('只能发布当前已成功运行的候选')
      return api.miningPublish(currentResult.run_id, candidate.signature)
    },
    onSuccess: value => {
      queryClient.invalidateQueries({ queryKey: QK.screenerStrategies(draft.assetType) })
      toast(`策略已发布 · ${value.strategy_id}`, 'success')
    },
    onError: error => toast(`发布失败 · ${String((error as Error).message || error)}`, 'error'),
  })
  const saveSchedule = useMutation({
    mutationFn: (value: MiningScheduleConfig) => api.updateMiningConfig(value),
    onSuccess: value => {
      setScheduleDraft(value)
      queryClient.setQueryData(QK.miningConfig, value)
      toast('自动挖掘配置已保存', 'success')
    },
    onError: error => toast(`保存失败 · ${String((error as Error).message || error)}`, 'error'),
  })

  const attachRun = (run: MiningRun) => {
    const params = new URLSearchParams(searchParams)
    params.set('run', run.run_id)
    params.delete('candidate')
    setSearchParams(params, { replace: true })
  }

  return (
    <div className="grid min-h-[calc(100vh-9rem)] grid-cols-1 overflow-hidden rounded-card border border-border bg-surface xl:grid-cols-[20rem_minmax(0,1fr)]">
      <aside className="border-b border-border bg-base/25 xl:max-h-[calc(100vh-9rem)] xl:overflow-y-auto xl:border-b-0 xl:border-r">
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <div>
            <div className="text-xs font-semibold text-foreground">挖掘配置</div>
            <div className="mt-0.5 text-[9px] text-muted">日频 · 嵌套样本外 · T-1 环境</div>
          </div>
          <span className="font-mono text-[9px] text-muted">{draft.factorNames.length}/48</span>
        </div>

        <div className="space-y-4 p-3">
          <section>
            <div className="mb-2 flex items-center gap-1.5 text-[10px] font-semibold text-secondary"><Database className="h-3 w-3" />数据范围</div>
            <div className="grid grid-cols-2 gap-2">
              <label><span className={LABEL}>资产</span><select className={INPUT} value={draft.assetType} onChange={event => changeAssetType(event.target.value as 'stock' | 'etf')}><option value="stock">股票</option><option value="etf">ETF</option></select></label>
              <label><span className={LABEL}>置信档</span><select className={INPUT} value={draft.profile} onChange={event => updateDraft('profile', event.target.value as MiningBudgetProfile)}><option value="exploratory">探索 · 219 日</option><option value="balanced">均衡 · 786 日</option><option value="strict">严格 · 1164 日</option></select></label>
              <label><span className={LABEL}>开始</span><input type="date" className={INPUT} value={draft.start} onChange={event => updateDraft('start', event.target.value)} /></label>
              <label><span className={LABEL}>结束</span><input type="date" className={INPUT} value={draft.end} onChange={event => updateDraft('end', event.target.value)} /></label>
            </div>
            {!validDateRange ? (
              <div className="mt-1.5 text-[9px] leading-4 text-danger">开始日期不能晚于结束日期。</div>
            ) : availabilityQuery.isPending || availabilityQuery.isFetching ? (
              <div className="mt-1.5 flex items-center gap-1 text-[9px] leading-4 text-muted"><LoaderCircle className="h-3 w-3 animate-spin" />正在核验有效交易日…</div>
            ) : availabilityQuery.isError || !availabilityQuery.data ? (
              <div className="mt-1.5 text-[9px] leading-4 text-danger">无法核验 enriched 交易日，暂不能开始挖掘。</div>
            ) : (
              <div className={`mt-1.5 text-[9px] leading-4 ${availabilityQuery.data.eligible ? 'text-secondary' : 'text-warning'}`}>
                <div>
                  当前范围 {availabilityQuery.data.trading_bars} 个交易日；{PROFILE_LABELS[draft.profile]}至少需要 {availabilityQuery.data.required_bars} 个，可生成 {availabilityQuery.data.outer_folds}/{availabilityQuery.data.required_outer_folds} 个 outer folds。
                </div>
                {!availabilityQuery.data.eligible && availabilityQuery.data.suggested_start && draft.start !== availabilityQuery.data.suggested_start && (
                  <button type="button" className="mt-0.5 text-left text-accent hover:underline" onClick={() => updateDraft('start', availabilityQuery.data!.suggested_start!)}>
                    使用建议开始日 {availabilityQuery.data.suggested_start}
                  </button>
                )}
                {!availabilityQuery.data.eligible && !availabilityQuery.data.suggested_start && draft.profile !== 'exploratory' && (
                  <div>本地历史数据不足；可改用探索档，系统不会自动降档。</div>
                )}
              </div>
            )}
          </section>

          {regimeLatestQuery.data && !regimeLatestQuery.data.row && (
            <Link to="/data" className="block rounded-btn border border-warning/40 bg-warning/5 px-2 py-1.5 text-[9px] leading-4 text-warning transition-colors hover:border-warning/70">
              尚未计算市场环境数据 — 挖掘含市场环境分组评估，缺少时启动即校验失败。<span className="underline underline-offset-2">前往数据页完成市场环境计算 →</span>
            </Link>
          )}

          <section className="border-t border-border pt-3">
            <div className="mb-2 flex items-center justify-between">
              <span className="flex items-center gap-1.5 text-[10px] font-semibold text-secondary"><FlaskConical className="h-3 w-3" />因子目录</span>
              <button type="button" className="text-[9px] text-accent" onClick={() => updateDraft('factorNames', draft.factorNames.length ? [] : (factorQuery.data?.columns ?? []).slice(0, 48).map(item => item.id))}>{draft.factorNames.length ? '清空' : '全选'}</button>
            </div>
            <div className="max-h-64 space-y-2 overflow-y-auto pr-1">
              {Object.entries(factorGroups).map(([group, items]) => (
                <div key={group}>
                  <div className="mb-1 text-[9px] font-medium text-muted">{group}</div>
                  <div className="grid grid-cols-2 gap-1">
                    {items.map(item => <label key={item.id} className="flex min-w-0 cursor-pointer items-center gap-1.5 text-[9px] text-secondary"><input type="checkbox" className="h-3 w-3 accent-accent" checked={draft.factorNames.includes(item.id)} onChange={() => toggleFactor(item.id)} /><span className="truncate" title={`${item.label} · ${item.desc}`}>{item.label}</span></label>)}
                  </div>
                </div>
              ))}
              {factorQuery.isLoading && <div className="text-[10px] text-muted">加载因子目录…</div>}
              {factorQuery.isError && <div className="text-[10px] text-danger">因子目录加载失败</div>}
            </div>
          </section>

          <section className="border-t border-border pt-3">
            <div className="mb-2 flex items-center justify-between text-[10px] font-semibold text-secondary"><span>已有策略对照</span><span className="font-mono text-[9px] text-muted">{draft.strategyIds.length}/8</span></div>
            <div className="max-h-32 space-y-1 overflow-y-auto pr-1">
              {strategies.map(item => <label key={item.id} className="flex min-w-0 cursor-pointer items-center gap-1.5 text-[9px] text-secondary"><input type="checkbox" className="h-3 w-3 accent-accent" checked={draft.strategyIds.includes(item.id)} onChange={() => toggleStrategy(item.id)} /><span className="truncate" title={item.description}>{item.name}</span></label>)}
              {strategyQuery.isError && <div className="text-[9px] text-danger">策略目录加载失败</div>}
              {!strategyQuery.isLoading && !strategyQuery.isError && !strategies.length && <div className="text-[9px] text-muted">无可用 matrix-native 策略</div>}
            </div>
          </section>

          <section className="border-t border-border pt-3">
            <div className="mb-2 flex items-center gap-1.5 text-[10px] font-semibold text-secondary"><Settings2 className="h-3 w-3" />成本与预算</div>
            <div className="grid grid-cols-3 gap-2">
              <label><span className={LABEL}>佣金 bp</span><input className={INPUT} inputMode="decimal" value={draft.commissionBps} onChange={event => updateDraft('commissionBps', event.target.value)} /></label>
              <label><span className={LABEL}>印花税 bp</span><input className={INPUT} inputMode="decimal" value={draft.stampTaxBps} onChange={event => updateDraft('stampTaxBps', event.target.value)} /></label>
              <label><span className={LABEL}>滑点 bp</span><input className={INPUT} inputMode="decimal" value={draft.slippageBps} onChange={event => updateDraft('slippageBps', event.target.value)} /></label>
              <label><span className={LABEL}>相关阈值</span><input className={INPUT} inputMode="decimal" value={draft.correlationThreshold} onChange={event => updateDraft('correlationThreshold', event.target.value)} /></label>
              <label><span className={LABEL}>组合上限</span><input className={INPUT} inputMode="numeric" value={draft.maxCombinationFactors} onChange={event => updateDraft('maxCombinationFactors', event.target.value)} /></label>
              <label><span className={LABEL}>Beam</span><input className={INPUT} inputMode="numeric" value={draft.beamWidth} onChange={event => updateDraft('beamWidth', event.target.value)} /></label>
              <label><span className={LABEL}>Finalists</span><input className={INPUT} inputMode="numeric" value={draft.maxFinalists} onChange={event => updateDraft('maxFinalists', event.target.value)} /></label>
              <label className="col-span-2 flex items-end gap-1.5 pb-1 text-[9px] text-secondary"><input type="checkbox" className="h-3 w-3 accent-accent" checked={draft.force} onChange={event => updateDraft('force', event.target.checked)} />忽略同配置缓存</label>
            </div>
          </section>

          <div className="sticky bottom-0 flex gap-2 bg-base/95 py-2">
            <button type="button" disabled={task.isPending || !draft.factorNames.length || (draft.strategyIds.length > 0 && strategyQuery.isLoading) || !validDateRange || availabilityQuery.isPending || availabilityQuery.isFetching || availabilityQuery.isError || !availabilityQuery.data?.eligible} onClick={runMining} className="inline-flex h-8 flex-1 items-center justify-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-semibold text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-50"><Play className="h-3.5 w-3.5" />开始挖掘</button>
            {task.isPending && <button type="button" title="取消任务" disabled={task.cancelling} onClick={() => void cancelMining()} className="inline-flex h-8 w-9 items-center justify-center rounded-btn border border-danger/40 text-danger hover:bg-danger/10 disabled:opacity-50"><Square className="h-3.5 w-3.5" /></button>}
          </div>

          <section className="border-t border-border pt-3">
            <div className="mb-2 flex items-center justify-between"><span className="text-[10px] font-semibold text-secondary">最近运行</span><button type="button" title="刷新历史" onClick={() => void runsQuery.refetch()} className="text-muted hover:text-accent"><RefreshCw className={`h-3 w-3 ${runsQuery.isFetching ? 'animate-spin' : ''}`} /></button></div>
            <div className="max-h-40 space-y-1 overflow-y-auto">
              {(runsQuery.data?.items ?? []).map(run => <button key={run.run_id} type="button" onClick={() => attachRun(run)} className={`flex w-full items-center gap-2 rounded-btn px-2 py-1.5 text-left hover:bg-elevated ${task.runId === run.run_id ? 'bg-accent/10' : ''}`}><span className={`h-1.5 w-1.5 shrink-0 rounded-full ${SUCCESS.has(run.status) ? 'bg-success' : ACTIVE.has(run.status) ? 'bg-accent' : 'bg-muted'}`} /><span className="min-w-0 flex-1 truncate font-mono text-[9px] text-secondary">{run.run_id}</span><span className="shrink-0 text-[9px] text-muted">{statusLabel(run.status)}</span></button>)}
              {runsQuery.isError && <div className="text-[9px] text-danger">运行历史加载失败</div>}
              {!runsQuery.isLoading && !runsQuery.isError && !(runsQuery.data?.items.length) && <div className="text-[9px] text-muted">暂无持久运行</div>}
            </div>
          </section>

          {scheduleDraft && <section className="border-t border-border pt-3"><div className="mb-2 text-[10px] font-semibold text-secondary">周度自动挖掘</div><label className="mb-2 flex items-center gap-1.5 text-[9px] text-secondary"><input type="checkbox" className="h-3 w-3 accent-accent" checked={scheduleDraft.mining_schedule_enabled} onChange={event => setScheduleDraft({ ...scheduleDraft, mining_schedule_enabled: event.target.checked })} />启用（默认关闭，不自动发布）</label><div className="grid grid-cols-2 gap-2"><label><span className={LABEL}>工作日</span><select className={INPUT} value={scheduleDraft.mining_schedule_weekday} onChange={event => setScheduleDraft({ ...scheduleDraft, mining_schedule_weekday: Number(event.target.value) })}>{['周一', '周二', '周三', '周四', '周五'].map((label, index) => <option key={label} value={index}>{label}</option>)}</select></label><label><span className={LABEL}>档位</span><select className={INPUT} value={scheduleDraft.mining_budget_profile} onChange={event => setScheduleDraft({ ...scheduleDraft, mining_budget_profile: event.target.value as 'balanced' | 'strict' })}><option value="balanced">均衡</option><option value="strict">严格</option></select></label></div><button type="button" disabled={saveSchedule.isPending} onClick={() => saveSchedule.mutate(scheduleDraft)} className="mt-2 inline-flex h-7 w-full items-center justify-center gap-1 rounded-btn border border-border text-[10px] text-secondary hover:border-accent/40 hover:text-accent disabled:opacity-50"><Save className="h-3 w-3" />保存自动配置</button></section>}
        </div>
      </aside>

      <section className="min-w-0 bg-surface xl:max-h-[calc(100vh-9rem)] xl:overflow-y-auto">
        <RunStatus run={task.run} progress={task.progress} error={task.error} reconnecting={task.reconnecting} />
        {showingPrevious && <div className="border-b border-warning/30 bg-warning/5 px-3 py-1.5 text-[10px] text-warning">历史结果 · run {result?.run_id}。当前 run {task.runId} {task.isPending ? '仍在执行' : '未成功完成'}，以下内容仅供参考，候选操作已禁用。</div>}

        {!result ? (
          <div className="min-h-[32rem]"><EmptyState icon={FlaskConical} title={task.isPending ? '挖掘任务正在执行' : '尚无挖掘结果'} hint={task.isPending ? '任务在独立 worker 中运行；可切换页面或刷新后按 run ID 重连。' : '选择因子和验证档位后开始。探索档结果仅用于研究，不代表已验证策略。'} /></div>
        ) : (
          <div className="min-w-0">
            <SummaryStrip result={result} />
            <RequestSummaryLine result={result} />

            <section className="border-b border-border">
              <div className="flex items-center justify-between px-3 py-2"><h2 className="text-xs font-semibold text-foreground">因子排名</h2><span className="text-[9px] text-muted">{result.methodology_version}</span></div>
              <div className="overflow-x-auto border-t border-border"><FactorTable result={result} /></div>
            </section>

            <div className="grid grid-cols-1 border-b border-border 2xl:grid-cols-2">
              <section className="min-w-0 border-b border-border 2xl:border-b-0 2xl:border-r">
                <div className="flex min-w-0 items-center justify-between gap-2 border-b border-border px-3 py-2">
                  <h2 className="shrink-0 text-xs font-semibold text-foreground">市场环境对比</h2>
                  {activeCandidate && candidates[0] && activeCandidate.signature !== candidates[0].signature && <span className="truncate text-[9px] text-warning">环境数据属于排名首位候选（{candidates[0].name}）</span>}
                </div>
                <RegimeComparisonChart rows={result.regimes.map(row => ({ state: row.state, label: row.label, nDates: row.n_dates, sharpe: row.sharpe, return: row.total_return, maxDrawdown: row.max_drawdown }))} />
              </section>
              <section className="min-w-0"><h2 className="border-b border-border px-3 py-2 text-xs font-semibold text-foreground">逐折样本外</h2><MiningOosChart folds={candidateFolds.map(row => ({ fold: row.fold, label: row.label || `Fold ${row.fold}`, return: row.total_return, sharpe: row.sharpe, skipped: row.skipped, reason: row.reason || undefined }))} /></section>
            </div>

            <section className="border-b border-border">
              <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
                <div className="min-w-0">
                  <h2 className="text-xs font-semibold text-foreground">相关矩阵</h2>
                  <div className="mt-0.5 text-[9px] text-muted">阈值 {result.correlation.threshold.toFixed(2)} · 按日截面 Rank</div>
                </div>
                <div className="inline-flex shrink-0 rounded-btn border border-border bg-base p-0.5" aria-label="相关矩阵范围">
                  {(['selected', 'all'] as const).map(scope => (
                    <button
                      key={scope}
                      type="button"
                      onClick={() => setCorrelationScope(scope)}
                      className={`h-6 rounded-[4px] px-2 text-[9px] ${correlationScope === scope ? 'bg-accent text-white' : 'text-secondary hover:bg-elevated'}`}
                    >
                      {scope === 'selected' ? '入选' : '全部'}
                    </button>
                  ))}
                </div>
              </div>
              <div className="overflow-x-auto border-t border-border">
                <div style={{ minWidth: `${Math.max(360, (correlation?.labels.length ?? 0) * 48 + 140)}px` }}>
                  <FactorCorrelationHeatmap
                    labels={correlation?.labels ?? []}
                    matrix={correlation?.matrix ?? []}
                    pairCounts={correlation?.pair_counts}
                    threshold={result.correlation.threshold}
                  />
                </div>
              </div>
            </section>

            <section className="border-b border-border"><div className="flex items-center justify-between gap-2 px-3 py-2"><h2 className="shrink-0 text-xs font-semibold text-foreground">策略候选</h2><span className="truncate text-[9px] text-muted">对照策略在全部 outer 折独立评估 · 发布始终需要人工确认</span></div><div className="grid min-h-72 grid-cols-1 border-t border-border lg:grid-cols-[18rem_minmax(0,1fr)]"><div className="border-b border-border lg:border-b-0 lg:border-r">{candidates.map(candidate => <button key={candidate.signature} type="button" onClick={() => setSelectedCandidate(candidate.signature)} className={`block w-full border-b border-border/60 px-3 py-2 text-left hover:bg-elevated ${activeCandidate?.signature === candidate.signature ? 'bg-accent/10' : ''}`}><div className="flex items-center justify-between gap-2"><span className="truncate text-[10px] font-medium text-foreground">{candidate.name}</span><span className="font-mono text-[9px] text-muted">{formatNumber(candidate.score, 2)}</span></div><div className="mt-1 flex items-center gap-1.5"><span className="min-w-0 truncate text-[9px] text-muted" title={definitionLabel(candidate)}>{definitionLabel(candidate)}</span>{candidate.gate && <span className={`shrink-0 rounded-full px-1.5 py-px text-[8px] font-medium ${candidate.gate.qualified ? 'bg-success/10 text-success' : 'bg-warning/10 text-warning'}`} title={gateTitle(candidate.gate)}>{candidate.gate.qualified ? '达标' : '未达标'}</span>}</div></button>)}{!candidates.length && <div className="p-4 text-[10px] text-muted">暂无晋级候选</div>}</div>{activeCandidate ? <div className="min-w-0 p-3"><div className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0"><div className="flex items-center gap-2"><span className="truncate text-xs font-semibold text-foreground">{activeCandidate.name}</span>{activeCandidate.gate && <span className={`shrink-0 rounded-full px-1.5 py-px text-[9px] font-medium ${activeCandidate.gate.qualified ? 'bg-success/10 text-success' : 'bg-warning/10 text-warning'}`} title={gateTitle(activeCandidate.gate)}>{activeCandidate.gate.qualified ? '达标' : '未达标'}</span>}</div><div className="mt-1 break-words font-mono text-[9px] text-muted">{definitionLabel(activeCandidate)}</div></div><div className="flex gap-2"><button type="button" disabled={!currentResult || promote.isPending} onClick={() => promote.mutate(activeCandidate)} className="inline-flex h-7 items-center gap-1 rounded-btn border border-border px-2 text-[10px] text-secondary hover:border-accent/40 hover:text-accent disabled:opacity-50"><Save className="h-3 w-3" />保存候选</button><button type="button" disabled={!currentResult || publish.isPending || activeCandidate.gate?.qualified === false} title={gateTitle(activeCandidate.gate)} onClick={() => { if (window.confirm(`确认发布“${activeCandidate.name}”？\n\n发布会创建独立策略，但不会自动用于实盘或监控。`)) publish.mutate(activeCandidate) }} className="inline-flex h-7 items-center gap-1 rounded-btn bg-accent px-2 text-[10px] font-medium text-white disabled:cursor-not-allowed disabled:opacity-50"><Rocket className="h-3 w-3" />显式发布</button></div></div>{activeCandidate.gate && !activeCandidate.gate.qualified && <div className="mt-2 rounded-btn border border-warning/30 bg-warning/5 px-2 py-1.5 text-[9px] leading-4 text-warning">未达晋级门槛，仅可保存为待定候选：{activeCandidate.gate.reasons.join('；')}</div>}<div className="mt-4 grid grid-cols-2 gap-px border border-border bg-border sm:grid-cols-6">{[['平均每折收益', formatPct(activeCandidate.oos_return)], ['Sharpe', formatNumber(activeCandidate.oos_sharpe)], ['最大回撤', formatPct(activeCandidate.oos_max_drawdown)], ['正收益折', formatPct(activeCandidate.oos_positive_fold_ratio)], ['有效折', activeCandidate.valid_folds == null ? '—' : String(activeCandidate.valid_folds)], ['交易数', activeCandidate.oos_n_trades == null ? '—' : String(activeCandidate.oos_n_trades)]].map(([label, value]) => <div key={label} className="bg-surface px-2 py-2"><div className="text-[9px] text-muted">{label}</div><div className="mt-1 font-mono text-[11px] font-semibold text-foreground">{value}</div></div>)}</div><div className="mt-3 overflow-x-auto"><div className="min-w-[650px]">{candidateFolds.map(fold => <div key={`${fold.fold}-${fold.evaluation_kind || 'selected'}-${fold.test_start}`} className="grid grid-cols-[60px_1fr_1fr_repeat(4,80px)] border-b border-border/60 py-1.5 text-[9px] text-secondary"><span className="flex items-center gap-1"><span>Fold {fold.fold}</span>{foldKindLabel(fold.evaluation_kind) && <span className="rounded-full bg-elevated px-1 py-px text-[8px] text-muted">{foldKindLabel(fold.evaluation_kind)}</span>}</span><span>{fold.train_start || '—'} → {fold.train_end || '—'}</span><span>{fold.test_start || '—'} → {fold.test_end || '—'}</span><span className="font-mono">{formatPct(fold.total_return)}</span><span className="font-mono">{formatNumber(fold.sharpe)}</span><span className="font-mono">{formatPct(fold.max_drawdown)}</span><span className={fold.skipped ? 'text-warning' : 'text-success'}>{fold.skipped ? fold.reason || '跳过' : `${fold.n_trades ?? '—'} 笔`}</span></div>)}</div></div></div> : <div className="grid place-items-center p-8 text-xs text-muted">选择候选查看定义与逐折结果</div>}</div></section>

            <section><div className="flex items-center gap-1.5 border-b border-border px-3 py-2"><Gauge className="h-3.5 w-3.5 text-muted" /><h2 className="text-xs font-semibold text-foreground">性能与复用</h2></div><div className="grid grid-cols-2 gap-px bg-border sm:grid-cols-4 lg:grid-cols-7">{[['总耗时', result.telemetry.elapsed_ms == null ? '—' : `${(result.telemetry.elapsed_ms / 1000).toFixed(1)}s`], ['峰值 RSS', formatBytes(result.telemetry.peak_rss_bytes)], ['面板扫描', result.telemetry.panel_scans ?? '—'], ['矩阵字节', formatBytes(result.telemetry.matrix_bytes)], ['缓存命中', result.telemetry.cache_hits ?? '—'], ['Fold 复用', result.telemetry.fold_reuses ?? '—'], ['IPC 结果', formatBytes(result.telemetry.serialized_result_bytes)]].map(([label, value]) => <div key={label} className="bg-surface px-3 py-2"><div className="text-[9px] text-muted">{label}</div><div className="mt-1 font-mono text-[11px] font-semibold text-foreground">{String(value)}</div></div>)}</div>{result.telemetry.phase_ms && <div className="flex flex-wrap gap-x-4 gap-y-1 border-t border-border px-3 py-2">{Object.entries(result.telemetry.phase_ms).map(([phase, ms]) => <span key={phase} className="text-[9px] text-muted"><span>{phase}</span> <span className="font-mono text-secondary">{ms.toFixed(1)}ms</span></span>)}</div>}</section>
          </div>
        )}

        {task.runId && <div className="flex items-center gap-1.5 border-t border-border px-3 py-2 text-[9px] text-muted"><Link2 className="h-3 w-3" />刷新后通过持久 run ID 自动重连；浏览器断开不会取消 worker。</div>}
      </section>
    </div>
  )
}
