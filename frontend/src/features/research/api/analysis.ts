import { asBoolean, asRecord } from '../model/parse'
import { researchRequest } from './transport'

export interface ResearchAnalysisRiskResult {
  status: string
  observations: number
  minSamples: number
  descriptive: {
    mean: number | null
    std: number | null
    annualizedVolatility: number | null
    skewness: number | null
    excessKurtosis: number | null
    min: number | null
    max: number | null
  }
  historicalVar: number | null
  historicalCvar: number | null
  parametricVar: number | null
}

export interface ResearchAnalysisPerformanceResult {
  status: string
  sortino?: number | null
  omega?: number | null
  max_drawdown?: number | null
  calmar?: number | null
  ulcer_index?: number | null
}

export interface ResearchAnalysisAdfResult {
  status: string
  adf_statistic?: number | null
  p_value?: number | null
  lags_used?: number | null
  is_stationary?: boolean | null
  observations?: number | null
}

export interface ResearchAnalysisGarchResult {
  status: string
  current_volatility?: number | null
  long_run_volatility?: number | null
  persistence?: number | null
  observations?: number | null
}

export interface ResearchSymbolAnalysisResult {
  risk: ResearchAnalysisRiskResult
  performance: ResearchAnalysisPerformanceResult
  statistics: {
    adf: ResearchAnalysisAdfResult
    garch: ResearchAnalysisGarchResult
  }
}

export interface ResearchSymbolAnalysisAvailableResponse {
  available: true
  source: string
  symbol: string
  start: string
  end: string
  data_as_of: string | null
  observations: number
  result: ResearchSymbolAnalysisResult
  warnings: string[]
  reason: null
}

export interface ResearchSymbolAnalysisUnavailableResponse {
  available: false
  source: null
  symbol: string
  start: null
  end: null
  data_as_of: null
  observations: 0
  result: null
  warnings: string[]
  reason: string
}

export type ResearchSymbolAnalysisResponse =
  | ResearchSymbolAnalysisAvailableResponse
  | ResearchSymbolAnalysisUnavailableResponse

export interface ResearchSymbolAnalysisRequest {
  start?: string
  end?: string
}

export function getResearchSymbolAnalysis(
  symbol: string,
  params: ResearchSymbolAnalysisRequest = {},
): Promise<ResearchSymbolAnalysisResponse> {
  const qs = new URLSearchParams()
  if (params.start) qs.set('start', params.start)
  if (params.end) qs.set('end', params.end)
  const query = qs.toString()
  return researchRequest(
    `/api/research/analysis/symbol/${encodeURIComponent(symbol)}${query ? `?${query}` : ''}`,
    undefined,
    parseResearchSymbolAnalysis,
    { acceptStatuses: [503] },
  )
}

function parseResearchSymbolAnalysis(value: unknown): ResearchSymbolAnalysisResponse {
  const record = asRecord(value)
  const available = record && asBoolean(record.available)
  if (!record || available === null) {
    throw new Error('研究分析响应格式无效')
  }
  return record as unknown as ResearchSymbolAnalysisResponse
}
