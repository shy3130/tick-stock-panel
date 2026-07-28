export type AdvisorActionState = 'OBSERVE_ONLY' | 'SIMULATE_ONLY' | 'RESEARCH_ONLY'
export type AdvisorResearchDecision = 'GO' | 'WAIT' | 'NO-GO'
export type AdvisorDataGateDecision = 'PASS' | 'BLOCK'
export type AdvisorDatasetKey = 'instruments' | 'daily' | 'adj_factor' | 'daily_enriched'

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

export interface BeginnerDailyBriefCandidate {
  symbol: string
  name: string
  research_decision: AdvisorResearchDecision
  deterministic_reasons: string[]
  observation_conditions: string[]
  invalidation_conditions: string[]
  risk_flags: AdvisorRiskFlag[]
}

export interface BeginnerDailyBriefResponse {
  as_of: string | null
  generated_at: string | null
  action_state: AdvisorActionState
  today_message: string
  next_step: string
  data_gate: DailyBriefDataGate
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
    label: '只观察',
    description: '保持观察，不记录模拟成交',
    tone: 'warning',
  },
  SIMULATE_ONLY: {
    label: '只模拟',
    description: '数据检查已通过，仅做模拟复盘',
    tone: 'accent',
  },
  RESEARCH_ONLY: {
    label: '可研究',
    description: '数据检查已通过，可继续规则化研究',
    tone: 'success',
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
  statusLabel: string
  reasons: string[]
  observationConditions: string[]
  invalidationConditions: string[]
  riskMessages: string[]
}

export function actionPresentation(state: AdvisorActionState): ActionPresentation {
  return ACTION_PRESENTATIONS[state]
}

export function resolvePaperActionState(
  actionState: AdvisorActionState | undefined,
  hasBriefError: boolean,
): AdvisorActionState | undefined {
  return hasBriefError ? 'OBSERVE_ONLY' : actionState
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

export function presentDailyBriefCandidate(
  candidate: BeginnerDailyBriefCandidate,
): DailyBriefCandidatePresentation {
  return {
    symbol: candidate.symbol,
    name: candidate.name,
    statusLabel: RESEARCH_DECISION_LABELS[candidate.research_decision],
    reasons: candidate.deterministic_reasons,
    observationConditions: candidate.observation_conditions,
    invalidationConditions: candidate.invalidation_conditions,
    riskMessages: candidate.risk_flags.map(flag => flag.message),
  }
}
