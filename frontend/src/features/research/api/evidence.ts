import { jsonBody, researchRequest } from './transport'
import {
  parseHypothesis,
  parseHypothesisList,
  parseRunCard,
  parseSchedule,
  parseScheduleList,
  parseScheduleRunNow,
  type AddEvidenceBody,
  type ConfirmTSuitabilityBody,
  type CreateHypothesisBody,
  type CreateScheduleBody,
  type ResearchHypothesis,
  type ResearchRunCard,
  type ResearchSchedule,
  type ResearchScheduleRunNowResponse,
  type UpdateHypothesisBody,
  type UpdateScheduleBody,
} from '../model/notebook'

export function listHypotheses(params?: { status?: string; query?: string }): Promise<{ items: ResearchHypothesis[] }> {
  const qs = new URLSearchParams()
  if (params?.status) qs.set('status', params.status)
  if (params?.query) qs.set('query', params.query)
  const q = qs.toString()
  return researchRequest(`/api/research/hypotheses${q ? `?${q}` : ''}`, undefined, parseHypothesisList)
}

export function getHypothesis(id: string): Promise<ResearchHypothesis> {
  return researchRequest(`/api/research/hypotheses/${encodeURIComponent(id)}`, undefined, (json) => {
    const hypothesis = parseHypothesis(json)
    if (!hypothesis) throw new Error('假设详情缺少 id')
    return hypothesis
  })
}

export function createHypothesis(body: CreateHypothesisBody): Promise<ResearchHypothesis> {
  return researchRequest('/api/research/hypotheses', jsonBody(body), (json) => {
    const hypothesis = parseHypothesis(json)
    if (!hypothesis) throw new Error('创建假设未返回 id')
    return hypothesis
  })
}

export function confirmTSuitabilityHypothesis(body: ConfirmTSuitabilityBody): Promise<ResearchHypothesis> {
  return researchRequest('/api/research/t-suitability/hypotheses', jsonBody(body), (json) => {
    const hypothesis = parseHypothesis(json)
    if (!hypothesis) throw new Error('做T确认未返回假设')
    return hypothesis
  })
}

export function updateHypothesis(id: string, body: UpdateHypothesisBody): Promise<ResearchHypothesis> {
  return researchRequest(
    `/api/research/hypotheses/${encodeURIComponent(id)}`,
    { method: 'PATCH', body: JSON.stringify(body) },
    (json) => {
      const hypothesis = parseHypothesis(json)
      if (!hypothesis) throw new Error('更新假设未返回 id')
      return hypothesis
    },
  )
}

export function addEvidence(id: string, body: AddEvidenceBody): Promise<ResearchHypothesis> {
  return researchRequest(
    `/api/research/hypotheses/${encodeURIComponent(id)}/evidence`,
    jsonBody(body),
    (json) => {
      const hypothesis = parseHypothesis(json)
      if (!hypothesis) throw new Error('追加证据未返回假设')
      return hypothesis
    },
  )
}

export function getRunCard(runId: string): Promise<ResearchRunCard | null> {
  return researchRequest(
    `/api/research/run-cards/${encodeURIComponent(runId)}`,
    undefined,
    parseRunCard,
    { nullOn404: true },
  )
}

export function listSchedules(): Promise<{ items: ResearchSchedule[] }> {
  return researchRequest('/api/research/schedules', undefined, parseScheduleList)
}

export function createSchedule(body: CreateScheduleBody): Promise<ResearchSchedule> {
  return researchRequest('/api/research/schedules', jsonBody(body), (json) => {
    const schedule = parseSchedule(json)
    if (!schedule) throw new Error('创建定时研究未返回 id')
    return schedule
  })
}

export function updateSchedule(id: string, body: UpdateScheduleBody): Promise<ResearchSchedule> {
  return researchRequest(
    `/api/research/schedules/${encodeURIComponent(id)}`,
    { method: 'PATCH', body: JSON.stringify(body) },
    (json) => {
      const schedule = parseSchedule(json)
      if (!schedule) throw new Error('更新定时研究未返回 id')
      return schedule
    },
  )
}

export function deleteSchedule(id: string): Promise<{ ok: boolean }> {
  return researchRequest(
    `/api/research/schedules/${encodeURIComponent(id)}`,
    { method: 'DELETE' },
    (json) => {
      const rec = json && typeof json === 'object' && !Array.isArray(json) ? json as { ok?: unknown } : null
      return { ok: rec?.ok === true }
    },
  )
}

export function runScheduleNow(id: string): Promise<ResearchScheduleRunNowResponse> {
  return researchRequest(
    `/api/research/schedules/${encodeURIComponent(id)}/run-now`,
    { method: 'POST' },
    parseScheduleRunNow,
  )
}

export const evidenceApi = {
  listHypotheses,
  getHypothesis,
  createHypothesis,
  updateHypothesis,
  addEvidence,
  getRunCard,
  confirmTSuitabilityHypothesis,
}

export const automationApi = {
  listSchedules,
  createSchedule,
  updateSchedule,
  deleteSchedule,
  runNow: runScheduleNow,
}
