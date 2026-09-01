import { parseFactorCatalog, parseFactorDetail, type FactorCatalogQuery, type FactorCatalogItem, type FactorDetail } from '../model/factor'
import { researchRequest } from './transport'

function catalogQuery(filters: FactorCatalogQuery): string {
  const params = new URLSearchParams()
  if (filters.category) params.set('category', filters.category)
  if (filters.engineering_status) params.set('engineering_status', filters.engineering_status)
  if (filters.data_status) params.set('data_status', filters.data_status)
  if (filters.verdict) params.set('verdict', filters.verdict)
  if (filters.scope) params.set('scope', filters.scope)
  if (filters.query) params.set('query', filters.query)
  const q = params.toString()
  return q ? `?${q}` : ''
}

export function listFactors(filters: FactorCatalogQuery = {}): Promise<{ items: FactorCatalogItem[] }> {
  return researchRequest(`/api/research/factors${catalogQuery(filters)}`, undefined, parseFactorCatalog)
}

export function getFactor(factorId: string): Promise<FactorDetail> {
  return researchRequest(`/api/research/factors/${encodeURIComponent(factorId)}`, undefined, parseFactorDetail)
}
