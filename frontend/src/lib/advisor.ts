export type AdvisorActionState =
  | 'OBSERVE_ONLY'
  | 'SIMULATE_ONLY'
  | 'RESEARCH_ONLY'
  | 'NO_CANDIDATE'
  | 'MODEL_WARNING'
export type AdvisorResearchDecision = 'GO' | 'WAIT' | 'NO-GO'
export type AdvisorCandidateState = 'GO1' | 'READY'
export type AdvisorModelHealthStatus = 'INSUFFICIENT_HISTORY' | 'OK' | 'WARNING'
export type AdvisorDataGateDecision = 'PASS' | 'BLOCK'
export type AdvisorDatasetKey = 'instruments' | 'daily' | 'adj_factor' | 'daily_enriched'
export type AdvisorDataPhase = 'LIVE_PROVISIONAL' | 'EOD_PENDING' | 'EOD_SEALED' | 'UNAVAILABLE'
export type AdvisorPlanMonitorStatus = 'PENDING' | 'TRIGGERED' | 'INVALIDATED'

export interface AdvisorRiskFlag {
  code: string
  message: string
}

export interface AdvisorDatasetReceipt {
  status: string
  provider: string | null
  coverage_ratio: number
  observed_start: string | null
  observed_end: string | null
  reasons: string[]
  next_actions: string[]
}

export interface DailyBriefDataGate {
  decision: AdvisorDataGateDecision
  provider: string | null
  coverage_ratio: number
  observed_end: string | null
  reasons: string[]
  next_actions: string[]
  datasets: Record<AdvisorDatasetKey, AdvisorDatasetReceipt>
  runtime_problems?: Array<{
    code: string
    reason: string
    next_action: string
  }>
}

export interface AdvisorDataPhaseState {
  phase: AdvisorDataPhase
  as_of: string | null
  sealed_as_of: string | null
  daily_as_of: string | null
  enriched_as_of: string | null
  strategy_as_of: string | null
  market_phase: string | null
  last_quote_ms: number | null
}

export interface BeginnerDailyBriefCandidate {
  symbol: string
  name: string
  research_decision: AdvisorResearchDecision
  candidate_state: AdvisorCandidateState
  go_streak: number
  global_rank: number | null
  lot_size: number
  lot_cost: number | null
  previous_as_of: string | null
  previous_decision: AdvisorResearchDecision | null
  deterministic_reasons: string[]
  observation_conditions: string[]
  invalidation_conditions: string[]
  risk_flags: AdvisorRiskFlag[]
  plan_monitor?: {
    status: AdvisorPlanMonitorStatus
    as_of: string
    strategy_ids?: string[]
    last_price: number | null
    change_pct: number | null
    evidence: string[]
  } | null
}

export interface BeginnerDailyBriefResponse {
  as_of: string | null
  generated_at: string | null
  snapshot_id: string | null
  snapshot_published_at: string | null
  plan_source_as_of?: string | null
  data_phase?: AdvisorDataPhaseState
  action_state: AdvisorActionState
  today_message: string
  next_step: string
  data_gate: DailyBriefDataGate
  model_health: {
    status: AdvisorModelHealthStatus
    sample_days: number
    window_days: number
    message: string
  }
  excluded_counts: {
    not_main_board: number
    st_or_risk_warning: number
    hard_risk: number
    over_practice_budget: number
  }
  method: {
    kind?: string
    policy_factors_included?: boolean
    ai_can_change_score?: boolean
    auto_trading?: boolean
  }
  candidates: BeginnerDailyBriefCandidate[]
  disclaimer: string
}

interface ActionPresentation {
  label: string
  description: string
  tone: 'warning' | 'accent' | 'success'
}

const ACTION_PRESENTATIONS: Record<AdvisorActionState, ActionPresentation> = {
  OBSERVE_ONLY: {
    label: '安全拦截',
    description: '数据或市场条件未通过，不新增模拟买入',
    tone: 'warning',
  },
  SIMULATE_ONLY: {
    label: '可模拟练习',
    description: '候选连续确认，仅允许本地模拟练习',
    tone: 'accent',
  },
  RESEARCH_ONLY: {
    label: '确认第1天',
    description: '只加入观察，等待下一可信交易日复核',
    tone: 'success',
  },
  NO_CANDIDATE: {
    label: '本批淘汰',
    description: '没有候选通过全部新手条件',
    tone: 'warning',
  },
  MODEL_WARNING: {
    label: '模型需校准',
    description: '连续10个完整交易日未出现连续确认',
    tone: 'warning',
  },
}

const DATASET_PRESENTATION: ReadonlyArray<{
  key: AdvisorDatasetKey
  label: string
}> = [
  { key: 'instruments', label: '股票清单' },
  { key: 'daily', label: '日线行情' },
  { key: 'adj_factor', label: '复权因子' },
  { key: 'daily_enriched', label: '衍生日线' },
]

const DATASET_STATUS_LABELS: Record<string, string> = {
  ok: '正常',
  success: '正常',
  complete: '正常',
  partial: '不完整',
  missing: '缺失',
  duplicate: '重复',
  error: '异常',
  invalid: '无效',
  empty: '无数据',
}

const RESEARCH_DECISION_LABELS: Record<AdvisorResearchDecision, string> = {
  GO: '可继续研究',
  WAIT: '等待更多确认',
  'NO-GO': '暂不纳入',
}

const CANDIDATE_STATE_LABELS: Record<AdvisorCandidateState, string> = {
  GO1: '确认第1天',
  READY: '可模拟练习',
}

