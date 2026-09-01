import { parsePreflight, type PreflightRequest, type PreflightResult } from '../model/preflight'
import { jsonBody, researchRequest } from './transport'

export function createPreflight(body: PreflightRequest): Promise<PreflightResult> {
  return researchRequest('/api/research/preflights', jsonBody(body), parsePreflight)
}
