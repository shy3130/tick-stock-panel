export type DataTrustStatus = 'ok' | 'partial' | 'empty' | 'invalid' | 'error'

export interface DataTrustAudit {
  schema_version: number
  provider: string
  dataset: string
  status: DataTrustStatus
  row_count: number
  returned_symbols: string[]
  missing_symbols: string[]
  coverage_ratio: number
  fallback_used: boolean
  synthetic: boolean
  issues: string[]
  observed_start: string | null
  observed_end: string | null
  recorded_at: string
}

export interface DataTrustResponse {
  overall_status: 'ok' | 'warning' | 'error' | 'unconfigured'
  audits: DataTrustAudit[]
}

export interface DataTrustRow {
  dataset: string
  datasetLabel: string
  provider: string
  status: DataTrustStatus
  statusLabel: string
  rowCount: number
  coverageLabel: string
  observedEnd: string | null
  issueText: string
}

const DATASET_LABEL: Record<string, string> = {
  instruments: '证券主表',
  daily: '日K',
  daily_enriched: '衍生日K',
  adj_factor: '复权因子',
  financial: '财务数据',
  financial_metrics: '核心财务指标',
  financial_income: '利润表',
  financial_balance_sheet: '资产负债表',
  financial_cash_flow: '现金流量表',
  financial_shares: '股本历史',
}

const STATUS_LABEL: Record<DataTrustStatus, string> = {
  ok: '完整',
  partial: '部分覆盖',
  empty: '无数据',
  invalid: '已拒绝',
  error: '拉取失败',
}

const REQUIRED_RESEARCH_DATASETS = new Set(['daily', 'daily_enriched', 'adj_factor'])
const MIN_RESEARCH_COVERAGE = 0.95

export function getDataTrustSummaryLabel(response: DataTrustResponse): string {
  if (response.overall_status === 'unconfigured') return '尚无回执'
  if (response.overall_status === 'error') return '存在失败'
  if (response.overall_status === 'ok') return '校验正常'

  const requiredCoverageInsufficient = response.audits.some(audit => (
    REQUIRED_RESEARCH_DATASETS.has(audit.dataset)
    && (audit.status === 'empty' || audit.coverage_ratio < MIN_RESEARCH_COVERAGE)
  ))
  return requiredCoverageInsufficient ? '覆盖不足' : '基本可用，少量缺失'
}

export function buildDataTrustRows(response: DataTrustResponse): DataTrustRow[] {
  return response.audits.map((audit) => ({
    dataset: audit.dataset,
    datasetLabel: DATASET_LABEL[audit.dataset] ?? audit.dataset,
    provider: audit.provider,
    status: audit.status,
    statusLabel: STATUS_LABEL[audit.status],
    rowCount: audit.row_count,
    coverageLabel: `${(audit.coverage_ratio * 100).toFixed(2)}%`,
    observedEnd: audit.observed_end,
    issueText: audit.missing_symbols.length > 0
      ? `缺少 ${audit.missing_symbols.length} 只标的`
      : audit.issues.join('；'),
  }))
}
