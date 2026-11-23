/** 扩展专用端点和查询键；原模拟仓继续复用 lib/api 与 QK.paper*。 */
import { ApiError } from '@/lib/api'
export interface ExecutionSource {
  strategy_id?: string
  candidate_id?: string
  config?: Record<string, unknown>
  strategy_info?: Record<string, unknown>
  label?: string
}

export interface ExecutionSnapshot {
  strategy_id: string
  name: string
  source: string
  params: Record<string, unknown>
  overrides: Record<string, unknown> & { entry_signals: string[]; exit_signals: string[] }
  symbols: string[] | null
  max_positions: number
  max_exposure_pct: number
  position_sizing: string
  initial_capital: number
  fees: { commission_pct: number; stamp_tax_pct: number; slippage_bps: number }
}

export interface ExecutionPreview {
  snapshot: ExecutionSnapshot
  revision: string
  differences: string[]
}

export interface ExecutionBinding extends ExecutionPreview {
  enabled: boolean
  status: 'waiting' | 'running' | 'paused' | 'blocked'
  message: string
  last_bar: string | null
  checked_at?: string
  risk_message?: string
  events: Array<{ at: string; symbol: string; side: string; reason: string; message: string; order_id?: string }>
}

export const executionListKey = ['custom', 'paper-execution', 'bindings'] as const

export const executionKey = (account: string) => ['custom', 'paper-execution', 'state', account] as const

async function request<T>(account: string, endpoint: string, method = 'GET', body?: unknown): Promise<T> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 30_000)
  try {
    const response = await fetch(`/api/custom/paper-execution/${endpoint}?account=${encodeURIComponent(account)}`, {
      method, headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal,
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok) {
      const detail = payload?.detail
      const message = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(v => v.msg).join('；') : `策略跟单请求失败 (${response.status})`
      throw new ApiError(response.status === 401 ? `未登录 (401)：${message}` : message, response.status)
    }
    return payload as T
  } catch (error) {
    if (controller.signal.aborted) throw new Error('策略跟单请求超时，请稍后刷新状态核对结果')
    throw error
  } finally {
    window.clearTimeout(timeout)
  }
}

export const executionApi = {
  list: () => request<{ bindings: Array<{ account: string; name: string; enabled: boolean; status: string; message: string }> }>('default', 'bindings'),
  state: (account: string) => request<{ binding: ExecutionBinding | null }>(account, 'state'),
  preview: (account: string, source: ExecutionSource) => request<ExecutionPreview>(account, 'preview', 'POST', source),
  bind: (account: string, source: ExecutionSource, revision: string, disableLegacy: boolean) =>
    request<{ binding: ExecutionBinding }>(account, 'binding', 'PUT', { source, revision, disable_legacy: disableLegacy }),
  enabled: (account: string, enabled: boolean) => request<{ binding: ExecutionBinding }>(account, 'enabled', 'POST', { enabled }),
}

/** 接受完整回测结果或配置；大体量交易记录不发给后端。 */
export function sourceFromReport(value: unknown): ExecutionSource {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('请选择有效的策略回测 JSON')
  const report = value as Record<string, unknown>
  if (report.error) throw new Error('失败的回测结果不能用于跟单')
  const config = (report.config ?? report) as Record<string, unknown>
  if (!config || typeof config !== 'object' || Array.isArray(config) || typeof config.strategy_id !== 'string') throw new Error('回测配置缺少策略 ID')
  return { config, strategy_info: report.strategy_info as Record<string, unknown> | undefined, label: '导入的回测配置' }
}