const PLAN_MONITOR_PRESENTATIONS: Record<AdvisorPlanMonitorStatus, {
  label: string
  tone: 'warning' | 'success' | 'danger'
}> = {
  PENDING: { label: '未触发', tone: 'warning' },
  TRIGGERED: { label: '已触发', tone: 'success' },
  INVALIDATED: { label: '已失效', tone: 'danger' },
}

export interface TrustDatasetPresentation {
  key: AdvisorDatasetKey
  label: string
  statusLabel: string
  provider: string
  coverageRatio: number
  observedStart: string | null
  observedEnd: string | null
  reasons: string[]
  nextActions: string[]
}

export interface DailyBriefCandidatePresentation {
  symbol: string
  name: string
  readyForSimulation: boolean
  goStreak: number
  lotSize: number
  lotCost: number | null
  statusLabel: string
  reasons: string[]
  observationConditions: string[]
  invalidationConditions: string[]
  riskMessages: string[]
  planMonitor: {
    label: string
    tone: 'warning' | 'success' | 'danger'
    asOf: string
    lastPrice: number | null
    changePct: number | null
    evidence: string[]
  } | null
}

export interface ResearchSnapshotPresentation {
  statusLabel: '已发布' | '未发布'
  shortId: string
  fullId: string | null
  publishedAt: string
}

export interface DataPhasePresentation {
  label: string
  tone: 'success' | 'warning' | 'danger'
}

export function actionPresentation(state: AdvisorActionState): ActionPresentation {
  return ACTION_PRESENTATIONS[state]
}

export function dataPhasePresentation(
  phase: AdvisorDataPhase,
  dataPassed: boolean,
): DataPhasePresentation {
  if (phase === 'LIVE_PROVISIONAL') {
    return { label: '盘中数据未封存', tone: 'warning' }
  }
  if (phase === 'EOD_PENDING') {
    return { label: '等待盘后封存', tone: 'warning' }
  }
  if (phase === 'EOD_SEALED' && dataPassed) {
    return { label: '数据检查已通过', tone: 'success' }
  }
  return { label: '数据检查未通过', tone: 'danger' }
}

export function formatAdvisorCoverage(coverageRatio: number): string {
  return `${(coverageRatio * 100).toFixed(2)}%`
}

export function presentResearchSnapshot(
  snapshotId: string | null,
  publishedAt: string | null,
): ResearchSnapshotPresentation {
  if (!snapshotId) {
    return {
      statusLabel: '未发布',
      shortId: '无',
      fullId: null,
      publishedAt: '未提供',
    }
  }
  return {
    statusLabel: '已发布',
    shortId: snapshotId.slice(0, 12),
    fullId: snapshotId,
    publishedAt: publishedAt || '未提供',
  }
}

export function resolvePaperActionState(
  actionState: AdvisorActionState | undefined,
  hasBriefError: boolean,
): AdvisorActionState | undefined {
  return hasBriefError ? undefined : actionState
}

export function presentTrustDatasets(
  dataGate: DailyBriefDataGate,
): TrustDatasetPresentation[] {
  return DATASET_PRESENTATION.map(({ key, label }) => {
    const receipt = dataGate.datasets[key]
    return {
      key,
      label,
      statusLabel: DATASET_STATUS_LABELS[receipt.status] ?? '状态待核对',
      provider: receipt.provider || '未提供',
      coverageRatio: receipt.coverage_ratio,
      observedStart: receipt.observed_start,
      observedEnd: receipt.observed_end,
      reasons: receipt.reasons,
      nextActions: receipt.next_actions,
    }
  })
}

export function selectDailyBriefCandidates(
  candidates: readonly BeginnerDailyBriefCandidate[],
): BeginnerDailyBriefCandidate[] {
  return candidates.slice(0, 3)
}

export function selectPlanMonitorStrategyIds(
  candidates: readonly BeginnerDailyBriefCandidate[],
): string[] {
  const strategyIds = new Set<string>()
  for (const candidate of candidates) {
    for (const strategyId of candidate.plan_monitor?.strategy_ids ?? []) {
      if (strategyId) strategyIds.add(strategyId)
    }
  }
  return [...strategyIds]
}

export function presentDailyBriefCandidate(
  candidate: BeginnerDailyBriefCandidate,
): DailyBriefCandidatePresentation {
  const monitor = candidate.plan_monitor
  const monitorPresentation = monitor
    ? PLAN_MONITOR_PRESENTATIONS[monitor.status]
    : null
  return {
    symbol: candidate.symbol,
    name: candidate.name,
    readyForSimulation: candidate.candidate_state === 'READY',
    goStreak: candidate.go_streak,
    lotSize: candidate.lot_size,
    lotCost: candidate.lot_cost,
    statusLabel: CANDIDATE_STATE_LABELS[candidate.candidate_state]
      ?? RESEARCH_DECISION_LABELS[candidate.research_decision],
    reasons: candidate.deterministic_reasons,
    observationConditions: candidate.observation_conditions,
    invalidationConditions: candidate.invalidation_conditions,
    riskMessages: candidate.risk_flags.map(flag => flag.message),
    planMonitor: monitor && monitorPresentation
      ? {
          label: monitorPresentation.label,
          tone: monitorPresentation.tone,
          asOf: monitor.as_of,
          lastPrice: monitor.last_price,
          changePct: monitor.change_pct,
          evidence: monitor.evidence,
        }
      : null,
  }
}
