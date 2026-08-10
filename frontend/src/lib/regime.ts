/**
 * 市场环境(Regime) 前端数据层 — 类型 / 常量 / queryKey / API 客户端。
 *
 * 独立模块, 不污染全局 api.ts: regime 是自包含的特性切片, 所有后端契约集中在此。
 * fetch 走与 api.ts 相同的同源约定(Vite 代理 /api → 后端), 错误处理对齐(非 401 弹 toast)。
 */
import { toast } from '@/components/Toast'

const BASE = ''

/** regime 离散状态(与后端 classify_state 一致) */
export type RegimeState = 'strong' | 'lean_strong' | 'range' | 'lean_weak' | 'weak'

/** 状态中文标签(与后端 STATE_LABELS 对应) */
export const REGIME_STATE_LABELS: Record<RegimeState, string> = {
  strong: '强势',
  lean_strong: '偏强',
  range: '震荡',
  lean_weak: '偏弱',
  weak: '弱势',
}

/** 状态语义色(涨红跌绿系, strong 偏暖红 / weak 偏冷绿, 对齐 A 股语义) */
export const REGIME_STATE_COLORS: Record<RegimeState, string> = {
  strong: '#F04438',
  lean_strong: '#FB923C',
  range: '#9CA3AF',
  lean_weak: '#60A5FA',
  weak: '#12B76A',
}

/** 单日环境行(后端 _aggregate_daily 产出, 子维度分/原始指标为可选以兼容旧数据) */
export interface RegimeRow {
  date: string
  state: RegimeState
  score: number
  limit_up: number
  limit_down: number
  broken_limit: number
  max_consecutive: number
  seal_rate: number
  up_count: number
  down_count: number
  up_ratio: number
  index_pct: number
  above_ma20_pct: number
  total_amount: number
  avg_turnover: number
  avg_pct?: number
  median_pct?: number
  strong_up_pct?: number
  strong_down_pct?: number
  profit_score?: number
  speculation_score?: number
  resilience_score?: number
  trend_score?: number
}

export interface RegimeHistoryResponse {
  rows: RegimeRow[]
  total: number
}

export interface RegimeStateItem {
  state: RegimeState
  label: string
  count: number
  pct: number
}

export interface RegimeStatesResponse {
  distribution: RegimeStateItem[]
  days: number
}

export interface RegimeCoverage {
  rows: number
  earliest_date: string | null
  latest_date: string | null
}

export interface RegimeRecomputeResult {
  ok: boolean
  computed: number
}

// ── queryKey 工厂(集中管理, 供 useQuery / invalidate 共用) ──
export const RQK = {
  history: (range: unknown) => ['regime-history', range] as const,
  latest: ['regime-latest'] as const,
  states: (days: number) => ['regime-states', days] as const,
  coverage: ['regime-coverage'] as const,
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {}
  if (!(init?.body instanceof FormData)) headers['Content-Type'] = 'application/json'
  const res = await fetch(`${BASE}${path}`, { ...init, headers })
  if (!res.ok) {
    let detail = ''
    try {
      const j = JSON.parse(await res.text())
      const rawDetail = j.detail ?? j.message ?? ''
      detail = typeof rawDetail === 'string' ? rawDetail : JSON.stringify(rawDetail)
    } catch { /* ignore */ }
    const msg = detail || `${res.status} ${res.statusText}`
    if (res.status !== 401) toast(msg, 'error')
    throw new Error(msg)
  }
  return res.json() as Promise<T>
}

/** regime API 客户端(与后端 /api/regime/* 对齐) */
export const regimeApi = {
  history: (start?: string, end?: string, limit?: number) => {
    const params = new URLSearchParams()
    if (start) params.set('start', start)
    if (end) params.set('end', end)
    if (limit != null) params.set('limit', String(limit))
    const qs = params.toString()
    return request<RegimeHistoryResponse>(`/api/regime/history${qs ? `?${qs}` : ''}`)
  },

  latest: () => request<{ row: RegimeRow | null }>('/api/regime/latest'),

  states: (days: number) =>
    request<RegimeStatesResponse>(`/api/regime/states?days=${days}`),

  coverage: () => request<RegimeCoverage>('/api/regime/coverage'),

  recompute: (start?: string, end?: string) => {
    const params = new URLSearchParams()
    if (start) params.set('start', start)
    if (end) params.set('end', end)
    const qs = params.toString()
    return request<RegimeRecomputeResult>(`/api/regime/recompute${qs ? `?${qs}` : ''}`, { method: 'POST' })
  },
}
